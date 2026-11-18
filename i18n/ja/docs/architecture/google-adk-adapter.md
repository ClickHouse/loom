<!-- i18n: source=docs/architecture/google-adk-adapter.md blob=6c709e61102ee7413e9b4764bfd5c80f4511830e model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md), which is authoritative.

<a id="google-adk-memory-service"></a>

# Google ADK メモリサービス

Google ADK アダプターは、Loom の Python SDK と TypeScript SDK を通じて、フレームワークのメモリサービスを実装します。フレームワークのパッケージはオプションの依存関係です。ナレッジドキュメントは、その名前空間のすべての読み取り側と共有されるため、各アプリケーションは個別の Loom 名前空間を使用します。

セッションの書き込みでは、有効なユーザーイベントとモデルイベントからテキストを抽出し（ツールのペイロードと thoughts は除外）、上限付きのバッチで `memory.set_from_messages` を呼び出します。どちらの SDK も、空のユーザー ID、センチネル値のユーザー ID、前後に空白が付いたユーザー ID を、呼び出しの前に拒否します。各呼び出しには、エンドユーザーと、そのユーザーおよびネイティブのセッション ID から導出したセッションが含まれます。Python は、インストールされているフレームワークの増分イベント、直接メモリ、削除の各メソッドも実装します。Python の削除 ID には、キー、セッション、ライター、正確なバージョンが含まれます。削除 ID は `memory.delete_version` を直接呼び出します。存在しないバージョンや書き換えられたバージョンは何も行わず、他のライターの行が選択されることはありません。メモリ検索ヒットに追加された `agent` レスポンスフィールドは、パークされたページも含め、保存された行の ID に由来します。ライターまたはバージョンが不明なヒットには、削除 ID がありません。古いクライアントは追加フィールドを無視しますが、ADK での正確な削除には、このフィールドを提供するサーバーが必要です。この変更で、永続化される列やマイグレーションは追加されません。TypeScript はライター単位の参照用 ID を返します。インストールされているフレームワークのインターフェースには、削除メソッドがありません。

検索では、まずバインドされたユーザーに対して `memory.search` を呼び出し、その後、空いている結果枠を名前空間単位の `knowledge.search` の結果で埋めます。ナレッジは共有されるアプリケーションコンテキストであり、ユーザー個人のメモリではありません。2 つの SDK は同じダイジェストエンコーディングを使用し、フレームワーク固有のタイムスタンプ表現は境界で変換されます。1 つのサービスインスタンスは 1 つのアプリケーションにバインドされます。相互に信頼できないアプリケーション同士は、名前空間の権限付与によって分離します。

<a id="source-entry-points"></a>

## ソースのエントリーポイント

- `sdk/python/loom/integrations/google_adk.py`: `LoomMemoryService`。
- `sdk/typescript/src/integrations/google-adk.ts`: `LoomMemoryService`。

<a id="invariants"></a>

## 不変条件

アクターとソースの来歴は、サーバーが通常の書き込みパスを通じて提供します。アダプターは、データベース、スキーマ、プロバイダーを追加しません。既存のメモリ削除、ナレッジ消去、ローリングアップグレードのルールが適用されます。セマンティック抽出によってファクトが修正されることがあるため、このサービスは正確なトランスクリプトストアではありません。ナレッジの結果にはユーザーメモリ ID がなく、メモリサービスを通じて削除することはできません。
