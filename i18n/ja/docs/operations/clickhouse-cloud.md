<!-- i18n: source=docs/operations/clickhouse-cloud.md blob=3fc467ae1903f7f94d3cdaccb6831c92c883fd58 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md), which is authoritative.

<a id="clickhouse-cloud-and-managed-postgres"></a>

# ClickHouse Cloud と Managed Postgres

このガイドでは、2 つのマネージドサービス上の Kubernetes で Loom を実行します。プロジェクション、ベクトル、履歴、分析には ClickHouse Cloud を使い、メモリのオーソリティには ClickHouse Managed Postgres を使います。扱う内容は、各サービス、チャートの `postgres` 値、インストール後の確認、Postgres パスワードと CA のローテーション、SQL サーフェスユーザーです。チャート自体については [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) を、オーソリティについては [Postgres メモリオーソリティ](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) を参照してください。

オーバーレイ `deploy/helm/loom/values-clickhouse-cloud.yaml` は EKS 用のオーバーレイに重ねて使います。これは空の ClickHouse へのデプロイ向けです。オーソリティはデプロイの初回起動時に決まります。すでにメモリを保持している ClickHouse では Pod の起動が拒否されます。ClickHouse オーソリティから Postgres へメモリを移行する手段はないためです。

<a id="the-services"></a>

## サービス

