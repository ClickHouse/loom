<!-- i18n: source=docs/features/integrations/grep-shim.md blob=1ce5e2990b99fc9c2e3b50a80f6b06f33fa415f0 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md), which is authoritative.

<a id="the-grep-shim"></a>

# grep shim

grep shim は `grep`/`rg` のラッパーで、コードインデックスから取得した各ヒットを囲む関数を、マッチ行の末尾に追加します。Node 18 以降、viewer トークン、および[コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)内のリポジトリが必要です。シェルの grep しか使わないエージェントや、フックが動作しない環境（`claude --bare` は [Claude Code プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)のコード shunt をスキップします）で動くエージェントを対象としています。エージェント、そのプロンプト、ハーネスは変更不要です。

```text
$ grep -rn "getSettingsRef().max_threads" src/Processors
src/Processors/QueryPlan/ReadFromMergeTree.cpp:1466:    ... max_threads ...   ⟵ DB::ReadFromMergeTree::selectRangesToRead
src/Processors/Executors/PipelineExecutor.cpp:298:      ... max_threads ...   ⟵ DB::PipelineExecutor::initializeExecution
loom: 2/2 hits scoped to their enclosing function via code.enclosing, index at c7a9fe7c9f, 180 ms
```

<a id="install"></a>

## インストール

```bash
export PATH=/path/to/loom/integrations/grep-shim/bin:$PATH
export LOOM_BASE_URL=https://loom.example.com   # or ~/.loom/config.json {url, token}
export LOOM_API_KEY=<viewer token>              # or ~/.loom/token
export LOOM_CODE_NAMESPACE=code-myrepo
export LOOM_AGENT=pr-analyst                    # optional: names the caller in the ledger
```

`which grep` が shim を指している必要があります。エージェントは、名前空間がインデックスしているチェックアウト内で実行してください。パスは `git rev-parse --show-toplevel` からの相対パスに変換されます。ブランチが一致しない場合は、誤った結果ではなく未解決のヒットとして現れます。

shim は、`PATH` 上にある自分自身ではない最初の `grep` または `rg` を実行します（`LOOM_GREP_REAL` と `LOOM_RG_REAL` でパスを強制できます）。

<a id="guarantees"></a>

## 保証

- **実際のツールが出力するバイトはすべて、順序どおりに出力されます。** 変更は、インデックスが囲む関数を把握しているマッチ行への任意のサフィックス `   ⟵ <qualified name>` と、stderr への 1 行のサマリーだけです。
- **終了コードは保持されます**（grep: 0 はヒットあり、1 はヒットなし、2 はエラー）。
- **注釈が付くのは 2 つの形式です。** `path:line:text`（複数のオペランド、ディレクトリへの `-r`、`-H`）と、ファイルオペランドがちょうど 1 つの場合の `line:text`（`grep -n pattern file.cpp`。ファイルを把握したエージェントが落ち着く形式で、パスはオペランドそのものなので `-h` でも問題ありません）です。それ以外はそのまま通過します：`-n` がない出力はそのまま通過します。`-l`、`-L`、`-c`、`-o`、`-z`、`-Z`、`-b`、`-q`、`--color=always` を使った呼び出しも、そのまま通過します。rg では `-N`、`--json`、`--files`、`-0` の各モードが、それぞれ別にそのまま通過します。stdin、および `-h` を使った複数ファイルの場合もそのまま通過します。
- **レイテンシの上限があります。** `LOOM_GREP_SHIM_TIMEOUT_MS`（デフォルト 2500）。エラー、タイムアウト、トークンの欠如、または 8 MB を超える出力（`LOOM_GREP_SHIM_MAX_BYTES`）の場合は、元の出力をそのまま出力します。
- **サーバー負荷に上限があります。** 異なる 200 ヒットごとに `code.enclosing` を 1 回呼び出し、並列は最大 5 です。注釈が付くのは 1,000 ヒットまでで、残りは注釈なしで出力されます。

<a id="the-dependency-graph-block"></a>

## 依存グラフブロック

