<!-- i18n: source=docs/operations/upgrades.md blob=8caf8c789d2fc39ab89d9830d45a7e1229be2051 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md), which is authoritative.

<a id="upgrades"></a>

# アップグレード

アップグレードでは、イメージ（および状態を持たないドキュメント抽出器のイメージ）を置き換え、新しいビルドに含まれるマイグレーションを適用します。それ以外の状態は移動しません。永続的なデータはすべて ClickHouse に保存されており（`LOOM_DOCUMENT_OBJECT_STORE=s3` で書き込まれた原本については、ドキュメントバケットにも保存されています）、コードミラーはそのままマウントされます。このページは運用者向けの内容です。クライアントから見た未知のフィールドや削除されたフィールドの扱いについては、[HTTP API リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)を参照してください。修正が提供されるリリースと、サーバーごとに動作する SDK の対応については、[サポート対象バージョン](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md)を参照してください。

<a id="how-migrations-run"></a>

## マイグレーションの実行方法

ジャーナル、番号付け、ランナーディレクティブ、マイグレーションを書く際のルールは [マイグレーション](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md) にあります。オペレーターが知っておくべき内容は次のとおりです。

- **起動時。** サーバーは、準備完了を報告する前、およびワーカーを起動する前に、未適用のファイルを適用します（`LOOM_BOOT_MIGRATE=true`、デフォルト）。どのコンテナのどのプロセスも `loom._migration_lock` 内の `KeeperMap` ロックを取得するため、各ファイルは一度だけ適用されます。Keeper のないサーバーは拒否されます。
- **プロバイダーの再構築。** 未適用のファイルがある状態で起動すると、まずプロバイダー参加者として登録します。次にプロバイダー設定ロック（Keeper のキー 1 つで、このチェックの間だけ保持）の下で、未完了のプロバイダー再構築がないことを確認し、あれば `Finish the provider rebuild before applying schema migrations` を出力してマイグレーションを拒否します。その後、ファイルはマイグレーションロックのみの下で適用されます。その間に開始された再構成は、起動中のプロセスが確認応答するまで待機するため、再構築がマイグレーションと並行して実行されることはありません。
- **マイグレーションや再構成の最中にコンテナが強制終了された場合**、他のコンテナはしばらくの間だけブロックされます。マイグレーションロックは、保持者が更新を止めてから（`ttl_s`）引き継がれます。プロバイダーロック（TTL 60 秒、20 秒ごとに更新）は、期限切れになり、かつ保持者の参加者行も期限切れになった時点で引き継がれます。つまり、60 秒間ハートビートがなく（ハートビートは 10 秒ごと）、どのノードの `system.processes` にもその id でタグ付けされたクエリがない状態です。同時に起動したコンテナは、失敗せずにプロバイダーロックを最大 120 秒待ちます。保持者に参加者行がないロックは、そのプロセスが停止したことを確認してから `loom provider-lock clear --holder <holder>` で解放します。保持者が更新、ハートビート、またはクエリ実行を続けている間は、このコマンドは拒否されます。
- **ミューテーション。** ファイルは、そのミューテーションが完了して初めて記録されます。ミューテーションが失敗した場合、または `LOOM_MIGRATE_MUTATION_WAIT_S`（デフォルト 600）を超えて実行された場合は、そのファイルが失敗となり、ログにファイル名を示して起動が停止します。
- **キーの変更。** ソートキーまたはパーティションキーを変更するマイグレーションは、空のテーブルであれば自身で再構築し、行のあるテーブルでは起動を失敗させます。そのテーブルについては、デプロイ前に `scripts/ops/table_rebuild.py` を実行してください。厳密なドライランで検出できます。
- **新規インストール。** 新規インストールとアップグレードは同じ経路です。サーバーは、台帳にないものをすべて再生します。空のデータベースでは、ジャーナル全体が対象となり、数分かかります。
- **途中で止まった台帳。** ベースラインが置き換えるファイルの途中で台帳が止まっているデータベースは拒否されます（`stranded`）。先に、そのベースラインより前のビルドでマイグレーションしてください。
- **番号が振り直されたファイル。** リポジトリに該当ファイルがなくなった台帳の行は `ledger-only` となり、`--strict` は 1 で終了します。ただし、記録された SHA-256 が現行ファイルのものと一致する場合は例外で、これは新しい番号の下にある同じファイルです。ランナーはこれを renamed として報告し、未適用であれば再実行せずに新しい名前を記録し、`--strict` は成功します。古い名前で実行された後に編集されたファイルは SHA-256 が異なるため、ledger-only のままです。新しい名前は未適用ファイルとして実行され、古い行は手動で削除します（[トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)）。それまでは、`/v1/health` が古い行を `schema.ledger_only` の下に列挙します。

