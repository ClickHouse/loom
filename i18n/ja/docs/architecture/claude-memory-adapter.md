<!-- i18n: source=docs/architecture/claude-memory-adapter.md blob=b72a628092f2f86d148e87a01ed0f9e99469b83f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md), which is authoritative.

<a id="claude-api-memory-file-tree"></a>

# Claude API メモリファイルツリー

Claude API メモリアダプタは、フレームワークの Python メモリツール基底クラスと TypeScript メモリハンドラを、Loom のナレッジドキュメントを通じて実装します。フレームワークの SDK はオプションの依存関係です。どちらの実装も同じドキュメント URI とストアタグを使うため、一方の言語で作成したファイルをもう一方で読み取れます。

ファイルは `loom-memory://<store>/memories/<path>` で表されます。ディレクトリは URI プレフィックスであり、ディレクトリ専用の行はありません。パスは正規化され、`/memories` の配下に収まらないものは拒否されます。ディレクトリの読み取りには、ストアタグ、URI プレフィックス、射影した URI フィールドを指定した `knowledge.list` を使い、ファイルの読み取りには `knowledge.get` を使います。編集には `knowledge.upsert` を使い、削除には件数を制限した `knowledge.bulk_delete` の呼び出しを使います。名前変更では、各ファイルをコピーしてから古い URI を削除します。名前変更に失敗するとコピーが残ることがあり、同時に読み取り・変更・書き込みを行う編集では、後の書き込みが残ります。

<a id="source-entry-points"></a>

## ソースのエントリポイント

- `sdk/python/loom/integrations/claude_memory.py`：`AsyncLoomMemoryTool` と `LoomMemoryTool`。
- `sdk/typescript/src/integrations/claude-memory.ts`：`loomMemoryTool` と `loomMemoryHandlers`。

<a id="invariants"></a>

## 不変条件

ストアはファイルをグループ化するものであり、アクセスは名前空間の認可で制御されます。ユーザーごとの非公開ツリーには、別々の名前空間が必要です。アダプタがストア名をアクセス許可として解釈することはありません。アクター、ソースの来歴、ドキュメントのリビジョン、blob のクリーンアップ、消去は、既存のナレッジ操作に従います。ローカルの永続状態、データベース、スキーマは追加されません。ディレクトリ操作はアトミックなスナップショットではありません。