- **ClickHouse Cloud**：`loom` という名前のデータベースと Loom 用のユーザーを持つサービス。IP アクセスリストでクラスターの NAT アドレスを許可します（[Kubernetes、EKS の場合](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md#on-eks)）。
- **ClickHouse Managed Postgres**：**2 台の HA スタンバイ**、データベース、そのデータベースを所有するロールを持ち、ポート 5432 の**ダイレクト**エンドポイントを使うサービス（[Postgres、ClickHouse Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#clickhouse-managed-postgres)）。Loom がどのスタンバイが接続されているかを確認できるように、ロールに `pg_read_all_stats` を付与します。

  ```sql
  GRANT pg_read_all_stats TO loom;
  ```

<a id="secrets"></a>

## シークレット

オーバーレイは `loom-secrets` のほかに、2 つの Secret を読み込みます。ロールのパスワードを格納した `loom-postgres`（キーは `password`）と、CA バンドルを格納した `loom-postgres-ca`（キーは `ca.crt`）です。どちらも `loom-secrets` と同じ方法で作成し、それぞれに専用の ExternalSecret を用意します。

`ca.crt` には、サービスの CA が現在公開しているすべてのルート証明書を PEM 形式で連結して格納します。ドライバーはこのファイルをバンドルとして読み込み、それ以外は信頼しません。ルート証明書は CA が公開している一覧から取得します（サーバー証明書の "CA Issuers" の URL が CA を示します）。`openssl s_client -starttls postgres -showcerts` は通常、ルートを含まないチェーンを出力します。DER 形式で取得したルート証明書は、先に変換します。

```bash
openssl x509 -inform DER -in root.der -out root.pem
```

`root.pem` のファイルを連結して `ca.crt` を作成します。これが Secret に格納するファイルです。サーバーが送信するチェーンに対して、ドライバーと同様にバンドル以外を一切信頼せずに検証します。`leaf.pem` は `s_client -showcerts` が最初に出力する証明書、`chain.pem` はそれ以降の証明書です（`-no-CAstore` には `openssl` のバージョン 3 が必要です）。

```bash
openssl verify -no-CApath -no-CAstore -CAfile ca.crt -untrusted chain.pem \
  -verify_hostname <postgres host> leaf.pem
```

バンドルに含まれない証明書は検証に失敗して接続が拒否されます（フェイルクローズ）。この状態で起動した Pod は起動に失敗し、ログに `SSLCertVerificationError` が出力されます。稼働中の Pod では、新しい接続がすべて失敗します。CA が階層の変更を告知したら、新しい Secret 名でバンドルを更新します（後述）。

<a id="install"></a>

## インストール

```bash
helm install loom deploy/helm/loom -n loom \
  -f deploy/helm/loom/values-eks.yaml -f deploy/helm/loom/values-clickhouse-cloud.yaml \
  --set publicBaseUrl=https://loom.example.com \
  --set clickhouse.host=<service>.<region>.aws.clickhouse.cloud \
  --set postgres.host=<postgres direct host>
```

`postgres.host` を設定すると、チャートは Loom を実行するすべての Pod（web、ワーカー、フック Job、分析エクスポート）に次の設定を渡します。

- `LOOM_STORAGE_AUTHORITY=postgres`、および `postgres.durability` と `postgres.poolMaxSize` から設定される `LOOM_PG_DURABILITY` と `LOOM_PG_POOL_MAX_SIZE`
- パスワードを含まない `LOOM_PG_DSN`（`postgres://<user>@<host>:<port>/<database>?sslmode=verify-full&sslrootcert=/etc/loom/pg-ca/<key>`）。これによりパスワードが Pod の仕様に含まれることはありません
- `postgres.passwordSecret` から設定される `PGPASSWORD`。DSN にパスワードがない場合、ドライバーがこれを読み取ります
- `/etc/loom/pg-ca` に読み取り専用でマウントされる `postgres.caSecret`
- `networkPolicy.enabled` が有効な場合、`postgres.port` への egress

チャートは、そのまま渡すことができない `postgres` ブロックのレンダリングを拒否します。また、レンダリングされる名前が `env`、`extraEnv`、`secrets.extra`、`settingsFile` で重ねて設定されている場合も拒否します。拒否の際には修正すべき値の名前が示され、ルールは `deploy/helm/loom/values.yaml` にあるこのブロックのコメントに記載されています。チャートの値は、`existingSecret` や `envFrom` にある同じ名前の値より優先されます。

<a id="connections"></a>

### 接続

各 Loom プロセスは最大 `postgres.poolMaxSize` 個の接続を保持します。すべての接続が使用中の場合、呼び出しは `LOOM_PG_STATEMENT_TIMEOUT_MS` まで接続の空きを待ち、その後 503 を返します。ロールアウト中は旧 Pod と新 Pod が並行して稼働するため、サービスは定常時の接続数の 2 倍を想定してサイズを決めてください。

```text
poolMaxSize × (webWorkers × web pods + worker processes) × 2
  + booting processes + running erasures + CLI runs  ≤  max_connections − reserved
```

web Pod の数は、オートスケーリングが有効な場合は `autoscaling.maxReplicas`、無効な場合は `replicaCount` です。ワーカープロセスの数は、`workers.enabled` が有効な場合は `workers.replicaCount`、無効な場合は 1 で、これは web Pod が実行します。実行中の消去は、Postgres のステップ用にそれぞれ接続を 1 つ追加で保持します。オーバーレイは `webWorkers` を 2（チャートの 2 CPU 制限に対してイメージが選ぶ値）、`poolMaxSize` を 5 に設定します。web Pod が 3 つ、workers Pod が 1 つの場合は 70 接続となり、`max_connections` で Loom 用に 100 を確保できるサービスに収まります。`webWorkers` を増やす場合は、それに合わせて `resources.limits.cpu` とメモリ（プロセスあたり 1 GiB）も増やす必要があります。チャートの指定数を超えて他の手段で追加されたレプリカは、この計算の対象外です。

<a id="after-the-install"></a>

## インストール後

- サービスの `synchronous_standby_names` が空の場合、Loom は起動を拒否します。指定された数よりも少ないスタンバイしか接続されていない場合は、WARNING `Postgres durability: fewer synchronous standbys are attached than required` をログに出力して起動します。その後、コミットは最大で `LOOM_PG_STATEMENT_TIMEOUT_MS` に 1 秒を加えた時間だけスタンバイを待ち、503 `POSTGRES_UNAVAILABLE` と `outcome_unknown` を返します（[起動時のチェック内容](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md#what-the-boot-checks)）。
- `GET /v1/health` は `storage_authority: postgres` と `pg_ok` を返します。`GET /v1/health?include=postgres` を使うと、`standby: synchronous` を含む `postgres` ブロックが追加されます。`pg_ok` が false の場合、またはチェックの `SELECT 1` が何らかの理由で失敗した場合や 2 s を超えた場合は、このブロックは含まれません。
- `pg_read_all_stats` を持たないロールでも起動はしますが、起動時に WARNING `Postgres durability: cannot see pg_stat_replication; standbys are unverified` が出力され、`standby` は `null` になります。確実な目印となるのはこの WARNING です。`standby` は、その読み取りが失敗した場合や 2 s を超えた場合にも `null` になります。

<a id="rotating-the-password-and-the-ca"></a>

## パスワードと CA のローテーション

パスワードは、次の順序で続けて実施します。

1. シークレットストアのパスワードを更新し、`loom-postgres` が同期されたことを確認します。
2. サービスに対して `ALTER ROLE loom PASSWORD '…'` を実行します。
3. web と workers の Deployment に対して、まとめて `kubectl rollout restart` を実行します。

すでに開いている接続は引き続き動作します。手順 2 から手順 3 の間は、新しい接続を開く必要がある Pod（プールは 300 s アイドル状態の接続を閉じます）の呼び出しが 500 を返し、サーバーログに `InvalidPasswordError`（SQLSTATE 28P01）が出力されます。Pod を再起動するまで、再試行しても解消しません。手順 1 から手順 2 の間に起動した Pod（スケールアップ、退避、分析エクスポート、フック Job など）は、手順 2 まで起動に失敗し、次の再起動で起動します。

CA については、新しいバンドルを新しい Secret 名で作成し、`postgres.caSecret.name` にその名前を設定して `helm upgrade` を実行します。Pod の spec が変わるため、Pod はローリング更新されます。古い Secret は、ロールアウトが完了し、ロールバック先になりうるどのリビジョンもその Secret を参照しなくなってから削除してください。

<a id="the-sql-surface-user"></a>

## SQL サーフェスのユーザー

組織は、`loom sql-access grant` が作成する ClickHouse ユーザーを通じて、エージェントのメモリを SQL として読み取ります（[SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)）。このコマンドは、ユーザーを作成できる ClickHouse の管理者資格情報を使って web ポッド内で実行します。管理者パスワードはファイルから渡し、入力が表示される端末で直接入力しないでください。

```bash
web=$(kubectl get deploy -n loom -o name \
  -l app.kubernetes.io/instance=loom,app.kubernetes.io/component=web)
kubectl exec -i -n loom "$web" -- sh -c \
  'read -r LOOM_CH_PASSWORD; export LOOM_CH_PASSWORD LOOM_CH_USER=default;
   exec loom sql-access grant --user acme_sql --org acme --namespaces support,research' < admin.pass
```

このコマンドは SQL ユーザーのパスワードを一度だけ、端末にのみ出力します。exec ストリームはコンテナーログではありません。`--namespaces '*'` は、後から作成される名前空間も対象にします。サインアップで共有される組織には、これを指定しないでください（[アクセス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)）。`--org … --namespaces …` の代わりに `--all` を使う方法は、データが単一の組織に属するデプロイメントに適しています。アンインストールする前にユーザーを取り消してください。同じ方法で `loom sql-access revoke --user acme_sql` を実行します。

<a id="restoring-a-service"></a>

## サービスの復元

どちらのサービスを復元した場合も、その後に `loom reconcile` を実行します。これは、リリースを停止した状態で、web Pod の spec から作成した Job の中で実行します（[1 つのストアの復元](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md#restoring-one-store)）。

<a id="related"></a>

## 関連ドキュメント

[Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Postgres メモリオーソリティ](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) ·
[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) ·
[Postgres から Loom にクエリする](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md)
