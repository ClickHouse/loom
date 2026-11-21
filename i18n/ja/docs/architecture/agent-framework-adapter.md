<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=6a5670ddd5050886dcda1b0bf590a83063c1e4aa model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Microsoft Agent Framework コンテキストプロバイダー

`LoomContextProvider` は、非同期 Python SDK 上でフレームワークのコンテキストプロバイダーインターフェイスを実装します。オプションの extra は、テスト済みのフレームワークのマイナーシリーズのみを許可します。コア SDK とサーバーはフレームワークをインポートしません。

実行前に、プロバイダーは最新のユーザーテキストを検索し、一致するファクトをフレームワークの instructions に追加します。実行後は、新しいユーザーテキストとアシスタントテキストを、上限付きのバッチで `memory.set_from_messages` に送信します。このフックはフレームワークのミドルウェアループを含め、1 ターンにつき 1 回だけ実行されます。フレームワークは独自の履歴を保持します。このプロバイダーは履歴プロバイダーを置き換えるのではなく、永続的なファクトを保存します。

<a id="invariants"></a>

## 不変条件

ユーザーとフレームワークのセッション ID から Loom のセッションが導出されるため、2 人のユーザーの同名セッションが、抽出されたチャンクを共有することはありません。ユーザーを省略すると、トークンが許可する名前空間内のすべてのメモリを読み取り、共有行を書き込みます。明示的なユーザー ID の前後に空白を含めることはできません。アクセスを認可するのは名前空間の許可であり、ユーザーパラメーターはフィルターです。
サーバーはアクターとソースの来歴を付与し、通常の競合、消去、ローリングスキーマのルールを適用します。データベース、キャッシュ、ローカル履歴は追加されません。
エラーは、モデルの応答後に発生したものも含め、フレームワークのプロバイダーフックを通じて伝播します。同時書き込みは、サーバーの通常の競合ルールに従います。

<a id="source-entry-points"></a>

## ソースのエントリポイント

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`。
- `sdk/python/tests/test_agent_framework.py`: ネイティブのプロバイダーとエージェントの動作。
- `tests/integration/test_sdk_agent_framework_memory.py`: 実環境での抽出と想起。

[フレームワークのセットアップ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
