<!-- i18n: source=docs/operations/backup-and-recovery.md blob=02de0a5c915dc18bf4a94189075292e0ca3d2cc0 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md), which is authoritative.

<a id="backup-and-recovery"></a>

# バックアップとリカバリ

Loom のバックアップは ClickHouse のバックアップです。カタログのいずれかの行がドキュメントバケット内のオブジェクトを指している間は、そのバケットも対象に含めます。Postgres をオーソリティとして使う場合は、Postgres のバックアップも必要です。
ClickHouse には、元ドキュメントのカタログ（`loom.document_blobs`。デフォルトのストアではそのバイト列も）、トークンの署名鍵（`loom.signing_keys`）、管理者のランタイムオーバーライド（`loom.runtime_overrides`）が保存されています。次のものは ClickHouse の外にあります。

- `LOOM_JWT_SECRET`：ルートシークレットです。保存された認証情報と署名鍵を暗号化しているため、これなしで復元したバックアップでは、どちらも開けません。env 設定とともに保管してください。
- `var/code_repos` 配下のコードインデックスの git ミラー：ClickHouse に記録されたリモートのキャッシュです。ミラーのないホストは、再度クローンします。
- `LOOM_DOCUMENT_S3_BUCKET`：`LOOM_DOCUMENT_OBJECT_STORE=s3` の間に書き込まれた元ドキュメントのバイト列を保持します（設定を戻しても、バイト列はそこに残ります）。各オブジェクトの場所は ClickHouse が保持します。

<a id="choosing-a-backup"></a>

## バックアップの選び方

