<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# コーディングエージェントのモデルコストを下げる

Loom プラグインとコードインデックスを使うと、Claude Code が ClickHouse のコードベースに関する質問に答える際のモデルコストは、測定した 3 つの質問セットのすべてで、使わない場合より 6% から 21% 低くなりました。削減率は、回答を採点した 24 問で 20%(回答品質は 0.78 に対して 0.84)、概念に関する 36 問で 21%、マルチホップの 24 問で 6% でした。マルチホップの質問のうち、バグをたどってリリースブランチ上の修正を見つける質問では、どちらの方法でもコストは同じでした。

<a id="what-was-measured"></a>

## 計測内容

両アームとも、同じチェックアウトで Claude Code(`claude-sonnet-5`)を実行しました。一方は通常のセッション、もう一方は Loom プラグインと `coding` toolset を有効にしたセッションです。コストはセッション全体のモデル利用料金であり、Loom 自身のサーバー側モデル呼び出し、ホスティング、インデックス作成のコストは含みません。この比較で測っているのはプラグイン全体の効果であり、コードインデックスとメモリを切り分けてはいません。結果は、テストしたワークロードに固有のものです。Loom を使うと、質問1件あたりの所要時間(実時間)は長くなります。プラグインによるプロンプトごとの検索と MCP のラウンドトリップがレイテンシを加えるためです。質問ごとのコストを実行ごとのばらつき(ノイズ)とどう比較したかは、結果ページに記載しています。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) には、すべてのセット、プロトコル、成果物があります。自分のリポジトリで計測する方法は [evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) を参照してください。

チャット履歴全体ではなく、モデルが読み取るメモリについては、[agent memory](agent-memory.md#measured) を参照してください。

<a id="how-the-plugin-is-designed-to-save"></a>

## プラグインが節約する仕組み

- **ファイルを読む代わりにインデックスで答える。** `code.symbol`、`code.callers`、`code.search` は、構造に関する質問に答える行を返します。[Code index](code-intelligence.md)
- **再発見する代わりに保存したコンテキストを使う。** セッションが保存した決定事項や調査結果は、次のセッションの開始時に読み込まれます。[Shared memory](agent-memory.md)

Loom は、埋め込みと LLM 呼び出しに費やしたコストも、操作ごとに記録します。
[Cost and usage](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
