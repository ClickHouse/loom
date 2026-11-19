<!-- i18n: source=docs/architecture/agno-adapter.md blob=3cda341b0d2ad39a80b6546d3002b103a90de53d model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md), which is authoritative.

<a id="agno-user-memory-database"></a>

# Agno ユーザーメモリデータベース

Agno アダプターは、同期版 Loom SDK を通じて、3.2 の `BaseDb` インターフェースにおけるユーザーメモリのメソッドを実装します。マイナーバージョンを固定したオプションの依存関係は、コア SDK とサーバーには含まれません。`MemoryManager` は、エージェントのセッションデータベースとは独立して、このデータベースを受け付けます。メモリ以外の機能については、フレームワーク既定のストアを使わずに拒否します。

<a id="invariants"></a>

## 不変条件

各アダプターは、ユーザーとアプリケーションストアに紐づきます。すべての SDK 呼び出しには、そのユーザーと、そこから導出したセッションが付与されます。キーには、同じスコープダイジェストとネイティブ ID のダイジェストが含まれます。明示的に指定された別のユーザーは拒否されます。ネイティブのユーザーメモリは、Loom のエピソード行に JSON として保存され、異なるネイティブレコードをセマンティックにマージすることなく、ID、メタデータ、リビジョンが保持されます。アクターとソースの来歴は、Loom の通常の書き込みパスから取得されます。通常のメモリ削除、消去、ローリングスキーマのルールが適用されます。

一覧取得は、件数を制限し項目を絞り込んだページに沿って行われ、重複する部分を取り除き、切り詰められた値を補完してからネイティブオブジェクトに変換します。内容、トピック、帰属のフィルター、並べ替え、ページネーション、統計は、このユーザーとストアにスコープされたスナップショット上で実行されます。ローカルの永続状態はありません。同時編集では、サーバーへの最後の書き込みが採用されます。一括操作は書き込みの連続であり、失敗時には一部だけが完了することがあります。相互に信頼できないアプリケーションは、名前空間のグラントで分離します。

<a id="source-entry-points"></a>

## ソースのエントリポイント

- `sdk/python/loom/integrations/agno.py`: `LoomMemoryDb`。
- `sdk/python/tests/test_agno.py`: ネイティブのメモリマネージャーの動作。
- `tests/integration/test_sdk_agno_memory.py`: 実環境でのメモリのスコープと来歴。
