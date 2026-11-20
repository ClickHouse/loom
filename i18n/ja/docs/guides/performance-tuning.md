<!-- i18n: source=docs/guides/performance-tuning.md blob=285ede1bf84d35f39aa2de165f838e15263c115e model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/performance-tuning.md), which is authoritative.

<a id="tune-looms-performance"></a>

# Loom のパフォーマンスをチューニングする

このページで扱う調整項目は、すべて設定またはリクエストのフィールドです。各設定のデフォルト値と意味については、[設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)を参照してください。トレードオフを計測できる項目については、計測に使うコマンドを併記しています。

<a id="measure-first"></a>

## まず計測する

| 知りたいこと                                       | コマンド                                                                              | 必要なもの                                           |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 並行数 N での 1 つの op の p50 / p95 / p99 と RPS  | `make bench OP=memory.search C=5 D=60` (`scripts/ops/benchmark.py`)                   | 稼働中のサーバー。書き込みは `bench-…` 名前空間に入る |
| ランキングや embedder の変更で再現率が下がったか   | `make bench-quick` (回答ジャッジ付きの、公開されている長期メモリベンチマークの一部)   | 実際のプロバイダー、`OPENAI_API_KEY`                 |
| 記録済みベースラインに対する再現率                 | `make bench-quality-gate MIN_RECALL=…`                                                | 実際のプロバイダー                                   |
| 検索モード × rerank × プール幅                     | `make bench-matrix` (`bench/MATRIX_RESULTS.md` に書き出す)                            | 稼働中のサーバー                                     |
| 抽出およびグラフ抽出のリグレッション               | `make bench-algorithm-gate`                                                           | 稼働中のサーバー                                     |
| コードプレーンのシンボル、呼び出し元、再現率、トークンコスト | `make bench-code-intel`, `make bench-code-legs`                             | 実際の embedder                                      |
| デプロイ後に何が変わったか                         | `loom deploy-delta` (入れ替えの前後での、台帳と ClickHouse ステートメントの形)        | `var/deployed.json`                                  |

> **警告:** fake embedder で測定した再現率は、実際の検索について何も示しません。品質ゲートが CI に含まれていないのはこのためです。デプロイ予定のプロバイダーを使うサーバーに対して実行し、embedder を変更したらベースラインを取り直してください。

遅いリクエストを 1 件調べる場合は、設定に手を加える前に、その `Server-Timing` ヘッダー (`app` と `ch`) と `system.query_log` の行を確認してください ([デバッグ](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md))。デプロイ全体での操作ごとのコストは、`log_comment` に対する 1 つのクエリで取得できます。

```sql
SELECT splitByChar(' ', log_comment)[1] AS source,
       count() AS queries, sum(read_bytes) AS bytes, sum(query_duration_ms) AS ms
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR AND log_comment != ''
GROUP BY source ORDER BY ms DESC LIMIT 20;
```

入れ替え直後のベンチマークトラフィックはキャッシュが冷えた状態で読み込まれます。デプロイ後のレイテンシーを以前のビルドと比較する前に、しばらく待ってください。

<a id="ranking-and-thresholds"></a>

## ランキングとしきい値

`memory.search` は第 1 段階の候補プールをランク付けし、その後にランク項とゲートを適用します（[検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)）。ほとんどの項はデプロイごとに設定でき、リクエストごとに上書きもできるため、変更をまず 1 つの呼び出し元で A/B テストできます。

