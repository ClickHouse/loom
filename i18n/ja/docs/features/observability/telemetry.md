<!-- i18n: source=docs/features/observability/telemetry.md blob=6ecde6689a67135feecc80bca8d455821803fdca model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md), which is authoritative.

<a id="telemetry"></a>

# テレメトリ

Loom は自身の状態を次の 3 つのレイヤーで報告します。

| レイヤー                     | 記録先                                                                      | 分かること                                            |
| ---------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------- |
| 台帳                         | `loom.agent_events`、操作またはワーカーの 1 回の処理につき 1 行             | 誰が何を呼び出したか、所要時間、コスト                |
| OpenTelemetry                | `default.otel_traces`、`otel_logs`、`otel_metrics_*`（または OTLP コレクター） | リクエスト内で時間がどこに費やされたか、傾向とアラート |
| ClickHouse クエリの帰属      | `system.query_log.log_comment`                                              | どの操作またはワーカーが ClickHouse に処理をさせたか  |

3 つのレイヤーはすべてリクエスト ID を共有します。[監査ログ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)は、誰が何を変更したかを改ざん検知付きで記録する、別個の記録です。

<a id="the-ledger"></a>

## 台帳

`loom.agent_events` には、操作ごとに1行が記録されます。列は `operation`、ID 列（`org`、`namespace`、`agent`、`user_id`、`session_id`、`run_id`）、`request_id`、`trace_id`、`latency_ms`、`success`、`error_code`、トークン数、`upstream_cost_usd`、`model`、`llm_calls`、`consumer`、`traffic_class`、`subject`、`build_commit`、`instance`、`instance_label`、そして上限付きでマスク済みのリクエストフィールドと出力フィールドを持つ `args` マップです。ワーカーのパスは `kind='worker'` の行を書き込みます。[コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)はこのテーブルを基に構築されています。

各リクエスト行には、サービスレベルの列も含まれます。

- `transport`：`http` または `mcp`。
- `slo_class`：`code`、`search`、`admin` のいずれか。読み取り専用でないその他の op は `write`、それ以外は `read`。
- `status_class`：`ok`。4xx の拒否、または再試行された冪等性キーがまだ実行中の場合は `client`。それ以外は `server`。
- `budget_ms`：そのリクエストに課されたレイテンシ予算。予算が 0 の場合（違反を強制するオーバーライド）、成功したリクエストはすべて遅いとみなされます。

org や名前空間を指定しない呼び出しは、ゲートがその呼び出しを受け入れた org と名前空間のもとで記録されます（code op のデフォルトは、デプロイのコード用名前空間です）。そのため、その行はそのテナントのものとなり、テナントの SLI、コストの集計、`subject_last_seen`、履歴ポリシーの件数のすべてにそこで反映されます。org を指定せず、ゲートの前で拒否された呼び出しは、トークンの org のもとで記録されます（org 全体のトークンの場合や認証がオフの場合は `-`）。そのため `cost.summary`、接続中エージェントのビュー、`subject_last_seen` も、その org の呼び出しとして数えます。

サーバー側の問題として数えられるもの：

- 履歴ポリシーを評価できなかったために拒否された書き込み。
- チェックに必要な読み取りが失敗したために行われた拒否（権限付与、サブジェクトのエイリアス、サービスアカウントまたは委任トークンの作成者、名前空間の分類）。そのような形で応答自体が拒否または非表示にされたリクエスト（id 検索の `found=False`、ハンドラー自身のチェックによる 403 や 404、org 全体の集計が丸ごと空になった場合）は、ワイヤ上では成功していても `server` となり、その場合 `error_code` は空になります。
- 次のものは数えません。それなしでも応答が完全である場合に、応答から外された名前空間です。Loom が自ら追加したもの（コードリンク、グラウンディング、リポジトリのドキュメント、トランク、呼び出し元が指定していないアイテムのコード回答を補強するトラッカー）、呼び出し元がスコープを指定していない一覧やデフォルトの選択で除外されたエントリ、投稿者がいずれにせよ受け入れられるタスクでの所有者チェックがこれにあたります。これらはリクエストを `ok` のままにします。

呼び出しが受け入れられる前の拒否：

- MCP は、ディスパッチ前に拒否した呼び出しについても行を書き込みます（トークンが使用できない org や名前空間、MCP から除外された op、読み取り専用接続での書き込み）。HTTP が拒否を記録するのと同様です。この時点では呼び出し元がまだバインドされていないため、その行にはトークンの org（トークンがその org で動作できる場合は呼び出しが指定した org）、サブジェクト、consumer が入り、残りは `-`、引数はなしになります。
- どちらのトランスポートでも、トークンが動作できない org を指定した拒否は、指定された org ではなく、トークンの org で記録されます。
- 呼び出しが分類される前に記録された拒否には、トークンの `traffic_class` クレームが入り、クレームがなければ '' になります。
- 1つの MCP バッチ内では、ハンドラーが実行される前の呼び出し元の拒否は、操作、コード、org、ステータスクラスごとに1回記録され、サーバー障害は最大10回までです。書き込まれなかった行は、`loom.ledger.batch_repeats`（呼び出し元の拒否）または `loom.ledger.dropped{reason="batch_repeat"}`（サーバー障害、つまり失われたサーバーエラー）に計上されます。冪等性の拒否（レートリミッターの後）とハンドラーの失敗は、必ず独自の行を持ちます。