CLI では、同じランナーを手動で実行できます。

```bash
loom migrate --status              # applied and pending; changes nothing
loom migrate --dry-run --strict    # pending files; exit 1 on ledger-only rows (not renames) or checksum drift
loom migrate                       # apply pending migrations
loom migrate --target N            # apply through file number N only
```

`make migrate`、`make migrate-status`、`make migrate-dry` は、チェックアウトしたリポジトリから同じモジュールを実行します。

<a id="upgrade-procedure"></a>

## アップグレード手順

1. リリースの [GitHub リリース](https://github.com/ClickHouse/loom/releases)のノートを読み、オペレーター向けの手順（新しい設定、デプロイメントで実行が必要なバックフィル）と、クライアントから見えるワイヤ形式の変更を確認します。
2. 新しいビルドのチェックアウトから、本番の ClickHouse に対して `loom migrate --dry-run --strict` を実行します。起動時に適用される内容を一覧し、台帳に問題があれば停止します（[トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)）。リリースがテーブルの再構築を宣言している場合は、そのテーブルに対して今のうちに `scripts/ops/table_rebuild.py` を実行します。Postgres authority のデプロイメントが、ナレッジドキュメントのヘッドを Postgres に保持するビルドへ移行する場合は、今のうちに `LOOM_RETENTION_DOCUMENTS_DAYS` を手作業で unset します。新しいビルドはこれが設定されたままだと起動を拒否し、その authority では保持期間の TTL が `loom.documents` に及ばず、名前空間の `retention_days` も `loom.chunks` に及びません（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 38）。セッションに印を付け、org トリガー（ルール 47）を備えるビルドへ移行する[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)は、先に `LOOM_PG_DSN` をプーラーを介さず Postgres へ直接向けます（[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)）。また、準備したあとに `LOOM_CELL` を unset して取りやめた後発のセルは、先にその状態をクリアします（[トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)）。新しいビルドは、自身のプロセスをその状態でゲートするためです。すでに準備済みの後発のセルでは、新しいビルドの最初のマイグレーション起動で org トリガーが作成されます。そのため、そこで動き続けている古い Pod は、置き換えられるまでテナント org の行の挿入がすべて拒否されます（呼び出し元にはマップされないエラーとなります）。セルは 1 ステップでロールアウトしてください。
3. 新しいイメージをデプロイします。最初に起動したコンテナがマイグレーションを適用し、他のコンテナはその完了を待って、保留中のものがないことを確認します。
4. トラフィックを向ける前に、`/v1/health` が `status: ok` を報告し、`schema.pending` が空になるまで待ちます。[ゼロダウンタイムペア](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair)は、ステップ 2 で挙げた設定の unset を除き、ステップ 2 から 4 を代行します。`redeploy.sh` は事前チェックで strict ドライランを実行し、新しい色の準備ができてから切り替えます。
5. リリースにバックフィルがあれば実行します（[メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#backfills-and-repairs)）。
6. リリースでプラグインの変更が触れられている場合は、クライアントプラグインを更新します。フックはクライアントマシン上で動作し、クライアントのプラグインマネージャー（Claude Code では `claude plugin update loom@loom`）からしか変更できず、サーバーのデプロイでは変わりません。
7. 最後の旧レプリカが停止したら、ロールアウト中に完了した消去を再検証します：`loom erasure-re-verify --completed-since <when step 3 began> --resume`。旧レプリカの saga は、新しいビルドが追加したテーブルを知りません（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 14）。このコマンドが削除するのは孤立した行リンクだけです。つまり、各消去のスコープ内にある `entity_triple_rows` と `entity_edge_rows` の行のうち、そのメモリに有効な行がなく、消去の census 以降に書かれた行もないものです。`--resume` を付けない場合は件数を数え、残りがあれば 1 で終了します。消去の後に書かれたデータは残ります。
8. 前のビルドが対応できない機能は、すべてのレプリカが新しいビルドで動くようになってから有効にします。対象は次のとおりです。`memory_extract_agent_learnings`：前のビルドは `loom.learnings_supporters` を消去しないためです（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 23）。`LOOM_DOCUMENT_OBJECT_STORE=s3`：前のビルドはバケット行の空の `data` を読み、そのダウンロードに `410 BLOB_GONE` を返すためです（ルール 9）。`LOOM_PG_PAYLOAD_CLEAR_ENABLED`：新しいビルドが単独で稼働してから、クリア期間（clearing horizon）1 つ分より早くならないようにします。前のビルドは遅れて届いた配信に印を付けないためです（ルール 43）。
9. `LOOM_CODE_ASK_MEMORY_NAMESPACE` が現在、または過去のいずれかの時点で設定されているデプロイメントが、キャッシュされた `code.ask` の回答が引用したメモリを示さないビルド（`086_code_answer_memory_scope.sql` より前）から、示すビルドへ移行する場合（ロールバック後のロールフォワードも含む）：新しいビルドが単独で稼働し、プロバイダーの再構築が実行されていない状態になったら、`TRUNCATE TABLE <LOOM_CH_DATABASE>.code_answers`（デフォルトでは `loom.code_answers`）で回答キャッシュをクリアします。古いビルドの回答とトゥームストーンはメモリを示さず、ロールアウト中にそのビルドが実行した消去や redact もこのテーブルを知らなかったため（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 36）、どの saga もそこに残るものを見つけられません。再構築はテーブルのコピーを上から公開するので、truncate をまたいで実行中の再構築があると、行が戻ってきます。truncate はパートを即座に、レプリケートされたテーブルでは全レプリカで削除し、待つべき mutation はなく、loom ユーザーはその権限を持っています。クリアされた質問は、次に尋ねられたときに 1 回ずつ合成のコストがかかります。
10. ユーザーのメモリを集約した統合サマリーが取り込んだものにまで、ユーザー消去が及ぶビルドへ移行する場合（ロールバック後のロールフォワードも含む）：新しいビルドが単独で稼働したら、それ以前に `purge` なしで要求された各ユーザー消去を再度要求します（`loom.erasure_requests` の `user_id` 行、90 日間保持）。新しいビルドが完了させたものも含みます。古いビルドの saga は、サマリーの履歴、チャンク、ベクトル、グラフ行、共有コピーを残していました（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 37）。新しい saga は、そのユーザーがその後に書いたものも消去します。有効なメモリ行を持たないユーザーに対して、または、それが望ましい場合にだけ要求してください。古いビルドが完了させたユーザー purge も同じ行を残しており、以後のユーザー消去ではそれらを見つけられません。ユーザーのキーがすでにないためです。`shared.tombstone` を過ぎるところまで実行された消去は、どのビルドが完了させたかにかかわらず、そのユーザー自身のメモリの共有コピーの検索行も残しました。古いビルドが `agent="*"` で昇格させた共有コピーは、ソースエージェントとして `*` を名乗り、コピー元のユーザーの消去ではどれも見つからないことがあります。それらを一覧し、消去されたユーザーのテキストを保持しているものは、それぞれ確認のうえ `memory.demote` で処理します。

    ```sql
    SELECT org, namespace, memory_key FROM loom.shared FINAL
    WHERE tombstoned_at IS NULL AND arrayExists(x -> splitByChar('/', x)[3] = '*', promoted_from)
    ```

11. Postgres authority のデプロイメントが、ナレッジドキュメントのヘッドを Postgres に保持するビルド（`002_knowledge_documents.sql`）へ移行する場合（ロールバック後のロールフォワードも含む）：新しいビルドが単独で稼働したら、新しいビルドが最初に稼働して以降、古いビルドのレプリカが稼働している間に完了したすべての消去を再実行します。古いビルドの消去は、ヘッドも配信も削除しません。消去されたドキュメントのテキストは、消去が再実行されるまで `loom.knowledge_heads` に残り、残された配信は配信ワーカーによって再度公開されます（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 38）。パークされた配信を再試行する前に実行してください。
12. Postgres authority のデプロイメントが、ナレッジ取り込みジョブ（`005_knowledge_jobs.sql`）またはデータセットカタログ（`007_knowledge_datasets.sql`）を Postgres に保持するビルドへ移行する場合（ロールバック後のロールフォワードも含む）：新しいビルドが単独で稼働したら、新しいビルドが最初に稼働して以降、古いビルドのレプリカが稼働している間に完了したすべての消去を再実行します。古いビルドの消去は、`loom.knowledge_jobs` からジョブを、`loom.knowledge_datasets` からカタログ行を削除しません。新しいビルドのワーカーはそのようなジョブを `loom.knowledge_ingestions` に射影し直して実行し、カタログ行は消去が再実行されるまで残ります（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 40 と 42）。ステップ 11 の実行は、ヘッド、ジョブ、カタログ行をまとめてカバーするため、消去 1 件につき 1 回の実行で足ります。その期間に古いビルドが応答した `knowledge.delete` と `knowledge.bulk_delete` も、それぞれ再度要求してください。これらはソースのデータセットを `loom.datasets` からしか削除しませんでした。
13. あらゆる名前空間でドキュメントソースを保持するビルドへ移行する場合（ロールバック後のロールフォワードも含む）：新しいビルドが単独で稼働したら、古いビルドの `namespace.delete`、`collection.delete`、または名前空間の消去がアクティブなまま残したソースを廃止します（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 44）。削除された名前空間上のソースは次のとおりです。

    ```sql
    SELECT s.org, s.namespace, s.name
    FROM (SELECT org, namespace, name FROM loom.knowledge_sources FINAL WHERE active) AS s
    INNER JOIN (SELECT org, namespace FROM loom.namespaces FINAL WHERE NOT active) AS n
        ON s.org = n.org AND s.namespace = n.namespace
    ```

    また、名前空間の消去が完了する前に登録され、その消去が廃止しなかったソース（古いビルドが実行した消去のもの、または新しいビルドの消去の実行中に古いビルドが追加したもの）は次のとおりです。

    ```sql
    SELECT s.org, s.namespace, s.name, e.id
    FROM (
        SELECT org, namespace, name, active, created_at, updated_at
        FROM loom.knowledge_sources FINAL
    ) AS s
    INNER JOIN (
        SELECT id, org, completed_at,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    ) AS e ON s.org = e.org AND s.namespace = e.namespace
    WHERE s.created_at <= e.completed_at AND (s.active OR s.updated_at > e.completed_at)
    ```

    1 つ目のクエリの結果については、一覧された各ソースを削除する（`knowledge.source.remove`）か、新しいビルドでその名前空間を再度削除します。再削除すればソースは廃止されます。2 つ目については、一覧された各ソースを `knowledge.source.remove` と `delete_documents=true` で削除します。これにより、そのソースが書き戻したドキュメントが廃止されます（すでに廃止されたソースも受け付けます。たとえば、その名前空間を後から削除して廃止されたソースです）。さらに、それらから抽出された decision 行を `memory.delete` で削除します。新しいビルドによる名前空間の消去は両方を行いますが、最初の消去以降にその名前空間へ書かれたものもすべて消去します。再度要求するのは、それ以降に誰も書き込んでいない名前空間に限ってください。消去の後に登録されたソースは一覧に出ません。その消去はそのソースを対象にしていなかったためです。2 つ目の一覧は一度だけ処理するための作業リストです。上のように削除したソースも、その削除行が消去より新しいため、一覧に残り続けます。

    古いビルドが開始し、新しいビルドが完了させた名前空間の消去には、ここでの作業は不要です。最終 census が、そのソースが書き戻したドキュメントと decision を見つけてそのステップを再度開くため、それらがなくなって初めて saga が完了します。

14. 名前空間の消去が `loom.shared` に及ぶビルドへ移行する場合（ロールバック後のロールフォワードも含む）：新しいビルドが単独で稼働したら、古いビルドの名前空間の消去が残した昇格済みの行をクリアします（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 45）。以下のクエリは、台帳（90 日間保持）にある `namespace` または `namespace_list` の消去が及んだ名前空間のうち、昇格済みの行を持つものを一覧します。あわせて、purge なし（`erased_at`）と purge あり（`purged_at`）それぞれの最後の消去が census を実行した時刻を示します。この後の文は、その時刻（秒）より前に昇格された行を対象とするため、census より後に昇格された行は残ります。census と同じ秒の中でそれ以前に昇格された行も、`promoted_at` の秒単位の精度では区別できないため残ることがあります。新しいビルドのステップは、自身が実行した消去ではそのような行もすでに処理しています。purge するのは `namespace_list` だけであり、古いビルドの `namespace` 行には、要求されたフラグが入っていることがあります。

    ```sql
    SELECT org, namespace,
           maxIf(counted_at, NOT purged) AS erased_at,
           maxIf(counted_at, purged) AS purged_at
    FROM (
        -- The census's time: a row promoted after it was never in the erasure's reach.
        SELECT org, ifNull(verified_at, completed_at) AS counted_at,
               scope_type = 'namespace_list' AND purge AS purged,
               arrayJoin(if(scope_type = 'namespace', [scope_value],
                            if(JSONType(scope_value) = 'Array',
                               JSONExtract(scope_value, 'Array(String)'),
                               JSONExtract(scope_value, 'namespaces', 'Array(String)')))) AS namespace
        FROM loom.erasure_requests FINAL
        WHERE scope_type IN ('namespace', 'namespace_list') AND status = 'completed'
    )
    -- Only a namespace that holds a promoted row has anything to clear.
    WHERE (org, namespace) IN (SELECT org, namespace FROM loom.shared)
    GROUP BY org, namespace
    ```

    それぞれについて、新しいビルドのステップが実行する内容を、消去の census より前に昇格されたすべてのバージョンに対して実行します。同じキーの後の昇格によって `FINAL` から隠れたバージョンも含みます。`purged_at` が設定されている場合は、それらを削除します。

    ```sql
    ALTER TABLE loom.shared DELETE
    WHERE org = '<org>' AND namespace = '<namespace>' AND promoted_at < '<purged_at>'
    SETTINGS mutations_sync = 2
    ```

    `erased_at` が設定されている場合は、残っている有効な行をトゥームストーンにします。

    ```sql
    ALTER TABLE loom.shared UPDATE tombstoned_at = toDateTime('<the time you run it>')
    WHERE org = '<org>' AND namespace = '<namespace>' AND tombstoned_at IS NULL
      AND promoted_at < '<erased_at>'
    SETTINGS mutations_sync = 2
    ```

    この設定により、各文は全レプリカで完了するまで待たれます。時刻はステップがバインドするのと同様にリテラルで書きます。レプリケートされたテーブルは、`now()` のような非決定的な関数を呼ぶ mutation を拒否するためです。台帳の 90 日より前に要求された名前空間の消去は一覧に出ず、それが残した昇格済みの行は有効な名前空間のものと区別できません。それらは古いビルドが残したままになります。

15. 組織を宣言するすべての CLI コマンド（コードインデックスのコマンド、インポート）がデプロイメントを確認するビルドへ移行する場合：どちらの authority でも、その ClickHouse ユーザーには `loom.storage_authority` への `SELECT` が必要で、コマンドは別の authority を記録しているデプロイメントに対しては実行を拒否します。Postgres authority では、さらに `LOOM_PG_DSN` が必要で、セルの状態を読みます（`LOOM_CELL` が unset の場合は、専用のセッションで読みます）。`LOOM_CELL` が設定されている場合、どの org にも書き込まないがセルのストアを変更するコマンド（`loom settings set`、`loom secrets reseal`、`loom code compact` など）やアクセスを取り消すコマンドも、同じ方法でデプロイメントを確認し、専用のセッションでセルの `first` 行を読みます。接続に使う Postgres ロールに、次の権限を付与します。
    - `LOOM_CELL` が unset の場合：スキーマ `loom` への `USAGE`、`loom.cell_first`、`loom.tenancy_mode`、`loom.authority` への `SELECT`、`loom.cell_self` への `SELECT (updated_at)`、`loom.tenancy` への `SELECT (since)`（列権限により、フィンガープリントと org にはアクセスできません）、そして `LOOM_PG_POOL_MAX_SIZE` に、同時に実行されるそのようなコマンドの数を掛けた値以上の `CONNECTION LIMIT`。各コマンドは書き込み用に専用のプールを開くためです（状態の読み取りは、上限に達した場合に短時間再試行したのち、拒否されます）。
    - `LOOM_CELL` が設定されている場合：上と同じ権限に加えて、`loom.cell_self` と `loom.tenancy` への `SELECT` を全列に付与します。最初のセルが `recording` の間に新しい org を記録するには、`loom.tenancy` への `INSERT` と `loom.tenancy_mode` への `UPDATE (since)` も付与します。セルが接続（attach）されたら、`LOOM_CELL_ROUTER_TOKEN` とともに両方を取り消します。ドキュメントバケットの設定（`LOOM_DOCUMENT_S3_BUCKET`、`LOOM_DOCUMENT_S3_PREFIX`）はサーバーのものと同じにします。

    列権限のみを持つロールが、`recording` を過ぎたセルに対して行う書き込みは、`LT001` ではなく権限エラー（SQLSTATE 42501）として拒否されます。

16. 最初のセルの attach の前に、すべての CLI と CI イメージは、その状態を読み取るビルドで実行され、組織向けに書き込むすべての CLI には `LOOM_CELL` が設定されています。CLI はハートビートを送信しないため、attach ではどちらも確認できず、古いビルドの CLI は `enforced` のセルで拒否されません（[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#failure-modes)）。

17. 最初のセルのプロセスが、タイマーでルーターからそのセルの状態を読み取るビルドへ移行する場合：どのセルのプロセスよりも先に、すべてのルーターレプリカ（`loom router serve`）がこのビルドで実行されている必要があります。準備済みの最初のセルの各プロセスは、最初のセル状態の読み取りから `serving` を読み取るまで、ルーターの状態ルートに問い合わせます。以前のビルドのルーターは、そのような呼び出しのたびにディレクトリを読み取ります。読み取りは、サーバープロセスごとにおよそ `min(LOOM_INSTANCE_HEARTBEAT_SECONDS, LOOM_TENANCY_MODE_CACHE_S)` あたり 1 回で、デフォルト設定では、8 ワーカーの Pod 1,000 個で毎秒 1,600 回の読み取りになります。このビルドのルーターは、レプリカごとに `LOOM_ROUTER_DIRECTORY_CACHE_S` あたり 1 回の読み取りでこれらに応答します（[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-heartbeats-keys)）。

マイグレーションの適用中も、古いビルドは稼働し続けます。すべてのマイグレーションは追加的なものだからです。2 つのビルドを並行して稼働させるためのルールは、[ローリングアップグレード](../architecture/rolling-upgrades.md)にあります。

<a id="rollback"></a>

## ロールバック

以前のイメージを再デプロイします。ペアの場合は `./redeploy.sh --rollback` を実行します。アシスタントターンの learnings を持たないビルドへロールバックする前に、`memory_extract_agent_learnings` をオフにし、`loom.learnings_supporters` の行と、`loom.learnings_queue` の `assistant_turn` 行を削除します（[抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#rules-the-assistant-states-about-itself)）。以前のビルドは、昇格したボットのルールを、そのボットだけでなく名前空間内のすべてのエージェントに提供します。これを防ぐには、ロールバックの前に `knowledge.delete` で `source-kind:assistant_turn` タグの付いたドキュメントを削除します。削除しない場合は、提供範囲が広がることを受け入れてください。
名前空間ガイドが存在するようになった後に、以前のビルドが実行した名前空間の消去は、その名前空間のガイドを残します。新しいビルドが再び提供を始めたら、消去をもう一度実行してください（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 24）。以前のビルドが実行した名前空間の消去は、その名前空間の昇格済みの行を有効なまま残します。新しいビルドが再び提供を始めたら、アップグレード手順のステップ 14 でそれらを一覧できます（ルール 45）。
ボットのトークン最大値を持たないビルドへロールバックすると、すべてのボットのトークンの有効期間が 1 時間になり、そのビルドが保存するボットは最大値として 3600 を保持します。新しいビルドが提供を始めたら、より短い値または長い値を再設定してください（[ローリングアップグレード](../architecture/rolling-upgrades.md)、ルール 26）。
以前のビルドが実行したユーザーの消去は、そのユーザーのメモリの要約が取り込んだ内容を残します。新しいビルドが提供を始めたら、`purge` なしで実行した消去をそれぞれ、ステップ 10 と同様にもう一度要求してください。完了した purge は再び見つかりません（ルール 37）。
ドキュメントバケットを追加したビルドより前へは、カタログのいずれかの行がバケット内のオブジェクトを指している間はロールバックしないでください。古いビルドはそれらのダウンロードに `410 BLOB_GONE` を返し、その消去は行を削除してもオブジェクトには到達できず、そのスイープはバケットを走査しません（[原本の保存場所](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#where-originals-are-stored)）。
`086_code_answer_memory_scope.sql` より前のビルドへロールバックし、メモリの名前空間を `code.ask` にバインドしている場合は、ステップ 9 と同様に回答キャッシュを 2 回クリアします。1 回目は古いビルドだけが提供しているとき、2 回目は新しいビルドが再び提供を始めたときです。マイグレーションはすでに台帳にあるため、ロールフォワードでは 2 回目を促すものは何も適用されません。
ロールバックの前に `LOOM_PG_PAYLOAD_CLEAR_ENABLED` をオフにします。オンに戻すのは、新しいビルドが再び単独で提供を始めてから、クリアの期間（clearing horizon）が 1 回分経過した後にしてください。クリアのパスが一度実行された後は、`010_revision_delivered_at.sql` を含むビルドでのみ `loom reconcile` を実行し、その後はそのビルドでデプロイメントを起動してください。ロールバックするのは、新しいビルドに、クリア済みリビジョンに対する取得可能な未配信がなくなってからにします。次のクエリが 0 を返せば、その状態です（ルール 43）。

```sql
SELECT count(*) FROM loom.memory_deliveries d
JOIN loom.memory_revisions r USING (org, namespace, seq)
WHERE r.row IS NULL AND NOT d.settled AND d.lease_until <> 'infinity';
```

0 になるまでは、新しいビルドにそれらを配信させてください。以前のビルドは保留中（parked）の配信を取得しないため、保留中のものは残ってかまいません。以前のビルドが提供している間、再試行はクリア済みのリビジョンを除外し（[Postgres の運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)）、`loom reconcile` は実行しません。実行すると、reconcile が保持していたものが以前のビルドに解放されてしまいます。
準備済みのセル（`loom.cell_first` の行を持つもの）を、[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)の org トリガーを持たないビルドへロールバックする前に、新しいビルドのすべての Pod を停止してください。その後、[LOOM_CELL が未設定のプロセスはゲートされる](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#a-process-with-loom_cell-unset-is-gated)のトリガー削除のみを実行し、その状態トランザクションは実行しません（セルは準備済みのままです）。ロールバックが終わる前に新しい Pod がマイグレーション有効で起動した場合は、もう一度実行してください。古いビルドのセッションはテナンシーマーカーを送らないため、新しいビルドがトリガーと `loom.tenancy` ガードを作成した後は、セルがテナンシー行を持たない org に対する書き込みはすべて拒否されます（アタッチ前の後続セルでは、すべてのテナント org が対象です）。新たに配置された各 org のテナンシー行も同様に拒否され、その他の書き込みにはトリガーの状態読み取りのコストがかかります。
元に戻すマイグレーションはありません。以前のビルドは新しいスキーマを読み書きします。以前のビルドによる `loom.memory` やその他の `ReplacingMergeTree` メモリテーブルへの書き込みは、そのビルドが知らないカラムを保持します（[ローリングアップグレード](../architecture/rolling-upgrades.md)）。

ダウンマイグレーションはありません。以前のスキーマに戻すには、アップグレード前に取得したバックアップをリストアしてください（[バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)）。

<a id="downgrading-an-installer-install"></a>

### インストーラーによるインストールのダウングレード

各インストーラーバンドルには、ビルドに使われた ClickHouse のリリースが指定されています。そのため、以前の Loom バージョンをインストールすると、新しい ClickHouse が書き込んだボリュームで以前の ClickHouse も動くことになり、読み取れない場合があります。Loom だけをロールバックするには、新しい ClickHouse を維持します。以前のインストーラーを実行する前に（スタックはすぐに起動します）、現在 ClickHouse が使っているイメージを、インストールディレクトリの `.env` に固定してください。次のコマンドは、そこに記述する行を出力します。既存の `LOOM_CLICKHOUSE_IMAGE` の行があれば置き換えてください。

```bash
dir=${LOOM_INSTALL_DIR:-$HOME/.loom}
echo "LOOM_CLICKHOUSE_IMAGE=$(docker inspect -f '{{.Config.Image}}' \
  "$(docker compose --project-directory "$dir" ps -q clickhouse)")"
```

次に、以前のリリースのインストーラーをそのバージョンを指定して実行します。古いインストーラーは、新しいインストールに対してそのままでは実行を拒否するためです。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/vX.Y.Z/install.sh -o loom-install.sh && LOOM_VERSION=X.Y.Z sh loom-install.sh && rm loom-install.sh
```

ホームディレクトリ、またはあなただけが書き込める[別のディレクトリ](../getting-started/quickstart.md#fast-path-released-image-no-clone)から実行してください。

インストーラーは `.env` を保持するため、この固定は以降のアップグレードでも ClickHouse を維持します。新しい Loom バージョンをインストールする前に `LOOM_CLICKHOUSE_IMAGE` の行を削除してください。また、シェルに `LOOM_CLICKHOUSE_IMAGE` をエクスポートしたままにしないでください。インストーラーは、与えられた値を `.env` に書き戻すためです。両方を元に戻すには、アップグレード前に取得したバックアップをリストアしてください。

<a id="what-stays-the-same-across-an-upgrade"></a>

## アップグレード前後で変わらないこと

| アップグレード前後                      | 保証される内容                                                                                       | 保証する仕組み                            |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| 新しいスキーマに対する以前のビルド      | 既知のすべてのテーブルを引き続き読み書きできるため、ロールバックにスキーマ操作は不要                 | `tests/unit/test_migrations_additive.py`  |
| 適用済みのマイグレーションファイル      | 編集も削除もしない。修正は新しいファイルとして追加する                                               | ledger のチェックサム、`--dry-run --strict` |
| サーバーが削除したフィールドを送るクライアント | 処理される。そのフィールドは `ignored_args` に記載される                                             | `server/transport/arg_check.py`           |
| 以前のビルドと新しいビルドの同時稼働    | 新しいカラムを含め、それぞれが相手の書き込んだ内容を読める。ただしロールアウト後（手順 8）に初めて有効にした機能は除く | `make test-mixed-version`                 |

アップグレードでは、オペレーションやフィールドが変更または削除されることがあります。デプロイメントが提供している内容は、機能カタログで確認できます（[HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)）。2 つのビルド間の変更点は `scripts/codegen/wire_changelog.py <previous> <new>` で一覧できます（[コード生成](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md#wire-changelog)）。

<a id="related"></a>

## 関連ドキュメント

- [デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#zero-downtime-pair): アップグレードを入れ替えだけで済ませるためのペア構成
- [マイグレーション](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md): 新しいマイグレーションが従うルール
- [ローリングアップグレード](../architecture/rolling-upgrades.md): ビルド N-1 と N のためのルール
- [トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md#migrations-pending-or-checksum-drift)
