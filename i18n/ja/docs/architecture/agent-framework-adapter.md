<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=d7c22dbc73bf2a6dd64de2cb8471a3fafc9d76ba model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Microsoft Agent Framework コンテキストプロバイダー

`LoomContextProvider` は、非同期 Python SDK 上でフレームワークのコンテキストプロバイダーインターフェースを実装します。オプションの extra は、テスト済みのフレームワークのマイナーシリーズのみを許可します。コア SDK とサーバーはフレームワークをインポートしません。

実行前に、プロバイダーは最新のユーザーテキストを検索し、一致するファクトをフレームワークの instructions に追加します。実行後は、新しいユーザーテキストとアシスタントテキストを、上限付きのバッチで `memory.set_from_messages` に送信します。このフックはフレームワークのミドルウェアループを含め、1 ターンにつき 1 回だけ実行されます。フレームワークは独自の履歴を保持します。このプロバイダーは、履歴プロバイダーを置き換えるのではなく、永続的なファクトを保存します。

<a id="invariants"></a>

## 不変条件

ユーザーとフレームワークのセッション ID から Loom のセッションが導出されるため、2 人のユーザーが同じ名前のセッションを使っても、抽出されたチャンクが共有されることはありません。ユーザーを省略すると、トークンが許可する名前空間内のすべてのメモリを読み取り、共有行を書き込みます。明示的なユーザー ID の前後に空白を含めることはできません。アクセスは名前空間の権限付与によって認可され、ユーザーパラメーターはフィルターです。
サーバーはアクターとソースのプロベナンスを提供し、通常の競合、消去、ローリングスキーマのルールを適用します。データベース、キャッシュ、ローカル履歴は追加されません。
エラーは、モデルの応答後に発生したものも含め、フレームワークのプロバイダーフックを通じて伝播します。同時書き込みは、サーバーの通常の競合ルールに従います。

<a id="conversation-protection"></a>

## 会話の保護

プロバイダーは 1 つの論理的な会話ファミリーを扱います。構築時とコールバック開始時に存在する、アプリケーション所有の保護ハンドルを保持します。これには、アンビエントなコントリビューターと、その後にバインドされる各クライアントハンドルが含まれます。ネイティブのユーザー ID とセッション ID はリクエストのルーティングに使われ、この権限を作成も置換もしません。復元では、プロバイダーを構築する前に既存のカプセルを再度開きます。

所有された recall と抽出は、SDK の自動キャプチャコンテキストで実行されます。トランスポートは、認証情報の確認後かつディスパッチ前に保持している各ファミリーを再確認し、変換前に応答を観測し、マーク付き応答を解放する前にすべてのコントリビューターを永続的にマークします。最初のマーク付き recall はモデルに到達する場合があります。以降の自動処理は拒否され、各抽出バッチは次のバッチが開始される前に確認されます。モデルの応答後の拒否はネイティブのプロバイダーライフサイクルを通じて伝播しますが、回答やツールの効果を取り消すことはできません。
プロバイダーは、機能宣言も復元されたホストの適格性確認も追加しません。

<a id="source-entry-points"></a>

## ソースのエントリーポイント

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`。
- `sdk/python/tests/test_agent_framework.py`: ネイティブのプロバイダーとエージェントの動作。
- `tests/integration/test_sdk_agent_framework_memory.py`: ライブでの抽出と recall。

[フレームワークのセットアップ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