リフレッシュ可能なマテリアライズドビューが、台帳を1分に1回 `loom_telemetry.sli_minute` に集約します（リクエスト数、サーバーエラーとクライアントエラー、クライアントエラーに含まれるレート制限されたリクエスト、予算超過の成功、レイテンシの分位数を 400 日間保持します。レイテンシはハンドラーの処理時間で、予算の基準となるものであり、その前段のゲートの時間は含みません）。これにより、SLO ダッシュボードやアラートルールは台帳をスキャンせずに読み取れます。集約は、行の `ts` ではなく ClickHouse が各行を受け取った時刻（`ingested_at`）から再開するため、フラッシュが遅れた行も、その `ts` の分に1回だけ数えられます。行は ClickHouse が受け取ってから少なくとも 70 秒後（最大で約 130 秒後）に集約され、その間に挿入がコミットされます。リフレッシュは1つのパートを書き込むため、リフレッシュが失敗しても、再試行で二重に集約されたり飛ばされたりするものは残りません。ClickHouse が行にスタンプを付けてから 70 秒を超えてコミットされた台帳のパートは集約されません。`loom.sli.rollup_missed_rows` は過去1日のそのような行を数え、ロールアップを再集約する（[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md)）ことで復旧できます。SLO クラスを持たない行（`/v1/me/*` ルート、および `initialize` のように登録済み op を指定しない MCP 呼び出し）は除外されます。同じ分が、マージされるまで複数の行として現れることがあるため、読み取り側はキーでグループ化し、件数には `sum()`、レイテンシには `quantilesTDigestMerge(0.5, 0.95, 0.99)(latency)` を使います。各リフレッシュは、すべてのキーが空で件数がすべて 0 の行も書き込み、次のリフレッシュはそこから再開します。読み取り側は `slo_class != ''` でフィルターしてください。テストトラフィックを除くには、`traffic_class NOT IN
('bench', 'background')` でフィルターします。呼び出しが分類される前に記録された拒否は、クラスが空です。

エミッターはバッチ処理され、リクエストを失敗させることはありません。クラッシュすると、フラッシュされていないキューが失われることがあります。

行を書き込むすべてのプロセスは、起動元の名前を持つ必要があります。compose ファイルは `LOOM_DEPLOYMENT=1` を設定し、サービスに `deploy:web`、`deploy:workers`、`deploy:dev` のラベルを付けます。それ以外のプロセス（`make server`、テストサーバー）は `LOOM_INSTANCE_LABEL=<who>:<what>:<port>` を設定する必要があり、設定しないと起動を拒否します。`loom serve` のデフォルトは `cli:serve:<port>` です。これにより、同じ ClickHouse 上にある他のものとデプロイの行が区別されます。

<a id="traces-and-logs"></a>

## トレースとログ

- **トレース。** スパンは `default.otel_traces`（または OTLP エンドポイント）に書き込まれ、`loom.org`、`loom.namespace`、`loom.agent`、`loom.operation`、`loom.request_id`、`enduser.id` のタグが付きます。ヘルスチェックのポーリングではスパンを開始しません（`LOOM_OTEL_EXCLUDED_URLS`、デフォルトは `/v1/health`）。すべての HTTP レスポンスに `X-Request-Id`（クライアントが 1〜64 文字の英字、数字、`.`、`_`、`-` を送った場合はその値）と `X-Trace-Id` が付きます。スパンにクエリ文字列は含まれません。
- **モデル呼び出し。** LLM と埋め込みの各呼び出しは、それぞれ独立したスパン `llm.<stage>` または `embed.provider` になり、OpenTelemetry GenAI 属性が付きます。属性は `gen_ai.provider.name`、`gen_ai.operation.name`（`chat` または `embeddings`）、`gen_ai.request.model`、`gen_ai.usage.input_tokens` と `output_tokens`、プロバイダーのプロンプトキャッシュ分を示す `gen_ai.usage.cache_read.input_tokens` と `cache_creation.input_tokens`（どちらも `input_tokens` に含まれます）、および Loom の価格表に基づく `gen_ai.usage.cost` です。呼び出しを行った処理段階は `loom.llm.stage` に入ります。Loom 自身の埋め込みキャッシュから返されたトークンは、課金対象の入力とは別に `loom.embed.cached_tokens` に記録されます。HyperDX の LLM ビューと Langfuse は、これらのキーをそのまま読み取ります。
- **ログ。** `docker logs loom-server` と `docker logs loom-workers`、および `default.otel_logs` への OTel ログエクスポートがあります。ログパイプライン向けには `LOOM_LOG_FORMAT=json`、詳細度の調整には `LOOM_LOG_LEVEL` を使います。起動ログには解決されたプロバイダーと適用された各マイグレーションが記録され、スーパーバイザーは失敗した各パスと `DEGRADED` 行を記録します。
- **Langfuse。** `LOOM_LANGFUSE_URL` と `LOOM_LANGFUSE_ADMIN_TOKEN` を設定すると、スパンツリーは 2 つ目のスパンプロセッサーとして Langfuse にも転送されます。転送ではサンプリングとフィルタリングが行われます。詳細は [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md) を参照してください。