| 方法                                                                    | 対象                                                      | 一貫したスナップショット     | 用途                                                                                                         |
| ----------------------------------------------------------------------- | --------------------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------ |
| ClickHouse Cloud のバックアップ                                         | サービスのすべてのテーブル                                | あり（サービス単位）         | Cloud でのディザスタリカバリ                                                                                 |
| `BACKUP DATABASE loom TO Disk(...)` / `S3(...)`、または `clickhouse-backup` | すべての `loom.*` テーブル                                | あり                         | セルフマネージドの OSS でのディザスタリカバリ                                                                |
| `BACKUP DATABASE loom_telemetry TO ...`                                 | SLI ロールアップ（`loom_telemetry.sli_minute`）           | あり                         | 台帳の再フォールドで復元できる 31 日を超えて SLO 履歴を保持する                                              |
| `dev/backup_loom.py` ポータブルダンプ                                   | すべての Loom テーブル（下記）                            | テーブルごと。テーブル間ではなし | デプロイメント、組織、名前空間の移行（元データはバケット内：[下記](#the-portable-dump)）、コールドコピー |
| `namespace.snapshot`                                                    | 1 つの名前空間のキュレーション済みメモリ、型、権限付与、監査ログの末尾 | 1 回の呼び出し               | API 経由での名前空間のコピーまたは凍結                                                                       |

<a id="the-portable-dump"></a>

## ポータブルダンプ

`dev/backup_loom.py` はテーブルごとに 1 つの `JSONEachRow` ファイルを書き出し、続いて `manifest.json` を書き出します。manifest には元のデータベースと、テーブルごとのファイル、行数、バイトサイズ、SHA-256、`CREATE` 文（失敗した場合は停止の原因となったエラー）が記録されます。終了コード 1 はテーブルの失敗（`complete: false`）を、終了コード 2 は不正な引数、ClickHouse の接続先がないこと、または出力先ディレクトリに既にバックアップがあることを表します。

データベースのすべての MergeTree ファミリーのテーブルを `system.tables` から読み取ってダンプします。マイグレーションランナーの `_` で始まるテーブル、`.inner` テーブル、KeeperMap のリース（データではなくライブの協調状態）、および `loom migrate` が作成するビューは対象外です。manifest の `backup_version` は 3 です。`--org` は `org` 列を持つすべてのテーブルを絞り込み、その列を持たないテーブル（`idempotency`、`signing_keys` など）はスキップします。これらのテーブルにはすべての org の行が入っています。

`dev/restore_loom.py` は、すべてのファイルを manifest と照合して検証し（サイズ、行数、SHA-256）、計画を表示し、記録された `CREATE` 文から対象のデータベースと不足しているテーブルを作成します。その後、各ファイルをそのテーブルのステージングコピーに読み込み、ステージされたパーツをテーブルにアタッチします。アタッチではマテリアライズドビューが発火しないため、復元された行は、バックアップが同じく復元するテーブルに再度コピーされません（そうでなければ `memory_versions` はすべてのバージョンを 2 重に保持します）。また、ビューはライブデータベースへの書き込みに対しては動作し続けます。復元先は、`loom migrate` が準備したデータベース（ビューが存在する）か、スクラッチデータベース（テーブルは記録された DDL から作成される）にしてください。コード化されたベクトルテーブル（記録された `CREATE` に `Quantized(` を含む）を持つバックアップは、何かを書き込む前に拒否されます（`--dry-run` も同様）。これは、復元先のコーデック設定を読み取れない場合、または `enable_quantized_codec` を有効にできない場合です。有効にできる場合でも、`allow_suspicious_codecs` を有効にできないときは、テーブルから `Quantized` の後の汎用コーデックが外されます（[制限付き設定](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)）。挿入は `optimize_on_insert = 0` で実行されるため、`ReplacingMergeTree` キーの重複を含め、ダンプされたすべての行が書き込まれ、テーブルは自身のマージで重複をまとめます。各テーブルは、アタッチの前に `… attaching <table>` を、後に `✓` を表示します。実行が途中で打ち切られた場合、最後の行がそのときのテーブルを示します。`--into-nonempty` で再実行する前に、そのテーブルの件数を確認してください。再実行すると同じ行が再び追加されます。

manifest には、挿入で発火する各マテリアライズドビューのソーステーブルとターゲットテーブルが記録されます。アタッチではビューが発火しないため、復元は、ビューの片方の端だけを含む選択を拒否します。`memory` を `memory_versions`（および `memory_code_link_queue`）なしで復元すると、`memory.history`、`memory.redact`、`namespace.rollback` が復元されたバージョンを認識できなくなります。`code_term_freq` を `code_chunks` なしで復元すると、コードの再取り込み時に再び埋められることになります。`documents` と `document_blobs` を結ぶビューはありませんが、復元は片方だけの選択も拒否します。原本のない documents は、ダウンロードのたびに 410 を返します。documents のない原本は、原本スイープが削除する対象です。両ツールは `documents` を他のどのテーブルよりも先に処理するため、`--into-live` でワーカーが動いていても、スイープは、それを参照する document より先に復元された原本の行を見ることがありません（バケットのオブジェクトについては後述）。両方の端を選択するか、復元先に既に行がある場合は `--allow-view-split` を渡してください。`dev/backup_loom.py --exclude t1,t2` は、パスが再構築するテーブルと、ビューがそれらから埋めるすべてのテーブルを除外します（`code_chunks` を除外すると `code_term_freq` と `code_doc_stats` も除外されます）。`restore_loom.py --exclude` はバックアップのテーブルを除外します。

ドキュメントの原本（`document_blobs`）はバイナリです。ダンプはそれを生のバイト列として書き出し、`output_format_json_validate_utf8 = 0` を自身で設定するため、UTF-8 を検証するプロファイルでも置き換えられることはありません。バイナリの原本は、ダンプ内で元のサイズの約 1.6 倍になります。

`LOOM_DOCUMENT_OBJECT_STORE=s3` で書き込まれた行にはオブジェクトの場所だけが入り、バイト列はバケットにあります。バケットは、そのバケット自身のバージョニングまたはレプリケーションでバックアップしてください。復元は、オブジェクトを参照する ClickHouse バックアップの後に、同じ名前のバケット（各行がその名前を保持しています）へ行うか、両方が戻るまで `LOOM_DOCUMENT_BLOB_GC_ENABLED=false` にして行います。復元されたオブジェクトは古い更新時刻を保持することがあり（復元方法に依存します）、その場合、バケットの走査は、`document_blobs` の行がまだ反映されていないオブジェクトをすべて削除してしまいます。

バージョニングされたバケットは、削除されたオブジェクトをすべて非現行バージョンとして保持し、レプリケーションは既定では削除をレプリケートしません。そのため、消去およびスイープされた原本は、ライフサイクルルール（`NoncurrentVersionExpiration`、およびレプリカ側の有効期限）が削除するまで、バケット（またはそのレプリカ）に残ります。このルールの期間が、消去された原本をまだ復元できる期間であり、これは ClickHouse バックアップの保持期間が行に対して持つ意味と同じです。

ダンプにはこのような行の原本のバイト列が含まれないため、移行できません。別のデプロイメントに復元された行は元のバケットを指したままで、その消去はバケットとプレフィックスの外にあるものとして拒否され、元のデプロイメントのスイープは、元の行がなくなるとオブジェクトを削除します。また、元のバケットとプレフィックスを指すターゲットは、そのカタログが参照していない元のオブジェクトをすべてスイープしてしまいます。移すには、ターゲットにドキュメントを再アップロードするか、元を停止してデプロイメント全体を移動してください。

ステージングコピーはテーブルのエンジンを繰り返しますが、`Replicated` ではないデータベース内のレプリケートエンジンは、同じパラメータのローカル形式になります（コピーがテーブルの Keeper パスを再び要求してしまうためです）。その後、アタッチによってパーツがレプリケートされます。復元が作成するレプリケートテーブルは、記録された Keeper パスを、バックアップ元と同じデータベースでのみ、かつそのパスでレプリケートしているものが他になくなった後でのみ保持します（ライブのレプリカが残っている間は拒否されます）。別のデータベースでは独立したパスになります。サーバーの `default_replica_path` の下に新しい UUID を付けたもの、または Replicated データベースではそのデータベース独自のパスです。その既定値に必要なマクロがサーバーにない場合は拒否されます。
各実行は、ステージングテーブルを `_restore_<run id>_<table>` と名付け、最初に run id を表示し、自分のものだけを削除します。1 つのデータベースへの復元（たとえば 2 つの org）は 1 度に 1 つずつ実行されます。実行は `default._loom_restore_lock_<db>` を要求し（他の実行の要求を最大 5 分待ちます）、ターゲットをもう一度調べ、前の実行が残したものに以下の拒否を適用します。そのため、同じスコープの 2 回目の復元は、最初の行に折り込まれてしまう行をアタッチせずに、拒否されます。この要求は `default` 内のテーブルなので、セルフマネージドクラスタでは同じサーバー経由の実行の順序づけだけを行います。すべての復元を 1 台に向けてください。クラッシュした実行は、ステージングテーブルと要求を残します。その実行のプロセスが残っていなければ、`restore_loom.py --database <db> --drop-staging <run id>` で削除できます。
挿入のステップより前には何も書き込まれません。`--dry-run` は計画の後で停止します。次のものは拒否されます。

- `--into-live` なしでのライブデータベース（`loom`、またはサーバー設定が指定するデータベース）
- `--into-nonempty` なしでの、バックアップのスコープ内で既に行を持つテーブル。挿入は追記なので、通常の MergeTree テーブル（`audit_log`、`agent_events`）はすべての行を 2 重に保持することになります。

両ツールはサーバーと同様に ClickHouse の接続先を解決し（環境変数または `.env` の `LOOM_CH_*`。それぞれ `--host`、`--port`、`--user`、`--database`、`--secure` で上書きできます）、ホストが指定されていない場合は実行を拒否し、パスワードは `LOOM_CH_PASSWORD` からのみ読み取ります。

```bash
python dev/backup_loom.py --out-dir ./backups/$(date +%F) [--org acme] [--tables memory,namespaces]
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --dry-run
python dev/restore_loom.py --in-dir ./backups/<date> --database loom_restore --skip-idempotency
python dev/restore_loom.py --in-dir ./backups/<date> --into-live \
  --tables memory,memory_versions,memory_code_link_queue   # one end of a view needs the other
./scripts/ops/backup_drill.sh [--full-restore]   # round-trip check into a scratch database
```

`scripts/ops/backup_drill.sh` はライブデータベースを一時ディレクトリにダンプし、manifest を行数のスナップショットと照合します。`--full-restore` を付けるとスクラッチデータベース `<db>_drill` に復元し、すべてのテーブルの行数を比較し、終了時にスクラッチデータベースを削除します。同じキーの行をまとめるテーブル（`ReplacingMergeTree`、`AggregatingMergeTree` など）は、アタッチの直後にスクラッチデータベース内でマージされることがあります。そこでの行数の減少は、マージが実行された後なら警告、そうでなければ失敗です。このスクリプトはライブデータベースを読み取るだけです。ドリルは定期的に実行してください。

ダンプはテーブルごとのもので、テーブル間のスナップショットではありません。クラスタ間でデータを移すために使い、ディザスタリカバリには使わないでください。

<a id="verifying-the-audit-log-after-a-restore"></a>

## リストア後の監査ログの検証

`python dev/verify_audit_chain.py [--org X] [--ts-since YYYY-MM-DD] [--json]` は、すべての `audit_log` 行のコンテンツハッシュを再計算します。不一致があれば終了コード 1 で終了し、検証を実行できなかった場合（不正な引数、対象なし、クエリの失敗）は 2、それ以外は 0 で終了します（行が 0 件の場合も成功として扱います）。不一致のレポートでは、行をパーティション、タイムスタンプ、リクエスト ID で示し、アクター、サブジェクト、名前空間は示しません。`audit_verifier` ワーカーは、同じ行単位のチェックをスケジュールに従って実行します。このチェックは行単位の改ざん検出であり、行にわたるハッシュチェーンではありません。1 時間ごとのチェックポイントは別途チェーンされています（[監査](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)）。

<a id="undoing-changes-inside-a-namespace"></a>

## 名前空間内の変更の取り消し

これらの操作は、誤った書き込み、誤動作したエージェント、誤ったインポートを、ClickHouse のリストアなしで取り消します。いずれも 1 つの名前空間を対象とします。`namespace.snapshot`、`namespace.restore`、`namespace.rollback` は MCP の `all` 一覧に含まれません。名前を指定するか HTTP 経由で呼び出すか、[ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)の Health → Repairs タブを使用してください。このタブでは、名前空間の管理者がスナップショットのダウンロード、リストア、ロールバックを行えます。

| 操作                 | ロール                                                 | 内容                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.restore`     | `WRITER`                                               | トゥームストーン化された 1 つのキーを元に戻します                                                                                                                                                                                                                                                                                                                         |
| `namespace.snapshot` | `ADMIN`                                                | 名前空間のメモリ、型、グラント、監査ログの末尾（`include_audit_tail_n`、デフォルトは 1000）を JSON ペイロードとして固定します。`include_embeddings=false`（デフォルト）の場合、ターゲット側は自身のモデルで再埋め込みを行います                                                                                                                                             |
| `namespace.restore`  | 新規ターゲットでは `VIEWER`、既存ターゲットでは `ADMIN` | スナップショットのペイロードを `target_namespace` に書き込みます（任意で `rebind_agent` を指定できます）。`snapshot_id` により冪等です。行は `version`、`content_sha256`、およびアテステーションのカラムを保持するため、`verified_by` は残ります。カラムに格納できない値（UUID ではないメモリ ID など）がペイロードにある場合は 422 で拒否されます。コレクションは拒否されます。そのスナップショットは通常の名前空間にリストアします |
| `namespace.rollback` | `ADMIN`                                                | すべてのメモリヘッド（または `memory_keys`）を `as_of` 時点のバージョンに戻します。古いバージョンを新しいヘッドとして復元し、それ以降に作成されたキーはトゥームストーン化します。デフォルトはドライランです（適用するには `dry_run=false`）。1 回の呼び出しで処理するのは最大 `max_keys` 件です（再度呼び出すべきかどうかは `truncated` が示します）                       |

Postgres オーソリティの下では、消去によってターゲットから削除されたメモリは、その[削除エビデンス](../architecture/storage-authority.md)が保持されている間、リストアの対象から除外され、`counts.erased_skipped` に計上されます。エビデンスは、消去によってそのメモリが削除された名前空間のみを対象とします。そのため、消去前のスナップショットを別の場所にリストアすると、消去されたメモリが復活します。そのようなリストアの後は、同じ消去（そのスコープ）を再実行してください。

`namespace.rollback` と `memory.history` は `loom.memory_versions` を読み取ります。これは `loom.memory` へのすべての挿入を記録する追記専用のミラーです（マージでは、置き換えられたバージョンが `loom.memory` 自体から削除されます）。ロールバックの動作は次のとおりです。

- 名前空間の履歴より古い、または未来の `as_of` は拒否します。
- ハード削除されたキーはスキップします（`skipped_no_head`）。
- 復元されたヘッドの再埋め込みは、埋め込みワーカーに任せます。

```bash
AS_OF=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ)   # GNU date; the instant before the bad writes
curl -s -XPOST http://localhost:7777/v1/namespace.rollback -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","as_of":"'"$AS_OF"'"}' | jq '{dry_run, reinstate, remove, truncated}'
```

<a id="under-the-postgres-authority"></a>

## Postgres を権威とする場合

`LOOM_STORAGE_AUTHORITY=postgres` では、ライブメモリ、そのリビジョンジャーナル、ナレッジヘッド、取り込みジョブ、データセットカタログが Postgres にあり（[ストレージ権威](../architecture/storage-authority.md)）、デプロイメントでバックアップすべきストアは Postgres と ClickHouse の 2 つになります。

| ストア | 単独で保持するもの | RPO | バックアップ手段 |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Postgres | すべてのメモリヘッドとリビジョン、ナレッジヘッド、取り込みジョブ、データセットカタログ、冪等性の応答 | 同期スタンバイへのフェイルオーバーをまたいでも、確認応答済みの書き込みについては 0。ポイントインタイムリストアでは、そのポイント以降の内容が失われます | HA マネージャーまたはサービスのベースバックアップと WAL アーカイブ |
| ClickHouse | 検索ドキュメントとベクトル、ナレッジドキュメントおよび長いメモリ値のチャンク、Postgres が保持する範囲を超えた履歴、アナリティクス、トレース、監査行 | ClickHouse のみが保持するものについては、最後のバックアップからの経過時間。Postgres が保持するメモリリビジョンとナレッジドキュメントは再公開されますが、そのチャンクとデータセットの行は再公開されません。`loom reconcile` は再チャンク化すべき各ナレッジソースを示し、20 を超える場合はその名前空間（`loom rechunk`）を示し、あわせて再取り込みすべき各ソースを示します。長いメモリ値は、再設定されるまで抜粋で検索可能です | 上記の方法 |

リストアの復旧時間は、ストア自体のリストア時間に `loom reconcile` の実行時間を加えたものです。この実行は、リストアされたストアが失った、もう一方のストアの記録を再実行します。

<a id="restoring-one-store"></a>

### 片方のストアのリストア

`LOOM_PG_PAYLOAD_CLEAR_ENABLED` を有効にした状態で ClickHouse をリストアする前に、ペイロードのクリアを停止し、[Postgres ジャーナルと ClickHouse のリストア](#the-postgres-journal-and-a-clickhouse-restore)の説明に従ってバックアップを選んでください。クリア処理が一度でも実行された場合は、手順 3 の `loom reconcile` を `010_revision_delivered_at.sql` を含むビルドで実行し、手順 4 でもそのビルドでデプロイメントを起動してください（ローリングアップグレードの[ルール 43](../architecture/rolling-upgrades.md#the-rules)）。そのうえで次を行います。

1. デプロイメントのすべてのサーバーとワーカーを停止します。
2. バックアップからストアをリストアし、バックアップ自体のメタデータからバックアップ時刻を控えます（ClickHouse では `BACKUP` の開始時刻、Postgres ではリカバリターゲット）。
3. `loom reconcile --restored clickhouse --point <time>`（または `--restored postgres`）を実行します。再実行される内容が出力されます。続いて同じコマンドを `--apply` を付けて実行します。
4. デプロイメントを起動します。
5. ClickHouse のリストア後は、reconcile が出力した各 `loom rechunk` を実行し、「to ingest again」の下に挙げられた各ソースをアップロードして再取り込みします。ナレッジドキュメントのチャンクとデータセットの行は ClickHouse にのみ存在します。

[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)の Postgres には、そのセルがテナンシー行を持たない組織の行を拒否する組織トリガーと、自身を示さないセッションからのテナンシー行を拒否する `loom.tenancy` ガードがあります。セルへの論理リストアはこのようなセッションに当たります。セルのすべてのプロセスを停止した状態で、マーカーを付けた 1 つのコマンド `PGOPTIONS='-c loom.tenancy_checked=on' pg_restore ...` として実行し、その後、他のポッドより先に `LOOM_BOOT_MIGRATE` を有効にしたポッドを 1 つ起動してください。既存スキーマへのデータのみの `pg_restore --disable-triggers` は、最後にすべてのトリガーをオリジントリガーとして有効にするため、`session_replication_role = replica` の下で `loom.cell_first` の 2 つのトリガーが無効のままになります。そのポッドの起動時にそれらが `ALWAYS` に戻され、戻るまでは `loom doctor` がそれらを指摘します。

Helm チャート（[Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)）で、リリース名を `loom` とした場合：

1. 停止します：`kubectl scale deploy/loom --replicas=0`。`workers.enabled` の場合は `deploy/loom-workers` も停止します。アナリティクスエクスポートが有効なら一時停止します：`kubectl patch cronjob loom-analytics-export -p '{"spec":{"suspend":true}}'`。
2. マネージドサービスは新しいサービスにリストアされます。`helm upgrade ... --set replicaCount=0 --set workers.replicaCount=0 --set autoscaling.enabled=false`（および新しい `clickhouse.host` または `postgres.host`）でリリースをそのサービスに向け、ポッドが起動しないようにします。ポイントは、サービスがバックアップとして示す開始時刻、または Postgres のリカバリターゲットです。
3. 下記の Job はリリースのイメージを実行します。クリア処理が一度実行されていて、リリースが `010_revision_delivered_at.sql` を含まないビルドの場合は、まず `helm upgrade ... --set image.tag=<a build that carries it>`（または `image.digest`。設定されていればこちらが優先されます）を、手順 2 の新しいホストと 3 つの `--set` オーバーライドとともに実行してポッドが起動しないようにし、手順 4 でもそのイメージを維持します。reconcile は、Web ポッド自身の spec から作成した Job で実行します。これによりリリースの環境、Secret、Postgres CA が引き継がれ、固有のコンポーネントラベルを持ち（NetworkPolicy はこれを選択しますが、Service は選択しません）、プローブはありません。

   ```bash
   kubectl get deploy/loom -o json | jq '{apiVersion: "batch/v1", kind: "Job",
     metadata: {name: "loom-reconcile"}, spec: {backoffLimit: 0, template: {
     metadata: {labels: (.spec.selector.matchLabels
       | .["app.kubernetes.io/component"] = "reconcile")},
     spec: (.spec.template.spec | .restartPolicy = "Never" | .containers |= [.[0]
       | del(.livenessProbe, .readinessProbe, .startupProbe)
       | .command = ["loom", "reconcile", "--restored", "clickhouse", "--point", "<time>"]])}}}' \
     | kubectl apply -f -
   kubectl logs -f --pod-running-timeout=10m job/loom-reconcile
   kubectl wait --for=condition=complete --timeout=10s job/loom-reconcile
   ```

   Postgres のリストア後は `"postgres"` を使います。Job がまだ実行中（`kubectl get job loom-reconcile -o jsonpath='{.status.active}'` が `1`、ログストリームが切れると `logs -f` が早く終わります）の間に `wait` が失敗した場合は、まだ実行中なので、再びログを追ってください。失敗した Job（`.status.failed` が `1`）は非ゼロで終了しています。出力を読み、Job を削除して、再実行してください。ドライランが問題なければ、`"--apply"` を末尾に加えて実行します。

4. 再び起動します：リリースの values ファイルで `helm upgrade` を実行します。手順 2 の新しいホストと、手順 3 で設定した場合は `image.tag` または `image.digest` を引き継ぎ、手順 2 の 3 つの `--set` オーバーライドは付けません。そしてアナリティクスエクスポートを再開します。reconcile が出力した各 `loom rechunk` を `kubectl exec deploy/loom -- loom rechunk ...` で実行し、「to ingest again」の下に挙げられた各ソースをアップロードして再取り込みします。

`loom reconcile --apply` は次を行います。

- もう一方のストアがポイント以降に完了と記録している各消去を再実行します（台帳は秒単位で保持されるため、ポイントの秒の先頭から）。カウント（census）は行いません（もう一方のストアには、消去後にユーザーが書き込んだ内容が残っているため）。また、リストアされたコピーがポイント以前に実行済みと示すステップは除外します。ClickHouse の場合、除外されるのはポイント以前に書かれた最後の ClickHouse チェックポイントのステップから、census が再オープンしたものを除いたものです（`BACKUP` は開始時からテーブルを順にコピーするため、すべてのコピー済みテーブルに含まれるのはそれらの削除だけです。マージで消えた行がある場合、すべてのステップが実行対象になります）。Postgres の場合は、台帳行が、各ステップがコミットされたトランザクション内で記録したステップです（前のビルドのプロセスが実行したステップは記録されず、再実行されます）。これらを再実行すると、その後にユーザーが書き込んだ内容を削除してしまうためです。リストアされた台帳行は、すべてのステップが実行されると completed になります。それまでは、ClickHouse のリストア後は「replay after a restore pending」とともに `failed` と読まれ、この行はリカバリワーカーも `loom erasure-re-verify --resume` も実行しません。リストアされた行がポイント以前に completed だったもの、または以前の実行が完了させたものは再実行されません。リプレイ応答のステップは、あらゆる消去と同様に、組織のリプレイキャッシュ全体を両方のストアで削除します。終了状態のない消去はリカバリワーカーに任され、そのチェックポイントは、リストアで取り消されたステップを再オープンして書き直されます。ClickHouse のリストア後に、リストアされた行が completed または abandoned を示す消去は、代わりに Postgres の台帳行にその旨が記録されます。再開された saga は通常どおり census を含めて実行され、その範囲でユーザーがその後に書き込んだ内容を数えます。ポイント以降に完了したのに Postgres の台帳行が完了を取りこぼした消去も同様です。この端のケースは、リカバリパスが、要求の 10 分後、または完了後のどちらか遅い方の最初のパスで解消します。abandoned の消去は決して実行されません（`loom erasure-re-verify` が扱います）。
- ClickHouse のリストア後：まず、ポイント以降にコミットされたすべての Postgres リビジョンとナレッジヘッド（ライターのリースと挿入の上限、つまりポイント以前に採番された書き込みがポイント以降にコミットされうる余裕分を差し引いたもの）と、ClickHouse がより古いバージョンで保持しているすべての Postgres ヘッドを、まだ期限に達していない配信として保留します。次に消去を実行し、その Postgres のステップはスキップします（保持されており、再実行すると消去後にユーザーが書き込んだ内容を削除してしまうため）。保留した配信を公開します。その後、Postgres がポイント以降に記録しているすべてのリダクションを再実行し、ClickHouse がリストアした内容と、それを引用するキャッシュ済みの応答や先例を消去します。
- Postgres のリストア後：リストアされたコピーに対して、消去の Postgres ステップとリプレイ応答のステップを実行し、ClickHouse がポイント以降に記録しているすべてのリダクションを実行します。リダクション済みのバージョンがコピー上で再びライブヘッドになっている場合（それを置き換えた書き込みが失われたため）は、その場で消去されます。ポイント以降に書き込まれた内容は、ポイントインタイムリストアの通例どおり失われます。ClickHouse にはそのコピーがまだ残っていますが、Postgres に対応するヘッドがなくなるため、検索はそれを返しません。

データセットの行は ClickHouse にのみ存在し、取り込みだけがそれを構築します。そのため ClickHouse のリストア後は、ポイント以降に行が書かれたデータセット（公開、または列の型を変更した承認）は、Postgres にあるカタログ行がそれらの行を指しているにもかかわらず、行が欠けた状態で読み取られます。`loom reconcile` は、ドライランでも、ポイント以降にカタログ行が書かれた各ソースを、アップロードして再取り込みすべきものとして示します。これには、残存していた行の上に承認またはメタデータ編集が書き込んだ行も含まれます。

ナレッジ取り込みジョブには専用のステップは不要です。ジョブの読み取りは Postgres の行と ClickHouse の行のうち新しい方を取り、Postgres が保持するジョブは、キューが提示したときに再び射影されます。ClickHouse のリストアポイント以降に終了したジョブは再提示されないため、ClickHouse を読む `knowledge.inspect` は、その行が期限切れになるまでそれを一覧表示しません。

完了しなかったリプレイは、その ClickHouse 行を「replay after a restore pending」とともに `failed` のままにします。デプロイメントを起動する前、および reconcile より前のビルドへロールバックする前に（そのビルドのリカバリワーカーは census 付きでその行を再開してしまいます）、`loom reconcile` を終了コード 0 になるまで再実行してください。

何かを書き込む前に、ポイントが現在より後の場合、ワーカーが workers-lease の 2 期間（`LOOM_SUPERVISOR_LEASE_TTL_S`、デフォルトでは 1 分）以内にパスを報告している場合、ポイントが `LOOM_ERASURE_LEDGER_DAYS`（ClickHouse の台帳読み取りは 89 日分を保持）より古い場合、および `--restored both` の場合は、実行が拒否されます。両方のリストアポイント以降に行われた消去とリダクションはどちらのストアにも存在せず、監査エクスポートから再発行する必要があります。両方の台帳は消去を要求された時刻で保持するため、消去の最長実行時間の範囲内にあるポイントでは、その端より前に要求されてポイント以降に完了した消去を取りこぼすことがあります。バックアップは、台帳の日数よりその分だけ新しいものを保ってください。書き込みの前にコードリポジトリレジストリを読み取り、その読み取りが失敗した場合は拒否します。実行するすべてのステップは冪等であるため、途中で打ち切られた reconcile や終了コード 1 の reconcile は、デプロイメントを起動する前に最初から再実行してください。保留された配信は、実行が完了するまで `GET /v1/health?include=postgres` で parked として一覧表示されます。

<a id="what-is-and-is-not-recoverable"></a>

## 復旧できるものとできないもの

| 状況                                                       | 復旧可否       | 方法                                                                                                                                                                                     |
| ---------------------------------------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.delete` または `memory.clear` で削除マーク（tombstone）が付いたメモリ | 可能           | `memory.restore`、または削除前の時点への `namespace.rollback`                                                                                                                            |
| 後のバージョンで上書きされたメモリ                         | 可能           | `memory.history` でバージョンを確認し、`namespace.rollback` で 1 つを復元する                                                                                                            |
| `purge: true` を指定した消去、またはパージされたベンチマーク名前空間 | 不可           | purge は完全に削除するため、復元期間はありません。それより前に取得したバックアップのみが頼りです                                                                                         |
| 削除マークを付けた消去（`purge` なし）                     | 確定として扱う | saga の census により、対象範囲がすべての読み取り経路から消えていることが確認済みです。元に戻すと削除要求を取り消すことになるため、消去そのものが誤りだった場合にのみバックアップから復元します |
| イメージをロールバックした後の、新しいビルドのスキーマ     | 不要           | マイグレーションは追加のみのため、以前のビルドでも新しいスキーマを読み取れます（[アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#rollback)）     |
| 以前のスキーマ                                             | バックアップから | ダウンマイグレーションはありません。アップグレード前に取得したバックアップから復元してください                                                                                         |
| 単一ノードの ClickHouse OSS サーバーのディスク             | バックアップから | レプリケーションもポイントインタイムリカバリもなく、ノードの耐久性は自身のディスクに限られます                                                                                           |
| コードインデックスのテーブル                               | 再構築可能     | `loom code backfill` が git ミラーから再生成します（[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)）        |
| オプトインの保持 TTL によって削除された行                  | 不可           | TTL による削除は確定です。それより前に取得したバックアップのみが頼りです                                                                                                                 |

自己管理の単一 OSS ノードでは、`BACKUP DATABASE`（または `clickhouse-backup`）をスケジュールして、ホスト外のストレージに保存してください。ClickHouse Cloud はサービス自体をレプリケーションしてバックアップするため、そこでのポータブルダンプはデータの移動用であり、耐久性のためのものではありません。

<a id="the-postgres-journal-and-a-clickhouse-restore"></a>

## Postgres ジャーナルと ClickHouse のリストア

Postgres をオーソリティとする構成では、`loom.memory_revisions` がすべてのリビジョンを保持します。ClickHouse をリストアすると、バックアップ以降にコミットされたバージョンはジャーナルにはありますが、リストアされた `memory_versions` にはありません。そのコンテンツが残るのは、ペイロード（`row`）がまだジャーナルにある間だけです。`LOOM_PG_PAYLOAD_CLEAR_ENABLED` を有効にすると、`pg_payload_clear` ワーカーが、利用可能なすべてのバックアップが保持しているバージョンのペイロードをクリアします（[ペイロードのクリア](../architecture/storage-authority.md#payload-clearing)）。ClickHouse をバックアップしていない場合は無効のままにしてください。その場合、ジャーナルはすべてのペイロードを保持し続けます。

**ホライズン。** `LOOM_CH_BACKUP_INTERVAL_MINUTES` には、ClickHouse のバックアップを開始する間隔を設定します。パスは、ClickHouse への配信からホライズンより長く経過したバージョンだけをクリアします。ホライズンは `LOOM_PG_PAYLOAD_RESTORE_HORIZON_MINUTES` で指定します。

- 0（デフォルト）の場合、ホライズンは間隔の 2 倍に 60 を足した値になります。これにより、最新のバックアップが失敗していても、その前のバックアップが予定どおりに開始されていれば、リストアで 1 つ前のバックアップに戻れます。
- 間隔に 60 を足した値を下回ることはなく、挿入の上限に 60 を足した値も下回りません。これより小さい値を設定した場合は、2 つのうち大きい方に引き上げられます。
- 間隔に 60 を足した値以上、間隔の 2 倍に 60 を足した値未満の場合は、最新のバックアップが予定どおりに開始されていたときに限り、そのバックアップだけをカバーします。

どちらの設定も LIVE です。変更は、プロセスがオーバーライドを再読み込みした後（`LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS` ごと）の最初のパスで反映されます。すでに実行中のパスは、開始時のホライズンのまま動作します。

**マージンの理由。** 60 分は、バックアップの開始時刻からはわからない次の要素を吸収します。

- バックアップが読み取るレプリカが、挿入を受け付けたレプリカより遅れていること
- Postgres とバックアップホストの間のクロックスキュー

クリアされたバージョンはすべて、クリアが停止する時点よりホライズンを超えて前に配信されています。したがって、クリアが停止した時点より前に、ホライズンから 60 分を引いた時間未満の前に開始されたバックアップ（その時点でのバックアップの経過時間が H から 60 分を引いた時間未満であるバックアップ）は、遅延とスキューの合計が 60 分未満である限り、クリアされたすべてのバージョンを保持しています。開始が遅れたバックアップは、その開始時刻に現れます。これを以下のステップ 4 で確認します。最小のホライズンでは、最新のバックアップは予定どおりに開始されていれば条件を満たしますが、遅れていた場合は満たさないことがあります。`BACKUP ... ON CLUSTER` はすべてのシャードを一度に処理します。ノードごとのツールを使う場合は、バックアップの開始時刻を最も早いノードの開始時刻として数えてください。

**リストアの前に、クリアを停止する。** バックアップを選ぶ前に、ClickHouse にまだバックアップにはないバージョンが残っているうちに、次を行います。

1. デプロイメントの env で `LOOM_PG_PAYLOAD_CLEAR_ENABLED=false` を設定します。この設定は LIVE ではなく、`loom.runtime_overrides` をリストアしても再び有効になることはありません。
2. その env で、すべての Loom プロセスを再起動します。再び起動される可能性のある停止中のインスタンスは削除するか、新しい env で再作成します。
3. 実行中または起動可能なすべての Loom プロセスで、このスイッチが明示的に `false` になっていることをオーケストレーターで確認します。未設定のままだと、スイッチの値は dotenv ファイル（`LOOM_ENV_FILE`。デフォルトは作業ディレクトリの `.env`）、または環境変数もしくはその dotenv が指す設定ファイル（`LOOM_SETTINGS_FILE`）から決まり、どちらもスイッチを有効にしうります。
4. バックアップを選ぶのは、その後です。上の手順 3 の確認（クリアが停止した時刻）より、ホライズンから 60 分を引いた時間未満の前に開始されたバックアップを選びます。ホライズンを下げていた場合は、直近のホライズンの期間内に有効だった最小のホライズンを使います。デフォルトのホライズンでは、予定どおりに開始されていれば、最新のバックアップかその 1 つ前のバックアップが該当します。それより古いバックアップには、ペイロードがすでにクリアされたバージョンが含まれていない可能性があり、その場合そのバージョンのコンテンツは失われます。
5. スイッチを再び有効にするのは、[1 つのストアのリストア](#restoring-one-store)のステップ 4 でデプロイメントが再び起動するときで、それより前ではありません。その時点では、`loom reconcile --apply` が、そのポイント以降にコミットされたすべてのリビジョンについて配信を再び未完了にしており、リビジョンは配信が未完了の間はペイロードを保持します。まず、`LOOM_CH_BACKUP_INTERVAL_MINUTES` と `LOOM_PG_PAYLOAD_RESTORE_HORIZON_MINUTES` をバックアップのスケジュールと照らして確認してください。リストアによって、これらのオーバーライドはバックアップ時点のものに戻っているため、それ以降に変更していた場合は再適用します。リストア後も ClickHouse にないバージョンはペイロードを保持し、そのバージョンに到達した各パスが `missing_in_clickhouse` に数えるため、この値は 0 を超えたままになることがあります。[1 つのストアのリストア](#restoring-one-store)のステップ 4 で、`010_revision_delivered_at.sql` を含むビルドを、それを含まないビルドを提供していたデプロイメントで起動する場合、そのビルドが単独で提供を始めてから 1 回分のクリアホライズンが経過するまで、スイッチは無効のままにします（[ルール 43](../architecture/rolling-upgrades.md#the-rules)）。

[ゼロダウンタイムペア](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)では、次のようにします。

1. `.env` を編集します。
2. `origin/main` より遅れていないチェックアウトから `./redeploy.sh --force` を実行します。
3. 再デプロイが成功したか失敗したかにかかわらず、`docker inspect -f '{{.Config.Env}}'` に `LOOM_PG_PAYLOAD_CLEAR_ENABLED=false` が表示されない `loom-*-workers` コンテナーを、実行中か終了済みかを問わずすべて停止し、`docker rm` で削除します。web コンテナーはワーカーを実行しません。前のカラーのワーカーコンテナーがなくなれば、`--rollback` は現在のワーカーを維持します。
4. これによってワーカーコンテナーが 1 つも残らない場合は、原因を修正して、ステップ 2 をもう一度実行します。

これらの手順を省略してリストアすると、バックアップからリストアまでの間にコミットされたバージョンのコンテンツが失われることがあります。そのメタデータはジャーナルに残ります。

<a id="the-rollback-colour"></a>

## ロールバック用のカラー

[ゼロダウンタイムのペア](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)では、`redeploy.sh` は以前のカラーのコンテナ（`loom-<colour>`、`loom-<colour>-workers`）を削除せずに停止し、以前のイメージに `loom:prev` のタグを付けます。`./redeploy.sh --rollback` はそれらを起動し、ヘルスチェックを行ってから、プロキシを元に戻します。以前のカラーのコンテナが存在しない場合、ロールバックは拒否されます（"the previous build is gone"）。その場合は、正常に動作することが確認できているコミットを再デプロイしてください。

> **警告：** ペアを実行しているホストでは、`docker container prune` や `docker system prune` を決して実行しないでください。停止中のロールバック用カラーが削除されます。ディスク容量を空けるには `docker builder prune` と `docker image prune` を使用してください。また、使い捨てのコンテナに `loom-blue` や `loom-green` という名前を付けないでください。名前を指定してそのコンテナを削除すると、カラーが削除されます。

ロールバックの後も、ロールフォワードされていたカラーの web コンテナは、停止するまで動き続けます（ロールバック時に表示されるとおり、`docker stop -t 75 loom-<colour>` で停止します）。そのカラーの workers コンテナは切り替えの前にすでに停止されているため、workers のリースはロールバック先のカラーが保持します。ロールバック先のカラーに workers コンテナがない場合に限り、ロールフォワードされていたカラーが workers を動かし続けます。

<a id="moving-a-deployment"></a>

## デプロイメントの移行

同じ ClickHouse（`LOOM_STORAGE_AUTHORITY=postgres` の場合は Postgres も）を指す新しい Loom プロセス、または移行・復元した ClickHouse を指す新しい Loom プロセスは、すべての機能を取り戻します。Postgres をオーソリティとする場合は、どちらのストアをコピーする前にもソースを停止してください。別の時点で取得したバックアップから片方のストアだけを復元する場合は、[片方のストアの復元](#restoring-one-store)にあたります。次のものを持ち越します。

- ClickHouse：同じサービス、Keeper の状態を含むコピー、または `loom migrate` で準備した ClickHouse に復元した `dev/backup_loom.py` のダンプ。
- Postgres（`LOOM_STORAGE_AUTHORITY=postgres` の場合）：同じサービス、またはそのコピー。
- 環境設定（`.env` またはコンテナの環境変数）。
- `LOOM_JWT_SECRET`。これがないとどのトークンも検証できず、保存済みの認証情報と署名鍵も開けません。
- カタログの行がバケット内のオブジェクトを指している間は、同じ名前とプレフィックスの `LOOM_DOCUMENT_S3_BUCKET`。新しいホストから到達できる同じバケット、または別のエンドポイント上の同名のバケットを使えます（`LOOM_DOCUMENT_S3_ENDPOINT_URL` は行の場所の一部ではありません）。各行はバケット名を持っており、Loom にはそれを書き換えるツールがありません。別の名前でコピーすると、ダウンロードも削除もすべて古いバケットを指したままになります。

それ以外はすべて ClickHouse から復元されます。ClickHouse に保存されたドキュメントの原本、署名鍵、ランタイムのオーバーライド、スーパーバイザーのリース、失効情報、サインイン試行回数、グラフ走査の無効化マークです。Git ミラーは、次回の取り込みパス、または新しいホストでの最初の履歴読み取りの際に、記録されたリモートから再クローンされます。

限定的な例外：

- イベントストリーム（予算違反）は `SIGTERM` で排出されます。`SIGKILL` や OOM では、このテレメトリが最大で 1 回のフラッシュ間隔分失われますが、監査行やメモリの行が失われることはありません。
- プロバイダーの再構成は、停止したプロセスについて、同じホスト上のプロセスがそのプロセスの消滅を確認するまで、Kubernetes ではワーカーのリーダーがその Pod の消滅を確認するまで待機します。それ以外の場合、ホストごと失われたもの（新しいホストへの移行）は、`loom participant retire --participant <id> --yes` で手動で退役させます（[プロバイダーの再構成](../architecture/provider-reconfiguration.md)）。
- レート制限バケットの充填状態はホストごとです。数秒で補充されるため、新しいホストは満杯のバケットで始まります。

<a id="related"></a>

## 関連ドキュメント

- [アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)：ロールバック手順と追加のみのマイグレーションのルール
- [メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#erasure)：消去サーガとその復旧
- [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)：`memory.history` が返す内容
- [障害と復旧](../architecture/failure-and-recovery.md)：コンポーネントに障害が発生したときのシステムの動作
