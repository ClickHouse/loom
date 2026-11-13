<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# 自律エージェントのためのメモリ、リコール、監査、消去

Loom は、自律エージェントが後で呼び出せるメモリ、誰が何を書き込んだかの監査記録、そして Loom が保持するすべての場所にあるスコープの消去を提供します。これらはサーバー操作であり、ClickHouse（ライブメモリとナレッジ状態については Postgres）に保存されます。

<a id="what-you-get"></a>

## 提供される機能

- **メモリと想起。** 出所（provenance）付きでバージョン管理される事実、十分に近いものがないときは回答を控える検索、そして成功した実行が頼りにしたメモリに対するランキングの加点。 [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **拒否。** ピン留めされた事実は、それと矛盾する書き込みを拒否します。`blacklist.add` は、どの書き込みにも保存を許さないパターンを登録します。 [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **監査。** メモリを変更する各操作は、その内容の SHA-256 を持つ監査行を残し、ワーカーが1時間ごとのウィンドウをハッシュチェーンに封じるため、編集や削除を検出できます。トランスポートの監査書き込みは fail-open です（挿入に失敗するとその行は失われます）。Postgres authority では、メモリのリビジョンの監査行はそのリビジョンとともに保存されます。
  [監査ログ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **確認できる消去。** `erasure.request` は、メモリの内容を保持するすべてのテーブルからスコープを消去し（デフォルトはトゥームストーン。`purge=true` では `user_id` または `namespace_list` スコープの行を削除します）、最後に残存データの集計と受領書を出します。
  [保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **ロールバック。** `namespace.rollback` は、名前空間内のすべてのメモリを、ある時点のバージョンに戻します。
  [履歴と出所](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **ヘッドレス運用。** ボットは自分専用のアイデンティティを持ち（セルフサインアップが開放されている環境では自分でサインアップします）、最初の実行の前に接続を確認でき、応答がなくなったときには原因を診断できます。
  [ヘッドレスボット](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

監査と消去はエンジニアリング上のプリミティブであり、コンプライアンス認証ではありません。
[セキュリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記されています。
