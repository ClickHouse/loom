<!-- i18n: source=docs/use-cases/multi-agent.md blob=a7e1e817573b25059be35ee63c5f9ec80b03af73 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# マルチエージェントワークフローの共有コンテキスト

Loom は、マルチエージェントワークフローのエージェントが文脈を共有するための場所を一つ提供します。namespace 内のすべてのエージェントが読み取れるメモリ、あるエージェントから別のエージェントへ作業を引き継ぐためのタスク受信箱、そして意思決定の記録です。ワークフローを実行するのはエージェントとオーケストレーターであり、Loom はそれらが学んだことと引き継いだ内容を保持します。

<a id="what-you-get"></a>

## 提供される機能

- **来歴付きの共有メモリ。** すべての行にどのエージェントが書き込んだかが記録されます。デフォルトの `auto_reinforce` ポリシーでは、2 つのエージェント間で矛盾が生じた場合、黙って上書きされることなくレビュー対象としてフラグが立てられます。[Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [Namespace conflict policy](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **タスク受信箱。** 各エージェントは、リース、リトライ、レビュールールを備えた永続キューを持ちます。取得されたタスクの所有者は、同時に 1 つだけです。[Tasks, decisions and policies](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **意思決定。** 同様のケースで何が決定されたか、その理由、そして結果としてうまくいったかどうかを記録します。
  [Decisions](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **ワーキングメモリ。** 検索で見つけさせるのではなく、エージェントに毎回渡されるテキストです。長期メモリとは別に管理されます。[Working memory and profiles](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **コンテキストパック。** `context.pack` は、エージェントがタスクの開始時に読み込む、サイズに上限のあるバンドルを構築します。[Unified search and context packs](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **フレームワークアダプター。** SDK は、各エージェントフレームワーク固有のメモリインターフェースを通じて Loom を提供します。[Agent frameworks](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="start"></a>

## はじめに

[Loom をエージェントに組み込む](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)では、各エージェントが送信する ID と、書き込む内容を説明しています。[トークンと権限付与](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)では、各エージェントに個別のトークンを発行します。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
