<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=96269df2f0037fdcb16f6a101e85bf9aafedef73 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# OpenAI Agents の会話セッション

`LoomSession` は、Loom のエピソード記憶メモリの上に、Python と TypeScript のネイティブなセッションインターフェースを実装します。既存のツールは、会話の外にある事実を別途保持できます。フレームワークのパッケージは、引き続きオプションの依存関係です。

各アイテムは 1 回の `memory.set` 呼び出しで保存されます。値にはネイティブの JSON、キーにはタイムスタンプとランダム値、さらにエンドユーザーと、ユーザーおよびネイティブのセッション ID から導出したセッションを使います。ポイント書き込みは SDK のコアレッシングを経由しないため、同時に書き込まれた同一のアイテムも別々に残ります。エピソード行は、セマンティック抽出や重複のマージを行わずに、繰り返されたアイテムを保持します。Python と TypeScript ではネイティブのアイテム形式が異なるため、導出セッションのプレフィックスも別々です。

読み取りでは `memory.list` をページ送りし、重複するキーを除外したうえで、欠けている値や切り詰められた値を `memory.get` で補完します。アイテムはキー順に返され、limit を指定した場合は最新のアイテムが残ります。正の limit を指定した読み取りでも履歴全体をスキャンします。サーバーへの到着時刻では、書き込み側が生成したキーの範囲を絞り込めないためです。TypeScript アダプターは、構築時に検証済みのユーザーをスナップショットします。ネイティブの画像とファイルのバイトペイロードは、保存時には JSON 配列を使い、読み取り時には `Uint8Array` として返されます。通常の JSON とメディア情報は変更されません。削除ではユーザーと導出セッションを指定します。`pop_item` は最新のアイテムを削除し、`clear_session` は一覧取得した履歴を削除します。同時に行われる追加やクリアは個別のサーバー操作であり、会話全体にわたるトランザクションではありません。
補完の前にアイテムが消えた場合、読み取りは不完全なネイティブアイテムを返さず、失敗します。キーは書き込み側のクロックを使うため、同時に書き込む複数のライターがサーバー側のシーケンスを共有することはありません。

<a id="source-entry-points"></a>

## ソースのエントリポイント

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`。
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`。

<a id="invariants"></a>

## 不変条件

各履歴は、エージェントの組織、名前空間、名前によってスコープが決まります。ユーザーを省略すると、共有の名前空間の行に書き込まれ、ユーザーを明示すると、そのユーザーの履歴が選択されます。アクターとソースの来歴はサーバーが付与します。通常のメモリ削除、消去、ローリングアップグレードのルールが適用されます。ローカルの履歴データベースやスキーマは追加されません。
