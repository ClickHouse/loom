<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# マルチエージェントワークフローのための共有コンテキスト

Loom は、マルチエージェントワークフローのエージェントがコンテキストを共有する場所を一つ提供します。名前空間内のすべてのエージェントが読み取るメモリ、あるエージェントから別のエージェントへ作業を渡すタスク受信箱、そして決定の記録です。ワークフローを実行するのはエージェントとオーケストレーターで、Loom はそれらが学んだことと引き継いだ内容を保持します。

<a id="what-you-get"></a>

## 提供される機能

- **出所を記録する共有メモリ。** すべての行にどのエージェントが書き込んだかが記録されます。デフォルトの `auto_reinforce` ポリシーでは、2 つのエージェント間の矛盾は黙って上書きされず、レビュー対象としてフラグが付けられます。[名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [名前空間の競合ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **タスク受信箱。** 各エージェントは、リース、リトライ、レビュールールを備えた永続キューを持ちます。取得されたタスクの所有者は常に一つです。[タスク、決定、ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **決定。** このようなケースで何が決定されたか、その理由、そしてうまくいったかどうかを記録します。[決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **ワーキングメモリ。** 検索で見つけさせるのではなく、毎回エージェントに与えるテキストです。長期メモリとは別に扱われます。[ワーキングメモリとプロファイル](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **コンテキストパック。** `context.pack` は、エージェントがタスク開始時に読み込む、サイズが制限されたバンドルを構築します。[統合検索とコンテキストパック](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **フレームワークアダプター。** SDK は、エージェントフレームワーク固有のメモリインターフェースを通じて Loom を提供します。[エージェントフレームワーク](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## はじめに

[Loom をエージェントに統合する](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)では、各エージェントが送信する ID と書き込む内容を説明しています。[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)では、各エージェントに専用のトークンを割り当てます。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental とされています。
