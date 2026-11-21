<!-- i18n: source=docs/features/tracker/record-contract.md blob=88cada3e321cc9c82e54440ec12b93a202fcc39a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md), which is authoritative.

<a id="tracker-record-contract"></a>

# トラッカーレコードコントラクト

`tracker.*` op、および `code.*` と `loom_search` のトラッカー部分は、チーム自身の ClickHouse から、読み取り専用の認証情報を使って、チームの issue、プルリクエスト、インシデントをライブで読み取ります。Loom はレコードをコピーしません。すべてのステートメントは、下記のテーブルとカラムからなる 1 つの論理モデル、つまり**コントラクト**に対して記述されます。

チームは、次の 2 つの方法のいずれかでスキーマを接続します。

| モード | 方法 |
| ------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| ビュー | チームがコントラクトのテーブル名とカラム名を持つビューを作成します。Loom はそれらをそのまま読み取ります。 |
| マッピング | Loom は認証情報で読み取れるテーブルを一覧表示し、マッピングを提案します。確定したマッピングはデータとして保存され、リモート側では何も作成されません。 |

マッピングはコントラクトの手前にある射影にすぎないため、どのソースに対しても同じステートメント、テスト、プライバシー述語が実行されます。コード上のコントラクトは `server/tracker/domain/contract.py`（`REQUIRED`、`OPTIONAL`、`FEATURES`、`ALIASES`）、マッピングは `server/tracker/domain/mapping.py`、テーブルごとに読み取るカラムは `server/tracker/domain/columns.py` の `COLUMNS_READ` です。

<a id="adding-a-tracker-source"></a>

## トラッカーソースの追加

1. **`readonly = 2` で、Loom 用の読み取り専用 ClickHouse ユーザーを作成します。** `readonly = 1` では、Loom がすべての読み取りに付与するクエリ単位の設定が拒否されるため、どの読み取りも実行されません（`CREDENTIAL_READONLY_MODE`）。

   ```sql
   CREATE USER loom_ro IDENTIFIED BY '…' SETTINGS readonly = 2;
   GRANT SELECT ON system.columns TO loom_ro;
   -- views mode: the views only
   GRANT SELECT ON loom_tracker.* TO loom_ro;
   -- mapping mode instead: each mapped table
   GRANT SELECT ON tracker_db.tickets TO loom_ro;
   ```

2. **views モードのみ：ビューを作成します。** データベース（例：`loom_tracker`）に `github_issues`、`github_prs`、`edges` を作成し、データがあるオプションのテーブルも追加します。ビューは `DEFINER = CURRENT_USER SQL SECURITY DEFINER` で作成してください。これにより、`loom_ro` に必要なのはビューへの `SELECT` だけで、背後のテーブルへの権限は不要です。通常のビューは読み取り側自身の権限で読み取るため、すべての読み取りが `ACCESS_DENIED` で失敗します。登録時のチェックがこれを検出し、修正方法を示します。ClickHouse 以外のソースは、`PostgreSQL`、`MySQL`、`S3` エンジンのテーブルの背後に置けます。`tracker.contract` は、すべてのテーブルとカラムについて保持する内容と、テーブルごとの空のビューテンプレートを返します。これには、項目の `id` 用の `loom_uuid5(name)` SQL 関数も含まれます。

