<!-- i18n: source=docs/use-cases/index.md blob=faab303a28ac13877ddbd45c24561cb50d90c53e model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/index.md), which is authoritative.

<a id="use-cases"></a>

# ユースケース

各ページは、人やエージェントがツールを探すときに挙げる一つの疑問に答えます。そのニーズに対して Loom が何をするか、そしてどこから始めるかを説明します。

| ニーズ                                                                           | ページ                                                     |
| -------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| AI エージェント向けの長期メモリと共有メモリ（MCP メモリサーバー）                | [エージェントメモリ](agent-memory.md)                      |
| エージェント向けのナレッジ検索とドキュメント検索（RAG）                          | [ドキュメント検索](document-search.md)                     |
| コーディングエージェント向けのコードインデックスとコードインテリジェンス         | [コードインデックス](code-intelligence.md)                 |
| 言語ごとにコードインデックスが提供するもの（C++、Python、TypeScript、Go、Rust、Java） | [言語別コードインデックス](code-intelligence-by-language.md) |
| 対象のコードに紐づいたメモリ。コードが変更されると通知される                     | [コードに紐づいたメモリ](memory-linked-to-code.md)         |
| コーディングエージェントのモデルコストの削減                                     | [コーディングエージェントのコスト](coding-agent-cost.md)   |
| マルチエージェントのワークフローにおけるエージェント間の共有コンテキスト         | [マルチエージェント](multi-agent.md)                       |
| 自律型エージェント向けのメモリ、リコール、監査、消去                             | [自律型エージェント](autonomous-agents.md)                 |
| エージェントの実行、トレース、トラッカー、PR 履歴                                | [実行と PR 履歴](runs-and-pr-history.md)                   |
| ClickHouse 上のセルフホスト型エージェントメモリ（Docker または Kubernetes 利用）  | [ClickHouse でのセルフホスト](self-hosted-clickhouse.md)   |

[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)では、他のツールのほうが適しているニーズを挙げています。
