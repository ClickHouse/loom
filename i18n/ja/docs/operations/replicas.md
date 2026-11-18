<!-- i18n: source=docs/operations/replicas.md blob=f4c8729ef4e795f5b6ab89dcd363a0bf9c30aad0 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md), which is authoritative.

<a id="running-n-replicas"></a>

# N 個のレプリカの実行

すべての Loom プロセスは完全なレプリカです。1 つの ClickHouse サービスと 1 つの URL の背後で、必要な数だけ実行できます。スティッキーセッションは不要です。各プロセスはすべての op を処理し、すべてのトークンを検証し、共有サービスに書き込みます。デプロイメントごとに 1 回だけ行う必要がある処理は、ClickHouse のリースを保持しているプロセスで実行され、そのプロセスが停止すると別のプロセスに移ります。

| デプロイメントごとに 1 回 | 実行される場所 |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| スキーマのマイグレーション | 未適用のマイグレーションを含むビルドを最初に起動したプロセス（migrate リース下）。他のプロセスはその完了を待つ |
| バックグラウンドワーカー | `workers_supervisor` リースの保持者（[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)） |
| デプロイメントの設定の公開 | 同じリース保持者（後述） |

各プロセスは、独自の git ミラー（履歴の読み取り時に、存在しないミラーはクローンされます。`LOOM_CODE_HISTORY_CLONE_ON_READ`）、独自のキャッシュ、独自のレート制限バケットを持ちます。

<a id="one-env-for-all-of-them"></a>

## すべてのレプリカで同じ env を使う

すべてのレプリカを、同じ env と `LOOM_DEPLOYMENT=1` で起動します。

```bash
LOOM_DEPLOYMENT=1
LOOM_CH_HOST=...                 # the one ClickHouse service
LOOM_CH_PORT=8443
LOOM_CH_USER=...
LOOM_CH_PASSWORD=...
LOOM_JWT_SECRET=...              # the same root secret on every replica
LOOM_EMBEDDING_MODEL=...         # the same model and width on every replica
LOOM_EMBEDDING_DIM=...
OPENAI_API_KEY=...               # each replica makes its own provider calls
LOOM_TRUSTED_PROXIES=...         # the load balancer, so per-client limits see the client
```

`LOOM_JWT_SECRET` は kid `default` で署名し、他のすべての署名鍵と保存されたすべての認証情報を封印します。そのため、値が 1 つであれば、すべてのトークンがすべてのレプリカで有効です。署名鍵、失効、グラント、サービスアカウントは共有サービス内の行です。ローテーションや失効は、`LOOM_JWT_KEYRING_REFRESH_SECONDS` と `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS` の範囲内ですべてのレプリカに反映されます。

<a id="configuration-drift"></a>

## 設定のドリフト

各プロセスは、`LOOM_INSTANCE_HEARTBEAT_SECONDS`（デフォルトは 10 秒）ごとにハートビート行を `loom.instances` に書き込みます。内容は、ビルド、MUST_MATCH 設定（埋め込み空間：`LOOM_EMBEDDING_PROVIDER`、`_MODEL`、`_DIM`、`_BASE_URL`、`_SEND_DIMENSIONS`）のフィンガープリント、ルートシークレットのフィンガープリント、起動時刻です。3 間隔のあいだ更新されない行、またはプロセスがシャットダウン時に停止済みとマークした行は、稼働中とは見なされません。