3. **ソースを登録します。** 組織にサインインしているメンバーであれば誰でも、ダッシュボードの _Add a tracker_ フロー（[トラッカーの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#adding-a-tracker-from-the-dashboard)）または以下の op で登録できます。登録者は、そのトラッカーの名前空間の最初の `admin` になります。何かを書き込む前に、Loom は次の点を確認します。ホストが公開アドレスに解決されること（`HOST_REFUSED`）、認証情報が読み取り専用であること（`CREDENTIAL_NOT_READONLY`、`CREDENTIAL_READONLY_MODE`）、接続できること（`SOURCE_UNREACHABLE`）、スキーマまたはマッピングがコントラクトを満たすこと（`CONTRACT_FAILED`）です。読み取り専用とは、`SHOW GRANTS` と `SHOW GRANTS FINAL` が列挙するすべてのアクセス種別（付与されたロールの種別を含む）が、`SELECT`、データベース・テーブル・カラム・ディクショナリの `SHOW`、`dictGet`、`CREATE TEMPORARY TABLE` のいずれかであり、`WITH GRANT OPTION` や `WITH ADMIN OPTION` で付与されたものが（ネストされたロールも含め）ないことを意味します。それ以外の種別は拒否されます。ユーザーに付与されたロールはすべてデフォルトロールである必要があり、これによりチェックがロールの連鎖全体を確認できます。`SHOW GRANTS FINAL` を受け付けないサーバーは拒否されます（`SOURCE_UNREACHABLE`）。`dry_run: true` を指定すると、スキーマが不合格の場合は代わりにレポートが返され、不合格のテーブルごとに `CREATE OR REPLACE VIEW` の草案が付きます。

   | Op                           | 内容                                                                                                                                                                   |
   | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | `tracker.contract`           | 接続の前に、コントラクトとビューテンプレートを返します。                                                                                                                |
   | `tracker.source.inspect`     | 接続して、読み取り可能なすべてのテーブルをカラムとともに一覧し、マッピング案も示します。何も書き込みません。                                                                     |
   | `tracker.source.add`         | `name`、`host`、`port`（8443）、`secure`（true）、`database`、`user`、`password`、`namespaces`、オプションの `mapping`。`dry_run: true` はすべてのチェックを実行し、何も書き込みません。 |
   | `tracker.source.set_mapping` | 登録済みソースのマッピングを置き換えます（`dry_run` も可）。`mapping: null` で views モードに戻します。                                                                       |
   | `tracker.source.check`       | コントラクトチェックを今すぐ再実行します。                                                                                                                                         |
   | `tracker.source.rotate`      | 認証情報を置き換えます。                                                                                                                                                |
   | `tracker.source.remove`      | ソースとその名前空間を廃止します（`confirm`）。                                                                                                                              |
   | `tracker.source.list`        | 組織のすべてのソースについて、コントラクトの判定、ブレーカーの状態、名前空間、各名前空間での呼び出し元の立場を返します。認証情報は一切返されません。                                |

   `namespaces` の各要素は `{namespace, repos, restricted, join_policy}` です。`repos` は、その名前空間が読み取れる `source_id` の `Org/repo` プレフィックスです（一覧にないリポジトリは読み取られません）。`restricted` は、その名前空間を非公開の記録にします。`join_policy` は `open`（デフォルト）で、標準の名前空間では `approval` も指定できます。セルフサービス登録には `LOOM_TRACKER_ORG` が必要で、デフォルトで有効です（`LOOM_TRACKER_SELF_SERVICE_ENABLED`。組織あたり最大 `LOOM_TRACKER_SELF_SERVICE_MAX_SOURCES` 件、デフォルトは 10。ローカル開発用の ClickHouse には `LOOM_TRACKER_SELF_SERVICE_ALLOW_PRIVATE_HOSTS=true`）。パスワードは暗号化して保存され、返されることはありません。名前での `inspect` には、そのソースのいずれかの名前空間に対する `admin` が必要です。`set_mapping`、`rotate`、`remove` は、ソースのすべての名前空間の読み取り内容を変えるため、それをすべての名前空間に対して持っている必要があります。標準の名前空間は `admin` でのゲートにより判定され、制限付きの名前空間は、トークンがその名前空間を指定する必要がない点を除き、制限付きゲートのルールにより判定されます。登録者の権限が取り消された後は、ソースを登録しただけでは足りません（[制限付き名前空間](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)）。プラットフォーム管理者以外のダッシュボードセッションは `writer` トークンであるため、非公開（制限付き）の名前空間を持つソースを変更または廃止できません。それには `admin` ロールのトークンが必要で、オペレーターが `loom mint-token --roles admin` で発行します。ページにはこれらのボタンが理由付きで無効表示されますが、判断するのはサーバーです。

   オペレーターは、代わりに同じソースを設定ファイルで構成することもできます（[設定](#configuration)）。設定されたエントリは、同名の登録済みエントリより優先され、変更は設定でのみ行えます（`SOURCE_FROM_CONFIG`）。

4. **Loom が名前空間のディレクトリ行を作成します。** `kind = tracker`、非公開で、制限付きの名前空間は `classification = restricted` です。`join_policy` は、標準の名前空間では `open`（または `approval`）、制限付きの名前空間では `approval` になります。

5. **読み取り側が参加します。** オープンなトラッカー名前空間への `role: "viewer"` を指定した `namespace.join` は、サービスアカウントを含め、即座に承認されます。制限付きの名前空間は、その管理者の承認を待ち、読み取り側のトークンがその名前空間を指定する必要があります（[ID と名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)）。

6. **トラッカーをリポジトリにリンクします。** これにより、そのチェックアウトで作業するエージェントがトラッカーを読めるようになります。方法は、`tracker_namespaces` を指定した `code.repo.set_source`（リポジトリの `admin`）、ダッシュボードのリポジトリカード、またはオペレーター自身のリポジトリに限り `LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON` です。1 つのリポジトリに複数のトラッカーを順番にリンクでき、最初のものがプライマリで、その番号がリポジトリの issue 番号と PR 番号になります（[トラッカーのリンク](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#linking-a-tracker-to-a-repository)）。`tracker.binding` は、コード名前空間が呼び出し元に対して何に解決されるかを返します。

<a id="configuration"></a>

## 設定

同じエントリのオペレーター向けの形式：

```bash
LOOM_TRACKER_ORG=acme
LOOM_TRACKER_SOURCES_JSON='{
  "sources": {
    "widgets": {"host": "abc.clickhouse.cloud", "port": 8443, "secure": true, "database": "loom_tracker"}
  },
  "namespaces": {
    "tracker-widgets":         {"source": "widgets", "repos": ["acme/widgets"]},
    "tracker-widgets-private": {"source": "widgets", "repos": ["acme/widgets-private"], "restricted": true}
  }
}'
LOOM_TRACKER_CREDENTIALS_JSON='{"widgets": {"user": "loom_ro", "password": "…"}}'
LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON='{"code-widgets*": ["tracker-widgets", "tracker-helpdesk"]}'
```

認証情報はマップとは別の変数に置くため、シークレットを含めずにマップを表示したり差分を取ったりできます。バインディングの値は、1 つのトラッカー名前空間、または先頭をプライマリとする順序付きのリストです。キーはオペレーターのリポジトリのコード名前空間名に対する glob です（セルフサービスのリポジトリは、そのメンテナーがリンクします）。メンテナーが行う `code.repo.set_source` のリンクは、そのリポジトリについてはマップを置き換えます。トラッカー名前空間は `LOOM_TRACKER_ORG` に属します。これが空の場合、トラッカー名前空間は一切提供されません。

<a id="source-entry-options"></a>

### ソースエントリのオプション

ソースエントリには、次の項目も指定できます。

- `"mapping"`：保存済みのマッピング（[マッピングモード](#mapping-mode)）。指定がない場合はビューモードになります。
- `"checks": {"from": "<table or table function>", "repo": "Org/repo"}`：トラッカーの CI 結果が置かれている場所で、`tracker.flaky` と `tracker.test_signal` が読み取ります。解決順序は、このエントリ、次にソース自身の `checks` テーブル、最後に `LOOM_TRACKER_CHECKS_FROM` / `LOOM_TRACKER_CHECKS_REPO`（どちらもデフォルトは空で、CI チェックなしを意味します）です。テーブル関数を包んだビューではなく、テーブル関数そのものを指定してください。ビューのフィルターはスキャンの後にリモート側で実行されるためです。`repo` を指定しない場合、リポジトリが 1 つだけの名前空間はそのリポジトリを使います。
- `"bisect": {"code_prefixes": ["src/"]}`：bisect でソースコードとみなすパスです。指定がない場合は、docs、tests、CI、examples、benchmarks のいずれの配下でもないすべてのパスが対象です。トラッカーのないリポジトリでも、Loom 自身の PR 履歴に対して bisect を行います（その場合、回答には `candidates_from: "code_history"` が含まれます）。
- `"columns": {"<table>": {"<contract column>": "<your column>"}}`：お使いのテーブルが別の名前で持っているコントラクトのカラムです。たとえば `{"github_issues": {"triage_bisect_outcome": "bisect_result"}}` のように指定します。実際のカラムも、対応するコントラクトのカラムと同様に検査されます。使用できるのは単純な識別子のみです。

ルール：

- 未知のソースを指定した名前空間は提供されません（起動時にエラーを 1 行出力するだけで、推測はしません）。
- 各ソースは独自の接続、サーキットブレーカー、時間予算を持つため、あるチームのサービスが遅くなっても、他のチームのブレーカーが作動することはありません。
- 登録済みのソース（`loom.qa_sources`）は `LOOM_TRACKER_SOURCES_REFRESH_SECONDS`（60）秒ごとに再読み込みされます。同じプロセス内での書き込みは、すぐに反映されます。

トラッカーの全設定は[設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker)にあります。

<a id="public-and-private"></a>

## 公開と非公開

トラッカーの名前空間は **standard** または **restricted** のいずれかで、Loom がステートメントごとに強制します。

- standard 名前空間では、すべてのステートメントに `is_private = 0` が付与されます。非公開データのみを保持するテーブル（`core_incidents`、`support_cases`、`slack_threads`）は、読み取りの前に拒否されます。
- restricted 名前空間ではこの述語が外れ、その名前空間自身の `repos` のみを読み取ります。アクセスは restricted 名前空間のルールによって制御され、その名前空間を指定するトークンと直接の許可（grant）が必要です。

次の 3 つの構成が使えます。

- `is_private` 列を持つ 1 組のビューを用意し、両方の名前空間から読み取る構成（読み取り専用ユーザーに対して `is_private` の行ポリシーを設定すると、ClickHouse 側でも確認が加わります）。
- 2 組のビューと 2 人のユーザーを用意し、一方は公開行のみを保持させ、2 つのソースとして登録する構成。
- restricted 名前空間のみの構成。

<a id="the-contract"></a>

## 契約

型は**ファミリー**単位で比較します。`String`、`LowCardinality(String)`、`Nullable(String)`、`FixedString`、`Enum` は同一ファミリー、`DateTime`、`DateTime64`、`Date` は同一ファミリー、`UInt*` と `Int*` の各型と `Bool` は同一ファミリーです。`Array(x)` は `x` で比較します。別のストアに対するビューは、コーデックや null 許容性が異なっていてもかまいませんが、構造が異なってはいけません。ビューテンプレートでは、各ファミリーを最も幅の広い型（`UInt64`、`DateTime64(3)` など）で記述します。

<a id="required-tables"></a>

### 必須テーブル

これらがないソースは提供されません。

**`github_issues`**：issue ごとに 1 行です。

| カラム                                                                                       | ファミリー    | 意味                                                                                                                         |
| -------------------------------------------------------------------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_issue:' \|\| source_id)`（プライベート行では `github_issue_private:`）。`edges` が使うキーです |
| `source_id`                                                                                  | String        | `Org/repo#number`。リポジトリは tracker 名前空間に列挙されているものでなければなりません                                     |
| `number`                                                                                     | UInt          | issue 番号                                                                                                                   |
| `url`, `title`, `body`, `author`                                                             | String        | 人が開けるリンク、すべての検索が読むタイトル、重複チェックが読むテキスト、issue を作成した人                                 |
| `labels`, `assignees`                                                                        | Array(String) |                                                                                                                              |
| `primary_component`, `severity_level`                                                        | String        | チームの分類体系。ない場合は `''`                                                                                            |
| `state_reason`                                                                               | String        | `completed`、`not_planned`、`duplicate`、または `''`                                                                         |
| `affected_versions`, `fix_versions`                                                          | Array(String) | issue が影響するバージョン、修正を含むバージョン                                                                             |
| `is_flaky_test`, `is_crash_ci`, `is_performance`                                             | UInt          | 0/1 のフラグ。不明な場合は `0`                                                                                               |
| `has_test`                                                                                   | UInt          | 関連するテストがあるか。不明な場合は NULL                                                                                    |
| `error_code`                                                                                 | UInt          | 報告が示すサーバーのエラーコード。ない場合は `0`                                                                             |
| `is_private`                                                                                 | UInt          | プライベートリポジトリの行では `1`                                                                                           |
| `created_at`, `updated_at`, `closed_at`                                                      | DateTime      | `closed_at` は未クローズの間はエポック 0                                                                                     |
| `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | String        | トリアージボットの判定。tracker にない場合は `''` として提供されます                                                         |
| `triage_reproduces_on`, `triage_clean_on`                                                    | Array(String) | 同様に、ない場合は `[]` として提供されます                                                                                   |

**`github_prs`**：プルリクエストごとに 1 行です。

| カラム                                                                                       | ファミリー    | 意味                                                                       |
| -------------------------------------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------- |
| `id`                                                                                         | UUID          | `uuid5(NAMESPACE_DNS, 'github_pr:' \|\| source_id)`（`github_pr_private:`） |
| `source_id`, `number`, `url`, `title`, `body`, `author`                                      | 上記と同じ    |                                                                            |
| `pr_number`                                                                                  | UInt          | `number` と同じ                                                            |
| `primary_component`, `severity_level`, `is_private`, `created_at`, `updated_at`, `closed_at` | 上記と同じ    |                                                                            |
| `state`                                                                                      | String        | `open`、`closed`、または `merged`                                          |
| `is_merged`, `has_test`, `review_requests_test`                                              | UInt          |                                                                            |
| `test_types`, `reviewers`, `src_components`                                                  | Array(String) |                                                                            |
| `ci_result`, `changelog_category`, `minor_version`, `base_branch`                            | String        |                                                                            |
| `is_backport_pr`, `is_cherrypick_pr`, `is_sync_pr`                                           | UInt          | 派生 PR。アクティビティの読み取りからは除外されます                        |
| `upstream_pr_number`                                                                         | UInt          | バックポートが繰り返す元の PR。ない場合は `0`                              |
| `additions`, `deletions`, `time_to_merge_days`                                               | UInt          |                                                                            |

**`edges`**：アイテム間のリンクです。

| カラム                 | ファミリー | 意味                                                                                                      |
| ---------------------- | ---------- | --------------------------------------------------------------------------------------------------------- |
| `from_id`, `to_id`     | UUID       | 上記のアイテム id                                                                                         |
| `from_type`, `to_type` | String     | `github_issue`, `github_pr`, `github_issue_private`, `github_pr_private`, `core_incident`, `support_case` |
| `rel_type`             | String     | `fixes`, `related_to`, `duplicate_of`, `caused_by`, `backport_of`, `reverts`, …                           |
| `created_at`           | DateTime   |                                                                                                           |

Loom はアイテムの `id` をローカルで導出し、それをキーに `edges` を結合するため、ビューはまったく同じ値を生成する必要があります。ClickHouse には `uuid5()` がありません。`tracker.contract` が返す `loom_uuid5` 関数は、RFC 4122 のバージョン 5 の構成を SQL で実装したもので、Python の `uuid.uuid5` と同じ値になります。

<a id="optional-tables"></a>

### 任意テーブル

テーブルがない場合、それを必要とする op は `not_provided` を返します。テーブルはあっても最小カラム（`contract.py` の `OPTIONAL`）のいずれかが欠けている場合は `incomplete` と報告され、ないものとして扱われます。

| テーブル                                     | 最小カラム                                                                                                                                                    |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `core_incidents`                             | `id`, `source_id`, `title`, `body`, `primary_component`, `severity_level`, `stack_functions Array(String)`（スタックフレームの照合）, `error_code`, `created_at` |
| `support_cases`                              | `id`, `source_id`, `title`                                                                                                                                    |
| `slack_threads`                              | `source_id`, `title`, `body`                                                                                                                                  |
| `component_risk_snapshot`                    | `computed_at`, `component`                                                                                                                                    |
| `release_items`                              | `release_version`, `issue_number`                                                                                                                             |
| `release_health_snapshot`                    | `computed_at`, `release_version`                                                                                                                              |
| `version_history`, `version_history_private` | `commit_sha`, `version`                                                                                                                                       |
| `checks`                                     | `check_name`, `test_name`                                                                                                                                     |
| `cloud_instances`                            | `instance_id`                                                                                                                                                 |
| `rel_instance_mentions`                      | `from_id`, `instance_id`                                                                                                                                      |
| `slack_incidents`                            | `incident_id`                                                                                                                                                 |

`core_incidents`、`support_cases`、`slack_threads` は、制限付きの名前空間を通してのみ読み取られます。

<a id="feature-columns"></a>

### 機能カラム

必須テーブル上の任意カラムで、存在するか存在しないかが報告されます。ソースにない機能カラムは、そのエイリアス、なければデフォルトとして読み取られます（[エイリアス](#aliases)を参照）。それを読むステートメントは、そのカラムを持たない行に対するものとして応答し、機能は存在しないままになります。

| 機能         | カラム                                              | 有効になるもの                                                                    |
| ------------ | --------------------------------------------------- | --------------------------------------------------------------------------------- |
| `bisect`     | `github_prs.diff`, `github_prs.affected_file_paths` | bisect 候補と原因コミットのスキャン                                               |
| `merge_time` | `github_prs.merged_at`                              | マージ時間に関する事実                                                            |
| `vector`     | `github_issues.embedding`, `github_prs.embedding`   | 報告のみ。`tracker.similar` は Loom 自身が計算する埋め込みを使用します            |

<a id="aliases"></a>

### エイリアス

ソースに契約のカラムがない場合でも、`contract.py` の `ALIASES` が、そのソースにあるカラム上で受け入れ可能な式を指定していれば、契約を満たします。チェックはエイリアス自身のカラムをファミリーで検証し、`aliased` として報告します。ステートメントは引き続き契約上の名前を読みます。テーブル参照が、エイリアスを追加する射影に置き換えられるためです（クエリアナライザーが、ステートメントが読むカラムに絞り込みます）。自身のカラムを持たないエイリアスは**デフォルト**（`defaulted`）です。すべての行が `''`、`0`、`[]`、`NULL` のいずれかとして読まれ、レポートにもその旨が示されるため、デフォルトがデータと取り違えられることはありません。

| テーブル                                         | カラム                                                                                       | 受け入れられる形式（順序どおり）                                           |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `github_issues`                                  | `severity_level`                                                                             | `severity`                                                                 |
| `github_issues`                                  | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)`（JSON の `metadata` カラム）、なければ `NULL` |
| `github_issues`                                  | `triage_bisect_outcome`, `triage_introduced_commit`, `triage_component`, `triage_confidence` | `''`                                                                       |
| `github_issues`                                  | `triage_reproduces_on`, `triage_clean_on`                                                    | `[]`                                                                       |
| `github_prs`                                     | `has_test`                                                                                   | `toUInt8(metadata.has_test.:Bool)`、なければ `toUInt8(tests_added > 0)`   |
| `github_prs`                                     | `upstream_pr_number`                                                                         | `ifNull(toUInt32(metadata.original_pr_number.:Int64), 0)`、なければ `0`   |
| `github_prs`                                     | `review_requests_test`                                                                       | `NULL`                                                                     |
| `github_prs`                                     | `test_types`, `src_components`                                                               | `[]`                                                                       |
| `github_prs`                                     | `merged_at`                                                                                  | `if(is_merged = 1, closed_at, toDateTime64(0, 3))`                         |
| `github_prs`, `core_incidents`                   | `severity_level`                                                                             | `''`                                                                       |
| `qa_bugs`（回答ルートのクロスソースビュー）      | `severity_level`, `has_test`                                                                 | `severity`、`toUInt8(metadata.has_test.:Bool)`、なければ `NULL`            |

契約に載っていないリネームは、依然としてカラム欠落として扱われます。ソースエントリの `columns` マップ（[ソースエントリのオプション](#source-entry-options)）で宣言するか、`contract.py` に `Alias` を追加して、すべてのソースで別の表記を受け入れるようにしてください。

<a id="mapping-mode"></a>

## マッピングモード

マッピングは、論理テーブルごとに、チームのどのテーブルを読み取るかを指定し、論理カラムごとに、そのテーブルのカラムに対する式を指定します。

```json
{
  "version": 1,
  "tables": {
    "github_issues": {
      "source": "tracker_db.tickets",
      "final": true,
      "columns": {
        "source_id": { "concat": [{ "lit": "acme/widgets#" }, "ticket_no"] },
        "number": "ticket_no",
        "title": "summary",
        "body": "description",
        "created_at": "opened_at",
        "state_reason": { "lower": "resolution" }
      }
    }
  }
}
```

- **式はデータであり、SQL ではありません**：実テーブルが持つカラム名（または `{"col": …}`）、`{"lit": …}`、あるいは `concat`、`coalesce`、`eq`、`in`、`has`、`cast`（`[expr, family]`）、`lower`、`digits`（キー末尾の数字。`PROJ-1234` → `1234`）のいずれかです。Loom は識別子をバッククォートで、リテラルを引用符で囲んで式を生成し、各式をステートメントが読み取るファミリーに変換して、読み取り専用の認証情報で実行します。
- `source` は `db.table` で指定します。トラッカーのデータベース内のテーブルであれば `table` だけで指定できます。`final: true` を指定すると、テーブルを `FINAL` 付きで読み取ります（Replacing、Collapsing、Aggregating、Summing の各エンジンで提案されます）。
- Loom は、マッピングされた `source_id` から各アイテムの `id` を、マッピングされた `from_ref` / `to_ref`（`Org/repo#number`）からエッジの `from_id` / `to_id` を導出します。これらはマッピングしません。
- マッピングする各テーブルには、コアカラムが必要です。`github_issues` は `source_id`、`number`、`title`、`created_at`、`github_prs` は `source_id`、`pr_number`、`title`、`created_at`、`edges` は `from_ref`、`to_ref`、`from_type`、`to_type`、`rel_type`、`core_incidents` は `source_id`、`title`、`created_at` です。マッピングでは `github_issues` を必ずマッピングしてください。マッピングされていないカラムにはデフォルト値が入るか、値が導出されます（PR の `state` はマージ済みとクローズ済みのフィールドから導出されます）。レポートにはそれらが一覧表示されます。マッピングで指定されていない論理テーブルは、空のテーブルとして読み取られます。
- レポートの capabilities（`items`、`duplicate_check`、`duplicate_calibration`、`pull_requests`、`links`、`severity_board`、`bisect`、`incidents`、`ci_checks`）は、マッピングされたデータで Loom が何に答えられるかを示します。

読み取り時には、各論理テーブルへの参照がマッピングされたプロジェクションに置き換えられるため、すべてのソースに対して同じステートメント、テスト、プライバシー述語が実行されます。

<a id="checking-a-source"></a>

## ソースの確認

```bash
loom tracker-contract-check                           # every configured source
loom tracker-contract-check --source widgets          # one source
loom tracker-contract-check --namespace tracker-widgets
loom tracker-contract-check --json
```

サーバーは、プロセスごとの最初のトラッカー読み取り時に同じチェックを実行します。ソース自身の認証情報でスキーマを読み取り（`system.columns`、それが非表示の場合はテーブルごとの `DESCRIBE`）、結果を `LOOM_TRACKER_CONTRACT_TTL_S`（3,600 秒）のあいだキャッシュします。失敗した結果は 60 秒だけキャッシュするため、修正したビューは 1 分以内に反映されます。チェックに失敗したソースは、不足している内容を示す `tracker_unavailable` をすべての読み取りに返し、部分的な応答は返しません。`tracker.source.check` を使うと、チェックをオンデマンドで実行できます。

<a id="related"></a>

## 関連ドキュメント

- [トラッカーの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)：各 op が答える内容、リンク、ダッシュボードのフロー
- [履歴と PR](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)：トラッカーを読み取るコードプレーンの op
- [設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#tracker)
- [操作リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#tracker)
