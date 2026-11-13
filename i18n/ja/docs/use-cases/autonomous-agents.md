<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# 自律エージェント向けのメモリ、リコール、監査、消去

Loom は、自律エージェントに対して、後から呼び出せるメモリ、誰が何を書き込んだかの監査記録、そして Loom が保持するあらゆる場所からのスコープ単位の消去を提供します。これらはサーバー操作であり、ClickHouse(ライブメモリとナレッジ状態については Postgres)に保存されます。

<a id="what-you-get"></a>

## 提供される機能

- **メモリと想起。** 出典情報を持つバージョン管理された事実、十分に近いものがなければ回答を控える検索、そして成功した実行が依拠したメモリに対するランキングの加点を提供します。 [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **拒否。** ピン留めされた事実は、それと矛盾する書き込みを拒否します。また `blacklist.add` は、どの書き込みでも保存できないパターンを登録します。 [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **監査。** メモリを変更する各操作は、その内容の SHA-256 を持つ監査行を残します。ワーカーは1時間ごとのウィンドウをハッシュチェーンに封印するため、編集や削除を検出できます。トランスポートの監査書き込みは fail-open です (挿入に失敗すると、その行は失われます)。Postgres authority の下では、メモリリビジョンの監査行はそのリビジョンとともに保存されます。
  [Audit log](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **確認できる消去。** `erasure.request` は、メモリの内容を保持するすべてのテーブルからスコープを消去します (デフォルトではトゥームストーン。`purge=true` を指定すると、`user_id` または `namespace_list` スコープの行を削除します)。最後に残存データの集計と受領証を出力します。
  [Retention and erasure](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **ロールバック。** `namespace.rollback` は、名前空間内のすべてのメモリを、ある時点のバージョンに戻します。
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **ヘッドレス運用。** ボットは独自の ID を取得し (セルフサインアップが開放されている環境では自分でサインアップします)、最初の実行の前に接続を確認でき、応答が途絶えたときには診断できます。
  [Headless bots](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

監査と消去はエンジニアリング上のプリミティブであり、コンプライアンス認証ではありません。
[Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

このページの一部の op は、[status page](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) で experimental と表示されています。