リース保持者は、自身の設定を `loom.deployment_config` に公開します。公開されるのは、MUST_MATCH 設定とルートシークレットの HMAC フィンガープリントです。すべてのプロセスは、起動時と各ハートビートのたびに、このスナップショットと自身を比較します。ルートシークレットが異なる場合（そのプロセスのトークンと保存された認証情報は他のレプリカでは開けません）や、MUST_MATCH の値が異なる場合（ベクトルが別の空間に入ってしまいます）はドリフトです。そのプロセスは ready にならないため、ロードバランサーはトラフィックを送らず、`/v1/health` は違いの内容とともに `config_drift` の問題を報告します。not-ready は新しい接続を止めるだけなので、そのプロセスはすべての API、MCP、サインインの呼び出しにも `503 CONFIG_DRIFT` で応答して接続を閉じます。これにより、クライアントのリトライは設定が一致するレプリカに届きます。比較される値は env の値です。ダッシュボードからのモデルの切り替えはデプロイメント全体に対するもの（すべてのレプリカが ClickHouse から読み取る）であり、ドリフトにはなりません。その後に同じ env で起動したレプリカは ready になります。`/v1/health` の `deployment_config` には、直近のチェック結果と稼働中のホストが表示されます。

リース保持者は、自身がドリフトしているスナップショットを上書きして公開することはありません。誤ったルートシークレットや埋め込みモデルを持つレプリカがリースを獲得しても、not ready のままで、公開済みの env で動くレプリカは ready のままです。自身と一致するスナップショットは、保存済みのものを実行している他の稼働中レプリカがなくなった場合にのみ書き換えられます。そのため、ロールアウト中の N-1 と N の形の異なるスナップショットが交互に入れ替わることはありません。

**MUST_MATCH 設定または `LOOM_JWT_SECRET` を変更する場合**は、ローリングでは行えません。新しい値のレプリカは not ready となり、古い値のレプリカが稼働を続けるためです。すべてのレプリカを停止し、env を変更し、新しい env で `loom deployment publish --yes` を実行し（変更内容が一覧表示されます）、その後で再び起動します（埋め込みを変更した場合は、続けて完全な再埋め込み `loom reembed` を行います）。シークレットを変更した後は、トークンを再発行し、認証情報を保存し直してください。

`LOOM_DEPLOYMENT` のないプロセス（同じサービス上のブランチや A/B サーバー）もハートビートを書き込みますが、公開は行わず、チェックの対象にもならず、レプリカとして数えられません。

<a id="rate-limits"></a>

## レート制限

設定された上限はデプロイメント全体に対するものです。各レプリカの取り分は、上限を `loom.instances` に稼働中の行があるホスト数で割った値です。そのため、レプリカを追加または削除すると、1 回のハートビート以内にすべてのバケットのサイズが変わります。`LOOM_REPLICA_COUNT` はこのホスト数の下限であり、行を読み取れない間のホスト数でもあります。1 つのホストの Web ワーカーは 1 つのバケットテーブルを共有します（[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)）。

<a id="code-repositories"></a>

## コードリポジトリ

すべてのプロセスは `LOOM_CODE_REPOS_REFRESH_SECONDS`（デフォルトは 30 秒）ごとに `loom.code_repos` を再読み込みします。そのため、あるレプリカで追加または制限されたリポジトリは、この時間内にすべてのレプリカに適用されます。

git 履歴を提供しないホスト（ミラー用のディスクを持たないエッジなど）では、`LOOM_GIT_HISTORY=none` を設定します。この場合、blame、コミットのチェック、PR 履歴は git 履歴なしで応答します。

<a id="stopping-a-replica"></a>

## レプリカの停止

正常に停止してください（`docker stop`、SIGTERM）。プロセスは自身のハートビート行を停止済みとマークし、プロバイダーの参加者行を破棄します。強制終了すると、どちらも残ります。ハートビート行は 3 間隔後に数えられなくなります。同じコンテナー内で再起動したプロセスは、停止したプロセスの参加者行を自分で破棄します。別の場所からのプロバイダーの再設定が、停止したプロセスの行を待ち続ける場合は、そのプロセスが停止したことを確認したうえで、`/v1/health` が報告した `participant_id` を指定して破棄します（ハートビートが新しい行や、クエリがまだ実行中の行は、コマンドが拒否します）。

```bash
loom participant retire --participant <participant_id> --yes
```