| 調整項目                           | 設定（リクエストフィールド）                                                                                                                                            | トレードオフ                                                                                                                         |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| 類似度ゲート                       | `LOOM_ADAPTIVE_THRESHOLDS`（デフォルトはオン）                                                                                                                          | オン：起動時にエンベッダー自身のコサイン分布からゲートを導出します。オフ：下記のスカラーゲートを自分で管理します。                   |
| スカラーゲート（フォールバック）   | `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`, `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`, `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`, `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD` | `text-embedding-3-small` 向けに調整されています。偽のエンベッダー、プローブの失敗、適応機能がオフの場合に使われます。               |
| フロア未満のヒット                 | `LOOM_MEMORY_SEARCH_FLOOR_MODE`（`floor_mode`）                                                                                                                         | `readmit` は `top_k` を満たすまで補充し、`below_floor` のタグを付けます。`ids_only` は本文が空の行で補充します。`drop` は少ない件数を返します。 |
| 候補プール                         | `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_STANDARD`, `_RERANK`, `_DEEP`。上限は `LOOM_MEMORY_SEARCH_MAX_POOL_TOP_K`                                                           | プールを広げると再現率とリランクの入力が増えます。その分、読み取るベクトル行とリランクのトークンが増えます。                         |
| リランクのスキップ                 | `LOOM_LLM_RERANK_SKIP_ABOVE`（適応ゲートにプローブがない場合のフォールバック）                                                                                          | ベクトル検索の最上位ヒットがすでに強い場合、リランク呼び出しをスキップします。                                                       |
| フォールバックカスケード           | `LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS`, `_MAX_WALL_MS`, `_MAX_LLM_CALLS`, `_MAX_VECTOR_QUERIES`                                                                       | 1 回目の結果が弱い場合に費やせる再試行（クエリ書き換え、HyDE）を制限します。                                                         |
| 強化                               | `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`（`reinforcement_weight`）, `_HALF_LIFE_DAYS`                                                                                  | 再呼び出しと良好な結果が行をどれだけ押し上げるかを決めます。0 で無効になります。関連度より低く抑えられます。                         |
| 出所                               | `LOOM_MEMORY_SEARCH_TRUST_WEIGHT`（デフォルトは 0）                                                                                                                     | 証明済みのソースを優遇する、上限付きの項です。                                                                                       |
| 新しさ                             | `LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT`（`recency_pool_weight`）                                                                                                       | 新しさを意図したクエリで、プールを新しい行寄りに再ランク付けします。0 でオフです。                                                   |
| コード形状クエリでの字句レッグ     | `rrf_adaptive_lexical`（リクエスト）, `LOOM_MEMORY_RRF_CODE_SHAPE_LO` / `_HI`                                                                                           | RRF において、パス形状やコード形状のクエリで字句レッグの重みを下げます。                                                             |
| デフォルトモード                   | `LOOM_MEMORY_SEARCH_DEFAULT_MODE`（`search_mode`）                                                                                                                      | 呼び出し元がモードもレシピも指定しない場合に使われます。                                                                             |
| キーごとの重複                     | `LOOM_MEMORY_SEARCH_PER_KEY_CAP`                                                                                                                                        | 1 つのキーが 1 ページに寄与できる行数です。                                                                                          |

> **警告：** あるエンベッダーで調整したしきい値は、別のエンベッダーでは適切ではありません。`LOOM_ADAPTIVE_THRESHOLDS=false` の場合や偽のエンベッダーを使う場合は、エンベッダーを変更するたびにベンチマークをやり直してください（`make bench-quick`、`make bench-quality-gate`）。`calibration.get` を使うと、名前空間で有効なゲートをどのティアが生成したかを確認できます。

<a id="vector-reads"></a>

## ベクトル読み取り

ベクトル列は `Array(BFloat16)` です。`enable_quantized_codec` を有効にできる場合、マイグレータは列をコード化した形式で作成します（[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)）。

| 調整項目                   | 設定                                                                                              | トレードオフ                                                                                                                                                                                                                                                                                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| コード優先スキャン         | `LOOM_VECTOR_SEARCH_QUANTIZED_CODES`（デフォルトはオン）、`LOOM_MEMORY_SEARCH_FACT_CODES_FIRST`（`auto`） | 量子化コードを読み取り、その後フル精度で再スコアリングします。`auto` は、`memory_embeddings` にパッチパートがある間はオフになります。この場合、再スコアリングがスキップされるためです。                                                                                                                                                                                  |
| コードのオーバーフェッチ   | `LOOM_VECTOR_SEARCH_QUANTIZED_FETCH_MULTIPLIER`、`LOOM_MEMORY_SEARCH_FACT_CODES_FETCH_MULTIPLIER` | 再スコアリングの前に、コード距離で残す行数です。値を小さくするとコストは下がりますが、上位ヒットが変わる頻度が高くなります。                                                                                                                                                                                                                                            |
| ファクト文ごとのスレッド数 | `LOOM_MEMORY_SEARCH_FACT_LEG_MAX_THREADS`（0 = 固定なし）                                         | スレッド数を固定すると、文ごとの CPU とピークメモリが制限されますが、レイテンシが多少増えます。                                                                                                                                                                                                                                                                         |
| 一括書き込みのピア読み取り | `LOOM_MEMORY_BULK_PEER_READ`（デフォルトは `batched`、`per_fact`）                                | `per_fact` は、`memory.set` と同様に、一括書き込みの各ファクトについて、競合ピアと値更新ピアをコード優先で読み取ります。大規模な名前空間向けです。バッチ化されたピア文が `MEMORY_LIMIT_EXCEEDED` で失敗し（サーバーログでは `batched conflict candidates failed`）、さらにファクトごとのフォールバックも失敗した場合、そのエントリは未チェックのまま書き込まれます。距離が同点の場合、ピアが異なることがあります。 |

既存のテーブルをコーデックやレイアウトに移行するには、`scripts/ops/vector_codec_rebuild.py` を使用します（ツインテーブル、整合性ゲートと再現率ゲート、`EXCHANGE`。[手順](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings)）。マイグレーション内で `ALTER` を使用しないでください。

