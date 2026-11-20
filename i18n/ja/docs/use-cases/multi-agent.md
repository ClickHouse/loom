<!-- i18n: source=docs/use-cases/multi-agent.md blob=d147692b665800cf10e0dfda74f2fdc9f9de8293 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/multi-agent.md), which is authoritative.

<a id="shared-context-for-multi-agent-workflows"></a>

# マルチエージェントワークフローのための共有コンテキスト

Loom は、マルチエージェントワークフローのエージェントがコンテキストを共有する場所を一つ提供します。名前空間内のすべてのエージェントが読めるメモリ、あるエージェントから別のエージェントへ作業を渡すためのタスクインボックス、そして決定の記録です。ワークフローを実行するのはエージェントとオーケストレーターで、Loom はそれらが学んだことと引き継いだことを保持します。

<a id="what-you-get"></a>

## 提供される機能

- **来歴付きの共有メモリ。** すべての行にどのエージェントが書き込んだかが記録されます。デフォルトの `auto_reinforce` ポリシーでは、二つのエージェント間の矛盾は黙って上書きされず、レビュー対象としてフラグが付けられます。[名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md) ·
  [名前空間の競合ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **タスクインボックス。** 各エージェントは、リース、リトライ、レビュールールを備えた永続キューを持ちます。取得されたタスクは、同時に一つのオーナーだけが持ちます。[タスク、決定、ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **決定。** 同様のケースで何が決定されたか、その理由、そしてうまくいったかどうかを記録します。[決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#decisions)
- **ワーキングメモリ。** 検索で見つけるのではなく、エージェントに毎回渡されるテキストです。長期メモリとは別に扱われます。[ワーキングメモリとプロファイル](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)
- **コンテキストパック。** `context.pack` は、エージェントがタスクの開始時に読み込む、サイズが制限されたバンドルを作成します。[統合検索とコンテキストパック](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **セッションブリーフ。** 一つの作業の流れについて、再開可能な状態を保持します。目標、ステータス、決定、行き止まり、完了した内容が含まれます。あるエージェントが `session.brief_save` で保存し、次のエージェントが `session.brief_get` で読み込みます。[セッションブリーフ](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md#session-briefs)
- **フレームワークアダプター。** SDK は、各エージェントフレームワーク固有のメモリインターフェースを通じて Loom を提供します。[エージェントフレームワーク](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)

<a id="two-agents-sharing-a-research-note"></a>

## 二つのエージェントで調査メモを共有する

一人のユーザーのラップトップ上では、[シングルユーザークイックスタート](../getting-started/quickstart.md)により、接続した二つのエージェントが同じ名前空間を読めます。[接続ガイド](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)に従って両方のクライアントを接続してください。たとえば、Claude Code で調査結果を集め、Cursor でそれをもとに文章を書きます。

1. 最初のクライアントで、`loom_memory_set` を使って、`example/research/next-step` を値「Check the paper's evaluation dataset before adding it to the reading list.」で保存するよう指示します。返された `memory_id` を確認し、ダッシュボードで該当の行を探します。
2. もう一方のクライアントで、`loom_memory_get` を使ってそのキーをそのまま読み取るよう指示します。次のステップを下書きする前に、`found`、`value`、`agent` を含むツールの応答を必須とします。最初のクライアントの値を読み取り、その書き込み元を特定できるはずです。
3. 終わったら、書き込んだクライアントからサンプルのキーを削除します。

エージェントが共有するのは保存された事実です。Loom がエージェントを実行したり、作業をスケジュールしたりすることはありません。別々の人のマシン上にあるエージェントの場合は、[認証付きのチームデプロイ](../getting-started/quickstart.md#start-in-multi-user-mode)を使い、各人が自分のトークンで同じ名前空間へのアクセス権を得るようにします。ローカルのシングルユーザー実行では、これらのアクセス制御はテストできません。

<a id="start"></a>

## 始める

[Loom をエージェントに統合する](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)では、各エージェントが送信する ID と、何を書き込むかを説明しています。[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)では、各エージェントに専用のトークンを付与します。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記されています。
