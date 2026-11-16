<!-- i18n: source=docs/use-cases/memory-linked-to-code.md blob=fab4f6c6a23848dde1feef736969b204d07adfd8 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/memory-linked-to-code.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/memory-linked-to-code.md), which is authoritative.

<a id="memory-linked-to-code-for-coding-agents"></a>

# コーディングエージェント向けのコードに紐づくメモリ

Loom は、コーディングエージェントのメモリを、そのメモリが名指しする関数、クラス、ファイルに紐づけ、リファクタリングをまたいで追跡し、対応するコードが変更されるとメモリにフラグを立てます。既定では、人が固定（pin）、ロック、または検証したメモリを除き、コードがすべて失われたメモリは検索から除外されます。また、教訓（lesson）を返すすべての読み取りで、そのコードがいつ変更されたか、または失われたかが示されます。この機能は、セルフホストの Loom がインデックスしたリポジトリ（[コードインデックス](code-intelligence.md)、`LOOM_CODE_INGEST_ENABLED=true`）に対して MCP、HTTP、SDK 経由で動作します。リンクを維持する `memory_code_links` ワーカーは既定で有効です。

<a id="what-you-get"></a>

## できること

- **教訓を対象のコードに紐づけます。** `repo:<id>` タグが付いたメモリ（Claude Code と Codex のプラグインは、フックで取得した内容にこのタグを付けます）、またはリポジトリのコード名前空間に書き込まれたメモリは、次のものにリンクされます。バッククォートで囲まれた定義（関数、クラスなどのシンボル）、本文中の任意の位置にある `A::b` や呼び出し（`a.b()`、`name()`）で、ちょうど 1 か所に解決されるもの（修飾なしの名前は 4 文字以上が必要です）。メモリが列挙するファイルのうち、camelCase または snake_case の単語だけで名指しされている定義。そして、メモリが列挙するファイルのうち、上記の定義を 1 つも含まないファイル。まだトランクにない修飾名は、`LOOM_MEMORY_CODE_LINKS_PENDING_DAYS`（14 日）以内にマージされればリンクされます。
  [コードに紐づくメモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **リファクタリングに追従するリンク。** インデックスされたコミットごとに、各リンクは `fresh`、`moved`、`changed`（変更したコミットを示します）、`unresolved`、`missing` のいずれかになります。リンクは定義のトークンを記録するため、空白、コメント、ほとんどのフォーマッタによる変更では `fresh` のままです。[リンクの状態](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **古くなったメモリを選んだ方法で扱います。** 名前空間の [`code_change_policy`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespaceregister) が決めます。`retire_removed`（既定）は、コードがすべて失われたメモリを検索から除外し、変更されたコードにフラグを立てます。`flag` はフラグを立てるだけです。`revise` はコードが失われたメモリも除外し、さらに変更されたメモリの主張を旧コードと新コードに照らしてモデルに確認させます。主張がなお成り立つ場合は再アンカーされ、矛盾する場合は修正案が提示されます。人が固定、ロック、または検証したメモリが除外されることはありません。どのポリシーでも、検索では通常、コードが変更されたメモリを隠さず、次の 3 件の結果より下に順位付けします。保持されたメモリと名前で見つかった行は順位を保ちます。Loom がメモリ自体を書き換えることはありません。[ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **エージェントがコードを読む場所すべてで教訓を提示します。** `code.symbol` は、定義に紐づくメモリを `lessons` として返します。`code.search`、`code.callers`、`code.callees`、`code.impact`、`code.outline`、およびタスクの `code.context_pack` は、返すファイルに対応する教訓を添付します。各教訓にはリンクの状態が付きます。Claude Code と Codex のフックは、エージェントがファイルを編集する前にそのファイルのメモリ（`memory.for_file`）を表示し、Cline のフックはファイルを読む、または編集するときに表示します。[`code.symbol`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/code.md#codesymbol) ·
  [Claude Code プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) ·
  [Codex プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) ·
  [Cline フック](https://github.com/ClickHouse/loom/blob/main/integrations/cline/README.md)（実験的）
- **チームで共有します。** 教訓は、リポジトリのコード名前空間と、呼び出しで指定された名前空間から読み取られます（MCP では、既定で呼び出し元自身の名前空間です）。そのため、リポジトリのコード名前空間に保存された教訓は、その名前空間を読めるすべてのエージェントに届きます（自分のユーザーまたはセッションに限定されたトークンでは、教訓は読み取れません）。
  [コードに紐づくメモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **ブランチは自身のコードを読みます。** 別のブランチでは、教訓はそのブランチ自身の定義と照合されます。そのため、古いテキストをまだ持つリリースブランチでは `fresh` と表示されます（git ミラーを持たない `LOOM_GIT_HISTORY=none` のホストでは、教訓はトランクの状態を持ちます）。[コードに紐づくメモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)
- **コードと並べてドキュメントを扱います。** リポジトリのコード名前空間（そのトランク）に登録されたソースのドキュメントにはリポジトリのタグが付くため、リポジトリに対する検索でコードと一緒に読み取られます（同期ワーカーには `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED` が必要です）。
  [ドキュメントソース](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **レビューリスト。** [`namespace.validate`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespacevalidate) は、コードが変更された、再発見されなかった、または失われたメモリを一覧表示し、ダッシュボードは各メモリのリンクとその状態を表示します。

![ダッシュボードのメモリ詳細：リンクされたコード。1 つの定義は最新で、もう 1 つはあるコミット以降に変更されています](https://github.com/ClickHouse/loom/raw/main/docs/assets/screenshots/memory-linked-code.webp)

<a id="start"></a>

## はじめに

[クイックスタート](../getting-started/quickstart.md)で Loom を起動し、[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)でリポジトリのコードインデックスを有効にします。コーディングエージェントを、そのプラグインまたは MCP で接続します（[コーディングエージェントの接続](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)）。4 文字以上の定義をバッククォートまたは呼び出し（`` `name()` ``）で名指しした教訓、または `` `Reader.run` `` のような修飾名を含む教訓を保存します。Claude Code と Codex のプラグインは、フックで取得した内容に `repo:<id>` のタグを付け、セッションのプライマーが id を示します（[導出方法](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)）。エージェントが `loom_memory_set` で自ら保存した教訓は、そのタグ（`tags: ["repo:<id>"]`）を持つか、リポジトリのコード名前空間に書き込まれていればリンクされます。どちらの方法も任意のクライアントから使えます。HTTP（または Python SDK の `agent.call`）では、自分の名前空間に保存された教訓は、呼び出しが `lesson_namespaces` でその名前空間を指定した場合にのみ読み取られます。各リンクパスは 60 秒以上前の書き込みをリンクし、パスは 60 秒ごとに実行されるため、トランクがインデックスされれば、その定義に対する `code.symbol` は約 2 分以内に教訓を `lessons` の下に返します。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で実験的と示されています。