`init_otel` は pytest の下では起動しないため、テストがデプロイ環境のオブザーバビリティテーブルに書き込むことはありません。`LOOM_OTEL_ENABLED=false` で出力を無効にできます。

<a id="exporters"></a>

### エクスポーター

| 設定                                              | デフォルト    | 効果                                                                                                                                                                   |
| ------------------------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_OTEL_EXPORTER`                              | `""`          | `ch` は Loom 自身の ClickHouse サービス上の `default.otel_*` テーブルに直接書き込みます。`otlp` はコレクターに送信します。空の場合、エンドポイントが設定されていれば `otlp`、そうでなければ `ch` になります |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT`                | `""`          | OTLP/HTTP コレクターのベース URL（`/v1/traces` と `/v1/metrics` が付加されます）                                                                                       |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`                 | `""`          | カンマ区切りの `K=V` ヘッダー。例：`Authorization=Bearer <token>`                                                                                                      |
| `LOOM_OTEL_SERVICE_NAME`, `LOOM_OTEL_ENVIRONMENT` | `loom`, `dev` | すべてのスパン、ログ、メトリクスに付く `service.name` と `deployment.environment.name`                                                                                 |
| `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S`             | `60`          | メトリクスのエクスポート間隔。スパンは独自の 3 秒のバッチ遅延を保ちます                                                                                                |
| `LOOM_OTEL_METRICS_DELTA_TEMPORALITY`             | `false`       | カウンターとヒストグラムを累積ではなくデルタにします。標準のダッシュボードは累積行を読むため、両方を同時に切り替えてください                                           |
| `LOOM_OTEL_GAUGE_HEARTBEAT_S`                     | `0`           | `ch` エクスポーターのみ：ゲージの点は、値が変化したとき、またはこの秒数が経過したときだけ書き込みます。0 の場合は毎回のエクスポートで書き込みます（ゲージのタイルは欠損を 0 で埋めます）。OTLP はすべての点をエクスポートします |
| `LOOM_OTEL_CENSUS_SCOPE`                          | `leader`      | テーブル全体の集計を行う主体：スーパーバイザーのリースを持つプロセスのみ、またはすべてのプロセス（高コスト）                                                           |

`ch` エクスポーターは ClickStack/HyperDX が読み取るテーブルに書き込むため、標準のスタックならコレクターなしでダッシュボードを利用できます。[ClickStack](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md) と `deploy/hyperdx_dashboards.md` を参照してください。

<a id="metrics"></a>

## メトリクス

メトリクスは `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S` ごとにエクスポートされます。`ch` エクスポーターでは `default.otel_metrics_sum`、`default.otel_metrics_histogram`、`default.otel_metrics_gauge` に書き込まれ、`otlp` ではコレクターの `/v1/metrics` に送られます。ダッシュボードの定義は `deploy/dashboards/` と `deploy/hyperdx_dashboards.md` にあります。

ラベルは閉じた集合（操作、名前空間、上限のある列挙値）から取ります。キー、ユーザー ID、セッション ID、クエリテキストはラベルになりません。これは `tests/unit/test_domain_metrics.py` が検証します。モデルラベル（`loom.llm.fallback`、`loom.llm.response_cache`）には、料金表に載っているモデル、またはデプロイの `LOOM_LLM_MODEL` / `LOOM_LLM_FALLBACK_MODEL` のモデル名が入ります。リクエストがモデルを指定できるため、それ以外は `unlisted` になります。

テーブルスキャンが必要なゲージは、workers-lease の保持者が計算してキャッシュします。整合性の不変条件と埋め込みバックログは `LOOM_OTEL_INVARIANTS_PROBE_S`（3600 秒）ごと、保持期間の集計は `LOOM_OTEL_RETENTION_CENSUS_S`（3600 秒）ごと、コードプレーンの件数は `LOOM_OTEL_CODE_COUNTS_CACHE_S`（3600 秒）ごとです。

<a id="requests-and-cost"></a>

### リクエストとコスト

| メトリクス                      | 型        | 意味                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.requests`                 | counter   | 処理した操作の数。操作、名前空間、成否、`loom.error_code`（失敗時のコード。成功時は空）ごと                                                                                                                                                                                                                                                                                                                     |
| `loom.request.duration`         | histogram | 操作の実時間（ms）                                                                                                                                                                                                                                                                                                                                                                                              |
| `loom.cost.usd`                 | counter   | 操作に帰属する上流プロバイダーの支出                                                                                                                                                                                                                                                                                                                                                                            |
| `loom.tokens`                   | counter   | `kind`（`embed`、`llm_input`、`llm_output`）別のトークン数                                                                                                                                                                                                                                                                                                                                                      |
| `loom.ops.paid_errors`          | counter   | すでに上流で支出が発生していた失敗リクエスト                                                                                                                                                                                                                                                                                                                                                                    |
| `loom.llm.fallback`             | counter   | `LOOM_LLM_FALLBACK_MODEL` が応答した LLM 呼び出し                                                                                                                                                                                                                                                                                                                                                               |
| `loom.llm.response_cache`       | counter   | プロンプト完全一致の LLM キャッシュ検索の結果別件数                                                                                                                                                                                                                                                                                                                                                             |
| `loom.memory.build.tokens`      | counter   | メモリの構築に使った LLM トークン。ステージと方向別                                                                                                                                                                                                                                                                                                                                                             |
| `loom.memory.extraction_yield`  | histogram | 書き込まれたメモリ 1 件あたりのプロンプトトークン数。`messages` の範囲別                                                                                                                                                                                                                                                                                                                                        |
| `loom.extract.cost_per_row_usd` | histogram | 1 回の抽出で書き込まれたメモリ行 1 行あたりの USD                                                                                                                                                                                                                                                                                                                                                               |
| `loom.auth.refusals`            | counter   | ベアラートークンを理由に拒否されたリクエスト（トークンなし、検証に失敗したもの、まだ確認できないもの。ClickHouse が失効済みと判断したトークンによる発行も含む）。`reason`（`missing`、`malformed`、`expired`、`revoked`、`invalid`、`unavailable`）と `loom.transport`（`http`、`mcp`、またはプロバイダートークンが欠落・不正・誤りの SCIM リクエストでは `scim`）別。SLI ロールアップには含まれません |

