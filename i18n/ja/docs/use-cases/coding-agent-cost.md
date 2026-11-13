<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# コーディングエージェントのモデルコストを削減

Loom プラグインとコードインデックスを使うと、Claude Code が ClickHouse のコードベースに関する質問に答える際のモデルコストが、使わない場合に比べて、測定した 3 つの質問セットすべてで 6% から 21% 低くなりました。削減率は、回答を採点した 24 問で 20%（回答品質は 0.78 に対して 0.84）、概念に関する 36 問で 21%、マルチホップの 24 問で 6% でした。マルチホップの質問のうち、バグをリリースブランチ上の修正まで追跡する質問では、どちらの方法でもコストは同じでした。

<a id="what-was-measured"></a>

## 測定内容

両方の条件で、同じチェックアウトに対して Claude Code（`claude-sonnet-5`）を実行しました。一方は通常のセッション、もう一方は Loom プラグインと `coding` ツールセットを使用したセッションです。コストはセッション全体のモデル利用料金で、Loom 自身のサーバー側のモデル呼び出し、ホスティング、インデックス作成は含みません。この比較はプラグイン全体を対象としており、コードインデックスとメモリを切り分けてはいません。結果は、テストしたワークロードに固有のものです。Loom を使うと、1 つの質問にかかる実時間は長くなります。プラグインによるプロンプトごとの検索と MCP のラウンドトリップがレイテンシを増やすためです。質問ごとのコストを実行ごとのばらつきとどう比較するかは、結果ページに記載しています。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) には、すべてのセット、プロトコル、成果物があります。また、[evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) では、自分のリポジトリで測定する方法を説明しています。

チャット履歴全体ではなく、モデルが読み取るメモリについては、[agent memory](agent-memory.md#measured) を参照してください。

<a id="how-the-plugin-is-designed-to-save"></a>

## プラグインが節約する仕組み

- **ファイルを読む代わりにインデックスで答える。** `code.symbol`、`code.callers`、`code.search` は、構造に関する質問に答える行を返します。[コードインデックス](code-intelligence.md)
- **再調査の代わりに保存済みのコンテキストを使う。** セッションが保存した決定事項や調査結果は、次のセッションの開始時に読み込まれます。[共有メモリ](agent-memory.md)

Loom は、埋め込みと LLM 呼び出しに費やしたコストも操作ごとに記録します。
[コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
