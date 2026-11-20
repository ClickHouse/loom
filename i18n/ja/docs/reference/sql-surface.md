<!-- i18n: source=docs/reference/sql-surface.md blob=cbe1b629ce7bff1c19c5d51c3a461f058ad2fd2c model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md), which is authoritative.

<a id="sql-surface"></a>

# SQL サーフェス

SQL サーフェスは、テナントのメモリ、メモリ履歴、会話、リクエスト台帳に対する、読み取り専用の ClickHouse ビューの集合です。組織が使用する SQL クライアント、BI ツール、ウェアハウスのジョブは、`loom sql-access` が作成する ClickHouse ユーザーでこれらのビューを読み取ります。このユーザーが読み取れるのはビューだけで、各ビューには、そのユーザーに付与された組織と名前空間のデータだけが表示されます。

<a id="version-and-compatibility"></a>

## バージョンと互換性

各ビューの名前にはバージョンが含まれます。`sql_v1_memories` はバージョン 1 です。列名、型、意味は公開された契約です。

- `v1` ビューには列が追加されることがあります。列が削除されたり名前が変更されたりすることはなく、列の型や意味が変わることもありません。
- この規則に反する変更は、`v1` と並べて `sql_v2_*` ビューとして提供されます。両方のビューは少なくとも 1 リリースの間提供され、リリースノートには廃止予定の `v1` ビューが明記されます。
- 列は名前で選択してください。`SELECT *` は追加された列も取得します。

<a id="views"></a>

## ビュー

すべてのビューは Loom のデータベース（`LOOM_CH_DATABASE` で別のデータベースを指定しない限り `loom`）にあり、ID 列 `org` と `namespace` を持ちます。`sql_v1_memories`、`sql_v1_memory_history`、`sql_v1_agent_events` は `agent`、`user_id`、`session_id` も持ちます。`sql_v1_conversations` は `agent` と `user_id` を、`sql_v1_usage_daily` は `agent` を持ちます。結合では、両側が持つ[ID タプル](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)のどの部分でも照合できます。

<a id="sql_v1_memories"></a>

### `sql_v1_memories`

メモリ（ID とキー）ごとに 1 行で、現在のバージョンを表します。削除または消去されたメモリは含まれず、拒否されたメモリも含まれません。それ以外のステータスは `status` 付きで一覧に出ます。`memory.list` は指定がない限り `duplicate`、`superseded`、`proposed` の行を隠すため、デフォルトの一覧と同じ結果を得るには `status = 'active'` で絞り込みます。`expires_at` を過ぎたメモリは、期限切れの掃除処理が実行されるまで、`memory.list` と同様に値付きで一覧に出ます。`memory.search` が返す結果にするには `(expires_at IS NULL OR expires_at > now())` を追加します。

| 列                                                                 | 意味                                                                                               |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| `org`, `namespace`, `agent`, `user_id`, `session_id`, `run_id`     | メモリの所有者と、それを書き込んだ実行（なしの場合は `-`）                                         |
| `memory_key`, `memory_id`, `memory_type`                           | キー、メモリの id（`version` と合わせて 1 つのバージョンを指す）、タイプ（`fact`、`decision` など）|
| `value`                                                            | `memory.get` が返す値。非公開の場合は NULL                                                         |
| `value_withheld`                                                   | `value` が返される場合は `''`、`high` の行は `sensitive`、秘匿化されたバージョンは `redacted`      |
| `content_sha256`                                                   | 値の SHA-256。`value` が NULL のときは常に NULL（短いシークレットのハッシュは推測できるため）      |
| `version`, `status`, `confidence`, `pinned`, `tags`                | `memory.get` と同じ                                                                                |
| `sensitivity`, `source_class`, `actor`                             | ラベル、出所クラス、書き込んだトークンのサブジェクト                                               |
| `created_at`, `updated_at`, `expires_at`, `valid_at`, `invalid_at` | 書き込まれた日時と最終更新日時、期限、事実が真になった日時と真でなくなった日時                     |

<a id="sql_v1_memory_history"></a>

### `sql_v1_memory_history`

すべてのメモリのバージョンごとに 1 行で、削除のバージョンも含みます（`status = 'tombstoned'`、`tombstoned_at` が設定されます）。拒否されたバージョンは含まれません。列は `sql_v1_memories` から `expires_at`、`valid_at`、`invalid_at` を除き、`tombstoned_at` を加えたものです。`value` と `content_sha256` は、秘匿化されたバージョンと、一度でも `high` とラベル付けされたキーのすべてのバージョンで NULL になります。感度の掃除処理が行にラベルを付ける前のバージョンには、ラベルのない同じ値が入っているためです。

