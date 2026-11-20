<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# コーディングエージェントのモデルコストを下げる

Loom プラグインとコードインデックスを使うと、Claude Code が ClickHouse のコードベースに関する質問に答える際のモデルコストは、測定した 3 つの質問セットすべてで、使わない場合より 6% から 21% 低くなりました。削減率は、回答を採点した 24 問で 20%（回答品質は 0.78 に対して 0.84）、概念に関する 36 問で 21%、マルチホップの 24 問で 6% でした。マルチホップの質問のうち、バグをたどってリリースブランチ上の修正を見つける質問では、どちらの場合もコストは同じでした。

<a id="what-was-measured"></a>

## 測定内容

どちらの条件でも、同じチェックアウトに対して Claude Code（`claude-sonnet-5`）を実行しました。一方はプレーンなセッション、もう一方は Loom プラグインと `coding` ツールセットを使ったセッションです。コストはセッション全体のモデル料金であり、Loom 自身のサーバー側のモデル呼び出し、ホスティング、インデックス作成は含みません。この比較はプラグイン全体を対象としており、コードインデックスとメモリを切り分けてはいません。結果は、テストしたワークロードに固有のものです。Loom を使うと、1 つの質問にかかる実時間は長くなります。プラグインによるプロンプトごとの検索と MCP のラウンドトリップが遅延を加えるためです。質問ごとのコストを実行間のばらつきとどう比較したかは、結果のページに記載しています。
[Loom の有無による比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)には、すべてのセット、プロトコル、成果物があります。[Loom を評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)では、自分のリポジトリで測定する方法を説明しています。

チャット履歴全体ではなくモデルが読み取るメモリについては、[エージェントメモリ](agent-memory.md#measured)を参照してください。

<a id="how-the-plugin-is-designed-to-save"></a>

## プラグインがコストを削減する仕組み

- **ファイルを読む代わりにインデックスで答える。** `code.symbol`、`code.callers`、`code.search` は、構造に関する質問に答える行を返します。[コードインデックス](code-intelligence.md)
- **再発見する代わりに保存したコンテキストを使う。** セッションが保存した決定事項や発見は、次のセッションの開始時に読み込まれます。[共有メモリ](agent-memory.md)

Loom は、埋め込みと LLM 呼び出しに費やした費用も、操作ごとに記録します。
[コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
