<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# 自律型エージェントのためのメモリ、リコール、監査、消去

Loom は、自律型エージェントに対して、後から呼び出せるメモリ、誰が何を書き込んだかの監査記録、そして Loom が保持するあらゆる場所からスコープを消去する機能を提供します。これらはサーバー操作であり、ClickHouse（ライブメモリとナレッジ状態については Postgres）に保存されます。

<a id="what-you-get"></a>

## 得られるもの

- **メモリと想起。** 出所（provenance）付きでバージョン管理されるファクト、十分に近いものがないときは回答を控える検索、成功した実行が依拠したメモリのランキング引き上げを備えます。[Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **拒否。** ピン留めされたファクトは、矛盾する書き込みを拒否します。`blacklist.add` は、どの書き込みでも保存できないパターンを登録します。[Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **監査。** メモリを変更する各操作は、内容の SHA-256 を含む監査行を残します。ワーカーが1時間ごとのウィンドウをハッシュチェーンに封印するため、編集や削除を検出できます。トランスポートの監査書き込みはフェイルオープンです（挿入に失敗すると、その行は失われます）。Postgres authority では、メモリリビジョンの監査行はそのリビジョンとともに保存されます。[Audit log](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **確認できる消去。** `erasure.request` は、メモリの内容を保持するすべてのテーブルからスコープを消去します（デフォルトではトゥームストーン。`purge=true` では `user_id` または `namespace_list` スコープの行を削除します）。最後に残存物の調査（residue census）と受領書を出力します。[Retention and erasure](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **ロールバック。** `namespace.rollback` は、名前空間内のすべてのメモリを、ある時点のバージョンに戻します。[History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **ヘッドレス運用。** ボットは独自のアイデンティティを持ち（セルフサインアップが開放されている場合は自分でサインアップします）、最初の実行の前に接続を確認でき、反応しなくなったときは診断できます。[Headless bots](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

監査と消去はエンジニアリング上のプリミティブであり、コンプライアンス認証ではありません。[Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記されています。
