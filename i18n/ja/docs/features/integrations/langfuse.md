<!-- i18n: source=docs/features/integrations/langfuse.md blob=6a704ef0765498c439dcf20e7c5ac032a353ecb4 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md), which is authoritative.

<a id="langfuse"></a>

# Langfuse

`LOOM_LANGFUSE_URL` と `LOOM_LANGFUSE_ADMIN_TOKEN` が必要です。両方を設定するまで、何も送信されません。両方を設定すると、OpenTelemetry のスパンツリーが OTLP 経由で Langfuse に転送されます。Langfuse は任意であり、Loom のローカル Compose スタックには含まれません。

スパンのエクスポートと消去は、セルフホスト版の **Langfuse 3.227.0** と **Langfuse 4.76.0** に対してテストされています。Langfuse Cloud は Langfuse 4 で動作しています。ローカルの Langfuse デプロイメントでは PostgreSQL、ClickHouse、Redis/Valkey、S3 互換オブジェクトストレージも動作するため、2 サービス構成の Loom プロファイルよりもかなり大規模になります。

Loom は独自のテレメトリを ClickHouse に保存します（[テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)）。Langfuse は、リクエストごとのトレース（ネストされたモデル呼び出しを含む）、生成ごとのコスト、フィルタ可能なスコア、プロンプトのバージョンを追加します。Loom は Langfuse に対して次の 4 つの役割を担います。

| 役割 | Loom の動作 | 必要なもの |
| --------------- | ------------------------------------------------------------------------------------------------- | ----------------------------- |
| トレースライター | 各リクエストの OTel スパンツリーを、その生成（generation）とともに OTLP 経由で転送します | `pk:sk` トークン、OTel が有効であること |
| スコアライター | 各検索（search）と質問（ask）のトレースに、検索品質のスコアを書き込みます | `pk:sk` トークン |
| プロンプトリンカー | コミット済みのロックファイルをもとに、各生成に、使用した Langfuse のプロンプト名とバージョンをタグ付けします | ロックファイル。実行時の呼び出しは不要 |
| 消去バックエンド | 消去されたユーザーまたは名前空間のトレースを削除します | URL とトークン（任意の形式） |

Loom はプロンプト取得の経路には**含まれません**。Langfuse でプロンプトを自前管理するエージェントは、Langfuse SDK の `get_prompt` で取得します。

<a id="setup"></a>

## セットアップ

```bash
LOOM_LANGFUSE_URL=https://langfuse.example.com
LOOM_LANGFUSE_ADMIN_TOKEN=pk-lf-...:sk-lf-...
```

トークンは `public_key:secret_key` の組で、HTTP Basic として送信されます。単一のベアラートークン（`:` を含まないもの）も消去には引き続き使えますが、トレースとスコアの送信は無効になります。その場合、サーバーはその旨の警告を1回ログに出力します。

結果は `GET /v1/health?include=langfuse` で確認します。このエンドポイントはローカルのエクスポーターの状態を返し、`scores` と `retrieval_judge` の下に、スコアの送信に成功した件数、失敗した件数、破棄された件数（キューが満杯、ブレーカーが開いている場合）を示します。これはリモートの Langfuse に対する準備完了のプローブではありません。両方の設定が存在する場合、起動ログの `mode` トークンに `langfuse` が含まれます。

<a id="traces"></a>

## トレース

Loom は ClickHouse エクスポーターと並べて 2 つ目のスパンプロセッサーを追加し、OpenTelemetry のスパンツリーを Langfuse の OTLP エンドポイントへ転送します。トレースには、実際の入れ子構造（HTTP スパン、ハンドラー、各 generation）、セッション ID、generation ごとのコストが含まれます。チャット呼び出しは `GENERATION` オブザベーション、プロバイダーに到達した埋め込み呼び出しは `EMBEDDING` になります。Loom 自身のキャッシュから返された埋め込みは通常のスパンです。呼び出し元が解決された後にリクエストが開くすべてのスパンには、そのリクエストのユーザー ID、セッション ID、`org:` タグ、`namespace:` タグ、トレース名が付きます。Langfuse 4 はこれらをトレースのルートではなくオブザベーションごとに読み取るためです。これらのキーは ClickStack のスパンにも付与されます。各検索ステージ（`search.<stage>`）は `retriever` オブザベーションとなり、Langfuse のエージェントグラフビューの描画に使われる種別の 1 つです。OTel を無効にすると（`LOOM_OTEL_ENABLED=false`）、Langfuse にトレースは届きません。

