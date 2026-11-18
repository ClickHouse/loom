<!-- i18n: source=docs/development/code-generation.md blob=3f6e27200eccce850139d9114282249d0644e35f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md), which is authoritative.

<a id="code-generation"></a>

# コード生成

リポジトリ内の複数のファイルは、コードから生成されています。SDK のオペレーションテーブルとワイヤ型、リファレンスページ、Codex プラグインのコピー、op reach マニフェスト、スキーマダンプがそれにあたります。いずれもコミットされているため、読む側は何も実行せずに内容を確認できます。また、以下で示すジェネレーターには `--check` モードがあり、コミット済みのファイルが生成される内容と異なる場合は終了コード 1 で終了します。`make lint` はこれらのチェックをそれぞれ実行します。CI も `lint` ジョブ、`sdk-typescript` ジョブ、およびユニットスイートでこれらを実行するため、ファイルが古い場合は、実行すべきコマンドが示されたうえで失敗します。

**生成ファイルをコミットする理由。** 必要なときにだけビルドされる生成ファイルは、レビューでは見えません。オペレーションの入力モデルを変更すると SDK の型も変わりますが、誰も読む差分は生まれません。生成ファイルをコミットすれば、変更の影響が変更そのものの隣に表れ、`--check` ゲートによって両者がずれることを防げます。その代償として、ソースを変更する際には、同じ変更の中で再生成の手順が必要になります。

<a id="the-generators"></a>

## ジェネレータ

