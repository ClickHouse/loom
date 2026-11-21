<!-- i18n: source=docs/development/migrations.md blob=bbea754e8750b1abe4dfb51af0636ead2246778b model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md), which is authoritative.

<a id="migrations"></a>

# マイグレーション

このページには、マイグレーションを書くためのルールをまとめています。デプロイメントがマイグレーションを適用する方法、およびアップグレードとロールバックの手順は、[アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run)を参照してください。[`schemas/README.md`](https://github.com/ClickHouse/loom/blob/main/schemas/README.md)は、`schemas/` ディレクトリの構成を示し、オペレーター向けのツールを一覧にしています。

<a id="the-journal-and-the-ledger"></a>

## ジャーナルと台帳

- スキーマは `schemas/migrations/` にある番号付き SQL ファイルのジャーナルで、`server/core/schema/migrate.py` が番号プレフィックスの順に適用します。`000_migrations.sql` が台帳を作成し、`016_baseline.sql` が開始時点のスキーマで、以降のファイルはすべて差分です。
- 適用された各ファイルは、その内容の SHA-256 とともに `loom._migrations` に記録されます。再実行では新しいファイルだけが適用されます。ファイルが変更された場合は**チェックサムのずれ**、リポジトリから失われたファイルは**台帳のみの行**となり、`loom migrate --dry-run --strict`（CI のステップおよびデプロイ前のプリフライト）はどちらの場合も失敗します。テキストを変えずにファイル名だけを変更した場合は、台帳のみの行にはなりません。SHA-256 でファイルを特定し、再実行せずに新しい名前で記録します。
- すべてのファイルは `loom.<table>` と `loom_telemetry.<table>` を指定します。ランナーは各ステートメントを送信する際にこれらを `LOOM_CH_DATABASE` と `LOOM_CH_TELEMETRY_DATABASE` に置き換える（`server/core/ch_names.py`）ため、チェックサムは書かれたままのファイルに対して計算されます。データベースは `currentDatabase()` またはバインドされた名前で値として比較し、リテラルの `'loom'` は使いません（`tests/unit/test_ch_names.py`）。名前が `_` で始まるテーブルのキーや TTL を変更する、またはそのテーブルの名前変更や交換を行うファイルは、[ストレージレイアウト](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md)にあるそのテーブルの行と、`tests/unit/test_storage_layout_rows.py` の再構成ガードも更新します。

<a id="numbering"></a>

## 採番

ファイル名は `NNN_description.sql` の形式にします。ランナーは名前ではなく整数のプレフィックスで順序を決めるため（`server/core/schema/journal.py`、`migration_order`）、`1030_x.sql` は `220_y.sql` の後に実行されます。次の空き番号を使い、`main` とオープン中のプルリクエストを確認したうえで、その番号を維持してください。同じ番号のファイルが 2 つあっても両方適用されますが、適用順はサフィックスの辞書順になり、これは誰も意図していない順序です。また、リベースでどちらかの名前が変わっても、再適用は行われません。`tests/unit/test_migration_numeric_order.py` は、番号が重複していると失敗します。

<a id="rules-for-writing-a-migration"></a>

## マイグレーションを書くときのルール

- **マージ済みのマイグレーションを編集または削除しないこと。** デプロイメントの台帳にはそのハッシュが記録されています。修正は新しいファイルで行います。
- **追加のみ。** テーブル、カラム、インデックス、プロジェクション、ビューを追加できます。`server/`、`scripts/`、`bench/`、`dev/` 配下のコードが名前を参照していないスキップインデックスやプロジェクションは削除できます（古いビルドが失うのは最適化だけで、カラムは失いません）。コーデック、デフォルト値、テーブル設定の変更、型の拡張、enum の拡張も可能です。テーブルやカラムの削除、名前変更、テーブルの入れ替えは決して行わないでください。これは `tests/unit/test_migrations_additive.py` が強制します。ビューとテーブルについては、挿入時のマテリアライズドビューに対する `DROP VIEW IF EXISTS` と、置き換え済みのテーブルに対する `DROP TABLE IF EXISTS` だけを許可します。いずれも、`server/`、`scripts/`、`bench/`、`dev/` 配下のどのファイルもその名前を参照していない場合に限ります。稼働中のビルドも、運用ツール（本番サービスに対して実行されます）も、それを読まないためです。型の縮小は、正規表現では拡張と区別できないため、レビューで判断します。
- **カラムの廃止は 2 回のリリースに分けること。** まず、そのカラムを読み書きしないコードを含むリリースを出し、どの稼働中のビルドもその名前を参照しなくなってから削除します。
- **すべてのライターが新しいカラムを扱うこと。** `loom.memory` または `loom.shared` にカラムを追加する場合は、同じ変更で `server/memory/store/rows.py` の行ビルダー（`build_memory_version_row`、`build_embedding_row`）と、それらに値を渡すすべてのプロジェクションを更新します。これらは `FINAL` で読み取る `ReplacingMergeTree(version)` テーブルであり、カラムを省略したバージョン行は、すべてのリーダーに対して黙ってそのカラムを 0 にします。`loom.memory` に新しいカラムを追加する場合は、同じマイグレーションで `loom.memory_versions` と `memory_versions_mv` ビューの `SELECT` にも追加してください。そうしないと履歴に記録されなくなります（`tests/unit/test_memory_versions_schema.py` がカラムリストを比較します）。
- **メモリの内容を保持しうるテーブル**には、消去サガのステップと残存データ調査の行（`server/erasure/store/census.py`、`_CENSUS`）も必要です。どのリストにも載っていない新しいテーブルは `tests/unit/test_every_table_is_classified.py` が失敗させます（[テーブルの追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-table.md)）。
- **エンジンに依存しない DDL。** ベクトルカラムは `Array(BFloat16) CODEC(NONE)` と宣言します。`Quantized` コーデックを使える環境では、ランナーがコーデック付きで作成します（[制限付き設定](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)）。エンジン名は通常の `MergeTree` を使ってください。ClickHouse Cloud ではこれが `SharedMergeTree` として動作します。

**追加のみとする理由。** マイグレーションの実行前に稼働していたビルドは、実行後も、それが把握していたすべてのテーブルを読み書きできなければなりません。そうすることで、ブルー/グリーンのロールバックはスキーマのロールバックではなくプロキシの切り替えで済み、新しい側が起動時にマイグレーションを適用している間も、古い側がサービスを続けられます。そのコストはここで支払います。カラムの廃止に 2 回のリリースが必要になることと、キーの変更に `ALTER` ではなく再構築が必要になることです。

<a id="runner-directives"></a>

## ランナーディレクティブ

マイグレーションファイルには、ランナーが読み取るコメントディレクティブを記述できます。

| ディレクティブ                      | 効果                                                                                      |
| ----------------------------------- | ----------------------------------------------------------------------------------------- |
| `-- loom: tolerate-error <code>`    | ファイルは指定した 1 つのサーバーエラーを許容して処理を続けます（例：一部のサーバーだけが受け付ける設定） |
| `-- loom: rebuild-if-empty <table>` | `<table>` の目標とするソートキーまたはパーティションキーを宣言します（後述）              |
| `-- loom: baseline covers N`        | リポジトリにもう存在しない、番号が N 以下のすべてのファイルを、このファイルが置き換えます |

**ベースライン。** ベースラインには、置き換える最新のファイルの後ろの番号 N が付きます。新規のデータベースではこのファイルを実行します。台帳にファイル N が記録されているデータベースでは、実行せずに記録だけを行い、置き換えられた行は保持します。strict モードはこれらの行をエラーではなく superseded として報告します。そのため、squash で台帳を手作業で編集する必要はなく、squash より前のビルドでも、自身のファイルがすべて適用済みであると認識できます。台帳に置き換えられたファイルの一部はあるがファイル N がない場合、そのデータベースは途中で止まっています。`loom migrate` はこれを拒否し（`stranded`）、何も変更しません。ベースラインを実行すると不足しているファイルがスキップされ、記録すると、それらのスキーマが適用済みであると見なされてしまうためです。不足しているファイルは squash より前のビルドで適用してから、もう一度 migrate を実行してください。squash はリリース時に行い、変更ごとには行わないでください。squash は `schemas/schema.sql`（残すファイルが作成するオブジェクトを除いたダンプ本体）からベースラインを作成し、新規のサーバー、完全に migrate 済みの台帳、途中までの台帳のそれぞれで動作を検証します。

ランナーは、ファイルやそのチェックサムを変更せずに、適用時に文を調整します。サーバーが禁止している場合は実験的な NLP フラグを取り除きます。使用できる場合は、ベクトルカラムを `Quantized` コーデックで作成し、`allow_suspicious_codecs` を使えない場合は、その後ろに連結された汎用コーデックを取り除きます（`server/core/schema/vector_codec_ddl.py`）。すべてのビューを、マイグレーションを実行しているユーザーで作成します（`DEFINER = CURRENT_USER`：ダンプには元のユーザー名が含まれ、別のサーバーにはそのユーザーが存在しないことがあります）。また、ファイルが発行するすべてのミューテーションの完了を待ってから記録します（`LOOM_MIGRATE_MUTATION_WAIT_S`）。

**事前チェック。** データにファイル単体では判別できない内容が含まれている間は実行してはならないファイルは、`server/core/schema/prechecks.py` にチェックを登録します。キーは番号を除いたファイル名なので、番号を振り直してもチェックは維持されます。ランナーはファイルの前にチェックを実行します。ドライランでも同様です。チェックが拒否した場合は、そのメッセージを表示して停止し、そのファイルは何も実行しません。`097_mirror_names_reserved.sql` がこれを使っており、予約済みのミラー形式の名前空間が登録されているか、有効な行を保持している間は実行を拒否します。

<a id="changing-a-sort-key-or-a-partition-key"></a>

## ソートキーまたはパーティションキーの変更

ClickHouse はどちらもその場では変更できないため、マイグレーションでは目標とする定義を宣言し、テーブルは変更ではなく再構築されます。

```sql
-- loom: rebuild-if-empty code_test_facets
CREATE TABLE loom.code_test_facets ( ...every column, unchanged... ) ... ORDER BY (...);
```

この文が目標 DDL の唯一の記述です。ランナー（`server/core/schema/table_rebuild.py`）は、稼働中のテーブルの `sorting_key` と `partition_key` をこの文と比較します。一致する場合はファイルを記録し、空のテーブル（新規インストール）はその場で目標 DDL を持つ複製と入れ替え、行を持つテーブルはツール名を示すメッセージとともに起動に失敗します。起動時に行がコピーされることはありません。データのあるデプロイでは、オペレーターが先にツールをテーブルごとに 1 つずつ実行します。

```bash
python -P -m scripts.ops.table_rebuild --table code_test_facets --dry-run
python -P -m scripts.ops.table_rebuild --table code_test_facets --yes
```

変更を伴うすべての操作（`--yes` のみ、`--rollback`、`--drop-pre-rebuild`）は、接続先の ClickHouse と実行計画を表示し、`--yes` がなければ何も変更しません。このツールは、全行（FINAL なし）を制限付きのバッチで `loom.<t>_rebuild` にコピーし、その間に書き込まれたパーツもコピーし、FINAL 付きの行数と、サンプルとして選んだキーの最新バージョンを検証し、`EXCHANGE TABLES` で入れ替えます。古いテーブルは `loom.<t>_pre_rebuild` として残り、`--drop-pre-rebuild` を実行するまで `--rollback` に使えます。コピー中に `ALTER` が稼働中のテーブルを変更した場合は、複製を残したまま入れ替えを拒否します。ClickHouse がミューテーションとして実行するもの（カラムの削除、名前変更、型変更、インデックスの削除、TTL）では終了コード 6、メタデータの時刻だけを動かすもの（インデックス、設定、カラムのデフォルト値やコーデック）では終了コード 3 です。カラムの追加はコピーを失敗させる（終了コード 1、入れ替えなし）か、最後の検証パスの後に終了コード 3 で終了します。再構築は `ALTER` を引き継ぎません。目標の文に反映してから `--restart` で再実行してください。また、どのマイグレーションにも宣言されていないテーブルは拒否されます。`loom migrate --dry-run --strict` は、宣言済みのテーブルが旧レイアウトのまま行を保持している間は失敗するため、デプロイ前の確認で手順の漏れを検出できます。

目標はテーブルのカラムを維持し（入れ替えが追加のみになるように）、そのソートキーは旧キーのカラムを並べ替えたものにします。このキーは `ReplacingMergeTree` がマージに使うキーでもあるためです。他のカラムを含むキーは別の行をマージしてしまうため、その理由とともに `tests/unit/test_migrations_additive.py` に記載されます。再構築はオペレーター向けの手順として、リリースノートに記載してください（[リリースプロセス](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)）。

<a id="postgres-migrations"></a>

## Postgres マイグレーション

`schemas/pg_migrations/NNN_*.sql` も同じ規則に従います。番号付きであること、追加のみであること、マージ後は編集しないことに加え、テーブルに対する `CREATE`、`ALTER`、`DROP` はすべて `loom.<table>` を指定します。ただし、リネーム後の新しい名前では Postgres がスキーマを受け付けないため、スキーマを付けません（`tests/unit/test_storage_layout_rows.py`）。起動時には、Postgres のアドバイザリロックを取得した状態で各ファイルを適用します。各ファイルは、台帳の行とともに 1 つのトランザクションで実行されます。マイグレーションがテーブルロックを待つのは最大 5 秒です（`lock_timeout`。超えた場合は再試行します）。そのため、長いトランザクションの背後でそのテーブルへの全クエリが待たされることは、`ALTER` によっては起こりません。ビルドは、より新しいビルドのファイルを含む台帳でも、そのファイルを示す警告を出して動作します。一方、自分が持たないファイルが台帳にあり、その番号が自身の最後のファイル以下である場合や、チェックサムが変わっている場合は、起動を拒否します。

すべてのファイルは 1 つのトランザクションで実行されるため、マイグレーションで大きなテーブルにインデックスを作成してはいけません。作成中はそのテーブルへの書き込みが止まり、終了するまですべてのコミットが 503 を返すためです。そのようなインデックスには、トランザクション外での `CREATE INDEX CONCURRENTLY` が必要です。ただし、アドバイザリロックを待つ起動処理とはデッドロックします（待機中のセッションが保持するスナップショットの終了を、ビルドが待つため）。そのため、このようなファイルを追加する変更では、`pg_try_advisory_lock` を再試行する形でロックも取得します。テーブルに初めて書き込むリリースがそのファイルを含む場合は、ファイルがどこで実行されてもテーブルは小さいため、インデックスをファイルのトランザクション内で作成してかまいません（`010_revision_delivered_at.sql`）。

<a id="schema-dump-and-check"></a>

## スキーマのダンプと検証

`schemas/schema.sql` は、すべてのマイグレーションを適用した結果をダンプしたものです。現在のスキーマの形を把握するために読むもので、手で編集してはいけません。マイグレーションを変更した後は、リリース CI がテストに使う新規の ClickHouse（`clickhouse/clickhouse-server:latest`）に対して再生成します。

```bash
bash dev/ci_start_clickhouse.sh     # that image with Loom's server config
make migrate
make schema-dump                    # rewrites schemas/schema.sql only when the dump differs
make schema-check                   # exit 1 with the head of the diff; writes nothing
```

ダンプに失敗した場合、ファイルは元のまま残ります。ClickHouse のバージョンが異なる場合、`Quantized` コーデックやその `LZ4` チェーンを有効にできないサーバーの場合、またはリビルドのツインを持つデータベースの場合は、ダンプ結果が変わります。固定したコンテナが重要なのはこのためです。内部テーブル（`_migrations`、および先頭が `_` または `.` の名前のテーブル）は除外されます。CI の `schema-check` ジョブは、新規サーバー上ですべてのマイグレーションを再実行し、strict dry run を行ってから、`make schema-check` を実行します。

CI は `latest` を使うため、DDL の出力形式が変わった新しい ClickHouse リリースが出ると、Loom に変更がなくても `schema-check` が失敗します。ジョブが出力する差分を確認してください。書式だけの違いであれば、上記の 4 つのコマンドを実行し（新しいリリースが取得されます）、`schemas/schema.sql` を単独でコミットします。意味の変更（型、デフォルト値、エンジン設定）は ClickHouse の挙動の変化なので、上書きしてダンプする前に原因を調べてください。比較のために、`LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D` を指定すると、問題なくダンプできた最後のリリースで上記のコマンドを実行できます。

<a id="checklist"></a>

## チェックリスト

1. 次の空き番号を取り、ファイルは追加的（additive）に書きます。
2. 変更が行ビルダー、`loom.memory_versions`、消去の棚卸し（erasure census）、アナライザーのケースに関わる場合は、それらを更新します。
3. ローカルの ClickHouse に対して `make migrate` を実行し、続けて `make schema-dump` を実行して、`schemas/schema.sql` をマイグレーションと一緒にコミットします。
4. `make test-unit`（追加的であること、採番、スキーマ分類のテスト）を実行し、新しい形式を読み込むパスについては統合テストスイートも実行します。
5. オペレーターが行う作業（再構築やバックフィルなど）があれば、リリースノート用に記録しておきます。

<a id="related"></a>

## 関連ドキュメント

- [アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): デプロイメントでランナーがファイルを適用する方法
- [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md)
- [スキーマリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)