| 設定                                                                          | デフォルト                                          | 効果                                                                                                     |
| ----------------------------------------------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `LOOM_LANGFUSE_SAMPLE_RATE`                                                   | `1.0`                                               | 転送するトレースの割合。トレース ID でサンプリングするため、トレースは丸ごと届きます                     |
| `LOOM_LANGFUSE_SPAN_DENYLIST`                                                 | `ch.query,ch.insert,GET /v1/health,POST /v1/health` | 転送しないスパン名のプレフィックス（インフラ系のスパンは ClickStack に置くのが適切です）                 |
| `LOOM_LANGFUSE_CIRCUIT_FAILURE_THRESHOLD`, `LOOM_LANGFUSE_CIRCUIT_COOLDOWN_S` | `5`, `30`                                           | ブレーカー：スコアの POST が N 回連続で失敗すると、クールダウンの間スコアを破棄し、その後 1 回だけ試行します |

Langfuse が Loom のリクエストパスに入ることはありません。ブレーカーは、失敗したスコア POST がリトライの負荷になるのを防ぎます。OTLP バッチプロセッサーは独自の配信キューを持つため、スパンのエクスポート失敗ではブレーカーは開き**ません**。したがって、`loom.langfuse.breaker` ゲージ（0 は closed、1 は half-open、2 は open）が示すのはスコア送信の状態であり、リモートの可用性ではありません。ワーカーの消費トレースは、ワーカーの org と名前空間を付けて `worker.<name>` と命名されます。トレースのルートとなる埋め込みスパン（埋め込みのバックフィル）とワーカーのパスのルートは転送されません。リクエストパス上の埋め込みはハンドラースパンの下に入れ子になり、転送されます。

> **ヒント:** スパン量の大半はワーカーの埋め込みが占めます。本番環境では、Langfuse を設定する前に `LOOM_LANGFUSE_SAMPLE_RATE` を下げてください。

<a id="what-text-leaves-loom"></a>

## Loom から外部に出るテキスト

Langfuse に届くユーザーコンテンツの範囲は次のとおりです。

- **リクエストとレスポンスのペイロード。** 送信されません。スパンにはリクエストボディもレスポンスボディも含まれません。
- **Loom 自身のモデル呼び出しのプロンプトと補完テキスト。** プロンプトの内容によって記録の可否が決まります。

  | `LOOM_LANGFUSE_CAPTURE_IO_LEVEL` | 記録内容                                                                                                              |
  | -------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
  | `off`（デフォルト）              | なし                                                                                                                  |
  | `query`                          | プロンプトがユーザー自身の検索テキストである呼び出しのみ（HyDE、クエリの書き換え、エンリッチメントなど）             |
  | `all`                            | 取得したメモリや会話のターンを含む呼び出しも対象（リランク、反復判定、抽出、セッション要約など）                      |

  名前空間の `capture_generation_io`（`namespace.register` で設定）は、この設定を上書きします。`false` はサーバー側の設定にかかわらず off、`true` は `all` を意味し、未設定の場合はサーバー側の設定を継承します。各フィールドは `LOOM_LANGFUSE_GENERATION_IO_MAX_CHARS`（2000）で上限が設けられ、切り詰めた箇所には明示的なマーカーが付きます。同じレベルが `loom.search_trace_log.query` も制御します。

トークン数、コスト、レイテンシ、品質スコア、プロンプトバージョンへのリンクにはユーザーコンテンツが含まれないため、制御の対象にはなりません。

<a id="scores"></a>

## スコア

`server/observability/langfuse_scores.py` は、各検索のトレースに検索品質のスコアを投稿します：`retrieval_confidence`、`top_similarity`、`abstained`、`abstain_reason`。`code.ask` は `code_ask_cache_hit` と `code_ask_coverage` を追加します。`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE` を 0 より大きく設定すると（デフォルトは 0。サンプリングされた検索ごとにモデル呼び出しが 1 回発生します）、検索と状態を一切共有しない独立した LLM ジャッジが `retrieval_relevance` スコアを書き込みます。

スコアの投稿はリクエストパスから切り離されています。上限付きのキューをバックグラウンドで処理し、Langfuse が遅い場合は古いものから順に破棄して、破棄した数を記録します。スコアは、そのトレースが転送された場合（同じサンプリングゲート）にのみ投稿され、同じブレーカーの背後に置かれます。

<a id="prompt-versions"></a>

## プロンプトのバージョン

Loom のシステムプロンプトは Python の定数です。`scripts/codegen/push_langfuse_prompts.py` がそれらを Langfuse に登録し、`sha256(prompt)[:12]` をキーとして `server/observability/langfuse_prompts.lock.json` を書き出します。実行時には `server/observability/genai.py` が、送信したプロンプトをこのロックファイルから探し、Langfuse を呼び出すことなく、Langfuse のプロンプト名と整数のバージョンを generation にタグ付けします。編集後にプッシュされていないプロンプトは、誤ったバージョンに紐付けられることなく、未リンクのままになります。

