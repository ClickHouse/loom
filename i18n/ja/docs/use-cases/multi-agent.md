<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# マルチエージェントワークフローのための共有コンテキスト

Loom は、マルチエージェントワークフローのエージェントがコンテキストを共有する場所を一か所にまとめます。具体的には、名前空間内のすべてのエージェントが読み取れるメモリ、エージェント間で作業を受け渡すためのタスクインボックス、そして意思決定の記録です。ワークフローを実行するのはエージェントとオーケストレーターであり、Loom はそれらが学んだことと引き継いだ内容を保持します。

<a id="what-you-get"></a>

## 提供する機能

- **来歴付きの共有メモリ。** すべての行に、どのエージェントが書き込んだかが記録されます。デフォルトの `auto_reinforce` ポリシーでは、2つのエージェント間の矛盾は黙って上書きされず、レビュー対象としてフラグが立てられます。[名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [名前空間の競合ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **タスク受信箱。** 各エージェントは、リース、リトライ、レビュールールを備えた永続キューを持ちます。取得されたタスクの所有者は、常に1つのエージェントだけです。[タスク、決定、ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **決定。** 似たケースで何を決めたか、その理由、そしてうまくいったかどうかを記録します。[決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **ワーキングメモリ。** 検索で探し出すのではなく、エージェントに毎回渡されるテキストです。長期メモリとは別に管理されます。[ワーキングメモリとプロファイル](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **コンテキストパック。** `context.pack` は、エージェントがタスクの開始時に読み込む、サイズに上限のあるバンドルを作成します。[統合検索とコンテキストパック](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **フレームワークアダプタ。** SDK は、エージェントフレームワーク独自のメモリインターフェースを通じて Loom を利用できるようにします。[エージェントフレームワーク](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## はじめに

[Loom をエージェントに組み込む](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)では、各エージェントが送信する ID と、書き込むべき内容を説明しています。[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)では、各エージェントに専用のトークンを割り当てます。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