`LOOM_GREP_SHIM_GRAPH=on`（デフォルトは off）にすると、実際の出力の後にブロックが追加されます。`[Related from loom dependency graph]` に続いて、囲む関数ごとに 1 行ずつ、そのモジュール、最大 `LOOM_GREP_SHIM_GRAPH_K`（5）件の呼び出し元、呼び出し先、テストが並び、グラフが不完全な箇所には `(lower bound: …)` の注記が付きます。ヘッダー行から後ろを切り取ると、元のバイト列に戻ります。このブロックは同じ `code.enclosing` 呼び出し、タイムアウト、フェイルオープンのルールを使います。関数が `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`（20）を超える場合、または出力がストリーミングされた場合（8 MB 超）は、ブロックは省かれます。

<a id="switches"></a>

## スイッチ

| 変数                                    | デフォルト                             | 効果                                                                                    |
| --------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_GREP_SHIM`                        | on                                     | `off` にすると shim は単なるパススルーになります                                        |
| `LOOM_GREP_SHIM_QUIET`                  | 未設定                                 | 設定すると stderr のサマリー行を出力しません                                            |
| `LOOM_GREP_SHIM_TIMEOUT_MS`             | 2500                                   | レイテンシの上限                                                                        |
| `LOOM_GREP_SHIM_MAX_BYTES`              | 8 MB                                   | これを超える出力は元のまま通過します                                                    |
| `LOOM_GREP_SHIM_GRAPH`                  | off                                    | `on` で依存グラフブロックを追加します                                                   |
| `LOOM_GREP_SHIM_GRAPH_K`                | 5                                      | 関数ごとの隣接数（1–20）                                                                |
| `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`      | 20                                     | これを超える関数数ではブロックを省きます                                                |
| `LOOM_CODE_NAMESPACE`, `LOOM_AGENT`     |                                        | コードの名前空間、呼び出し元のエージェント名（デフォルトは `grep-shim`）                |
| `LOOM_CONFIG_FILE`, `LOOM_API_KEY_FILE` | `~/.loom/config.json`, `~/.loom/token` | 環境変数で設定されていない場合に、URL、名前空間、トークンを読み込む場所                 |

<a id="server-side-codeenclosing"></a>

## サーバー側：`code.enclosing`

shim は 1 つのオペレーションに対する薄いクライアントです。`code.enclosing`（`POST /v1/code.enclosing`、viewer）は `locations: ["src/F.cpp:12", …]`（最大 200 件）を受け取り、入力順に場所ごとの結果を 1 つずつ返します。行がファイルスコープにある場合、またはパスがインデックスされていない場合は `symbol == ""` になります。解決された行にはすべて `symbol_id` が含まれます。

- `include_body: true`（`token_budget` と併用、デフォルト 4000）を指定すると、囲む各関数の本体全体が行に含まれます。本体は異なる関数ごとに 1 回取得され、予算を使い切るまで出現順に埋められます。残りには、そのまま貼り付けられる `next`（`code.symbol(symbol_id=…)`）が付きます。shim ではこれをオフにしています。
- `neighbours: true`（`neighbour_k` と併用、デフォルト 5）を指定すると、グラフブロックのデータが返されます。異なる囲む関数ごとに 1 エントリ（最大 20。それ以上が解決された場合は `anchors_capped`）で、`callers`、`callees`（コンパイラティアを優先し、リポジトリ外のシンボルと信頼度 0.8 未満のパーサーの推測は除外）、`tests`、`module`、および `epistemic` / `causes` エンベロープを含みます。

`code.symbol` も、単一の場所に対して `name: "src/F.cpp:12"` を受け付けます。また、Python SDK は `agent.code.enclosing([...], include_body=True)` を提供しています。詳しくは[コードインデックスのオペレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)を参照してください。

<a id="limits"></a>

## 制限

- **マージ済みのコードのみ。** インデックスはインデックス対象のブランチしか把握していません。コミットされていない編集内のヒットは、ファイルのインデックス済みバージョンに対して解決されるか、まったく解決されません。
- **ベストエフォート。** 想定外のことが起きた場合は元の出力をそのまま出力します。注釈のない行は、grep が失敗したことを意味しません。
- **検索は同じ。** エージェントは引き続き grep を使います。MCP ツールを呼び出せるエージェントは、`code.search`、`code.callers`、`code.impact` を直接使うべきです。

<a id="related"></a>

## 関連項目

- [コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)。
- [Claude Code プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)：フックを実行するエージェント向けの、フックベースのコード shunt。
- [インテグレーションの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)。