<a id="sql_v1_conversations"></a>

### `sql_v1_conversations`

各ユーザーの保存済み会話ターンごとに 1 行で、列は `org`、`namespace`、`agent`、`user_id`、`thread_id`、`msg_id`、`role`、`content`、`tokens`、`run_id`、`ts`（ターンの最新の書き込み）です。削除された、または圧縮されて消えたターン（`tombstoned_at` が設定されたもの）は含まれず、消去されたターンは行ごとなくなります。ターンには感度ラベルがないため、`conversation.get` がスレッドのターンを返すのと同様に、`content` は書き込まれたとおりに返されます。スレッドをまたぐ一覧が行う、`high` に相当するテキストの検査は、SQL ではなく Loom 内で実行されます。

<a id="sql_v1_agent_events"></a>

### `sql_v1_agent_events`

リクエスト台帳で、op の呼び出しまたはワーカーの 1 パスごとに 1 行です。リクエストの場合、`kind` は `''` です。
列は `ts`、`org`、`namespace`、`agent`、`user_id`、`session_id`、`run_id`、`operation`、`kind`、`request_id`、`trace_id`、`latency_ms`、`success`、`error_code`、`status_class`、`memory_key`、`input_tokens`、`output_tokens`、`embedding_tokens`、`llm_calls`、`upstream_cost_usd`（モデルの費用、USD）、`bytes_written`、`model`、`consumer`、`subject`（呼び出したトークンのサブジェクト）、`transport` です。リクエストの引数は含まれません。消去されたエンドユーザーの行では、`user_id` が `<erased>` になります。

今日より前の UTC の丸 31 日間と、今日のこれまでの分を保持します。各ステートメントは自身の `now()` を読みます。ウィンドウの長さと UTC の日付への揃え方は `v1` 契約の一部です。

<a id="sql_v1_usage_daily"></a>

### `sql_v1_usage_daily`

同じウィンドウについて、`sql_v1_agent_events` を UTC の `day`、`org`、`namespace`、`agent`、`operation`、`kind` ごとに集計したものです。列は `ops`、`errors`、`input_tokens`、`output_tokens`、`embedding_tokens`、`llm_calls`、`upstream_cost_usd`、`bytes_written`、`latency_sum_ms`（`ops` と合わせて行をまたぐ平均を求められます）、そして `latency_p50_ms` と `latency_p95_ms` で、後の 2 つは近似値で行ごとの値です。行をまたいで合算することはできません。今日を除くすべての `day` は丸 1 日分です。

<a id="what-a-read-costs"></a>

### 読み取りのコスト

ビューは、呼び出し側のフィルタを返す行に対して実行します。そのため、`ts`、`day`、`thread_id`、`memory_key` でのフィルタは結果を絞りますが、読み取られる量は減りません。`sql_v1_memories`、`sql_v1_memory_history`、`sql_v1_conversations` の読み取りは、いずれもグラントのスコープ全体を読み取ります。履歴ビューはスコープ内のすべてのバージョンをソートし、会話ビューはスコープ内のすべてのターンを content も含めてグループ化します。これらはスコープに保存されている量に応じて増え、広いスコープでは後述の読み取り上限を超えることがあります。台帳ビューの読み取りは、いずれもウィンドウ期間のグラントのスコープ全体を読み取ります。範囲は最大でウィンドウが始まる月の初めからで、通常はウィンドウとほぼ同じです。より広いグラント（`*` 名前空間、`--all`）では、さらに多く読み取ります。この読み取りには SQL ユーザーの読み取り上限（`LOOM_CH_ANALYSIS_MAX_ROWS_TO_READ`、`_MAX_EXECUTION_S`、`_MAX_MEMORY_BYTES`、`_MAX_THREADS`）が適用されるため、負荷の高いスコープでは上限を超えることがあります。その場合は、グラントを絞るか、これらの設定（`insights.query` と共有）を引き上げて、もう一度 `loom sql-access grant` を実行してください。これにより、設定が SQL ユーザーのプロファイルに書き込まれます。ウィンドウより古いイベントは、このサーフェスにはありません。[分析エクスポート](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)は、日次の使用量合計をハッシュ化した名前で、オペレーターのバケットに書き出します。`loom.agent_events` 自体には、`LOOM_RETENTION_AGENT_EVENTS_DAYS` で設定しない限り TTL はありません。

<a id="what-the-views-never-show"></a>

## ビューが決して表示しないもの