すべて [`scripts/codegen/`](https://github.com/ClickHouse/loom/blob/main/scripts/codegen) にあり、`scripts/codegen/common.py` を共有します（リポジトリのルート、SDK テーブルが op ごとに持つ情報、ruff によるフォーマット、アトミックな書き込み、書き込みまたは検査のプロトコル）。

| スクリプト                  | 書き出すもの                                                                                                                                                                                                           | 入力                                                                                                                                                                                                  | `make lint` での `--check` |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `sdk_py_gen.py`             | `sdk/python/loom/ops.py`：`OpName`、`OPS` テーブル（冪等性、ロール、成熟度、レイテンシ予算）、入力モデルと出力モデルごとの `TypedDict`                                                                                | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | あり                       |
| `sdk_py_client_gen.py`      | `sdk/python/loom/client.py`：`OpsClient`、op ごとに 1 つのメソッド（`agent.client.tracker.source_add`）、`ops.py` の名前で型付け                                                                                       | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | あり                       |
| `sdk_py_admin_gen.py`       | `sdk/python/loom/admin_routes.py`：`AdminRoutes`、`/v1/admin/*` ルートごとに 1 つのメソッド（`agent.deployment.routes.settings_set`）                                                                                  | `/v1/admin/*` のルーター：各ルートのメソッド、パス、ゲート、docstring                                                                                                                                | あり                       |
| `sdk_ts_gen.py`             | `sdk/typescript/src/generated/ops.ts`：モデルごとのインターフェース、`OPS` テーブル、`OpMap` 型                                                                                                                       | `server.operations.registry.ALL_OPERATIONS`（モデルの JSON Schema）                                                                                                                                   | あり                       |
| `agent_plugin_sync.py`      | Codex プラグインが持つフックスクリプト（`hooks/*.mjs`）、共有ライブラリ（`lib/*.mjs`）、スキル、掲載用アイコン（`assets/`）のコピー。実行ビットを含めてバイト単位で同一                                               | `integrations/claude-code-plugin/`                                                                                                                                                                    | あり                       |
| `settings_reference.py`     | 生成される[設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)                                                                                                            | `server/settings/catalog.py`：各フィールドの環境変数、型、デフォルト値、その上のコメント                                                                                                              | あり                       |
| `settings_schema.py`        | `schemas/loom-settings.schema.json`、YAML 設定ファイルの JSON Schema                                                                                                                                                   | 設定フィールドの型、範囲、デフォルト値、コメント（シークレットは除く）                                                                                                                                | あり                       |
| `settings_example.py`       | `loom.example.yaml`、設定ファイルのテンプレート：シークレットを除くすべての設定を、説明の下にデフォルト値でコメントアウトして領域ごとに並べたもの                                                                    | 設定カタログのフィールド、デフォルト値、コメント、live マーク                                                                                                                                         | あり                       |
| `docs_reference.py`         | `docs/reference/` の生成領域：`operations`、`workers`、`cli`、`errors`、`schema`、`status_ops`、`status_switches`、`admin_routes`（[オペレータールートの表](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#operator-routes)） | レジストリ、`server/workers/registry.py`（`_WORKERS`）、`tests/unit/snapshots/cli_tree.json`、`server/core/errors.py`、`schemas/schema.sql`、`server/settings/catalog.py`、`/v1/admin/*` のルーター | あり                       |
| `op_reach_census.py`        | `server/operations/reach_manifest.py`（`--write-manifest`）。それ以外では集計表                                                                                                                                       | レジストリと MCP ツールセット                                                                                                                                                                         | あり                       |
| `wire_changelog.py`         | なし。2 つのリビジョン間でクライアントにとって何が変わったかを出力する                                                                                                                                                 | 各リビジョンで構築した機能カタログ                                                                                                                                                                    | なし                       |
| `gen_py_library_methods.py` | `server/code/domain/extract/py_library_methods.py`                                                                                                                                                                     | 実行中のインタープリターの標準ライブラリ                                                                                                                                                              | なし（インタープリター依存） |
| `push_langfuse_prompts.py`  | `server/observability/langfuse_prompts.lock.json`（`--write`）                                                                                                                                                         | サーバー内のプロンプト定数。これらを Langfuse にプッシュする                                                                                                                                          | なし                       |

設定リファレンスはコメントから生成されるため、設定の上にあるコメントがそのままドキュメントになります。ページではなくコメントを編集してください。`docs/reference/` のページは、`<!-- BEGIN GENERATED: <name> -->` … `<!-- END GENERATED: <name> -->` の領域を手書きの文章で囲んだ構成です。書き換えられるのは一対のマーカーの間のテキストだけなので、ページ上でそのテーブルを説明できます。

<a id="sdk_py_gen-and-sdk_ts_gen"></a>

### sdk_py_gen と sdk_ts_gen

Python SDK は dict を受け取り dict を返す形のままで、生成ファイルが名前を加えます。これにより、エディタは `agent.call("memory.search", …)` を補完し、型チェッカーは手書きのモデル層なしでフィールド名を認識します。dict は `total=False` で、サーバーが必須とするフィールドには `Required[...]` が付きます。TypeScript のジェネレータは各モデルの JSON Schema をインターフェースに変換します（enum と const はリテラルのユニオンに、`anyOf` はユニオンに、共有される `$defs` は一度だけ巻き上げます）。そのため `client.call("memory.search", input)` は端から端まで型付けされます。op の登録やモデルを変更したら、毎回実行してください。

```bash
python scripts/codegen/sdk_py_gen.py
python scripts/codegen/sdk_py_client_gen.py
python scripts/codegen/sdk_ts_gen.py
```

両方の SDK は、すべての op を同じ位置のメソッドとして公開します。グループは op 名の最初のセグメント、メソッドは残りを `.` で連結したもので、Python では snake_case（`agent.client.tracker.source_add`）、TypeScript では camelCase（`loom.tracker.sourceAdd`）です。2 つの op テーブル、またはいずれかの op の 2 つのメソッド名が異なると、`tests/unit/test_sdk_parity.py` が失敗します。

<a id="agent_plugin_sync"></a>

### agent_plugin_sync（プラグインの同期）

2 つのコーディングエージェント用プラグインの違いは、マニフェストとフックの配線（`hooks/hooks.json`）だけです。Codex プラグインは `../` 参照ではなくコピーを持つため、各プラグインはマーケットプレイスが単独でチェックアウトできる自己完結したディレクトリになります。Claude Code プラグインにフックスクリプトやスキルを追加したら、同期を実行してコピーをコミットしてください。Codex に実行させたいフックは、Codex の `hooks.json` に手作業で配線します。

<a id="op_reach_census"></a>

### op_reach_census（op の到達経路の集計）

すべての操作は成熟度を宣言し、生の HTTP `POST` 以外の到達経路を持つか、`experimental` と宣言されている必要があります。到達の種別（`server/operations/reach.py`、`REACH_KINDS`）は、MCP の一覧（`mcp_core`、`mcp_atomic`、`mcp_all`。`server/operations/toolsets.py` からその場で導出）、SDK メソッド（`sdk_method`）、同梱のサンプルまたはオペレーター用スクリプト（`sdk_example`）、プラグインのフックまたは統合（`plugin_hook`）、ダッシュボードの呼び出し箇所（`dashboard`）です。後ろの 4 つはツリーを走査して得ますが、デプロイされたサーバーにはツリーがないため、`reach_manifest.STATIC_REACH` としてコミットされています。`--check` は次の点を検査します。この規則、ダッシュボードの呼び出し箇所を持たないすべての op が（持たない理由とともに）`server/operations/dashboard.py` にあること、コミットされたマニフェストがツリーと一致していること。`--write-manifest` はマニフェストを更新します。`--call-counts --days 30` は `loom.agent_events` から各 op の呼び出し回数を加えます（ClickHouse に接続する唯一のモードです）。`--owners` は、各 experimental op の宣言を導入したコミットを示します。

<a id="wire-changelog"></a>

### ワイヤー変更履歴

```bash
python scripts/codegen/wire_changelog.py <rev_a> <rev_b>           # markdown
python scripts/codegen/wire_changelog.py --since-hours 24 [rev_b]   # rev_a = rev_b's ancestor N hours back
python scripts/codegen/wire_changelog.py <rev_a> <rev_b> --json
python scripts/codegen/wire_changelog.py --snapshot <rev> > snap.json
python scripts/codegen/wire_changelog.py --diff a.json b.json
```

各リビジョンを `git archive` で展開し、子インタープリターでそのレジストリをインポートして、追加または削除された操作とリクエスト・レスポンスのフィールド、さらにロール、予算、MCP ティアの変更を出力します。出力はリリースノート（[リリースプロセス](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)）に入ります。「クライアントにとっての変更点」のセクションは、手書きではなく生成されます。

<a id="gen_py_library_methods"></a>

### gen_py_library_methods（標準ライブラリのメソッド一覧の生成）

Python エクストラクターが使う標準ライブラリのメソッド名の集合は、インタープリターのアップグレードで呼び出しエッジが動かないよう、ツリー内に固定されています。再生成するとエクストラクターのモジュールが変わり、コードインデックスのバージョンスタンプが動いて、インデックス済みのすべての名前空間が再チャンク化されます。差分を確認し、意図したときにだけ再生成してください。

<a id="push_langfuse_prompts"></a>

### push_langfuse_prompts（Langfuse へのプロンプトのプッシュ）

サーバーは、プロンプトのコンテンツハッシュをコミット済みのロックファイルで検索することで、LLM の生成を Langfuse のプロンプトバージョンに紐付けます。実行時の呼び出しはありません。`--write` を付けない場合、スクリプトはロックファイルを基にオフラインで計画を立てます。`--write` を付けると、変更されたプロンプトをプッシュし（`LOOM_LANGFUSE_URL` と `LOOM_LANGFUSE_ADMIN_TOKEN` が必要）、ロックファイルを書き換えます。ロックファイルに登録済みのプロンプトが欠けている間は、ユニットテストが失敗します。

<a id="the-schema-dump"></a>

## スキーマダンプ

`schemas/schema.sql` は、すべてのマイグレーションを適用した ClickHouse から `scripts/ops/dump_schema.py` によって生成されます。

```bash
make schema-dump    # python -m scripts.ops.dump_schema
make schema-check   # --check: exit 1 with the head of the diff; writes nothing
```

スキーマダンプはマイグレーションが変更されるたびに更新され、CI が固定している ClickHouse のバージョンで取得する必要があるため、詳細は[マイグレーション](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#schema-dump-and-check)で説明しています。

<a id="snapshots-that-are-tests-not-generators"></a>

## スナップショットはジェネレーターではなくテストである

CLI の表面（`tests/unit/snapshots/cli_tree.json`、`cli_help.json`）とワーカーレジストリ（`tests/unit/snapshots/worker_registry.json`）は単体テストで固定されており、テスト実行時に環境変数を指定すると再生成されます（[テスト](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md#ci-gates)）。CLI リファレンスは `cli_tree.json` から生成されるため、CLI スナップショットを再生成した後は `docs_reference.py` も実行してください。

<a id="before-you-push"></a>

## プッシュする前に

```bash
make lint            # every --check above, plus ruff, the gates and Biome's dashboard accessibility rules
```

失敗した場合は実行すべきスクリプトが表示されます。そのスクリプトを実行し、差分を確認して、再生成されたファイルを元のソースと同じ変更内でコミットしてください。

<a id="related"></a>

## 関連ドキュメント

- [操作の追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-an-operation.md)：op、ハンドラー、SDK のテーブル
- [ドキュメントスタイル](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md#generated-pages)：生成されるページ
- [テスト](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)
