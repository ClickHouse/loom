<!-- i18n: source=docs/use-cases/coding-agent-cost.md blob=d7a120fc7dc895117d934de1f2dc5ffd14bca474 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/coding-agent-cost.md), which is authoritative.

<a id="lower-model-cost-for-coding-agents"></a>

# コーディングエージェントのモデルコストを削減

Loom のプラグインとコードインデックスを使うと、Claude Code が ClickHouse のコードベースに関する質問に答えるときのモデルコストが、測定した 3 つの質問セットのすべてで、使わない場合より 6% から 21% 低くなりました。削減率は、回答を採点した 24 問で 20%（回答品質は 0.78 に対して 0.84）、概念に関する 36 問で 21%、マルチホップの 24 問で 6% でした。マルチホップの質問のうち、バグをリリースブランチ上の修正までたどる質問では、どちらの条件でもコストは同じでした。

<a id="what-was-measured"></a>

## 測定内容

両方の条件で、同じチェックアウトに対して Claude Code（`claude-sonnet-5`）を実行しました。一方は通常のセッション、もう一方は Loom プラグインと `coding` ツールセットを使うセッションです。コストはセッション全体のモデル利用料金で、Loom 自体のサーバー側のモデル呼び出し、ホスティング、インデックス作成は含みません。この比較はプラグイン全体を対象としており、コードインデックスとメモリは切り分けていません。結果は、テストしたワークロードに固有のものです。Loom を使うと、質問 1 件にかかる実時間は長くなります。プラグインによるプロンプトごとの検索と MCP の往復が、レイテンシを増やすためです。質問ごとのコストを実行ごとのばらつきとどう比較するかは、結果ページに記載しています。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)には、すべてのセットとプロトコル、成果物があります。自分のリポジトリで測定する方法は [Loom を評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)を参照してください。

チャット履歴全体ではなく、モデルが読み取るメモリについては、[エージェントメモリ](agent-memory.md#measured)を参照してください。

<a id="how-the-plugin-is-designed-to-save"></a>

## プラグインがコストを削減する仕組み

- **ファイルの読み取りではなくインデックスで回答する。** `code.symbol`、`code.callers`、`code.search` は、構造に関する質問に答える行を返します。[コードインデックス](code-intelligence.md)
- **再探索ではなく保存したコンテキストを使う。** セッションが保存した決定事項や調査結果は、次のセッションの開始時に読み込まれます。[共有メモリ](agent-memory.md)

Loom は、埋め込みと LLM 呼び出しに使った費用も、オペレーションごとに記録します。
[コストと使用量](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)
