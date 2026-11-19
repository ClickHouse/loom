<!-- i18n: source=docs/operations/maintenance.md blob=5df99557fe7d3a4299c8ea5cc297b48e586731cc model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md), which is authoritative.

<a id="maintenance"></a>

# メンテナンス

ここで紹介するコマンドはすべて `loom` サブコマンドです。デプロイメントの環境変数が設定されたコンテナ内で実行してください（`docker exec loom-server loom …`、分割構成のデプロイメントでは `loom-workers`）。すべてのフラグは [CLI リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)に記載されています。コードインデックスには専用の運用手順書があります：[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)。

<a id="before-you-run-a-write"></a>

## 書き込みを実行する前に

以下のコマンドの多くは、**デフォルトでドライラン**です。出力を確認し、`--org` と `--namespace` で対象を絞り込んでから、書き込みフラグ（`--apply`、消去の場合は `--erase`。各表にフラグ名と例外を記載しています）を渡してください。ミューテーション（`ALTER … UPDATE`）を発行するコマンドは、ClickHouse がそれを適用する前に処理を返します。適用状況は `system.mutations` で確認してください。

<a id="re-embed-and-re-extract"></a>

## 再埋め込みと再抽出

- **`loom reembed`** は、`embedding_model` が設定済みの埋め込みモデルと異なるメモリ、エンティティ、会話のベクトルを再埋め込みします。Loom が自動で再埋め込みを行うことはないため、`LOOM_EMBEDDING_MODEL`（および `LOOM_EMBEDDING_DIM`）を変更した後に実行してください。`--org` / `--namespace` で対象を絞り込めます。`--dry-run` は行数を数えます。`--delete-old` は、その後に旧モデルの行を削除します。`--key-only` は、有効な各行のキー軸ベクトルだけを再構築します。ただしインポートキー（`import:` とダイジェスト）にはキー軸ベクトルがないため、ベクトルのない行はスキップし、古いビルドが埋め込んだ行はクリアします。`--in-database <named collection>` は、Python のループではなく、テーブルごとに 1 つの `INSERT … SELECT` として、ClickHouse 内で `aiEmbed`（ClickHouse 26.7 以降）を使って埋め込みを実行します。顧客 ID ヘッダーまたは識別ヘッダーが設定されている場合、あるいは推論ゲートウェイが必須の場合は拒否されます。ClickHouse 自身の呼び出しには顧客も識別情報も指定されず、ゲートウェイのホストチェックも行われないためです。Python 経路は `--batch-size` 行ずつソートキー順に読み込み、ページごとに `resume:` トークンを出力します。`--resume <token>` を指定すると、中断した実行をそこから再開します。Python 経路は、テキストの機微度が high と判定される会話ターンを埋め込みモデルに送りません。その行は旧モデルのまま残り、検索はこれを読まず、`--delete-old` が削除するまで残ります。メモリ行のベクトルは、その機微度ラベルに従います。`--in-database` はテキストを確認できないため、そのようなターンも再埋め込みしますが、検索では引き続き除外されます（[機微度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。
  - Python 経路は、各ページの挿入の前に、そのページの行をキーで読み直します。ページの読み込み以降に、削除された行、トゥームストーン化された行、テキストや別のエージェントまたはユーザーに変更された行はスキップされます。それ以外の行は、2 回目の読み込みで得た他の列の値のまま書き込まれます。
  - スキップされた行が、別のテキストや識別情報のまま保存されている旧モデルの行である場合、`--delete-old` はその組織の旧モデルの行を次回の実行用に残します。次回の実行では、その組織の旧モデルの行がすべて再埋め込みされます（`--org` で絞り込めます）。`resume:` トークンにはそれまでに残された組織が含まれ、実行は削除の前にそれらの組織名を表示します。
  - その読み込みと挿入の間に行われた削除、墨消し、消去、あるいはミューテーションがまだその行に届いていない削除などは、その行が新しいベクトルを持ったまま有効に残る原因になります。そのような墨消しは実行の完了後にもう一度行い、そのような会話ターンやスレッドも再度削除してください。孤立ベクトルのスイープは、削除されたメモリの残りのベクトルをトゥームストーン化し、消去の検証ステップは行を見つけて再試行します。`--in-database` は、ステートメントの開始時に各テーブルを 1 回だけ読み込むため、墨消しや消去が行われていない間に実行してください。
- **幅の異なるモデル。** ClickHouse 26.8 以降では、`enable_quantized_codec` を有効にできる環境で、新規インストールのベクトル列はコード化（`Quantized`）されます（[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)）。コード化された列は、作成時の幅のベクトルしか受け付けません。幅の異なるモデル（8 の倍数）への変更は、設定 → モデルとキーで行います。これによりベクトルテーブルが新しい幅で再構築されます。`loom reembed` はテーブルが受け付けない幅を拒否します。`LOOM_EMBEDDING_DIM` だけを編集すると、すべてのベクトル書き込みが 503 `CODED_WIDTH_MISMATCH` を返し、`/v1/health` は `coded_width_mismatch` を報告します。
- **`loom rechunk --org ORG --namespace NS`** は、保存済みのコンテンツから、有効なすべてのドキュメントに対して `knowledge.upsert` を再実行します。これにより、ドキュメントは現在のチャンク分割、チャンクコンテキスト、ベクトルを取り込みます（1 つのドキュメントだけを対象にするには `--source-uri` を使います）。`--apply` を付けるまではドライランです。
- **`loom lexical-stats-rebuild --org ORG --namespace NS`** は、有効な行から BM25 の文書頻度を再計算します。語頻度ビューは挿入しか認識しないため、置き換え、トゥームストーン、一括削除によって生じた古い語彙は、このコマンドを実行するまで残ります（`--no-memory` / `--no-knowledge` で片側をスキップできます）。実行すると即座に書き込まれます。
- **`loom reextract --org ORG --namespace SRC --into DST`** は、ある名前空間に保存された `set_from_messages` のトランスクリプトを、実行中のサーバーの現在の抽出器に通して、別の名前空間へ再生します（`--token` は `DST` 用のライタートークンで、省略時は `$LOOM_API_KEY`、次に `~/.loom/token` が使われます）。ソースは読み取られるだけなので、リーダーを切り替える前に 2 つの名前空間を比較できます。`--dry-run` はセッションを一覧表示します。`--limit-sessions N` は一部のサンプルを再生します。
- **`loom recalibrate`** は、埋め込みモデルを再プローブし、適応型の類似度しきい値を直ちに再計算します。このコマンドを使わない場合は、`recalibration` ワーカーが週に 1 回行います。

再埋め込みが手動なのは、新しいベクトル空間ではすべての類似度スコアが変わるためです。手動で実行することで、切り替え、コスト、検索品質の再ベースライン化を、オペレーターの 1 つの作業にまとめられます。

<a id="backfills-and-repairs"></a>

## バックフィルと修復

| コマンド                         | 内容                                                                                                                                                      | 書き込みフラグ |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `loom integrity-repair`          | 整合性ゲージとその根拠となる行を出力する。検索で見えるように `dim0_with_vector` 行を再送出する（ドリフト行と `valid_to` 行は一覧表示のみ）                | `--apply`  |
| `loom sensitivity-sweep`         | 機密度検出器がラベルを付ける、ラベルなしの有効な行を数える。書き込み時にラベルを付与し、`high` 行の埋め込み行をトゥームストーン化する                    | `--apply`  |
| `loom reconcile-status`          | ステータスが `loom.memory` と食い違っている `memory_embeddings` 行を再送出する                                                                            | `--apply`  |
| `loom demote-activity-narration` | 永続的な成果を伴わずにツールの動作を語るだけの抽出済みファクトを見つけ、`memory.apply` を通じて降格またはラベル付けする。削除は行わない                   | `--apply`  |
| `loom namespaces-adopt`          | 有効なメモリを持つがレジストリ行のない名前空間に、オーナー行を付与する                                                                                    | `--apply`  |
| `loom namespaces-gc`             | プレフィックスでアイドル状態のベンチマーク名前空間を見つける（`--worker-pass` を付けると `namespace_gc` を 1 パス実行する）。それらの消去を送信する       | `--erase`  |
| `loom audit-namespace`           | 名前空間の使われ方（完全一致キーの読み取りと検索の比率、サイズ超過または二重エンコードされた値、バージョンの変動）を報告する。読み取り専用。重大な指摘がある場合は終了コード 1 | 読み取り専用  |

これらの修復が対応する整合性ゲージは、[モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#integrity-invariants)に一覧があります。行の形が誤っている名前空間（メモリとして保存されたドキュメント、ファクトとして保存されたリースなど）の再整備は、[名前空間の最適化](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)でガイド付きの手順として説明しています。

<a id="memory-ingest-drops"></a>

## メモリ取り込みのドロップ

取り込みパイプラインがデータを破棄、切り詰め、または処理を断念する箇所では、必ずドロップが記録されます（`server/memory/app/drops.py`、`record_drop`）。ドロップはプロセス内にバッファされ、`loom.memory_drops` にフラッシュされ（30 日間保持）、カウンター `loom.memory.drops{stage, reason}` にも反映されます。ドロップの記録が例外を送出することはなく、原因となった書き込みをブロックすることもありません。

```bash
docker exec loom-server loom drops --namespace NS [--org ORG] [--hours 24]
```

このコマンドは、対象期間の `stage` と `reason` ごとの件数と、各組み合わせが最後に確認された時刻を出力します。`SELECT * FROM loom.memory_drops WHERE namespace = '…' ORDER BY ts DESC` を実行すると個々のアイテムを確認できます（`item` にはキー名またはフィールド名が入り、メトリクスのラベルには使用されません）。stage には `extract.parse`、`extract.judge`、`extract.truncate`、`write`、`entity.extract`、`consolidate.merge`、`records.field`、`chunk.split`、`conflict.deferred` などがあります。

抽出が完全に失敗した場合はドロップではありません。`set_from_messages` は 502 `EXTRACTION_FAILED` を返し、部分的な失敗はレスポンスの `pipeline_errors` に含まれて返されます。

<a id="entity-graph-maintenance"></a>

## エンティティグラフのメンテナンス

`loom.entity_edges` のライターは、語彙的なノイズ（代名詞、感嘆詞、軽動詞、短縮形の断片、2文字のトークン）をグラフから除外します。

メモリより長く残ったグラフを持つ名前空間は、`loom erasure-re-verify --org ORG --orphan-graph` で一覧表示でき、`--erase` を付けて実行するとパージされます。

<a id="erasure"></a>

## 消去

| 呼び出し                                                                        | 効果                                                                                                                                       | 用途                                                                 |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| `memory.clear` (`ADMIN`)                                                        | 名前空間内の 1 つの `user_id` または `agent` のメモリを tombstone 化する。`confirm_subject` にはスコープの値を繰り返し指定する必要がある   | クリーンアップ、テストのリセット。`memory.restore` で元に戻せる      |
| `erasure.request` と `scope_type`: `user_id`、`namespace` または `namespace_list` | 対象のコンテンツを保持しうるすべての Loom テーブルにわたる再開可能なサガ。その後、設定済みの外部バックエンド（Langfuse、ClickStack）に対して実行する | ユーザーのデータの消去（監査証跡は保持される）、名前空間の廃止 |

サガは各ステップを `loom.erasure_requests`（`ReplacingMergeTree`、行は 89 日間読み取られ、90 日で削除される）にチェックポイントとして記録する。ステップの完了後、読み取りパスが参照するすべてのテーブルに対して残留チェック（residue census）を実行する。スコープ内に行が 1 つでも残っていればサガは失敗し、最後の行には `residue_count`、`residue`、`verified_at`、`census` が記録される。削除を確認できないバックエンドがある場合、サガはフェイルクローズで失敗する。`erasure_recovery` ワーカーは `max_attempts` までべき等に再試行し、その後は `failed` のままにする。`purge: true` は tombstone 化せず物理削除する（使い捨ての名前空間向け。復元の猶予期間はない）。`external: false` は外部バックエンドをスキップし、`namespace_list` でのみ指定できる。消去が保証する内容と保証しない内容については、[保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)を参照。

```sql
SELECT id, scope_type, scope_value, status, current_step, attempts, failure_reason, residue_count
FROM loom.erasure_requests FINAL
WHERE status != 'completed' ORDER BY requested_at DESC LIMIT 20;
```

`loom erasure-re-verify --org ORG` は、失敗した各サガの最後の失敗理由と、そのスコープに対する新しい残留チェックの結果を読み取り専用で表示する（1 つのサガだけを対象にするには `--id` を使う）。ユーザーのサガについては、要求時に指定された `purge` を使って確認する。`--resume` は、失敗したサガをチェックポイントから同じ方法で再実行する。recovery ワーカーの試行回数の上限を超えても実行できる。直前の残留チェックで行が見つかったテーブルを担当するステップは、チェックポイントで完了済みとなっていても再実行される。`--skip-external`（`--resume` と併用し、失敗した `namespace_list` のサガのみが対象。`--completed-since` とは併用できない）は、外部バックエンドが失敗し続ける場合に Loom のテーブルだけを完了させる。`--completed-since <ISO timestamp>` は、失敗したサガの代わりに、残留チェックがその時刻以降に実行された完了済みのサガを対象にする（タイムスタンプにオフセットがない場合は UTC）。用途は、ロールアウト後に、以前のビルドが実行した消去を確認することである。これらについては、サガのスコープ内の孤立した行リンク（対応するメモリに有効な行がなく、かつサガの残留チェック以降に書き込まれた行もない `entity_triple_rows` と `entity_edge_rows` の行）のみを数え、`--resume` を付けた場合はそれだけを削除する。サガのステップは再実行されないため、消去後に書き込まれたデータは、`memory.delete` で tombstone 化された後でも残る。サガを復旧できない場合は、同じ `erasure.request` を再度発行する。この操作はべき等である。

<a id="clickstack-erasure-index"></a>

### ClickStack 消去インデックス

ClickStack 側の処理では、属性値が一致する OTel の行を削除する。ClickStack のスキーマは、すべての OTel テーブルの属性マップに bloom filter インデックスを付けており、`schemas/otel/clickstack_otel_tables.sql` もそのインデックス付きでテーブルを作成する。`mapValues(Attributes)` なしで作成された `otel_metrics_*` テーブル（別のインストーラーによるもの）では、消去のたびにそのテーブルが全件スキャンされる。`loom doctor` と `/v1/health` は、インデックスの欠落を `otel_erasure_index` として報告する。`CREATE TABLE IF NOT EXISTS` は既存のテーブルにインデックスを追加しないため、テーブルごとに 1 回、手動で追加する。

```sql
ALTER TABLE default.otel_metrics_histogram
  ADD INDEX IF NOT EXISTS idx_attr_value mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1;
ALTER TABLE default.otel_metrics_histogram MATERIALIZE INDEX idx_attr_value;
-- repeat for otel_metrics_sum and otel_metrics_gauge (and the other metrics tables if populated)
```

ClickHouse Cloud では、`ADD INDEX` の直後に発行した `MATERIALIZE INDEX` が何も構築しないことがある。対象テーブルについて `system.data_skipping_indices` の `data_compressed_bytes` を確認し、0 であれば `MATERIALIZE` を再度発行する。

<a id="retention"></a>

## 保持期間

Loom は、書き込み元が削除するまですべての行を保持します。デフォルトでは、経過日数による削除は行われません。保持に関するゲージ（`loom.retention.*`）は、削除の候補を数えるだけです。

- **テーブルごと。** `audit_log`、`agent_events`、`memory`、`memory_embeddings`、`conversations`、`chunks`、`documents` には `LOOM_RETENTION_<TABLE>_DAYS=N` を使います。
- **名前空間ごと。** 名前空間の登録（`namespace.register`）にある `retention_days` を使います。管理対象テーブルの TTL は、これを設定したすべての名前空間にわたる 1 つの `multiIf` になります。
- ワーカーのリーダーは、起動時に `MODIFY TTL` を適用します。それ以降にテーブルの TTL を書き込むのは `retention_reconcile` ワーカーだけです。そのため、ポリシーの変更は `LOOM_RETENTION_RECONCILE_INTERVAL_SECONDS` 以内にテーブルへ反映され、登録が短時間に集中しても、登録のたびにテーブルが書き換えられることはありません。

ホット／ウォーム階層化には ClickHouse のストレージポリシーを使います。`LOOM_TIERING_<TABLE>_WARM_DAYS=W` と、`storage_policies` にあるボリューム名を指定する `LOOM_TIERING_WARM_VOLUME_NAME` を設定します。

一部の運用テーブルは、メモリではなく診断用のデータであるため、固定の TTL を持ちます。`loom.memory_drops`（30 日）、`loom.worker_status`（30 日）、`loom.erasure_requests`（90 日。読み取りは 89 日で止まります）です。各テーブルの TTL は[スキーマリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)に一覧があります。

<a id="compaction-and-optimize"></a>

## コンパクションと OPTIMIZE

`loom.memory` と `loom.shared` は `ReplacingMergeTree(version)` テーブルです。更新、トゥームストーン、復元のたびに新しいバージョンが書き込まれ、バックグラウンドのマージがそれらを集約します。リーダーは `FINAL` を使うため、未集約のバージョンが影響するのは読み取り時間であり、正しさではありません。`loom.memory` で `count() / uniqExact(memory_key)` の比率が大きい場合は、未集約のバージョンが多いことを意味します。大規模な消去の後は、マージが追いつくのを待つか、ピーク時間帯を避けて `OPTIMIZE TABLE loom.memory FINAL` を実行してください。

コードインデックスのベクタテーブルには、マージの前に計測を行うコマンドがあります。

```bash
docker exec loom-workers loom code compact                     # measure copies per key
docker exec loom-workers loom code compact --table all --apply # OPTIMIZE … FINAL where the ratio is reached
```

このコマンドは、キーあたりの物理コピー数が `--min-ratio`（デフォルトは `LOOM_CODE_COMPACT_MIN_RATIO`、1.5）に達したコードベクタテーブル（デフォルトは `code_chunks_prefix`。`code_chunks`、`all`、または `LOOM_CODE_VECTOR_TABLES` 内のテーブルも指定可能）に対して `OPTIMIZE … FINAL` を実行し、マージの完了を待ちます（`--timeout-s`）。`code_chunks` はコード中心のデプロイメントで最大のテーブルです。事前に告知したウィンドウでコンパクションしてください。バルク処理は、`LOOM_CODE_COMPACT_AFTER_BULK_ROWS` 行の後に自動でコンパクションします。

**テーブルのソートキー、パーティションキー、ベクタコーデックの変更**は再構築であり、`ALTER` では行いません。`scripts/ops/table_rebuild.py` と `scripts/ops/vector_codec_rebuild.py` は、ツインテーブルへコピーして検査し、`EXCHANGE TABLES` で入れ替え、古いテーブルを `--rollback` 用に残します。どちらも計画を表示し、`--yes` がなければ何も変更しません。ソートキーとパーティションキーの手順は[マイグレーション](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#changing-a-sort-key-or-a-partition-key)に、ベクタコーデックの手順は[推奨設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#recommended-clickhouse-settings)にあります。

`vector_codec_rebuild.py` には、すべてのライターと消去を停止し、処理中の書き込みをすべてドレインしたメンテナンスウィンドウが必要です。稼働中のテーブルを修復することはありません。コピーの前に、ソースのミューテーションが実行中の場合、または直近 1 分以内に作成された場合は実行を拒否します。この 1 分はレプリカ間の時計のずれを吸収するためのもので、スワップ前のゲートもこれを数えます。最後のミューテーションが完了してから 1 分後に再実行してください。`--reuse` を付けると、再利用するツインの作成以降のすべてのミューテーションが、完了済みのものも含めて数えられます。そのようなミューテーションがあるとツインは古くなるため、再実行は再コピーを行う `--restart --yes` になります。また `--reuse` は、実行が作成するツインと、名前付きカラム、粒度、ソートキーが異なるツインも拒否します（`--method`、`--index-granularity`、`--chain` が異なる場合や、別のツールのツインの場合）。ゲートは保存されたベクタとコードを読むため、その違いを検出できないからです。空のテーブルも再構築されますが、その幅は下の「Width」の項目に従います（コード化されたカラムは自身の幅、それ以外は `LOOM_EMBEDDING_DIM`）。この場合、リコールゲートには調べる行がありません。ツールの ClickHouse ユーザーには、テーブルに対する `SYSTEM FLUSH ASYNC INSERT QUEUE` と `SYSTEM SYNC REPLICA` の権限が必要です。`ReplicatedMergeTree` では、さらに `system.zookeeper` と `system.replication_queue` に対する `SELECT`（`loom ch-setup` は付与しません）、およびツイン `loom.<table>_rebuild`（ゲートが読み取りのたびに同期します）と、スワップが作成するロールバック用コピー `loom.<table>_pre_rebuild`（スワップ後のチェックと `--drop-pre-rebuild` が同期します）に対する `SYSTEM SYNC REPLICA` が必要です。プリフライトは最初の 2 つを一度実行し、いずれかが失敗するとエラーを示して拒否します（終了コード 3）。それ以外はテーブルが存在して初めて実行できるため、ツインの同期が拒否された場合はゲートが失敗します（終了コード 5）。`--dry-run` も最初の 2 つを実行するため、キューに溜まった行をテーブルに反映し、遅れているレプリカでは同期を待ちますが、それ以外は何も書き込みません。ゲートが失敗した場合（終了コード 5）は、書き込みと消去を停止して `--restart` で再実行してください。ただし、テーブルに行はあるものの調べるための実ベクタがない場合は、再実行しても同じ失敗になります。その場合はツインを削除し、実行結果の `next` に示されるとおり、テーブルに埋め込みが入るまでそのテーブルをスキップしてください。幅による拒否（終了コード 3）には、後述の再埋め込みが必要です。

再構築が完了するか中止され、すべての `<table>_rebuild` ツインと `<table>_pre_rebuild` ロールバック用コピーが削除されるまで、ライターと消去は停止したままにしてください。スワップに成功すると、そのログとサマリー行の `next` に、次のステップとしてこの内容が表示されます。コマンドを示す各ネクストステップには、実行時の `--method`、`--chain`、`--index-granularity`、`--gate-fetch-multiplier`、`--probes`、`--require-patch-free`、`--cluster`、`--statement-timeout-s` が付き、デフォルト以外の場合は `--abort-file`（ツインの記録はその隣にあります）も付くため、表示されたとおりに実行できます。検証後またはロールバック後にロールバック用コピーを削除するには `--drop-pre-rebuild --yes` を使います。放棄したツインを削除する `DROP TABLE loom.<table>_rebuild SYNC` は、そのテーブルの行に対する `--dry-run` が `not swapped` で始まるネクストステップを表示した場合（調査用に残したツイン、またはスワップされなかった残骸）に限って実行してください。ネクストステップに `RENAME TABLE loom.<table>_rebuild TO loom.<table>_pre_rebuild` が示されている場合、または 2 つのテーブルの比較を求められている場合は、先にそれを実行し、何も削除しないでください。rename のステップは、rename なしで exchange が実行されたこと、つまり `<table>_rebuild` が古いテーブルであることを意味します。rename が実行されるまで、そのテーブルの行に対するすべての実行（`--yes`、`--reuse`、`--restart` を付けた同じ行を含む）と `--drop-pre-rebuild` は、終了コード 3 で拒否されます。ツールはこれを、実行時のフラグではなく 2 つのテーブルのすべてのベクタカラムから判断するため、別の `--method`、`--chain`、`--index-granularity`、`--columns` での再実行も拒否されます。コード化されたベクタカラムを持つ稼働中テーブルの下にある、残骸と確認できないツインは、古いテーブルである可能性があるからです。稼働中テーブルでコード化されているカラムのそれぞれがツインでは通常のカラムである場合、ツールは rename を示します。ツインでもコード化されているカラムがある場合（`--require-patch-free` の実行、またはコード化されたテーブルの再構築）、コーデックからは exchange が実行されたかどうかが分かりません。その場合ツールは拒否し、rename も削除も行わず、どちらかを手作業で削除する前に 2 つのテーブルの行を比較するよう求めます。ソートキーまたはパーティションキーが稼働中テーブルと異なるツインは、`scripts/ops/table_rebuild.py` が rename なしで exchange した古いテーブルである可能性があります。このツールはツインと同じ名前を共有するためです。その場合はすべての行が拒否され、何も削除されず、`python -P -m scripts.ops.table_rebuild --table <table> --dry-run` が示され、そのレポートに rename が記載されます。一度も exchange されていないツインは、再実行がどのレイアウトを指定していても残骸です。`--restart --yes` で再コピーするか、削除してください。ツールは、ツールがツインを作成した時点で `$LOOM_REBUILD_DIR/<table>_rebuild.uuid`（abort ファイルのディレクトリ）に記録された UUID が、ツインと稼働中テーブルのものと一致する場合に、そのツインが一度も exchange されていないと判断します。これはどのデータベースエンジンでも成り立ちます。exchange が行われると、稼働中テーブルの名前には別の UUID が付くためで、別のディレクトリから実行した `--reuse` のスワップも同様であり、その場合は記録が残されたままになります。ツールは自身の exchange の直前にこの記録を削除し、削除できない場合はスワップを拒否します。一致する記録がない場合、`system.tables.metadata_modification_time` が稼働中テーブルより新しいツインは、一度も exchange されていないものとして扱います。これは Atomic と Replicated のデータベースで成り立ちます（`EXCHANGE TABLES` は各テーブル自身の時刻を保つため）。稼働中テーブルのメタデータ時刻がツインより新しい場合、スワップは拒否され（終了コード 3、ツインは保持）るからです。ゲートの実行中に稼働中テーブルへ行った `ADD INDEX` や `MODIFY SETTING` はミューテーションを作りませんが、ハーフスワップの後では古いテーブルのほうが新しくなってしまいます。そのような ALTER の後は `--restart` で再実行してください。時刻が等しい場合（このカラムは 1 秒精度です）や、それ以外のデータベースエンジンでは何も判断できないため、ツールはそのツインを古いテーブルの可能性があるものとして扱います。毎回同じ `LOOM_REBUILD_DIR` からツールを実行してください。`--drop-pre-rebuild` はまず、ポストコンディションと同じドレインを行ったうえで 2 つのテーブルを再度比較します。これは両方がターゲットのレイアウトにある場合（すでにそのレイアウトにあるテーブルの `--require-patch-free` 再構築）も同様です。[スワップ後のチェックで見つかること](#what-the-checks-after-a-swap-find)にある差異が 1 つでもあれば、拒否し（終了コード 3）、コピーを保持します。両方がターゲットのレイアウトにある場合、`--rollback` と `--drop-pre-rebuild` は、メタデータ時刻が新しいほうのテーブルを新しいテーブルとして扱います。これは Atomic と Replicated のデータベースに限られ、時刻が等しい場合や他のエンジンでは、どちらも拒否し（終了コード 3）、手作業で比較できるよう両方のテーブルを保持します。消去サガはこれらのコピーを対象としません。

- **Width（幅）。** 通常のカラムのターゲット幅は `LOOM_EMBEDDING_DIM` です。そのため、デプロイメントの env でツールを実行してください。コード化されたカラムは自身の幅を持ちます。`LOOM_EMBEDDING_DIM` は、提供しているモデルの幅と一致している必要があります。ライターは起動時にサーバーがモデルから調べた幅を使うため、古い `LOOM_EMBEDDING_DIM` はサーバーログに `embedding dim mismatch` 行を出すだけです。名前付きの通常カラムに別の幅の実ベクタ（すべてゼロではないもの）がある間、ツールは `--dry-run` も含めて拒否します（終了コード 3）。コピーすると実ベクタがゼロベクタに置き換わってしまううえ、コード化されたカラムは他の幅の書き込みを拒否するからです。メッセージには各幅と `LOOM_EMBEDDING_DIM` が示されます。すべての実ベクタが別の 1 つの幅で、サーバーログにその幅がモデルのものとして示されている場合は、`LOOM_EMBEDDING_DIM` をその幅に設定して再実行してください。再埋め込みはしないでください。それ以外の場合は `LOOM_EMBEDDING_DIM` に合わせて再埋め込みしてから再実行します。`memory_embeddings`、`entity_vectors`、`conversation_embeddings` には `loom reembed --delete-old` を使いますが、これは別の `embedding_model` の行だけを再埋め込みするため、同じモデルで幅だけが変わる場合は Settings → Models & keys から行います。`memory_chunks`、`code_chunks`、`code_summaries` など、それ以外のテーブルは、それぞれのライターが再埋め込みします。
- **Gates（終了コード 5）。** 整合性ゲートは、各キーの最新バージョン（バージョンカラムのないテーブルでは行数）と、すべてのカラムのハッシュを比較します。ソースの再構築対象ベクタカラムは、コピーが書き込むのと同じようにキャストとパディングを施して比較されます。ツインにないキー、またはバージョンが古いキーは、キーごとに再コピーされ、それでも差があるキーが残ればゲートは失敗します。ツインにしかないキーがあっても失敗します。コピーの後にソースから行が削除されており、スワップするとそれらが復活してしまうからです。リコールゲートは実ベクタだけを順位付けします。プレースホルダのコサイン距離は NaN になるためです。
- **キーとバージョンが同じ行。** `FINAL` の下では、ReplacingMergeTree は、キーの最新バージョンの行のうち最後に挿入されたものを残します。コピーの並列 `INSERT` はそれらを別の順序で挿入することがあるため、何度コピーしても、ツインが別の行を保持する可能性があります（`DateTime` でバージョン管理されたテーブルで、同じキーに同じ秒内で 2 回書き込むだけで起こります）。キーに差が残る場合、ゲートはソース上のそのようなキーを数え、メッセージにその名前を示します。ウィンドウ内でソースに対して `OPTIMIZE TABLE loom.<table> FINAL` を実行すると、キーごとに 1 行になります。その後 `--restart` で再実行してください。
- **スワップ前。** ツールはソースの非同期インサートキューをフラッシュします（レプリケートエンジンでは `SYSTEM SYNC REPLICA` も実行しますが、これは応答したレプリカだけを同期します。`ReplicatedMergeTree` では `LIGHTWEIGHT` を付けるため、待つのはフェッチであり、マージではありません）。`ReplicatedMergeTree` では、実行中ずっと 1 つのレプリカに直接接続してください。以降の整合性と幅の読み取りは、それぞれ同じステートメントの中で、応答したレプリカが読み取り対象テーブルのレプリケーションログ全体をコピー済みか、そのキューにマージとミューテーション以外のエントリが残っていないかも読み取ります。エントリが 1 つのログをまだ取り込んでいないレプリカは、空のログを持つレプリカと同じゼロのログポインタを報告します。そのため実行は `system.zookeeper` でログのエントリを読み、エントリのないログの場合に限ってゼロを通します。そのユーザーには `system.zookeeper` と `system.replication_queue` に対する `SELECT` が必要で、どちらも読めない場合は、遅れているレプリカでの読み取りと同様に拒否されます。追いついていないレプリカでの読み取りは、プリフライトで実行を拒否し（終了コード 3）、スワップ前のゲートを失敗させ（終了コード 5）、スワップ後のポストコンディションを失敗させます（終了コード 7）。以降のすべてのゲート読み取りと最後のデルタは `select_sequential_consistency = 1` を使います。`SharedMergeTree` では、別のレプリカが読み取りに応答しても、確認済みの書き込みがこれでカバーされます。`ReplicatedMergeTree` では、この設定がカバーするのは `insert_quorum_parallel = 0` のクォーラムインサートだけで、レプリカへの直接接続の代わりにはなりません。幅と整合性のチェックは、次の場合にゲートを失敗させます。削除、別の幅の書き込み、デルタが取り込めない書き込み（ツインの最大値より小さいバージョン、またはバージョンカラムのないテーブルでツインがすでに持つキーの下の行）、そして、キーとバージョンを保ったまま行のカラムを変更するあらゆる操作（再構築対象のベクタも含み、軽量 `UPDATE` や消去のトゥームストーンなど。パッチパートが残っているかどうかは問いません）です。それ以外のインサートはコピーに取り込まれ、スワップは進みます。ライターを止めておくのはツールではなくウィンドウの役目です。ゲートは、コピー開始以降（`--reuse` ではツイン作成以降）に作成された、または実行中のソースのミューテーションがあっても、レプリカの時計の分として 1 分を差し引いたうえで失敗します。
- **スワップ後（終了コード 7）。** ツールは整合性チェックをもう一度実行します。ロールバック用コピーをソース、新しいテーブルをツインとし、最後のゲート読み取りと同じ読み取り設定とレプリカチェックを使い、コピー以降の両テーブルのミューテーションを数えます。`ReplicatedMergeTree` では、まずロールバック用コピーに対して `SYSTEM SYNC REPLICA ... LIGHTWEIGHT` を実行します。このコピーには独自のレプリケーションキューがあり、スワップの前に別のレプリカに届いた書き込みは、rename の後ではこのコピーに属するためです。`LIGHTWEIGHT` を使うのは、スワップがコピーのマージを止めるため、フル同期ではマージのエントリを待ってしまうからです。マージはどちらのテーブルの行も変えないため、問題なく通ります。差異があればポストコンディションが失敗し、ロールバック用コピーが保持されます。表示されるネクストステップは[スワップ後のチェックで見つかること](#what-the-checks-after-a-swap-find)にあります。

`--statement-timeout-s`（デフォルトは 1800、クライアントの読み取りタイムアウトである 7200 未満）は、幅、整合性、ミューテーション、デルタ、リコールの読み取りに上限を設けます。スワップ後の読み取りと `--drop-pre-rebuild` 内の読み取りも対象で、スワップ前の各デルタと修復にも適用されます。テーブルの行数、カラム、カタログ行を読む小さな読み取りはこの制限なしで実行され、コピーとロールバックの追いつきコピー（その行を探す読み取りを含む）は、7000 秒という独自の制限の下で実行されます。スワップ後の読み取りがタイムアウトすると、ポストコンディションが失敗し（終了コード 7）、ロールバック用コピーが保持されます。`--drop-pre-rebuild` 内でタイムアウトした場合もコピーは保持されます。各幅チェック（プリフライト、スワップ前、スワップ後のロールバック用コピー）は、通常の名前付きカラムのすべてのベクタを読み取り（ReplacingMergeTree では `FINAL` の下で）、各整合性チェック（コピーまたは修復の後、スワップ前、スワップ後）は、ベクタを含む両テーブルのすべての行を読み取ります。そのため、テーブルを 1 回全件読み取れるだけの時間をタイムアウトに設定してください。

<a id="what-the-checks-after-a-swap-find"></a>

### スワップ後のチェックで見つかること

スワップ後の実行は、以下の各項目が見つかるとポストコンディションが失敗し（終了コード 7）、`--drop-pre-rebuild` は拒否します（終了コード 3）。どちらの場合もロールバック用コピーは保持されます。次のステップはサマリー行の `next` に示されます。

| チェックで見つかること | 表示されるネクストステップ |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| スワップ後に、ロールバック用コピーだけが持つ変更：そこにしかないキー、そこにある新しいバージョン、そこでの未完了の `ALTER UPDATE`、または別の幅のベクタを設定する `UPDATE` | `--rollback --yes`。変更を含めた状態で古いテーブルを元に戻します |
| スワップ後に、ライブテーブルだけにあるキー、またはライブテーブルにあるより新しいバージョン：スワップ後の新テーブルへの書き込み、または最後のチェック後の旧テーブルでの削除 | 両方のテーブルを残し、差分を手動で確認する |
| 1つのバージョンで内容が異なるキー：どちらかのテーブルでの軽量な `UPDATE`（マージ済みかどうかを問わない）、またはスワップ前に完了した `ALTER UPDATE` | 両方のテーブルを残し、差分を手動で確認する |
| スワップ後に、コピー開始以降のライブテーブルへのミューテーション。例：`loom reembed --delete-old` が実行する軽量な `DELETE` | 両方のテーブルを残し、差分を手動で確認する |
| 2つのテーブルで列の名前が異なる | 両方のテーブルを残し、差分を手動で確認する |
| ロールバック後に、新レイアウトのコピーだけにある変更、または作成以降のそのコピーへのミューテーション | 両方のテーブルを残し、差分を手動で確認する |
| チェックを実行できなかった：ドレインまたは同期の失敗、タイムアウトを超えた読み取り、または追いついていないレプリカ | 権限を修正する、1つのレプリカに直接接続する、または `--statement-timeout-s` を引き上げて、拒否されたアクション（`--drop-pre-rebuild` または `--rollback`）を再実行する |

`--rollback` は、旧テーブルにない行だけをコピーし戻します。バージョン列を持つテーブルでは、旧テーブルの最大値を超えるバージョンの行、バージョン列を持たない `chunks` では、旧テーブルにないキーの行が対象です。ライブテーブルでの軽量な `UPDATE` や `DELETE`、または消去用トゥームストーンは、キーとバージョンを保ちます。また、旧テーブルの最大値以下のバージョンで書かれた行はコピーし戻されないため、`--rollback` ではそのような変更が失われます。ロールバックは、交換の前に上記のチェックを実行し、次の手順が `--rollback` ではないすべての検出結果について、何も交換せずに拒否します（終了コード 3）。旧テーブルのマージを再開するため、ロールバックコピーに未完了のミューテーションがあれば、ロールバック後に適用されます。続く `--drop-pre-rebuild` では、そのミューテーションが触れたキーが、1つのバージョンで異なるものとして表示されます。ロールバック後の新レイアウトのコピーにある未完了の `ALTER UPDATE` は `FINAL` では表示されません。そのため、そのコピーへのミューテーションもカウントの対象です。ロールバックが成功すると、サマリーに次の手順が示されます。ライタと消去を停止したまま `--drop-pre-rebuild --yes` を実行します。このコマンドは、新レイアウトのコピーを削除する前に2つのテーブルをチェックします。2回目の `--rollback` は拒否され（終了コード 3）、その手順が示されます。ライブテーブルがもはや新レイアウトではないためです。
ライブテーブルでの削除は、そのキーをロールバックコピーにだけ残します。そのため、最初の行が `--rollback` を示すのは、コピー開始以降にライブテーブルへのミューテーションがない場合に限られます。

差分を手動で確認するには、ライタと消去を停止したまま、2つのテーブルで内容が異なるキーを、両側のバージョン列とともに一覧表示します。`<version>` はテーブルの ReplacingMergeTree のバージョン列です。`version`、`memory_chunks` と `entity_vectors` では `created_at`、`conversation_embeddings` では `ts` です。`<key>` はテーブルのソートキーです。`<copy_vectors>` と `<live_vectors>` には、リビルドで指定したベクトル列（`--columns`）だけを列挙します。`<columns>` は、他のベクトル列を含むそれ以外のすべての列で、両側で同じ名前を使います。ツールはそれらを保存された状態のままハッシュします。
リビルドで指定した列については、新レイアウト側（スワップ後はライブテーブル、ロールバック後はコピー）では列をそのままの名前で指定します。旧レイアウト側では、リビルドが書き込むとおりに `if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0)))` と書きます。`<dim>` はコーデックの幅（新レイアウトの `SHOW CREATE TABLE` にある `Quantized(<method>, <dim>, 0)`）で、これにより両方のレイアウトが同じようにハッシュされます。

```sql
SELECT <key>, s.<version> AS copy_version, t.<version> AS live_version,
       s.__present AS in_copy, t.__present AS in_live
FROM (SELECT <key>, <version>, cityHash64(tuple(<columns>, <copy_vectors>)) AS __hash,
             1 AS __present FROM loom.<table>_pre_rebuild FINAL) AS s
FULL OUTER JOIN
     (SELECT <key>, <version>, cityHash64(tuple(<columns>, <live_vectors>)) AS __hash,
             1 AS __present FROM loom.<table> FINAL) AS t USING (<key>)
WHERE s.__hash != t.__hash
SETTINGS join_use_nulls = 0
```

バージョン列のないテーブルでは、`FINAL` と `<version>` を外し、各側で `count() AS n, sum(cityHash64(...)) AS __hash ... GROUP BY <key>` を読み取ります。一覧表示された各キーの全行を両方のテーブルで読み取り、まだ適用されていないミューテーションがないか `system.mutations` を確認します。そのうえで次のようにします。

- ライブテーブルが、一覧表示された各キーの変更をライタの意図どおりに保持しており、チェックでカウントされたミューテーションがそこで適用済みである場合は、`DROP TABLE loom.<table>_pre_rebuild SYNC` でコピーを削除します。
- それ以外の場合は、ウィンドウ内で、コピーだけにある各行をライブテーブルにコピーします（`INSERT INTO loom.<table> (<all columns>) SELECT <all columns> FROM
loom.<table>_pre_rebuild FINAL WHERE (<key>) IN (...)`。同じバージョンでは、最後に挿入された行が優先されます）。ライブテーブルが残すべき変更だけが一覧表示されるようになるまで読み取りを再実行し、そのうえでコピーを削除します。`<all columns>` には MATERIALIZED 列と ALIAS 列を含めません。INSERT で拒否されるためです。スワップ後のライブテーブルはコード化されており、空のベクトルや別の幅のベクトルを拒否します。そのため `SELECT` では、リビルドで指定した各列をリビルドが書き込むとおりに書きます。
  `if(length(<column>) = <dim>, CAST(<column> AS Array(BFloat16)), arrayResize(CAST([] AS Array(BFloat16)), <dim>, toBFloat16(0))) AS <column>`。

手動での削除は、そのテーブルだけが持つものの唯一のコピーを消すため、読み取りの後に行い、前には行いません。

<a id="disk-pressure-hygiene"></a>

## ディスク逼迫への対策

Loom ホストでの対策：

- **Docker の JSON ログにサイズ上限を設定する。** 1 行ごとにスタックトレースを出力するコンテナは、ディスクをすぐに使い切ることがあります。`/etc/docker/daemon.json` の `log-opts` を設定します（例：`max-size` 100m × `max-file` 5）。
- **ビルドキャッシュと dangling イメージを削除する。** `docker builder prune` と `docker image prune` を使います。
- **コンテナを prune しない。** `docker container prune` は停止中のコンテナを削除します。[ゼロダウンタイムのペア](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)では、停止中の側の色がロールバック先になります（[バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#the-rollback-colour)）。
- **コードミラーを監視する。** `var/code_repos` には、インデックス対象のリポジトリごとに 1 つのベアクローンが保存されます。リポジトリを廃止すると、他のエントリがそのミラーを使っておらず、ミラーが `LOOM_CODE_REPOS_ROOT` 配下にある場合に限り、そのミラーも削除されます。運用者が別の場所に置いたミラーは削除されません。
- **プローブの出力を小さく保つ。** ディスクが満杯のときは、一時ファイルを書き込むコマンドも失敗します。診断情報は `/dev/shm` などのメモリ上のパスに書き込んでください。

ClickHouse での対策：

```sql
SELECT table, formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts WHERE active AND database = 'loom'
GROUP BY table ORDER BY sum(bytes_on_disk) DESC LIMIT 10;

SELECT name, formatReadableSize(free_space) FROM system.disks;
```

大規模な削除の後は、マージによってバージョンが統合されるのを待つか、コードのベクトルテーブルをコンパクト化するか、テーブルごとの保持期間設定を有効にします。症状から引ける版は[トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#the-host-or-clickhouse-runs-out-of-disk)にあります。

<a id="related"></a>

## 関連ドキュメント

- [モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)：修復が必要かどうかを示すゲージ
- [バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
- [コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
