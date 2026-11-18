<!-- i18n: source=docs/operations/runtime-settings.md blob=b519ead8aee373492c371073baaa1fe49501d971 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md), which is authoritative.

<a id="runtime-settings"></a>

# ランタイム設定

すべての設定は起動時に読み込まれる環境変数 `LOOM_<FIELD>` であり、YAML 設定ファイルにも記述できます（[設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)）。リファレンスで **live** と記された設定は、Loom の実行中にデプロイ全体へのオーバーライドも受け付けます（検索とランキングの `memory_search_*`、回答合成の `memory_ask_*`、抽出、コンテキスト、ナレッジのチューニング、ClickHouse のインサートバッチ処理 `ch_batched_insert*` など）。それ以外の設定は起動時に一度だけ読み込まれ、変更には再起動が必要です。キャリブレーションが置き換える類似度ゲート（`memory_search_min_similarity`、`memory_search_abstain_threshold`、`memory_search_hyde_fallback_threshold`、`memory_search_query_rewrite_threshold`）は live ではありません。`LOOM_ADAPTIVE_THRESHOLDS` が有効な場合、これらはキャリブレーションが存在しないときにのみ適用されます。

<a id="see-what-is-in-force"></a>

## 有効な設定を確認する

```bash
loom settings list --changed              # off its default, or overridden
loom settings list --prefix memory_ask_   # one area
loom settings list --json
```

各行には、値、その取得元、live かどうかが表示されます。

```text
memory_ask_recency_weight = 0.35  (override, live)
ch_host = "clickhouse"  (env)
```

| ソース          | 意味                                                                                     |
| --------------- | ---------------------------------------------------------------------------------------- |
| `override`      | `loom settings set` または `/v1/admin/settings.set` で設定された値                       |
| `dashboard`     | Settings → Models & keys に保存されたモデル設定                                          |
| `env`           | プロセスの環境変数                                                                       |
| `env_file`      | dotenv ファイル（`LOOM_ENV_FILE`、デフォルトは `.env`）。環境にコピーされた場合も含む    |
| `settings_file` | YAML 設定ファイル（`LOOM_SETTINGS_FILE`）。`scripts/serve.sh` がエクスポートした場合も含む |
| `inferred`      | 起動時に他の設定から補完された値（LLM プロバイダー、ローカルモデルの次元数）             |
| `default`       | 組み込みのデフォルト値                                                                   |

シークレットは長さのみが表示されます。

<a id="override-a-live-setting"></a>

## live 設定をオーバーライドする

```bash
loom settings set memory_ask_recency_weight 0.35
loom settings reset memory_ask_recency_weight
```

値は対応する環境変数と同じ形で書き込まれ、同じ方法で検証されます。範囲外の値や live ではない設定は拒否され、何も保存されません。オーバーライドは `loom.runtime_overrides` の 1 行です。保存したプロセスは即座に適用し、ワーカーを含む他のすべてのプロセスは `LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS`（デフォルトは 10）以内に適用します。オーバーライドは再起動後も残り、リセットするまで環境変数や設定ファイルより優先されます。リセットすると、各プロセス自身の値またはデフォルト値に戻ります。`reset` は保存済みのオーバーライドがない設定を拒否し、後のビルドに存在しなくなった設定のオーバーライドは削除します。変更のたびに、変更前後の値を含む `setting.changed` 監査行が書き込まれます。

`--expect <value>`（HTTP では `expected`）を指定すると、保存済みオーバーライドを新たに読み直した後に有効な値がその値である場合にのみ変更が行われます。そうでない場合は、見つかった値とともに `409 PRECONDITION_FAILED` を返します。これにより、別のレプリカや CLI からその後に行われた変更を、気づかずに上書きすることがありません。ただし、同時に送信された 2 つの変更が両方とも通る可能性は残ります。

<a id="see-and-undo-past-changes"></a>

## 過去の変更を確認して元に戻す

```bash
loom settings history memory_ask_recency_weight
loom settings history --limit 20      # every setting
```

各行は記録された 1 件の変更で、新しいものから順に、日時、実行者（`cli:<user>` またはトークンのサブジェクト）、変更前後の値が表示されます。変更前の値がオーバーライドだった場合、それはデプロイの値です。`loom settings set` でその値に戻すことで元に戻せます。それ以外の場合、変更前の値は変更を行ったプロセスの値（その環境変数またはデフォルト）であり、`(undo: reset)` と表示されます。この場合は `loom settings reset` で元に戻します。これにより各レプリカ自身の値に戻ります。変更前の値がない変更（このビルドに存在しなくなった設定のリセット）には、元に戻すものがありません。

オーバーライドはデプロイのデフォルトであるため、名前空間やリクエストで設定した値がそれより優先されます。たとえば、名前空間の `retrieval_defaults`（`top_k`、`min_similarity`、`reinforcement_weight`、およびそのほか `namespace.register` が受け付ける検索パラメーター）や、リクエスト自身のフィールドです。

HTTP 経由でも同じ操作ができます。プラットフォーム管理者トークンが必要です。認証が無効の場合、変更はループバックアドレス（`localhost`）からのみ受け付けられます。そうしないと、Web ページが DNS リバインディングを通じて変更できてしまうためです。

```bash
curl -s "$LOOM_BASE_URL/v1/admin/settings.list" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"changed": true}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "value": 0.35}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "clear": true}'
```

`POST /v1/admin/settings.history` は `{"name": "...", "limit": 50}` を受け取ります。Python SDK には `agent.deployment.settings()`、`set_setting(name, value, expected=...)`、`reset_setting(name, expected=...)`、`setting_history(name)` があります。

<a id="during-a-rolling-upgrade"></a>

## ローリングアップグレード中の動作

その設定を持たないビルド、live で読み込まないビルド、あるいはフィールドが保存された値を拒否するビルドは、保存されたオーバーライドを一度ログに記録して無視します。他のプロセスは引き続きそれを適用します。ランタイム設定が導入される前のビルドは、保存されたオーバーライドを何も通知せずに無視するため、そのようなビルドが稼働している間は、オーバーライドは新しいレプリカにのみ適用されます。ランキング設定は `memory.search` が返す結果を変えます。変更を維持する前に、ベンチマークで効果を測定してください（[ベンチマークの実行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。
