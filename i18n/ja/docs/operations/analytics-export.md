<!-- i18n: source=docs/operations/analytics-export.md blob=241a7492fe019249ba054a278a0059bcb928b7e0 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/analytics-export.md), which is authoritative.

<a id="exporting-usage-to-a-data-warehouse"></a>

# 使用状況のデータウェアハウスへのエクスポート

`loom export-analytics` は、UTC の1日分の使用状況と名前空間のスナップショットを、Parquet ファイルとして S3 バケットに書き出します。ファイルはエンティティごと、日ごとに1つで、データウェアハウスが読み込むためのものです。Helm チャートは、これを CronJob として1日1回実行します（`values.yaml` の `analyticsExport`）。テナントのメモリを内容ごと、またリクエスト台帳をその場でクエリするには、代わりに [SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)を使用してください。

```bash
loom export-analytics --bucket analytics-landing --prefix loom \
  --role-arn arn:aws:iam::123456789012:role/loom-analytics-writer \
  --expected-bucket-owner 123456789012
```

`LOOM_ANALYTICS_EXPORT_NAME_KEY` が必要です。これは名前のハッシュ化に使用するシークレットです（[ハッシュ化された名前](#hashed-names)を参照）。

`--day YYYY-MM-DD` を指定すると別の日をエクスポートします。デフォルトは前日（UTC）です。まだ終わっていない日は拒否されます。

<a id="files"></a>

## ファイル

```text
<prefix>/<entity>/<yyyy>/<mm>/<dd>/<entity>-<yyyy>-<mm>-<dd>.parquet    entity: usage, namespaces
```

各アップロードには `If-None-Match: *` が付くため、オブジェクトが置き換えられることはありません。同じ日を 2 回実行すると、1 回目のファイルが保持され、ファイルごとに `already there, kept` が出力されます。失敗した実行や重複した実行は、そのまま再試行できます。ある日を再びエクスポートするには、先にその日のオブジェクトを削除してください。

| エンティティ | 1 行の単位                                                                                                          | カラム                                                                                                                                                                                                                                   |
| ------------ | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `usage`      | その日の `org`、`namespace`、`agent`、`operation`、`kind`、`transport`、`traffic_class` ごと（`loom.agent_events` から） | `day`, `ops`, `errors`, `server_errors`, `slow`, `input_tokens`, `output_tokens`, `embedding_tokens`, `llm_calls`, `cost_usd`, `bytes_written`, `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`, `users`, `sessions`, `snapshot_at` |
| `namespaces` | `loom.namespaces` に登録されている、または有効なメモリ行を持つ `org`、`namespace` ごと                              | `registered`, `kind`, `visibility`, `active`, `created_on`, `live_memories`, `last_memory_on`, `day`, `snapshot_at`                                                                                                                      |

`usage` の `kind` は、リクエストなら `request`、ワーカーの処理なら `worker`、遅延実行される `memory.ask` の整合性チェックなら `verdict`、遅延実行されるコンフリクト解決なら `settle` です。`/v1/me/*` のルートはすべて 1 つの `operation`、`me` として扱われます。台帳ではこれらの行にリクエストパスの名前が付きますが、パスには呼び出し元が送った任意の文字列が入り得るためです。`users` と `sessions` は行内の重複を除いた件数で、名前を指定しなかった呼び出しの `-` は含まれません。行をまたいで合計すると、1 人のユーザーが行ごとに 1 回ずつ数えられます。`slow` とレイテンシの分位数は SLI ロールアップのものです。ロールアップが読む `ok` のリクエスト（SLO クラスを持つ operation）を対象とし、`slow` はレイテンシ予算を超えたものを数えます（予算が 0 の場合はすべてが slow になります）。該当するリクエストがない行のレイテンシは 0 です。`/v1/me/*` のルート、遅延実行される判定、ワーカーの処理は、どちらにも数えられません。`namespaces` ファイルは `--day` の指定にかかわらず `snapshot_at` 時点の状態です。過去の日をバックフィルすると、その日の usage と現在の namespaces が得られます。未登録の名前空間は `registered = false` で、レジストリのカラムは NULL になります。`created_on` と `last_memory_on` は時刻ではなく日です。ハッシュ化された個人の名前空間に正確な時刻が付いていると、その時刻にサインアップした人や書き込みをした人と突き合わせられる可能性があるためです。行はハッシュ化された名前でソートされ、元の名前でソートされることはありません。

<a id="what-never-leaves-loom"></a>

## Loom の外に出ないもの

列は許可リストで定められています（`server/insights/store/analytics_export.py`。統合テストが各エンティティの列の一覧を厳密に固定しています）。バケット内のファイルは[消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)の対象外となるため、エクスポートにはメモリの内容も、保存された個人の識別子も含まれません。メモリのキーや値、`args`、`user_id`、`session_id`、`subject`、`owner_email`、リクエスト ID は含まれません。ユーザーとセッションは件数のみが出力されます。

<a id="hashed-names"></a>

## ハッシュ化された名前

org、namespace、agent の名前は個人を特定しうる。個人用の名前空間はその所有者のメールアドレスにちなんで命名され、セルフサインアップは OS のユーザー名とホストからエージェントの名前空間と名前を組み立て、MCP クライアントは自分のエージェントに名前を付ける。そのため、`org`、`namespace`、`agent` は常に、秘密鍵を用いた名前のリファレンス SipHash-2-4-128 を小文字の16進数で表した値として出力される。

各名前は個別にハッシュ化されるので、同じ文字列はどこに現れても同じハッシュになる。名前空間の名前はどの org でも同じハッシュになり、同じ名前を持つ org、名前空間、エージェントは同じハッシュを共有する。ウェアハウスは鍵がなくてもこれらの行を関連付けられる。たとえば、2つの org にある同じ個人用名前空間がそうである。

- 鍵は `LOOM_ANALYTICS_EXPORT_NAME_KEY` に設定する32文字以上のランダムな文字列である。これより短い鍵、空の鍵、先頭または末尾に空白がある鍵ではエクスポートは実行を拒否する（`--from-file` で作成した Secret にはファイルの改行が残り、すべてのハッシュがその改行を含む鍵で計算されてしまう）。
- 16バイトの SipHash 鍵は、この文字列（UTF-8）の SHA-256 の先頭16バイトである。ハッシュには ClickHouse の `sipHash128ReferenceKeyed` を使う。これは、その16バイトがあれば任意の SipHash-2-4-128 ライブラリで再現できるため、鍵を持つシステムは自身の ID をハッシュ化して結合に使える。ClickHouse は鍵を2つのワードとして受け取り、それぞれ8バイトの半分をリトルエンディアンで読み取る。ID は Loom が保存するのと同じ文字列（UUID や数値の `toString`）としてハッシュ化すること。型付きカラムの SipHash はバイナリ形式をハッシュ化するため、一致しない。

  ```sql
  WITH SHA256({key:String}) AS d
  SELECT lower(hex(sipHash128ReferenceKeyed(
      (reinterpretAsUInt64(substring(d, 1, 8)), reinterpretAsUInt64(substring(d, 9, 8))),
      toString(tenant_id)))) AS hashed_tenant_id
  FROM tenants
  ```

- ハッシュは仮名であり、匿名の値ではない。鍵を持つ者は、知っている名前からハッシュを再計算できる。Loom で名前空間を消去しても、すでにバケットにある行は削除されない。
- ハッシュは鍵が変わらない限り安定している。新しい鍵ではすべてのハッシュが変わり、エクスポート済みの日には古いファイルが残る。新しい鍵で再度エクスポートする前に、それらを削除すること。
- 鍵を使うクエリは、鍵をパラメーターとしてバインドしても、リテラルとして書いても、鍵を記録する。ClickHouse はバインドされた値を `system.query_log` のクエリテキストに埋め込み、デバッグログとトレースログにも書き込む。ウェアハウス側では、結合を実行するサーバー上でこれらのログを読めるユーザーを制限するか、ClickHouse の外で任意の SipHash-2-4-128 ライブラリを使って ID をハッシュ化すること。Loom 側では、エクスポートの鍵ワードが Loom の ClickHouse の同じログに残るが、そのログを読めるユーザーはすでに名前を読める。

<a id="credentials"></a>

## 認証情報

このコマンドは、プロセスのデフォルトの AWS 認証情報を使用し、ClickHouse に対する処理を行う前に一度だけ確認します。`--role-arn` を指定した場合はロールを引き受けることで、指定しない場合は STS に認証情報の所有者を問い合わせること（`GetCallerIdentity`）で確認します。認証情報がない、期限切れである、またはロールを引き受けられない場合は、読み取りの後ではなくこの時点で失敗します。`s3:PutObject` の権限がない、バケットが間違っている、バケットの所有者が `--expected-bucket-owner` と異なるといった場合は、最初のアップロードで失敗します。`--role-arn` を指定した場合は、アップロードのたびにロールを引き受け直し、そのロールの認証情報で書き込みます。別のアカウントのバケットには、ライターロールを引き受ける方法が適しています。バケットのアカウントがライターロールに `s3:PutObject` を許可し、ライターロールはエクスポート自身の ID だけを信頼します。エクスポートにバケットの読み取り、一覧取得、削除の権限は不要です。カスタマー管理の KMS キーで暗号化されたバケットでは、ライターロールにそのキーに対する `kms:GenerateDataKey` も必要で、これがないとすべてのアップロードが拒否されます。`--expected-bucket-owner` を指定すると、別のアカウントがバケットを所有している場合に S3 が書き込みを拒否します。

プロセスが `AWS_DEFAULT_REGION` または AWS の設定ファイルからすでにリージョンを取得している場合を除き、`--region`（バケットのリージョン。チャートでは `analyticsExport.region`）を指定してください。boto3 はここでは `AWS_REGION` を読み取りません。

<a id="on-kubernetes"></a>

## Kubernetes 上での運用

```yaml
analyticsExport:
  enabled: true
  bucket: analytics-landing
  prefix: loom
  roleArn: arn:aws:iam::123456789012:role/loom-analytics-writer
  expectedBucketOwner: "123456789012"
  nameKeySecret:
    name: loom-analytics-export-key # key LOOM_ANALYTICS_EXPORT_NAME_KEY
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::210987654321:role/loom-analytics-export
```

```bash
kubectl create secret generic loom-analytics-export-key \
  --from-literal=LOOM_ANALYTICS_EXPORT_NAME_KEY="$(openssl rand -hex 32)"
```

この Secret を読むのはエクスポートの Pod だけです。チャートは `nameKeySecret.name` がない場合はレンダリングを拒否し、キーをすべての web Pod と workers Pod に渡してしまう設定も拒否します。対象は、`env`、`extraEnv`、`secrets.extra` 内の `LOOM_ANALYTICS_EXPORT_NAME_KEY`、`settingsFile`（すべての Pod がマウントする ConfigMap）内の `analytics_export_name_key`、そしてリリースの Secret（`secrets.existingSecret` またはチャート自身の Secret）や `envFrom` の Secret を指す `nameKeySecret.name` です。チャートはこれらの Secret の中身を確認できないため、キーをそれらに含めないでください。

チャートは ServiceAccount `<fullname>-analytics-export` と、同じ名前の CronJob を作成します。`<fullname>` はチャートのフルネームで、`fullnameOverride` が設定されていればその値、設定されていなければ、リリース名にチャート名（`nameOverride`、なければ `loom`）が含まれる場合はリリース名、それ以外は `<release>-<chart name>` となり、名前が 52 文字に収まるよう先頭 35 文字で切り詰められます（`kubectl get sa` で確認できます）。ジョブは UTC 00:30（`schedule`）に、一度に 1 つだけ実行され、最大 3 回までリトライし、全試行を通じて 3 時間の期限があります。各ステートメントは 20 分まで実行でき、ClickHouse クライアントが 1 つをリトライすることもあるため、処理が遅い日は試行が 4 回に満たないことがあります。拒否（終了コード 2）の場合はリトライせず、Job を即座に失敗させます。この Pod は web Pod の `podLabels`、`podAnnotations`、`affinity` を引き継ぎません。
web Pod と workers Pod は独自の ServiceAccount を使い続けるため、writer ロールを引き受けられるロールはエクスポートの Pod にのみ紐付けられます。IRSA を使う場合、そのロールの信頼ポリシーには `system:serviceaccount:<namespace>:<fullname>-analytics-export` を指定します。`roleArn` を設定した場合、そのロールに必要な権限は `roleArn` に対する `sts:AssumeRole` のみです。`roleArn` が空の場合はロール自身が書き込むため、バケットに対する `s3:PutObject`（カスタマー管理の KMS キーを使う場合は `kms:GenerateDataKey` も）が必要です。EKS Pod Identity を使う場合は、ロールを ServiceAccount に紐付け、`annotations` は空のままにします。

ジョブは、web Pod と同様にリリースの Secret、設定、`extraEnv`、`extraVolumes` を使って ClickHouse を読み取り、ClickHouse には何も書き込みません。実行が失敗すると失敗した Job として表示されます。CronJob の最後の成功した実行が 1 日以上前になった場合にアラートを出してください。

すべての試行が失敗した日や、開始ウィンドウ（`startingDeadlineSeconds`、1 時間）を逃した日は、後から自動ではエクスポートされません。次の実行がエクスポートするのはその前日分だけです。その日のデータは、CronJob から作成した Job でエクスポートします。これによりエクスポートの ServiceAccount とフラグのもとで実行され、コマンドに `--day` を追加します。

```bash
kubectl create job --from=cronjob/<fullname>-analytics-export analytics-export-backfill \
  --dry-run=client -o yaml > backfill.yaml
# add `- --day=YYYY-MM-DD` to the container's command in backfill.yaml, then:
kubectl apply -f backfill.yaml
```

usage ファイルはその日のものです。名前空間ファイルは実行時点のスナップショットです。
