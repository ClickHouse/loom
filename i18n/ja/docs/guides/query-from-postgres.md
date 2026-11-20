<!-- i18n: source=docs/guides/query-from-postgres.md blob=c49542f7fffa2ee2ced68d49d8ab0829854132f8 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md), which is authoritative.

<a id="query-loom-from-postgres"></a>

# Postgres から Loom にクエリする

このガイドでは、テナントのメモリ、会話、使用量を、組織自身の Postgres から読み取ります。Postgres 上ではアプリケーションのテーブルと結合できます。ここではオープンソースの拡張機能 [`pg_clickhouse`](https://github.com/ClickHouse/pg_clickhouse) を使います。この拡張機能は ClickHouse のテーブルとビューを Postgres の外部テーブルとしてインポートし、クエリを ClickHouse にプッシュダウンします。ビューが何を保持し、何を含まないかについては、[SQL サーフェスリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)を参照してください。

データは ClickHouse に残ります。Postgres からのクエリは、クエリ時にビューを読み取り、他の SQL クライアントと同じスコープ、非公開、鮮度のルールに従います。この Postgres はアプリケーションのものであり、Loom の[メモリ権威](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)ではありません。拡張機能は読み取りだけを行い、Loom がこれを通して書き込むことはなく、Loom 自身のデータベースに拡張機能は不要です。`LOOM_PG_DSN` が指すデータベースとは別のデータベース、または別のサービスを使ってください。

CI は、`.github/workflows/ci.yml` の `LOOM_PGCH_VERSION` が示すリリースを、Postgres 16 と 18 でテストしています（`tests/integration/test_pg_clickhouse_live_ch.py`）。

<a id="1-make-a-sql-user-for-the-tenant"></a>

## 1. テナント用の SQL ユーザーを作成する

Loom 側で、オペレーターとして実行します（[アクセス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)）。

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
```

ユーザーのパスワードは一度だけ表示されます。

<a id="2-install-the-extension"></a>

## 2. 拡張機能をインストールする

拡張機能は `ghcr.io/clickhouse/pg_clickhouse:<postgres major>-<version>` イメージと [PostgreSQL 拡張機能ネットワーク](https://pgxn.org/dist/pg_clickhouse/)にあり、セルフマネージドの Postgres に組み込めます（`pgxn install pg_clickhouse`）。マネージドの Postgres サービスでは、プロバイダーがこれを同梱し、許可している必要があります。関わるロールは3つです。

- この手順用のスーパーユーザー。拡張機能は trusted に指定されていないためです（マネージドサービスでは、プロバイダーが許可する拡張機能を作成できるロール）。
- 手順 3 と 4 用のオーナー。`CREATEROLE`、データベースへの `CREATE`、外部データラッパーへの `USAGE` を持つロールで、リーダーロールを作成するため、そのデフォルトも設定できます。
- 手順 5 用のアプリケーションのリーダーロール（ここでは `app_reader`）。手順 3 のユーザーマッピングと、手順 4 の最後のグラントが必要です。

スーパーユーザーとして実行します。

```sql
CREATE EXTENSION pg_clickhouse;
CREATE ROLE app_owner LOGIN CREATEROLE PASSWORD '<password>';
GRANT CREATE ON DATABASE app TO app_owner;
GRANT USAGE ON FOREIGN DATA WRAPPER clickhouse_fdw TO app_owner;
```

オーナーとして実行します。

```sql
CREATE ROLE app_reader LOGIN PASSWORD '<password>';
```

<a id="3-point-it-at-the-clickhouse-service"></a>

## 3. ClickHouse サービスを指定する

```sql
CREATE SERVER loom_ch FOREIGN DATA WRAPPER clickhouse_fdw
  OPTIONS (driver 'http', host 'abc123.us-east-1.aws.clickhouse.cloud', dbname 'loom');

-- The import in step 4 connects as the role that runs it; the queries in step 5 as theirs.
CREATE USER MAPPING FOR CURRENT_USER SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
CREATE USER MAPPING FOR app_reader SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
```

- `dbname` は Loom のデータベースです（`LOOM_CH_DATABASE`、デフォルトは `loom`）。
- ClickHouse Cloud のホストでは、拡張機能のデフォルト `secure 'auto'` により TLS が有効になり、`http` ドライバーのポートはデフォルトで 8443 になります。これは拡張機能のドキュメントに記載のとおりです。ここでのテストはローカルサーバーに対してプレーン HTTP で実行します。セルフマネージドのサーバーでは、`port` と `secure 'on'` をサーバーの TLS ポートに合わせて設定してください。
- テナントのデータを読み取ってよい Postgres ロールだけをマッピングしてください。ユーザーマッピングには ClickHouse のパスワードが含まれ、このサーバー経由のクエリはすべてその SQL ユーザーとして実行されます。

拡張機能のセッション設定をロールに設定すると、すべてのセッションが、サーフェスの SQL ユーザーが変更できる4つの設定だけを送信し、それ以外は送信しなくなります。

```sql
ALTER ROLE app_reader SET pg_clickhouse.session_settings =
  'join_use_nulls 1, group_by_use_nulls 1, final 1, transform_null_in 0';
```

これらは拡張機能のデフォルト値を明示したもので、後のリリースでデフォルトに設定が追加されても影響を受けません。別の設定を行うセッションは、ClickHouse で `READONLY` により失敗します。ロールの設定にかかわらず、SQL ユーザーのプロファイルがそれを拒否するためです（[アクセス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)）。

<a id="4-import-the-views"></a>

## 4. ビューをインポートする

```sql
CREATE SCHEMA loom;
IMPORT FOREIGN SCHEMA loom
  LIMIT TO (sql_v1_memories, sql_v1_memory_history, sql_v1_conversations,
            sql_v1_agent_events, sql_v1_usage_daily)
  FROM SERVER loom_ch INTO loom;
```

最初の `loom` は ClickHouse のデータベース、2つ目は Postgres のスキーマです。`LIMIT TO` でビューを名指しすると、インポートは `v1` のセットに固定され、後から追加される `sql_v2_*` ビューが意図せず取り込まれることはありません。続いて、リーダーロールに読み取りを許可します。

```sql
GRANT USAGE ON SCHEMA loom TO app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA loom TO app_reader;
```

`v1` のビューは、Loom のアップグレードで列が追加されることがあります。外部テーブルはインポート時の列を保持するため、新しい列を取り込むには、スキーマ（およびそのテーブルに基づく Postgres のビュー）を削除し、インポートとグラントをやり直します。`DROP SCHEMA loom CASCADE; CREATE SCHEMA loom;`

<a id="5-query-and-join"></a>

## 5. クエリと結合

```sql
-- The facts Loom holds about each account on the support team
SELECT a.email, m.memory_key, m.value, m.updated_at
FROM accounts AS a
JOIN loom.sql_v1_memories AS m ON m.user_id = a.loom_user_id
WHERE a.team = 'support' AND m.status = 'active'
ORDER BY m.updated_at DESC;

-- Model spend per day for the last month
SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= (now() AT TIME ZONE 'UTC')::date - 30
GROUP BY day ORDER BY day;
```

`value` は、Loom が非公開にしている場合は NULL になります。理由は `value_withheld` に示されます。

<a id="cost-and-scale"></a>

## コストとスケール

- `EXPLAIN (VERBOSE)` で、クエリのどの部分が ClickHouse で実行されるかを確認できます。外部テーブルとローカルテーブルの結合は Postgres で実行され、プッシュダウンされたフィルターを通過したビューの行がすべて取得されます。そのため、各ビューはクエリが許す限り絞り込んでください（名前空間、期間、ID のリテラルリストなど）。
- プッシュダウンされたフィルターは、ClickHouse が Postgres に送る量を絞りますが、読み取る量は絞りません。各ビューはグラントのスコープ全体を読み取り、台帳ビューは約 31 日のウィンドウを読み取ります（[読み取りのコスト](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#what-a-read-costs)）。
- SQL ユーザーの上限（`LOOM_CH_ANALYSIS_MAX_EXECUTION_S`、`_MAX_ROWS_TO_READ`、`_MAX_MEMORY_BYTES`）を超えるステートメントは、遅くなるのではなく ClickHouse で失敗し、Postgres がそのエラーを返します。
- マッピング経由でクエリする Postgres セッションはすべて同じ SQL ユーザーとしてログインし、そのユーザーが同時に実行できるステートメントは最大 `LOOM_CH_ANALYSIS_MAX_CONCURRENT` 件です。アプリケーションのリーダーセッションのプールはこれ以下のサイズにしてください。超えたステートメントは失敗します。

<a id="copies-and-erasure"></a>

## コピーと消去

外部テーブルはデータを保持しません。クエリのたびに ClickHouse を読み取るため、Loom での消去は即座に反映されます。この Postgres 内で取ったコピー（外部テーブルからの `CREATE TABLE … AS SELECT`、マテリアライズドビュー、ダンプ）は組織自身のデータであり、Loom の消去の対象外です。

<a id="revoking"></a>

## 取り消し

`loom sql-access revoke --user acme_bi` を実行すると、ユーザーマッピング経由のクエリがすべて即座に止まります。次のクエリはログインに失敗します。その後、オーナーとして `DROP SERVER loom_ch CASCADE` を実行すると、2つのユーザーマッピングとインポートした外部テーブルが削除されます。
