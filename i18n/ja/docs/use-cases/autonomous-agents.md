<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# 自律型エージェントのためのメモリ、リコール、監査、消去

Loom は、自律型エージェントに、後から呼び出せるメモリ、誰が何を書いたかの監査記録、そして Loom が保持するすべての場所にわたるスコープの消去を提供します。これらはサーバー操作であり、ClickHouse（ライブメモリとナレッジ状態については Postgres）に保存されます。

<a id="what-you-get"></a>

## 提供される機能

- **メモリとリコール。** 来歴（provenance）付きのバージョン管理されたファクト、十分に近いものがない場合は回答を控える検索、そして成功した実行が依拠したメモリに対するランキングの加点です。[メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **拒否。** ピン留めされたファクトは、矛盾する書き込みを拒否します。また `blacklist.add` は、どの書き込みも保存できないパターンを登録します。[競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **監査。** メモリを変更する各操作は、その内容の SHA-256 を持つ監査行を残し、ワーカーが 1 時間ごとのウィンドウをハッシュチェーンに封印するため、編集や削除を検出できます。トランスポートの監査書き込みはフェイルオープンです（挿入に失敗するとその行は失われます）。Postgres をオーソリティとする場合、メモリリビジョンの監査行はそのリビジョンとともに保存されます。[監査ログ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **確認できる消去。** `erasure.request` は、メモリの内容を保持するすべてのテーブルからスコープを消去し（デフォルトでは墓標（トゥームストーン）を残し、`purge=true` では `user_id` または `namespace_list` スコープの行を削除します）、残留データの集計とレシートで完了します。[保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **ロールバック。** `namespace.rollback` は、名前空間内のすべてのメモリを、ある時点のバージョンに戻します。[履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **ヘッドレス運用。** ボットは独自のアイデンティティを持ち（セルフサインアップが開放されている場合は自分でサインアップします）、最初の実行の前に接続を確認でき、応答しなくなったときには診断できます。[ヘッドレスボット](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

監査と消去はエンジニアリング上のプリミティブであり、コンプライアンス認証ではありません。[セキュリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で実験的と記されています。