<a id="clickhouse-connections-and-deadlines"></a>

## ClickHouse の接続とデッドライン

| 項目                      | 設定                                                                                                                                                                                | トレードオフ                                                                                                                                                                                                                                                   |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| プールサイズ              | `LOOM_CH_POOL_MAXSIZE`（デフォルトはプロセスあたり 64）                                                                                                                             | 想定されるリクエストの同時実行数にワーカー数を加え、さらにバッチ挿入が待機中に保持する接続数を足してサイズを決めます。ドライバー自体のデフォルト値は、大量の書き込みでは使い切られます。                                                                          |
| バッチ挿入                | `LOOM_CH_BATCHED_INSERTS`（デフォルトで有効）、`LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS`（10）、`LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS`（50）、`LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS`（200） | 同時に行われる挿入は 1 つのパートを共有し、それぞれ最大でウィンドウ分のレイテンシが加わります。単独の挿入は最小値だけ待機します。待機中の挿入はそれぞれプール内の接続を 1 つ保持するため、書き込みレートに応じてプールを大きくする必要があります（[バッチ挿入](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#batched-inserts)）。 |
| プール待機                | `LOOM_CH_POOL_WAIT_MS`（デフォルト 500）                                                                                                                                            | これを超えると、リクエストはキューに入らず 503 `OVERLOADED` で即座に失敗します。0 の場合はゲートなしです。                                                                                                                                                    |
| キープアライブ            | `LOOM_CH_KEEPALIVE_TIMEOUT`（デフォルト 5 秒）                                                                                                                                      | ロードバランサーのアイドル切断時間より短くしておく必要があります。これにより、Loom がプール内の接続を先に閉じます。                                                                                                                                              |
| ステートメントのデッドライン | `LOOM_CH_TIMEOUT_READ_S`、`LOOM_CH_TIMEOUT_WRITE_S`、`LOOM_CH_TIMEOUT_DDL_S`                                                                                                        | `max_execution_time` として、またプールのスロットを解放するクライアント側の上限として適用されます。0 の場合、そのクラスは無制限です。                                                                                                                            |
| トラフィッククラスごとの優先度 | `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS`（JSON）                                                                                                                                         | クラス（`production`、`interactive`、`bench`、`background`）ごとの `priority` と `max_threads` を指定します。これにより、ベンチとワーカーが本番の読み取りに道を譲ります。                                                                                        |
| Read-your-writes          | `LOOM_CH_READ_YOUR_WRITES`、`LOOM_CH_READ_YOUR_WRITES_WINDOW_S`                                                                                                                     | 書き込み後に逐次一貫性のある読み取りを行います。レプリケーションされたサービスではコストがかかり、単一ノードでは何もしません。                                                                                                                                  |
| オペレーションの予算      | `LOOM_LATENCY_BUDGET_MULTIPLIER`                                                                                                                                                    | すべての op の `budget_p99_ms` を拡大します。ClickHouse への WAN 経由のホップでは約 20 が必要です。予算は `budget.violated` イベントを発生させるだけで、リクエストを打ち切ることはありません。                                                                    |

Web の処理能力は、コンテナあたりの `LOOM_WEB_WORKERS` にコンテナ数を掛けた値です。レートリミッターは、`loom.instances` で数えた稼働中のレプリカ数で上限を割りますが、`LOOM_REPLICA_COUNT` を下回ることはありません（[レプリカ](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md#rate-limits)）。

<a id="providers"></a>

## プロバイダー

| 調整項目 | 設定 | トレードオフ |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| クエリ埋め込みの上限 | `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES` | クエリ埋め込みの1回ごとの試行に上限を設けます。リトライ後も完了しない場合、検索は 503 `EMBED_TIMEOUT` を返します。 |
| クエリの綴り | `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS` | この語数以下のクエリは、埋め込みと並行して名前空間を1回読み取り、小文字の単語をコーパスの綴りで埋め込みます。40k 行の名前空間で 100–300 ms かかり、結果は1分間再利用されます。`0` で無効になります。 |
| クエリ埋め込みのキャッシュ | `LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`, `LOOM_EMBEDDING_CACHE_SHARED`, `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS` | 共有の `loom.embedding_cache` の手前にプロセスごとの LRU を置きます。繰り返されるクエリはプロバイダー呼び出しを省略します。 |
| クラスごとの LLM レスポンスキャッシュ | `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` (JSON) | LLM のレスポンスをキャッシュし（`bench` ではデフォルトで有効）、クラスごとに strong ティアや mid ティアの利用を禁止できます。 |
| フォールバックモデル | `LOOM_LLM_FALLBACK_MODEL` | リトライ後もプライマリが利用できない場合、op を失敗させる代わりにこのモデルが応答します。 |
| 支出上限 | `LOOM_DEFAULT_DAILY_COST_CAP_USD`（名前空間ごとの `daily_cost_cap_usd`） | 上限を超えると、LLM を使うワーカーはその名前空間をスキップし、`memory.ask` は `budget_capped` で回答を控えます。通常の読み取りと書き込みは継続します。 |

<a id="embedder-choice"></a>

### エンベッダーの選択

| エンベッダー (`LOOM_EMBEDDING_MODEL`) | トレードオフ |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| `text-embedding-3-large`, `gemini-embedding-001` | Loom のデフォルトである 3072 次元です。品質とコストは、実際のデータで測定する必要があります。 |
| `text-embedding-3-small` | より狭い 1536 次元のベクトルを使う選択肢です。次元を変更すると再埋め込みが必要です。 |
| `local`, `local:bge-small`, `local:bge-base`, `local:minilm` | プロセス内で CPU により埋め込みを行います。プロバイダー呼び出しもキーも不要です。品質と引き換えにレイテンシとコストを抑えます。 |
| `fake-ngram-3072` | 標準の次元で文字 n-gram を使います。テストと配線の確認専用で、再現率の数値には意味がありません。 |

エンベッダーを変更するとベクトル空間が変わります。`LOOM_EMBEDDING_DIM` を合わせて設定し、`loom reembed` を実行してください。適応型しきい値が有効な場合は、起動時のプローブに再較正させます。変更の前後で `make bench-quick` を実行してください。抽出プロンプトが較正されている `LOOM_LLM_MODEL` についても同様です。

<a id="workers-and-batches"></a>

## ワーカーとバッチ

すべてのワーカーには `*_ENABLED` スイッチと `*_INTERVAL_SECONDS` による実行間隔があり、多くのワーカーには、何も見つからなかったパスの後にスリープする `*_IDLE_INTERVAL_SECONDS` もあります（[ワーカーのリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)）。間隔を短くするとバックグラウンド処理の遅延は小さくなりますが、スキャンの回数が増えます。アイドル間隔により、空のキューの負荷は低く抑えられます。

| ワーカー              | 調整項目                                                                                                                                                                                                                             |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `embedding_worker`    | `LOOM_EMBEDDING_WORKER_INTERVAL_SECONDS`、`_IDLE_INTERVAL_SECONDS`。`LOOM_EMBEDDING_WORKER_ORPHAN_FULL_SCAN_SECONDS` / `_ORPHAN_RECENT_SECONDS` は孤立レコードのアンチジョインの範囲を制限します。                                    |
| `extraction_queue`    | `LOOM_EXTRACTION_QUEUE_INTERVAL_SECONDS`、`_MAX_BATCHES_PER_PASS`、`_NAMESPACE_CONCURRENCY`、`_LEASE_SECONDS`。`LOOM_EXTRACTION_DEFER_AGENTS` は、列挙したエージェントの `set_from_messages` を後回しにし、抽出の前にリクエストが返るようにします。 |
| `memory_expiry_sweep` | `LOOM_MEMORY_EXPIRY_SWEEP_BATCH_SIZE`。                                                                                                                                                                                               |
| `task_lease_sweep`    | `LOOM_TASK_LEASE_SWEEP_INTERVAL_SECONDS`、`_IDLE_INTERVAL_SECONDS`。                                                                                                                                                                 |

ワーカーは `background` トラフィッククラスで動作するため、`LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` でも ClickHouse の優先度とスレッド数が制限されます。ワーカープロセスは必ず 1 つだけ実行してください。LLM を使うワーカーを 2 つ起動すると、スキャンとコストが 2 倍になります。コードプレーンのインジェストワーカーとコンパイラティアのワーカーには、それぞれ独自の調整項目があります（[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)）。

<a id="client-side-levers"></a>

## クライアント側の調整項目

大きな効果が得られるものの多くは、リクエスト側にあります。

- 取得する本文を減らす：`memory.search` では `bodies` と `body_budget_tokens`、`context.pack` と `code.search` では `token_budget` を指定します。
- ステップごとに検索するのではなく、タスクごとに一度だけコンテキストを読み込む（`context.pack`）。
- `retrieval_budget` を使って、呼び出しごとに低コストな検索プロファイルか、より深い検索プロファイルを選ぶ。
- 冪等な op は `Idempotency-Key` ヘッダー付きで再試行する（SDK が自動で生成します）。リプレイすると、op を再実行せずに保存済みのレスポンスが返されます。

<a id="related"></a>

## 関連項目

- [障害と復旧](../architecture/failure-and-recovery.md)：キャパシティとスケールの対象
- [モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)
- [ベンチマークの実行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)
- [デバッグ](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)