<a id="agent-trusts"></a>

### エージェントトラスト

`--agent-claim` 付きで登録したワークロードトラストは、最初の交換時にエージェントごとの名前空間を作成します。上限は `--max-agents` です（[ボットと CI](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md#bots-and-ci)）。`loom.trust` はトラストの名前で、登録できるのはオペレーターだけです。

| メトリクス                          | 型      | 意味                                                                                                                                                                              |
| ----------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.auth.agents_provisioned`      | counter | トラストのエージェントの最初の交換。`loom.org`、`loom.trust`、`outcome` 別。`made`（名前空間を作成した）または `at_cap`（拒否：トラストがすでに `--max-agents` 件作成済み） |
| `loom.auth.trust_agents`            | gauge   | 有効なトラストの稼働中エージェント数（解放済みは数えない）。上限の数え方と同じ。workers-lease の保持者が 5 分ごとに計算                                                           |
| `loom.auth.trust_agents_used_ratio` | gauge   | `loom.auth.trust_agents` をトラストの `--max-agents` で割った値。1 になると新しいエージェントはすべて拒否されます                                                                 |

<a id="retrieval-quality"></a>

### 検索品質

高速だが誤った検索は、HTTP メトリクスでは見えません。以下のメトリクスでそれを検出します。

| メトリクス                              | 型        | 意味                                                                                                   |
| --------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------ |
| `loom.search.results`                   | histogram | 検索ごとの結果数。0 に近づく傾向は、気づきにくい破損を示します                                         |
| `loom.search.top_similarity`            | histogram | 最上位ヒットのコサイン類似度。検索で最も診断に役立つシグナル（埋め込みモデルを変更したら基準を取り直す） |
| `loom.search.confidence`                | histogram | レスポンスの `retrieval_confidence`                                                                    |
| `loom.search.abstain`                   | counter   | 回答を控えた検索。`reason` 別                                                                          |
| `loom.search.plane_hits`                | counter   | 検索プレーンごとのヒット数                                                                             |
| `loom.search.assist`                    | counter   | 作動したオプションの補助（HyDE、rerank、反復ジャッジ）。`kind` 別                                      |
| `loom.search.judged_relevance`          | histogram | サンプリングした検索に対する独立した LLM ジャッジの評価（`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`、デフォルト 0） |
| `loom.search.judge_verdict`             | counter   | ジャッジの判定のカテゴリ別件数                                                                         |
| `loom.search.bm25_stats_failures`       | counter   | フェイルオープンした BM25 コーパス統計の読み取り失敗（語彙側が重みなしで実行された）。プレーンとエラー別 |
| `loom.memory.search.enumeration_shaped` | counter   | 保存済みレコードをクラスとして列挙する言い回しの検索。拒否ではなくシグナル                             |

<a id="memory-writes"></a>

### メモリの書き込み

| メトリクス                                     | 型      | 意味                                                                                                                              |
| ---------------------------------------------- | ------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `loom.memory.writes`                           | counter | 書き込み結果：created、updated、merged、duplicate、conflicted、rejected、blacklisted、too_large など                              |
| `loom.memory.drops`                            | counter | 取り込みパイプラインが破棄または切り詰めた項目。`stage` と `reason` 別（`loom.memory_drops` の行）                                |
| `loom.memory.attribute_tagged`                 | counter | 書き込み時に `attribute:<class>` というラベルが付いた行                                                                          |
| `loom.policy.decisions`                        | counter | 履歴ポリシーゲートの判定                                                                                                          |
| `loom.procedure.runs`, `loom.procedure.evolve` | counter | 結果別の手順の実行数。`procedure.evolve` の結果                                                                                   |
| `loom.memory.agent_labels_24h`                 | gauge   | 過去 24 時間にエージェントが書いた使用ラベル数。名前空間ごと（上位 `LOOM_OTEL_AGENT_LABELS_MAX_NAMESPACES` 件、残りは `other`）   |
| `loom.memory.agent_labels_namespaces_at_pace`  | gauge   | エージェントラベルが 1 日あたりのペース（月 1,000 件）に達している名前空間の数。組織ごと                                          |

`loom drops --namespace NS [--hours 24]` は、ステージと理由ごとの破棄件数を表示します。完全に失敗した抽出は破棄には含まれません。`set_from_messages` は 502 `EXTRACTION_FAILED` を返し、部分的な失敗はレスポンスの `pipeline_errors` に入ります。

<a id="backlogs-and-process-state"></a>

### バックログとプロセスの状態

レベルは 0 でないことが通常で、時間とともに減っていくものです。

| メトリクス                                                                                                                   | 型        | 意味                                                                                                                                                         |
| ---------------------------------------------------------------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `loom.embed.pending`                                                                                                         | gauge     | ベクトル待ちの行（`embedding_dim = 0`）。まだ検索できないメモリ                                                                                              |
| `loom.vectors.total`                                                                                                         | gauge     | 存在する埋め込み済みベクトルの数                                                                                                                             |
| `loom.vector_index.searches_total`, `adds_total`, `reads_indexed_ratio`                                                      | gauge     | プローブしたレプリカでのベクトルインデックスの利用状況：支払った構築コストに対して処理した検索数と、最近のベクトル読み取りのうちインデックスを開いたものの割合 |
| `loom.process.open_fds`, `loom.process.fd_limit` | ゲージ | プロセスごとの開いているディスクリプタ数とソフトリミット（`loom.pid`）。右肩上がりの線はリークを示す |
| `loom.host.disk.free_bytes`, `loom.host.disk.total_bytes` | ゲージ | `var/` を保持するディスクの空きバイト数と総バイト数 |
| `loom.ch.mutations_pending` | ゲージ | 未完了の ClickHouse ミューテーション |
| `loom.ch.table.active_parts`, `wide_parts`, `patch_parts`, `bytes_on_disk`, `rows_per_mark` | ゲージ | ハウスキーピング対象のテーブルごと（`loom.table`） |
| `loom.memory_embeddings.patch_parts` | ゲージ | `loom.memory_embeddings` のパッチパート数。0 以外になると量子化コードの読み取りが無効になる |
| `loom.ch.view_refresh.scheduled`, `loom.ch.view_refresh.age_s` | ゲージ | リフレッシュ可能ビューごと（`loom.view`。`loom` 内のものはビュー名のみ、それ以外はデータベース付き：`loom_telemetry.sli_minute_refresh`）。エラーなしでスケジュールされている間は 1。最後に成功してからの秒数 |
| `loom.sli.rollup_lag_s` | ゲージ | 直近のストレージプローブ（5 分ごと、workers-lease 保持者が実行）の時点で、`loom_telemetry.sli_minute` のマークが現在時刻から遅れていた秒数。最新であれば約 70 から 130。それより大きい場合はロールアップが追いつき中で、最新の数分は空に見える |
| `loom.sli.rollup_missed_rows` | ゲージ | 直近 1 日の台帳のリクエスト行のうち、ロールアップのマークが通過した後に insert がコミットされたため、`sli_minute` が集計しないもの（ストレージプローブ、5 分ごと、workers-lease 保持者が実行）。0 を超えたらアラートを出し、ロールアップを再集計する |
| `loom.audit.checkpoint_lag_windows` | ゲージ | チェックポイントチェーンにまだ封印されていない 1 時間単位の監査ウィンドウ数（[監査ログ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#checkpoint-lag)） |
| `loom.langfuse.breaker` | ゲージ | Langfuse スコアのブレーカー。0 は閉、1 は半開、2 は開（スコアは破棄される。スパンのエクスポートには影響しない） |
| `loom.health.problems` | ゲージ | このプロセスで開いている `/v1/health` の問題 1 件につき 1。`code` と `severity`（`provider_failing` では `provider`）別。ただし `/v1/health` が ClickHouse の読み取りで検出するもの（`otel_erasure_index`）は除く。解決した問題は報告されなくなるため、アラートは少なくとも `LOOM_OTEL_GAUGE_HEARTBEAT_S` に `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S` を 1 つ加えた期間（ハートビートなしの 2 インターバル）のウィンドウで読む。`ch` エクスポーターとハートビートを使う場合、値が変わらなくても各ハートビート後の最初のエクスポートで書き込まれる |
| `loom.ledger.dropped` | カウンター | このプロセスが起動以降に失った台帳行。`reason` 別（`queue_full`、`flush_failed`、`batch_repeat`：MCP バッチが 10 行を超えて書き込まなかったサーバー障害）。SLI はその分だけ過少に計上される |
| `loom.ledger.batch_repeats` | カウンター | MCP バッチが再度書き込まなかった、呼び出し元の拒否行（バッチごとに 1 回台帳に記録される）。呼び出し元に起因するもので、損失ではない |
| `loom.worker.passes` | カウンター | `worker` と `outcome` 別のパス数 |
| `loom.worker.pass_duration_ms` | ヒストグラム | パスの実時間 |
| `loom.worker.consecutive_failures` | ゲージ | ワーカーごとの現在の連続失敗数。3 でアラート |
| `loom.worker.partial_failures` | ゲージ | 直近のパスが耐えた失敗数 |
| `loom.retention.active_rows`, `never_recalled_ratio`, `low_information_rows`, `sensitive_without_expiry`, `stale_candidates` | ゲージ | 組織ごとのメモリ衛生カウント。Loom は経過時間では何も削除しない |

<a id="postgres-publication-pipeline"></a>

### Postgres パブリケーションパイプライン

`LOOM_STORAGE_AUTHORITY=postgres` の場合：書き込みが検索可能になるまでの時間と、ClickHouse にまだ届いていないもの（`loom.ch.inserts` はどちらの authority でも計上される）（[パイプライン SLO](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#pipeline-slos)）。

| メトリクス | 種類 | 意味 |
| --- | --- | --- |
| `loom.pg.search_wait_ms` | ヒストグラム | 読み取りがバリアで、ClickHouse が名前空間の書き込みを保持するのを待った時間。`loom.operation`（リクエストが呼び出した op）、`outcome`（`passed`、またはデッドライン `LOOM_PG_PROJECTION_WAIT_MS`、もしくは再入パスの残り時間に達した場合の `deadline`。その op は 503 `PROJECTION_BEHIND` を返し、レポートは projection の読み取りを省略してよい）、`loom.traffic_class` 別。遅れを検出した読み取りのみ |
| `loom.pg.projection_lag_ms` | ヒストグラム | リビジョンのコミットトランザクションの開始から、その配信が最後の検索ターゲットを解消するまで。Postgres の時計で計測し、`path`（flush、redelivered）別。ドキュメントが反映されたか、再配信でリビジョンがもう提供されていない（削除済み、またはキーが移った）ことが分かり、未送信のまま決着した場合 |
| `loom.pg.backlog` | ゲージ | 未配信の配信数。`source`（memory、audit_outbox、knowledge）と `state`（pending、parked）別。それぞれ `LOOM_PG_BACKLOG_COUNT_CAP` までカウントする。memory の pending は検索が待つ対象 |
| `loom.pg.oldest_pending_age_s` | ゲージ | `source` 別の最も古い pending 配信の経過時間。memory はコミットトランザクションの開始から、knowledge はそのソースの最新コミットから計測し、いずれもリース順の先頭 `LOOM_PG_BACKLOG_COUNT_CAP` 件が対象。監査アウトボックスは、期限を過ぎた全行を対象に、期限超過の秒数で表す |
| `loom.pg.backlog_reading_age_s` | ゲージ | 各ソースのバックログを読んでからの秒数（読み取りが新しい間）。2 エクスポート期間にわたって読み取りがないと、バックログのゲージは停止する |
| `loom.ch.inserts`, `loom.ch.insert_rows` | カウンター | メモリ書き込みパスによるメモリ行と検索ドキュメントの insert、および監査ライターの insert と、その行数。`table` 別。`memory` と `memory_embeddings` の insert はコミットされ、1 回ごとに 1 パートとなる。`audit_log` の insert はデフォルトでバッチ処理される（`LOOM_CH_BATCHED_INSERTS`） |

バックログは配信ワーカーが各パスの最初に読み取り、それらを実行するプロセス（デフォルトではワーカーの子プロセス、そうでなければ workers-lease 保持者）がエクスポートする。複数のプロセスが同時にエクスポートすることがあり（`LOOM_BACKGROUND_WORKERS=on`、リースの引き継ぎ）、それぞれがデプロイメント全体のバックログを報告する。これらのゲージは `sum` ではなく `max` で集計すること。

<a id="integrity-invariants"></a>

### 整合性の不変条件

いずれも常に 0 であるべきです。0 以外の値は、すでに発生した欠陥であり、自然には解消しません。

| メトリクス                                                      | 0 以外の値の意味                                                                | 対処                                            |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------- |
| `loom.integrity.divergent_entities`                             | あるメモリについて、`loom.memory` と `loom.memory_embeddings` でエンティティが異なっている | 書き込みパスを調査する                          |
| `loom.integrity.orphan_vectors`                                 | 対応するメモリ行が失われた埋め込みベクトル                                      | `orphan_vector_reconcile` ワーカー              |
| `loom.integrity.orphan_entity_vectors`                          | 対応するメモリ行が失われたエンティティベクトル                                  | `orphan_vector_reconcile` ワーカー              |
| `loom.integrity.orphan_graph_namespaces`                        | エンティティエッジはあるが、有効なメモリもナレッジもない名前空間                | `loom erasure-re-verify --orphan-graph --erase` |
| `loom.integrity.live_ns_unregistered`, `live_rows_unregistered` | `loom.namespaces` に行がない名前空間にある有効なメモリ                          | 名前空間を登録する                              |
| `loom.integrity.dim0_with_vector`                               | ベクトルはあるが `embedding_dim = 0` のため、検索で見えない行                   | `loom integrity-repair --apply`                 |
| `loom.integrity.tombstone_status_drift`                         | `tombstoned_at` が設定されているのに、別のステータスになっている行             | `loom integrity-repair` が一覧表示する          |
| `loom.integrity.active_past_valid_to`                           | 自身の `valid_to` を過ぎても active のままの行                                  | `loom integrity-repair` が一覧表示する          |
| `loom.integrity.high_with_embedding`                            | 機密度が high で、有効な埋め込み行がまだ残っている行                            | `loom sensitivity-sweep --apply`                |

<a id="code-plane"></a>

### コードプレーン

名前空間ごとのメトリクスです。

| メトリクス                                                              | 種類               | 意味                                                                                                                                                            |
| ----------------------------------------------------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.code.index_lag_commits`                                           | gauge              | インデックスのウォーターマークとブランチ先端の間にあるコミット数。古さを示すシグナル                                                                            |
| `loom.code.index_age_seconds`                                           | gauge              | ウォーターマークが最後に進んでからの秒数                                                                                                                        |
| `loom.code.stale_files`                                                 | gauge              | チャンクが head 時点のファイル内容より古い、インデックス済みパスの数                                                                                            |
| `loom.code.scip_lag_commits`, `scip_age_seconds`, `scip_py_age_seconds` | gauge              | 各コンパイラティア（`tier` 属性）が高速ティアにどれだけ遅れているか                                                                                             |
| `loom.code.tier_lag_seconds`                                            | gauge              | ティア（`fast`、`clang`、`python` など）がまだ到達していない最も古いコミットの経過時間                                                                          |
| `loom.code.tier_failing`                                                | gauge              | `code_tier_failing` ヘルス問題が報告する各ティアの連続失敗パス数。`tier`、エラー `kind`、および問題が評価するティアの `severity` ごと                            |
| `loom.code.tier_lag_commits`                                            | gauge              | それらの各ティアが取得済みの先端に遅れているコミット数（下限値：エントリが最後に公開したステータスで数えたもの）。`tier` ごと。遅れが判明している間に報告される   |
| `loom.code.pr_cursor_age_seconds`                                       | gauge              | PR 履歴の同期が GitHub にどれだけ遅れているか                                                                                                                   |
| `loom.code.files_errored`, `loom.code.embed_backlog`                    | gauge              | インデックスエラーのあるファイル数、ベクトル待ちのチャンク数                                                                                                    |
| `loom.code.symbols`                                                     | gauge              | `tier`（`treesitter`、`scip`）ごとの有効なシンボル数。減少した場合は不正な適用を示す                                                                            |
| `loom.code.edges_unqualified`, `loom.code.edges_low_confidence`         | gauge              | 名前のみで束縛されたパーサティアの呼び出しエッジ。`loom.code.symbols` と比較する                                                                                |
| `loom.code.grep.wrong_abstain_24h`                                      | gauge              | コーパスに存在するリテラルに対して `code.grep` が回答を控えた件数。上位 8 件のコンシューマーと `other`（`LOOM_OTEL_CODE_GREP_REPLAY_CACHE_S` ごとに再集計。0 = オフ） |
| `loom.code.repos_config_errors`                                         | gauge              | `LOOM_CODE_REPOS_JSON` 内の使用できないエントリ数（0 = 問題なく解析された）                                                                                     |
| `loom.code.outcome`, `loom.code.shunt`, `loom.code.shunt_tokens_saved`  | counter            | クライアントが報告した結果。コーディングエージェントの shunt の判断と、コンテキストに入れずに済んだトークン数                                                   |
| `loom.code.ask_cache`, `ask_cache_saved_ms`, `ask_cache_saved_usd`      | counter            | `code.ask` の回答キャッシュの結果と、ヒットによる節約量                                                                                                         |
| `loom.tracker.sections`, `loom.tracker.section_ms`                      | カウンター、ヒストグラム | テンプレートとステータスごとの `tracker.answer` セクション数、および読み取り時間                                                                            |

遅延ゲージが呼び出し側にとって何を意味するかは、[コードインデックスの鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)を参照してください。

<a id="suggested-alerts"></a>

### 推奨アラート

| 条件                                                                                                                     | 理由                                                                                                                                                 |
| ------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.search.top_similarity` の平均が、ベースラインを 30 分間下回る                                                      | 検索品質が低下した（エンベッダーまたはインデックスの変更）                                                                                           |
| `loom.embed.pending` が 15 分間 100 を超える                                                                             | 新しいメモリが検索可能になっていない                                                                                                                 |
| いずれかの `loom.integrity.*` が 0 を超える                                                                              | データ不整合が発生した                                                                                                                               |
| `loom.worker.consecutive_failures` ≥ 3                                                                                   | ワーカーが失敗を繰り返している                                                                                                                       |
| `loom.process.open_fds / loom.process.fd_limit` が 0.5 を超える                                                          | ディスクリプタのリーク                                                                                                                               |
| `loom.host.disk.free_bytes / loom.host.disk.total_bytes` が 0.10 を下回る                                                | データディスクが逼迫している                                                                                                                         |
| `loom.langfuse.breaker` = 2                                                                                              | Langfuse へのスコアが破棄されている                                                                                                                  |
| `loom.audit.checkpoint_lag_windows` が数時間にわたって増加している                                                       | 監査シーラーが停止している                                                                                                                           |
| `loom.code.index_lag_commits` が 15 分間、1 パスあたりのコミット予算を超える                                             | コードの取り込みが遅れている                                                                                                                         |
| `loom.code.tier_lag_seconds` が 10 分間 1800 を超える                                                                    | コンパイラティアが停止している（`deploy/dashboards/code_plane.json` の「code tier lag」アラート。しきい値は最も遅いティアの通常サイクルより大きく設定する） |
| trust ごとの `loom.auth.agents_provisioned{outcome=made}` が、そのコントロールプレーンの通常の頻度を超える（10 分間に 50 から始める） | trust が、フリートの増加より速く新しいエージェントを命名している：発行者キーの漏えい、またはコントロールプレーンの暴走                               |
| `loom.auth.trust_agents_used_ratio` が 0.8 を超える                                                                      | trust が `--max-agents` に近づいている                                                                                                               |
| `loom.auth.agents_provisioned{outcome=at_cap}` が 10 分間で 0 を超えて増加する                                           | その trust の新しいエージェントが拒否されている                                                                                                      |

HyperDX では、空の結果は 0 として読み取られます。そのため、最小サンプル数のガードは、行を返さないのではなく、正常範囲内の値を返す必要があります。

開始用のクエリ：

```sql
-- mean top similarity per namespace, last hour
SELECT Attributes['loom.namespace'] AS ns,
       round(anyLast(Sum) / greatest(anyLast(Count), 1), 3) AS mean_top_similarity
FROM default.otel_metrics_histogram
WHERE MetricName = 'loom.search.top_similarity' AND TimeUnix > now() - INTERVAL 1 HOUR
GROUP BY ns;

-- every gauge's latest value
SELECT MetricName, anyLast(Value) AS latest
FROM default.otel_metrics_gauge
WHERE TimeUnix > now() - INTERVAL 5 MINUTE
GROUP BY MetricName ORDER BY MetricName;
```

<a id="query-attribution-in-clickhouse"></a>

## ClickHouse でのクエリ帰属

すべての ClickHouse クエリには、発行元を示す `log_comment` 設定が付きます。リクエストの場合は `<operation>
<request_id>`、ワーカーのパスの場合は `worker.<name>`、CLI コマンドの場合は `cli.<command>`、それ以外の場合は `untagged:<module>.<function>` です。タグの表、`query_id` による結合、ステートメントのダイジェストについては、[ClickHouse: クエリ帰属](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#query-attribution)を参照してください。

```sql
-- one request end to end
SELECT * FROM loom.agent_events WHERE request_id = '<id>';
SELECT query_duration_ms, read_bytes, log_comment
FROM system.query_log
WHERE type = 'QueryFinish' AND log_comment LIKE '% <id>%';
```

<a id="not-telemetry-the-observe-operations"></a>

## テレメトリではない：`observe.*` オペレーション

`observe.record` と `observe.crystallize` は、メモリプレーンに属します。これらはエージェントのツール呼び出しを記録します。

- `observe.record` は、1 件のツール呼び出し（ツール名、入力、出力、ファイルパス、エラー）を、`obs:<session_id>:<sha8>` をキーとするエピソード記憶の検索不可なメモリ行として保存します。LLM 呼び出しも同期的な埋め込みも行いません。Loom 自身のツール呼び出しやハーネスのメタツールの呼び出しは、内容なしで保存されます（`skipped_reason: "self_referential"`）。内容がブラックリストのパターンに一致する呼び出しも同様です（`skipped_reason: "blacklisted"`）。
- `observe.crystallize` はセッション終了時のフラッシュです。そのセッションの保留中の observation を、抽出可能な状態にします。キューに追加するだけで、LLM は実行しません。

observation を要約するには、`observe_extract` ワーカー（デフォルトでは無効、`LOOM_OBSERVE_EXTRACT_ENABLED`）が必要です。無効のままの場合、observation は保存されるだけで、抽出されることはありません。

コーディングエージェントのプラグインがこれらをどのように使うかについては、[会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)を参照してください。

`ui.vitals` はダッシュボードに提供されます。1 ページ分の Web バイタルを、専用のレジャー行に記録します。

<a id="related"></a>

## 関連情報

- [モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)：ヘルスチェック、ダッシュボード、運用担当者が日常的に確認するビュー。
- [トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)。
- [コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)および[監査ログ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)。
- [ClickStack](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)および[Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)。
- [ワーカーのリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)および[設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#observability)。