```bash
python scripts/codegen/push_langfuse_prompts.py          # plan against the lockfile, offline
python scripts/codegen/push_langfuse_prompts.py --write  # push, then write the lockfile
```

`--write` には Langfuse の両方の設定が必要です。テキストがすでに最新バージョンと同じプロンプトは、再度プッシュされません。

`POST /v1/admin/prompts.evaluate`（プラットフォーム管理者向け）は、あるプロンプトバージョンについて、そのトレースのルーブリックスコアを最小件数としきい値と照らし合わせ、昇格すべきかどうかの判定を返します。プロンプトバージョンに紐付けられた各 generation は、そのトレースに `prompt:<name>:<version>` というタグを付けます。評価ではそのタグを持つトレースを最大 200 件と、そのスコアを読み取ります。これは Langfuse 3 と 4 のどちらでも同様です。Langfuse への書き込みは行いません。Langfuse が設定されていない場合は 501 `INTEGRATION_NOT_CONFIGURED` を返し、Langfuse を読み取れない場合は 502 `BACKEND_UNAVAILABLE` を返します。

<a id="erasure"></a>

## 消去

`erasure.request` は、saga の 1 ステップとして Langfuse バックエンド（`server/erasure/app/backends.py` の `LangfuseBackend`）を実行します。

- **ユーザースコープ**では、`userId` と `org:<org>` タグで絞り込んだユーザーのトレースを検索し、`DELETE /api/public/traces` でバッチ削除します。トレースを削除すると、そのオブザベーションとスコアも削除されます。org での絞り込みが必要なのは、すべての org が、キーペアから決まる 1 つの Langfuse プロジェクトに書き込むためであり、`user_id` は org 内でのみ一意だからです。
- **名前空間スコープ**では、`namespace:<ns>` と `org:<org>` の両方を持つトレースを削除します。

検索方法はサーバーのメジャーバージョンによって異なります。バージョンは `GET /api/public/health` から読み取ります。Langfuse 3 ではトレースの一覧（`GET /api/public/traces`）を取得します。Langfuse 4 ではこの一覧が提供されないため、オブザベーションの一覧（`GET /api/public/v2/observations`）を取得し、そこからトレース ID を集めます。Langfuse 4 の削除は非同期で行われるため、削除したトレースが数分間見え続けることがあります。サーバーのバージョンは、消去ステップごとに 1 回読み取ります。

Langfuse 3 からアップグレードしたデプロイメントでは、Langfuse 4 の検索はオブザベーション一覧にあるものだけを見つけ、消去はそれ以外について何も報告しません。Langfuse 3.227.0 が保存したトレースは一覧に含まれます（4.76.0 へのインプレースアップグレードでテスト済み）。Loom は `x-langfuse-ingestion-version: 4` ヘッダーを送信し、このリリースは Langfuse 4 が読み取る場所にトレースを書き込むためです。それより前の Langfuse 3 のリリースが保存したトレースについては、アップグレード後、消去に頼る前に Langfuse の historic backfill を実行してください。

このステップはフェイルクローズです。バージョンのないヘルス応答、5xx、タイムアウト、削除エンドポイントからの 404（古いセルフホスト版、または GET と POST のみを転送するプロキシ）が起きると例外を送出し、saga はリクエストを `failed` として記録し、`erasure_recovery` ワーカーが再試行します。すでに削除したトレースの削除は成功するため、再実行は冪等です。Langfuse 4 では、削除待ちのトレースを再度見つけて削除することがあります。Langfuse が未設定の場合は、スタブバックエンドが意図をログに記録し、0 を報告します。`LOOM_EXTERNAL_ERASURE_TIMEOUT_S`（30）が各呼び出しの上限時間を決めます。[保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)も参照してください。

<a id="joining-a-request"></a>

## リクエストの突き合わせ

Loom が `X-Request-Id` で返すリクエスト ID は、Langfuse のトレースメタデータ、`loom.agent_events.request_id`、および `loom.request_id` スパン属性に含まれています。これにより、Langfuse から Loom の台帳までリクエストを追跡できます。詳細は[システム間でのリクエストの突き合わせ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#joining-a-request-across-systems)を参照してください。

<a id="related"></a>

## 関連項目

- [テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)：同じスパンの ClickHouse 側の情報です。
- [ClickStack](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)。
- [設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#observability)：すべての `LOOM_LANGFUSE_*` 設定を掲載しています。
