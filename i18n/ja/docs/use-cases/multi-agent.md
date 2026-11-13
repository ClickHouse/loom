<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# マルチエージェントワークフローのための共有コンテキスト

Loom は、マルチエージェントワークフローのエージェントがコンテキストを共有する場所を一つ提供します。名前空間内のすべてのエージェントが読むメモリ、エージェント間で作業を引き渡すためのタスクインボックス、そして意思決定の記録です。ワークフローを実行するのはエージェントとオーケストレーターで、Loom はそれらが学んだことと引き継いだ内容を保持します。

<a id="what-you-get"></a>

## 提供される機能

- **出所情報付きの共有メモリ。** すべての行にどのエージェントが書き込んだかが記録されます。デフォルトの `auto_reinforce` ポリシーでは、2 つのエージェント間の矛盾は黙って上書きされず、レビュー対象としてフラグが付けられます。[Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Namespace conflict policy](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **タスクインボックス。** 各エージェントは、リース、リトライ、レビュールールを備えた永続キューを持ちます。取得されたタスクの所有者は、常に一つです。[Tasks, decisions and policies](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **意思決定。** このようなケースで何が決定されたか、その理由、そしてうまくいったかどうかを記録します。[Decisions](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **ワーキングメモリ。** 検索で見つけさせるのではなく、毎回エージェントに渡されるテキストです。長期メモリとは別に管理されます。[Working memory and profiles](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **コンテキストパック。** `context.pack` は、エージェントがタスクの開始時に読み込む、サイズに上限のあるバンドルを構築します。[Unified search and context packs](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **フレームワークアダプター。** SDK は、各エージェントフレームワーク独自のメモリインターフェースを通じて Loom を利用できるようにします。[Agent frameworks](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## はじめに

[Integrate Loom into an agent](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) では、各エージェントが送信する ID と書き込むべき内容を説明します。[tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) では、各エージェントに固有のトークンを割り当てます。

このページの一部の op は、[status page](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) で experimental と記載されています。