メモリビューは、Loom 自身の一覧表示と同じルール（[機密度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）を適用します。会話ビューは、上記の `conversation.get` と同じ方法でターンを返します。次のものは、どのビューにも表示されません。

- `high` の行の値とそのハッシュ。現在のビューでも、履歴にあるそのキーのすべてのバージョンでも表示されません。
- redacted バージョンの値とそのハッシュ。
- 却下されたメモリ、削除または消去されたメモリ、削除された会話ターン。
- `loom.namespaces` または `loom.code_repos` で `restricted` に分類された名前空間の行。付与（grant）の内容にかかわらず、分類された時点から表示されません。Loom は[制限付き名前空間](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)の読み取りをそのつど監査しますが、ビューにはそれができないためです。
- ミラー（`acl-` に 24 桁の 16 進数が続く名前の名前空間）の行。付与の内容にかかわらず、表示されません。ミラーの各ドキュメントは、そのストアの権限リストが許可する読み取り側にのみ表示されますが（[ドキュメントソース](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md#the-mirror)）、ビューにはそれを確認できないためです。
- 5 つのビュー以外のテーブル。トークン、付与、シークレット、監査ログ、ドキュメント、埋め込み、リクエスト引数が該当します。
- 他のユーザーのステートメントを表示する `system` テーブル。`system.query_log`、`system.mutations`、`system.processes` などです。ClickHouse が付与のないユーザーにこれらを拒否するのは、`access_control_improvements.select_from_system_db_requires_grant` が有効な場合（デフォルト）に限られます。そのため `loom sql-access grant` は、パスワードを設定した各ユーザーとしてログインし、`system.processes` が応答した時点でそのユーザーを削除します。Loom のデータベースでは、`system.tables` に 5 つのビューだけが表示されます。

メモリビューと会話ビューは、[消去サーガ](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)が削除対象とし、その残存確認の集計が数えるテーブルを読み取ります。そのため、消去されたユーザーや名前空間の行は、テーブルとともにビューからも消えます。台帳ビューは行を保持しますが、そこにコンテンツは含まれません。ユーザーを消去すると、`loom.agent_events` と同様に、そのユーザーの `user_id` が `<erased>` に書き換えられます。

<a id="freshness"></a>

## 鮮度

ビューはクエリ時に ClickHouse を読み取り、何もコピーしません。Postgres はライブメモリを保持し（[ストレージの権限](../architecture/storage-authority.md)）、`loom.memory` と `loom.memory_versions` は各コミットの後に、`LOOM_PG_PUBLISH_INTERVAL_MS`（デフォルトは 250 ms）ごとのフラッシュで公開されるプロジェクションです。コミットされたメモリ書き込みは、そのフラッシュが完了すると `sql_v1_memories` と `sql_v1_memory_history` に反映されます。通常は 1 インターバル程度以内です。停止したライターが残した配信は、配信ワーカーが公開するため、より時間がかかることがあります。ビューは何も待ちません。`LOOM_PG_PROJECTION_WAIT_MS` を過ぎると `PROJECTION_BEHIND` を返す `memory.search` とは異なり、ビューは ClickHouse が保持している内容をそのまま返します。会話とリクエスト台帳は ClickHouse に直接書き込まれます。

<a id="access"></a>

## アクセス

この SQL サーフェスのユーザーは、5 つのビューに対する `SELECT` 以外の権限を持たない ClickHouse ユーザーで、設定プロファイル `loom_sql_surface` に属します。

- `readonly = 1`：ステートメントが変更できる設定は `final`、`join_use_nulls`、`group_by_use_nulls`、`transform_null_in` だけです。これらはどのビューの行も変えず、クライアントが自分で送信します（`pg_clickhouse` など）。それ以外の設定はビューのフィルタを途中で打ち切る可能性があります。たとえば overflow mode、`additional_table_filters`、`FINAL` 下の PREWHERE です。
- サーバーのデフォルトにかかわらず、プロファイルが固定する値：`apply_deleted_mask = 1` と `apply_patch_parts = 1`（削除された行は非表示のままになります）、`use_query_cache = 0` と `use_query_condition_cache = 0`（どちらもステートメントのテキストをキーにしており、そのテキストはすべてのユーザーで共有されます）、`use_skip_indexes_if_final_exact_mode = 1`、`do_not_merge_across_partitions_select_final = 0`、`optimize_move_to_prewhere_if_final = 0`（`FINAL` は各キーの最新バージョンを読みます）、すべての overflow mode を `'throw'` にすること（read と timeout およびその `_leaf` 形式、set、transfer、join、sort、distinct、group by、result）、`send_logs_level = 'none'`、イントロスペクション関数の無効化、そして `query_plan_filter_push_down = 0` と `enable_optimize_predicate_expression = 0` です。これにより、呼び出し側のフィルタはビューが返した行に対してのみ実行され、他のテナントの行には適用されません（値に対してエラーを起こすフィルタは、その値を引用してしまいます）。
- `insights.query` の上限。ただし結果の上限は除きます。SQL クライアントは結果セット全体を読むためです。対象は `LOOM_CH_ANALYSIS_MAX_EXECUTION_S`、`_MAX_ROWS_TO_READ`、`_MAX_MEMORY_BYTES`、`_MAX_THREADS` と、同時に実行できるステートメント数 `_MAX_CONCURRENT` です。

各ビューは定義者の権限（`SQL SECURITY DEFINER`、マイグレーションを実行したユーザー）で実行され、テーブル `loom.sql_access` が `currentUser()`（ステートメントを送信したユーザー）に許可している `(org, namespace)` の行だけを通します。許可された行を持たないユーザーは何も読めません。Loom 自身を含め、他のすべての ClickHouse ユーザーも同様です。

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
loom sql-access grant --user acme_bi --org acme --namespaces '*'   # every namespace of acme
loom sql-access grant --user ops_bi --all                          # the whole deployment
loom sql-access grant --user acme_bi --org acme --namespaces support --rotate
loom sql-access list
loom sql-access revoke --user acme_bi
```

- `grant` は、ユーザーが存在しなければ作成し、パスワードを一度だけ表示します。Loom はパスワードのコピーを保持しません。`--org` と `--all` は同時に指定できず、`--all` には `--namespaces` を指定できません。再度実行すると、ユーザーのスコープは指定した内容にちょうど置き換わります。指定されなくなった名前空間は、新しい行が読み取りを処理するレプリカに届いた時点で表示されなくなります。`revoke` はログインを即座に終了します。`--rotate` は新しいパスワードを表示します。パスワードの後のステップが失敗した場合、`grant` はその旨を表示します。そのときは `--rotate` を付けて再実行してください。
- `--all` は、データが 1 つの org に属するデプロイ向けです。共有デプロイでは、各テナントにそれぞれの org を許可してください。
- `grant` は、制限された名前空間を指定するスコープを拒否します。また、ワイルドカードがそのような名前空間に及ぶ場合も、コードリポジトリの設定だけが分類しているものに限り拒否します。ビューはその設定を読めないため、サーバーと同じ設定でコマンドを実行してください。すでに許可された範囲に含まれる名前空間があとで制限された場合、その名前空間はその許可のビューに含まれたままです。`list` はそのような許可をそれぞれ示し、名前を指定した他の名前空間に置き換えるよう促して、終了コード 2 で終了します。
- `revoke` は、まずスコープの行を削除し、その後でユーザーを削除します。許可が指定している名前空間は、`revoke` がその行を削除するまで使用中として扱われます。その名前は新しい所有者に割り当てられず、最初の書き込みは所有者なしで登録されます。`*` の許可は特定の名前空間を指定しません。org のすべての名前空間を読み、あとから作成されたものも含みます。したがって、サインアップが共有される org（`LOOM_SELF_SIGNUP_ORG`）には決して付与しないでください。
- このコマンドは `LOOM_CH_USER` として実行されます。このユーザーにはアクセス管理の権限が必要です。`CREATE USER`、`ALTER USER`、`DROP USER`、`CREATE SETTINGS PROFILE`、`ALTER SETTINGS PROFILE`、5 つのビューに対する `SELECT ... WITH GRANT OPTION`、`SHOW USERS`、および `system.users` と `system.settings_profile_elements` の読み取りです。`loom_sql_surface` プロファイルに属さない ClickHouse ユーザーがすでに使っている名前と、Loom 自身のユーザー（`LOOM_CH_USER`、リーダーユーザー、`loom_analyst`、`default`）は拒否されます。
- ユーザーの削除には `DROP USER` ではなく `revoke` を使ってください。スコープの行は `revoke` が書き換えるまで許可されたままで、同じ名前の後続ユーザーがビューに対する `SELECT` を持っていると、それらを読めてしまいます。

<a id="querying"></a>

## クエリ

組織自身の Postgres からクエリする場合は、[Postgres から Loom をクエリする](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)を参照してください。

```sql
SELECT memory_key, value, updated_at
FROM loom.sql_v1_memories
WHERE namespace = 'support' AND status = 'active'
ORDER BY updated_at DESC
LIMIT 20;

SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= toDate(now(), 'UTC') - 30
GROUP BY day ORDER BY day;
```
