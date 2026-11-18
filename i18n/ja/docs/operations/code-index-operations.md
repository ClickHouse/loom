<!-- i18n: source=docs/operations/code-index-operations.md blob=386d854cb14cf44fb989df236eec8a7a0d02d1ee model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md), which is authoritative.

<a id="code-index-operations"></a>

# コードインデックスの運用

このページでは、コードインデックスのワーカーを有効にする方法、リポジトリを設定する方法、インデックスを最新に保つ方法を説明します。コードインデックスは、オペレーターがワーカーを有効にしてリポジトリを設定した場合にのみ動作します。`code_coverage_notify` と `code_backport_gaps` を除くすべての `code_*` ワーカーは、デフォルトでオフです。`code.*` オペレーションで何が分かるかは[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を、インデックスの構築方法は[コードインデックスのアーキテクチャ](../architecture/code-index-architecture.md)を参照してください。

1 つのコード名前空間は、1 つのリポジトリの 1 つのブランチをインデックス化します。追加する方法は 2 つあります。

| 方法 | 手順 | ティア | ページ |
| --------------- | ------------------------------------------- | ---------------------- | ------------------------------------------------------ |
| セルフサービス | ダッシュボードの **Repositories → Add repository** | パーサー（`code_ingest`） | [リポジトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) |
| オペレーター設定 | `LOOM_CODE_REPOS_JSON` | パーサー + コンパイラティア | このページ |

メンバーがページから追加した名前空間に対するオペレーターのエントリは、スキップされます（[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)）。

ワーカーを実行しているコンテナ（`dev/docker-compose.cloud.yml` の `loom-workers`、単一コンテナ構成の `loom-server`）の中で `loom code` を実行してください。これにより、デプロイメントの `.env`、git ミラー、ClickHouse が使われます。

```bash
docker exec loom-workers loom code stats --namespace code-myrepo
```

`--org` / `--namespace` のデフォルトは、それぞれ `LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE` です。デプロイメントが複数のリポジトリをインデックス化する場合は、両方を指定してください。

<a id="prerequisites"></a>

## 前提条件

- **イメージ。** パーサーグラマー（`code` extra）、Go と scip-go、Node と scip-typescript および scip-python、さらに TypeScript の依存関係を自動で準備するための npm、pnpm、Yarn、Bun が含まれています。Docker を使う場合、これらをホストにインストールする必要はありません。必須の稼働サービスは ClickHouse のみです。
- **ネイティブサーバーのツールチェーン。** Python 3.12 以上、Git、npm 付きの Node.js 22 以上をインストールし、サーバーを起動する前に Loom のチェックアウト内で次のコマンドを実行します。

  ```bash
  pip install -e ".[code]"
  sh scripts/install-code-toolchains.sh
  export PATH="$PWD/var/code-toolchains/bin:$PATH"
  ```

  このスクリプトは、バージョンを固定した TypeScript/Python のインデクサーとパッケージマネージャーを、root 権限なしでインストールします。この `PATH` を、サービスマネージャーを含む Loom サーバーの環境に設定したままにしてください。ネイティブの Go インデックス作成には、さらに Go と scip-go が必要です：`go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7`（Go の bin ディレクトリをサーバーの `PATH` に追加します）。C/C++ には scip-clang とプロジェクトのビルド設定が必要です。

- **エンベッダー。** チャンクはサーバーの `LOOM_EMBEDDING_MODEL` で埋め込まれます。`loom code backfill --embed` は、`--allow-fake-embedder`（開発専用）を指定しない限り、フェイクのエンベッダーを受け付けません。
- **書き込み可能な git ミラー。** ワーカーは uid 999 で実行され、`var/code_repos` 配下のベアクローンに fetch します（コンテナに bind マウントされます）。ホストのオペレーターは、グループ（ここでは gid 999）を通じてこのディレクトリをワーカーと共有します。最初に一度だけ準備します。

  ```bash
  sudo chgrp -R 999 var/code_repos && sudo chmod -R g+rwX,o-rwx var/code_repos
  sudo find var/code_repos -type d -exec chmod g+s {} +
  ```

  setgid ビットにより、新しいエントリはそのグループに属したままになります。次に、ホストのオペレーターをそのグループに追加し、`.env` に `LOOM_CODE_REPOS_SHARED_GID=999` を設定します。この設定はコンテナとホストの `loom code` コマンドの両方が読み取ります。Loom がそのグループで作成するミラーまたはチェックアウトのディレクトリはモード 2770 になり、それ以外はすべて 0700 で、サーバーのユーザーだけが開けます。そのため、この設定でグループを指定するまでは何も共有されません。この設定を行うと、サーバーとすべての `loom code` コマンドは umask 007 で実行されます。作成されるディレクトリとロックファイルはグループが書き込み可能になり、git のファイルはオーナーと同じ権限がグループに与えられ（オブジェクトは読み取り専用のままです）、その他のユーザーには何も与えられません。ルート配下のチェックアウトでハーネスを手動で実行するには、同じ umask で実行する `loom code entry-lock` を使います。Loom は、制限付きのエントリに対してのみ既存ディレクトリのモードを変更します。古いビルドや他のツールが作成したファイルについては、上記のコマンドを再度実行してください。

  グループは、制限付きのものを含むすべてのミラーを読み取れるため、Loom を実行するユーザー、つまりホストのオペレーターとコンテナのユーザーだけを含める必要があります（`getent group 999` で確認します）。gid 999 がすでに他のメンバーを持つシステムグループ（多くのディストリビューションでは `systemd-journal`）である場合は、代わりに専用のグループでコンテナを実行し（Compose の `user: "999:<gid>"`）、コマンドと設定でその gid を使います。ホストとコンテナが共有するすべてのパス（`code_repos_root`、`code_scip_checkout_dir`、および各エントリーの `git_dir` と `scip_checkout_dir`）は、準備したディレクトリ内に置いてください。それ以外の場所で Loom が作成するディレクトリやロックファイルは、親が setgid であれば親のグループを、そうでなければ作成したプロセスのグループを引き継ぎ、umask によってそのグループが書き込めてしまう可能性があります。

  umask は、これらのプロセスが書き込むすべてのファイルに適用されます。ルートの外も対象で、そこではファイルは書き込んだユーザーのプライマリグループになります。ホストのオペレーターのプライマリグループは、`users` のように他のユーザーと共有されるものではなく、そのユーザー専用のもの（多くのディストリビューションの既定であるユーザープライベートグループ）にしてください。

  `loom code` ジョブを実行するホストのチェックアウトは、イメージと同時にアップグレードしてください。また、カラーデプロイメントで以前のカラーが停止したら、上記のコマンドを再度実行してください。

- **GitHub トークン**（`LOOM_CODE_GITHUB_TOKEN`、またはエントリーごとの `github_token`）。マージ済み PR の履歴とプライベートリモートに必要です。

<a id="configure-the-entry"></a>

## エントリを設定する

`LOOM_CODE_REPOS_JSON` は、インデックス対象のブランチごとに 1 つのオブジェクトを持つ JSON 配列です。エントリで省略したフィールドは、スカラーの `LOOM_CODE_*` 設定にフォールバックします。スカラーの `LOOM_CODE_REPO` 以外のリポジトリのエントリでは、`git_remote`（`https://github.com/<owner>/<name>.git`）と `git_dir`（スカラーのクローンと同じ場所の `<name>.git`、スカラーのリポジトリがない場合は `var/code_repos` 配下）が `repo` から導出されます。

```bash
LOOM_CODE_REPOS_JSON='[
  {"org": "acme", "namespace": "code-myrepo", "repo": "acme/myrepo",
   "git_branch": "main", "public": true},
  {"org": "acme", "namespace": "code-myrepo-release-2", "repo": "acme/myrepo",
   "git_branch": "release-2", "embed_reuse_from": "code-myrepo"}
]'
```

| フィールド                           | 意味                                                                                                                          |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `namespace`, `org`                   | インデックスを置く場所                                                                                                        |
| `repo`                               | GitHub 上の `owner/name`                                                                                                      |
| `git_dir`, `git_remote`              | ベアクローンとそのリモート。同じリポジトリのブランチは 1 つのクローンを共有します                                             |
| `git_branch`                         | この名前空間が追跡するブランチ                                                                                                |
| `path_prefixes`                      | インデックス対象のプレフィックス（カンマ区切り）。指定しないエントリは、組み込みの拒否リストを除くツリー全体をインデックスします |
| `exclude`                            | インデックスしない glob（リストまたはカンマ区切り）                                                                           |
| `exclude_generated`                  | 内容から生成物と判断できるファイル（`Code generated … DO NOT EDIT.`、`@generated`）を除外します                               |
| `include_contrib_paths`              | ベンダリングされた `contrib/` のうち、あえてインデックスするパス。その定義がリポジトリの呼び出し箇所に結び付きます             |
| `depends_on`                         | このコードが呼び出す他のコード名前空間。ここにない定義は、そちらで探します                                                    |
| `embed_reuse_from`                   | チャンクのテキストが同一の場合に埋め込みを再利用する名前空間（同じリポジトリのリリースブランチ）                              |
| `public`                             | ホスト上でリポジトリが公開されている：組織内の誰でも、すぐにそのインデックスへリーダーとして参加できます                      |
| `classification`                     | プライベートリポジトリには `restricted`：メンテナが各リーダーを承認し、組織全体には決して読み取り可能になりません             |
| `scip_producer`, `scip_checkout_dir` | コンパイラティアのインデクサー（`clang`、`typescript`、`python`、`go`。未設定の場合はツリーのビルドファイルから選ばれます）とその作業ツリー |
| `scip_extra_producers`               | 同じツリーに対する追加のコンパイラティアのプロデューサー（TypeScript フロントエンドを持つ Python リポジトリなど）。それぞれ別のチェックアウトを使います |
| `git_extra_branches`                 | 共有ミラーが保持すべき他のリモートブランチの glob                                                                             |
| `github_token`                       | `LOOM_CODE_GITHUB_TOKEN` のエントリ単位の上書き                                                                               |

`git_remote` のホストは ASCII で指定してください。国際化ドメイン名は punycode で指定します。これ以外の表記は page add が拒否するため、両者でホストの綴りが一致します。

`git_remote` がローカルパスまたは `file://` のミラーであるエントリは、その `repo` で識別されます。同じリポジトリに対する page add は、これと一致している必要があります。GitHub 以外のホストにあるリポジトリのミラーは、`repo` に `host/owner/name` として指定してください。

`public` は、`loom code init` が最初に名前空間を登録するときに読み込まれます。後から編集しても参加ポリシーは変わりません。参加ポリシーはメンテナが `code.repo.set_access` で変更します。設定済みのエントリには、最初はメンテナがいません。`POST /v1/admin/namespace.adopt` は restricted の名前空間を拒否し、設定済みの restricted 名前空間に最初の直接グラントを付与するルートやコマンドもありません。そのため、プライベートリポジトリは次の順序で設定します。

1. エントリを `classification` なし、`public` は false（デフォルト）、`org_readable` なしで設定し、`LOOM_CODE_ORG_READABLE` はデフォルトの `false` のままにします。手順 3 までは、この名前空間は standard です。そこへのグラントを持つ場合、またはクレームが対象に含まれるトークン（名前空間を指定するものだけでなく `*` クレームも含む）は、これを読み取れます。
2. そのエントリに対して `loom code init` を実行します。これで名前空間のディレクトリ行が書き込まれます（それ以前は adopt が `NAMESPACE_NOT_FOUND` を返します）。続いてメンテナのために adopt します。まだ `loom code backfill`（や `loom code backfill-*` コマンド）は実行しないでください。設定済みのエントリは、backfill が `git_head` ウォーターマークを書き込むまで、最初のインデックス、PR 履歴、バックポートの判定、追加ブランチ（`code.repo.add_branch` は `REPO_NOT_INDEXED` を返します）が得られないため、実行するまでリポジトリは何もインデックスされません。その間もエントリの git ミラーはクローンされ、その履歴の読み取りも同じゲートを通ります。そのため、組織の `*` トークンを配布する前に手順 3 を完了してください。
3. メンテナが `code.repo.set_access` で制限します（`classification: "restricted"` と、`join_policy: "approval"` または `"closed"`。両方が必須です）。続いてエントリにも `classification: "restricted"` を設定して、どちらの入力でも制限されるようにし、`loom code backfill` を実行します。ミラーとコンパイラティアのチェックアウトは、`code_repos_shared_gid` に属する場合はモード 2770（サーバーのユーザーとそのグループのみに開放）、それ以外は 0700 で作成されます（「Writable git mirrors」を参照）。古いビルドが作成したもの、または手作業でモードを変更したものは、その呼び出しを処理するホストでは直ちに、それを保持する他のホストでは次のミラーラウンドで、そのモードに設定されます。これは、ホストが名前空間のディレクトリ行を restricted と読み取った後（30 秒以内）に行われます。変更されるのはディレクトリ自体だけです。古いビルドがその内部に作成したものについては、「Writable git mirrors」のコマンドが必要です。

制限はブランチ単位です。各ブランチのインデックスは、それぞれ独自の名前空間です。ホスト上でそのリポジトリを自身のトークンで読み取れる人は、`code.repo.add` で別のブランチを新しい standard 名前空間に追加できます。その人はすでにそのリポジトリを読み取れます。プライベートリポジトリの設定済みブランチは、それぞれ上記の順序で設定してください。

`scip_producer` を指定しないエントリは、`scip_checkout_dir` を C/C++ 用として扱います（[コンパイラティア](#compiler-tier)を参照）。そのため、別の言語向けにそのツリーを準備する場合は、プロデューサーを指定してください。

リポジトリの `.gitattributes` にある `linguist-generated` / `linguist-vendored` のパターンと、インデックス対象のコミットにある `.loomignore` ファイルは、除外設定として尊重されます。git サブモジュールとしてベンダリングされた依存関係はベアミラーに含まれません。それ専用のエントリを作成し、`depends_on` に指定してください。ゲージ `loom.code.repos_config_errors` は、サーバーが使用できなかったエントリ（不正な JSON、名前のないエントリ、`.` や `/` を含む名前空間、重複した名前空間）を報告します。0 は値が問題なく解析されたことを意味します。再起動前に JSON を検証するには、`docker compose --env-file .env -f dev/docker-compose.cloud.yml config` を実行します。

インデックスを誰が読み取れるかは、名前空間の参加ポリシーとグラントで決まります（[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)）。`LOOM_CODE_ORG_READABLE=true` にすると、すべてのコード名前空間が、グラントなしで組織内のすべてのプリンシパルに読み取り可能になります。デフォルトはオフです。

<a id="first-index"></a>

## 最初のインデックス

```bash
docker exec loom-workers loom code init     --namespace code-myrepo --org acme   # optional: bare clone + index state
docker exec loom-workers loom code backfill --namespace code-myrepo --org acme --embed
docker exec loom-workers loom code backfill-prs --namespace code-myrepo --org acme --months 12
```

- `init` は任意です。取り込みパスは、オペレーターが設定したエントリーであっても、存在しないミラーをバックグラウンドで clone し、clone 先のパスをログに記録します（`loom doctor` の `code_mirrors` 行は、存在しないオペレーター指定の `git_dir` を示すため、パスの誤入力が数 GB の clone として黙って実行されることはありません）。ミラーはリモートの ClickHouse レコードのキャッシュです。サービスを提供するすべてのホストは、起動時にインデックス済みエントリーのミラーを clone し（`LOOM_CODE_MIRROR_WARMUP`）、`LOOM_CODE_MIRROR_FETCH_INTERVAL_S` ごとに、リーダーのパスが ClickHouse に記録した ref のうち自ホストにないものを fetch します（`ls-remote` は行わず、最新であればネットワーク呼び出しも行いません）。ホストがミラーを持つまでは、そのホストの履歴読み取りは `no_git_mirror: cloning <entry>` と応答します。`/v1/health?include=mirrors` は、そのホストのミラーを `mirrors` に数えて返します。ディスク：すべてのホストがすべてのミラーのコピーを保持します（N レプリカなら N コピー）。また、オペレーター指定の `git_dir` は `loom` ユーザーが書き込める必要があります。ネットワーク：ディスクが空のホストは、インデックス済みリポジトリごとに 1 回フル clone を行います。ロールアウトのたびにすべての Pod がすべてのミラーを再 clone しないよう、`LOOM_CODE_REPOS_ROOT` は永続ボリューム上に置いてください。
- `backfill` は、各ファイルを解析し、チャンクに分割し、シンボルとエッジを抽出し、埋め込みを行います。再開可能なので、中断した場合は再実行してください。`--phase index` の後に `--phase embed` を実行すると処理を分割できます。未埋め込みの行が embed フェーズのキューになるためです。`--commit <sha>` はツリーを固定します（`--no-fetch` を暗黙的に指定したことになります）。`--concurrency N` はファイルを並列処理し、`--limit N` はサンプルのみをインデックスします。
- 名前空間の最初の完全な backfill では、名前テーブルが完成した後に全ファイルのエッジも再導出するため、ファイルをまたぐ参照が結び付きます（`--no-edges-second-pass` でスキップできます）。`--dialect-folder-pass` を指定すると、方言が未確定の SQL ファイルが、同じディレクトリで一致している方言を採用します。
- `backfill-prs` は GitHub からマージ済み PR の履歴を読み込みます。カーソルによる再開が可能で、GitHub のレート制限に制約されます（`LOOM_CODE_PR_BACKFILL_MONTHS`、デフォルトは 12）。`backfill-reviews` はインラインのレビューコメントを、`backfill-assignees` はリクエストされたレビュアーを、`backfill-items` は任意の状態の issue と PR を追加します。
- **ワーカーは、設定済みエントリーの最初のインデックスを開始しません。** `git_head` ウォーターマークのないエントリーは、「先に `loom code backfill` を実行してください」という注記とともにスキップされます（セルフサービスの最初のインデックスは、1 パスあたり `LOOM_CODE_BOOTSTRAP_FILES_PER_PASS` 個のファイルずつ構築します）。

その後：

- `POST /v1/code.calibrate`（`ADMIN`）は、`bench/fixtures/code_calibration_probes.jsonl`（イメージに同梱）から、コードインデックスの棄却しきい値を導出します。
- `loom code describe --top-pct 20`（任意、LLM のコストがかかります）は、最も中心的なチャンクに 1 行の説明を書き込みます。
- `loom code stats` は、ウォーターマーク（`git_head`、`scip_commit` など）と現在の行数を出力します。`POST /v1/code.index_status` は API から同じ内容を返します。

`scripts/ops/code_intel_bootstrap.sh` は、Loom ホスト上で設定済みの `LOOM_CODE_ORG` / `LOOM_CODE_NAMESPACE` に対してこの一連の手順（init、両方の backfill フェーズ、PR 履歴、calibrate、続いて `bench/code_intel_qa.py` のゲート）を実行します。フェイクの埋め込みモデルや、`/v1/health` が応答しないサーバーは拒否します。`--dry-run` は読み取り専用のプリフライトのみを実行し、コマンドを出力します。`LOOM_BASE_URL` と `LOOM_ADMIN_TOKEN` を読み込みます。

<a id="steady-state"></a>

## 定常運用

`.env` で workers コンテナのワーカーを有効にし、コンテナを再作成します。各ワーカーの実行間隔の設定とデフォルト値は [ワーカーリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md) にあります。

| ワーカー               | フラグ                                   | 動作                                                                                                                                                                                                                                                                                                                                                                                                 |
| ---------------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code_ingest`          | `LOOM_CODE_INGEST_ENABLED`               | `LOOM_CODE_INGEST_INTERVAL_SECONDS` ごとに、fetch を行い、first-parent の各コミットについて変更された内容のみを再チャンク化し、シンボルとエッジを更新し、削除されたものを tombstone にし、マージされた PR のコンテキストパックを作成し、`git_head` を進めます。1 時間ごとに、チャンクが head 時点の内容より古いインデックス済みファイルを検出し、1 回の実行につき最大 `LOOM_CODE_RECONCILE_HEAL_PER_PASS` 件を修復します。セルフサービスの初回インデックスも作成します。 |
| `code_embed_recovery`  | `LOOM_CODE_EMBED_RECOVERY_ENABLED`       | ベクトルが付いていないチャンク（プロバイダーの障害や `--phase index` による）を埋め込みます                                                                                                                                                                                                                                                                                                                               |
| `code_pr_history`      | `LOOM_CODE_PR_HISTORY_ENABLED`           | 新たにマージされた PR を GitHub から同期します                                                                                                                                                                                                                                                                                                                                                                   |
| `code_graph_stats`     | `LOOM_CODE_GRAPH_STATS_ENABLED`          | 週次：インクルードグラフの PageRank、ファイルごとの次数、テスト ↔ ソースのマップを計算します                                                                                                                                                                                                                                                                                                                     |
| `code_summary`         | `LOOM_CODE_SUMMARY_ENABLED`              | 週次：`code.map` 用に LLM でファイルとモジュールのカードを作成します（名前空間ごとにコスト上限あり）                                                                                                                                                                                                                                                                                                                         |
| `code_scip_apply`      | `LOOM_CODE_SCIP_APPLY_ENABLED`           | `code.scip_ingest` 経由でアップロードされたコンパイラティアの差分を適用します                                                                                                                                                                                                                                                                                                                                     |
| `code_coverage_notify` | `LOOM_CODE_COVERAGE_NOTIFY_ENABLED` (on) | 監視対象コミットのカバレッジがすべてのティアで利用可能になったときに、タスクイベントを発行します                                                                                                                                                                                                                                                                                                                                                                 |
| `code_backport_gaps`   | `LOOM_CODE_BACKPORT_GAPS_ENABLED` (on)   | 夜間のバックポートレポートを作成します。`code_pr_history` と併せてのみ実行されます                                                                                                                                                                                                                                                                                                                                        |

言語ごとのコンパイラティアのワーカーは次のセクションで説明します。

**プッシュによる更新のトリガー。** CI ジョブは、マージ直後に `POST /v1/code.refresh`（`WRITER`）を呼び出せます。これを受け取ったレプリカ上で、その名前空間に対して同じ ingest パスを即座に実行します。実行は名前空間のリース（`code_refresh:<org>/<namespace>`）の下で行われるため、別々のレプリカに届いたトリガーは 1 回のパスにまとめられます。どのエントリにも設定されていない名前空間に対しては 404 を返すため、プッシュ側がインデックス対象を決めることはありません。ポーラーが無効でも動作します。

オープン中のプルリクエストはインデックスされません。作業ツリーはクライアント自身のものとして扱われます。

<a id="compiler-tier"></a>

## コンパイラティア

パーサティアは tree-sitter から回答します。コンパイラ水準（SCIP）のインデックスがある場合は、そのシンボルと参照が優先されます。各言語にはワーカーがあり、自身のウォーターマークからエントリのミラー（`code_ingest` が取得）上のブランチ先頭までのコミットをインデックス化し、workers リースを保持しているプロセス上で、その差分をインプロセスで適用します。コンパイラティアはパーサティアの `git_head` を待ちません。待つのはエントリの最初のインデックスだけです。

| 言語       | `scip_producer` | ワーカーフラグ                                                | ホストの要件                                                                                                        |
| ---------- | --------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| C / C++    | `clang`         | `LOOM_CODE_SCIP_DELTA_ENABLED`, `LOOM_CODE_SCIP_FULL_ENABLED` | clang 21、CMake、ninja（イメージに含まれる）と scip-clang（amd64 イメージに含まれる。それ以外は `LOOM_CODE_SCIP_CLANG_PATH`） |
| TypeScript | `typescript`    | `LOOM_CODE_SCIP_TS_DELTA_ENABLED`                             | Node と固定されたインデクサ／パッケージマネージャ（イメージに含まれる）。依存関係は自動的に準備されます            |
| Python     | `python`        | `LOOM_CODE_SCIP_PY_DELTA_ENABLED`                             | scip-python（イメージに含まれる）                                                                                   |
| Go         | `go`            | `LOOM_CODE_SCIP_GO_DELTA_ENABLED`                             | Go と scip-go（イメージに含まれる）                                                                                 |

`scip_producer` を指定しないエントリには、ツリーにビルドファイルがあるすべてのティアが割り当てられます。Go は `go.mod`（任意の深さ）、TypeScript はルートの `tsconfig.json`、`jsconfig.json` または `package.json`、C/C++ はルートの `CMakeLists.txt`、Python はルートの `pyproject.toml`、`setup.py` または `requirements.txt` です。`code.index_status` はこれらを `compiler_candidates` として一覧表示します。テストツール用の `pyproject.toml` だけを持つ C++ リポジトリのように、ビルドファイルが付随的なものである場合は、プロデューサを明示してください。`scip_extra_producers` は、指定したプロデューサに加えてティアを追加します。各ティアは独自のツリーで動作します。指定されたプロデューサ（エントリが何も指定しない場合は C/C++）は `scip_checkout_dir` で、それ以外はすべて `<scip_checkout_dir>.<producer>` で動作します。

ワーカーはすべてデフォルトで有効で、Docker イメージにはすべてのインデクサが含まれています。scip-python、scip-typescript、scip-go、そして C/C++ 用の clang 21、lld、CMake、ninja、scip-clang です。アップストリームは scip-clang を x86_64 Linux 向けにのみビルドしているため、arm64 イメージには C/C++ ツールチェーンの残りは含まれますが scip-clang はありません。`LOOM_CODE_SCIP_CLANG_PATH` を自前のビルドに向けてください。向けない場合、C/C++ ティアは `scip-clang missing` をスキップとして記録します。各ワーカーは、存在しない作業ツリーを空のリポジトリとして作成し、エントリのミラーから各コミットをそこへ取得するため、新しいリーダーは何もない状態から始まります。プロジェクトの前提条件が揃っていないワーカーは、`workers.status` にスキップを記録します。Tree-sitter ティアは引き続き利用できます。TypeScript プロジェクトは、そのチェックアウト内で依存関係が自動的にインストールされます。

C/C++ ハーネスは、ツリーでの初回実行時、または `CMakeCache.txt` がない、別のディレクトリで作成された、ホストにもうないコンパイラを指している、のいずれかの場合に、リポジトリの CMake configure（コンパイルデータベースのみ、ビルドなし）を実行します。フルインデックスの前と、ウィンドウに `.cpp`、`.cc`、`.cxx` ファイルの追加、または `CMakeLists.txt`、`.cmake` ファイルの追加・変更・削除が含まれる差分では、既存のキャッシュに対して configure を再実行します。コンパイルデータベースには、configure したツリーの翻訳単位しか載らないためです。再 configure が失敗すると、実行は CMake の最後の数行とともに失敗し、エントリの `clang` レコード（フルインデックスでは `clang_full`）は種別 `cmake`（ディスクが満杯の場合は `disk`）の failing と表示されます。失敗した差分はウォーターマークを進めないため、追加される翻訳単位を含むそのウィンドウは毎回のパスで再試行されます。フルインデックスは、失敗したフル実行すべてと同様に、6 時間後に再試行されます。

大きな C++ ツリーは負荷の高いケースです。サブモジュールとビルドツリーを含む ClickHouse 規模のチェックアウトは、ミラーとは別に約 30 GB になります。また scip-clang はジョブごとに翻訳単位の AST（それぞれ数 GB）を保持するため、`LOOM_CODE_SCIP_CLANG_JOBS` はワーカープロセスが持つメモリに収まるように設定してください。このようなツリーのフルインデックスには約 1 時間かかります。

- **C/C++ 差分の上限。** ワーカーは、変更されたヘッダをインクルードする翻訳単位を含め、変更された翻訳単位を `LOOM_CODE_SCIP_DELTA_MAX_TUS`（200）までインデックス化します。上限を超えると、変更された `.cpp` ファイルのみをインデックス化します。CMake を再 configure する差分は、インデックスと同じ `LOOM_CODE_SCIP_DELTA_TIMEOUT_SECONDS`（3600）の範囲内で configure を実行します。タイムアウトした差分は `scip_commit` を元のままにするため、次のパスではより大きなウィンドウで再試行されます。
- **差分の上限。** サーバーは、コンパイラティアの差分（どのティアでも、差分実行でもフル実行でも）を、通常ファイルでありリンクでない場合にのみ読み取り、一度にファイル数件のラウンド単位で適用します（`server/code/app/index/delta_file.py`）。まず差分内の `{`、`[`、`,` のバイト数を数え、次にすべての値を一度パースして各ファイルエントリを検査し、その後で開き直して、適用のためにファイルをラウンド単位で再度読み込みます。そのため、上限を超える差分やパースできない差分は、何かが適用される前にエントリのパスを失敗させます。上限は差分を開いた時点の値が使われ、変更した値は次のパスから反映されます。
  - `LOOM_CODE_SCIP_DELTA_FILE_MAX_MB`（180 MiB）はファイルのバイト数を、`LOOM_CODE_SCIP_DELTA_MAX_NODES`（6,000,000）は数えたバイト数を制限します。後者はパースが構築する JSON 値を制限します。これらはパスの作業量を制限します。そのメモリは、下記のラウンド分に、各ファイルエントリと削除パスの記録（それぞれ約 70 B、ノード上限が許す数まで。最小のエントリは数えたバイトで 2、削除パスは 1 を消費するため、デフォルトでは約 300 万エントリで約 200 MiB、または 600 万の削除パスで約 400 MiB）と、そのパスのテキスト（各最大 4,096 バイト。バイト上限により制限されます）を加えたものです。大きな C++ ツリーのフル実行は、数十 KB のエントリで 500 MiB 超を書き出しますが、管理用のデータは数 MiB です。そのような場合は両方を引き上げてください。
  - `LOOM_CODE_SCIP_DELTA_ROUND_MAX_NODES`（3,000,000）は 1 ラウンドのメモリを制限します。1 回の適用ラウンドがパースする、数えたバイト数です。1 ラウンドには最大 100 ファイルが含まれ、次のラウンドをパースする前に解放されます。1 つのファイルエントリだけでラウンドを満たすことがあり、それを超えるファイルエントリはそのエントリ名を示してパスを失敗させます。それほど大きいそれ以外の値（削除パス、ヘッダ値、無視されるキー）は、ハーネスがそのような値を書き出すことはないため `delta invalid` と表示されます。削除パスは 1 件ずつ読み取られます。設定ではない固定の上限 64 million 文字が、1 つの値と 1 ラウンドのテキストを同じ形で制限します。実際のエントリは数えたバイトあたり約 32 文字なので、その場合は 2,000,000 バイト付近で上限に達します。大きな C++ ツリーのフル実行で最大のファイルエントリは、数えたバイトで約 600,000（エッジ約 100,000）で、ラウンド予算の約 5 分の 1、文字数上限の約 3 分の 1 です。リポジトリのコードが書き出せる最も密なラウンド（最小のシンボル）は、デフォルトの予算で読み込む間に約 1.8 GiB の常駐メモリでピークに達します（`ru_maxrss` で測定。Python オブジェクトで数えたバイトあたり約 330 B、残りはアロケータ分です）。文字数上限に近い値のテキスト（1 文字あたり最大 4 バイト）は、読み取りバッファの拡張中に最大 3 回保持されることがあるため、最悪のラウンドの読み取りは差分のサイズによらず約 2.5 GiB です。その適用では、そのラウンドの行（そのパスの既存の行と挿入する新しい行）がさらに加わり、新しいシンボルのラウンドでは読み取りと同程度になるため、ワーカーは読み取りの約 2 倍を見込んでサイズを決めてください。C++ 差分の実際のファイルエントリは、約 32 バイトにつき数えたバイト 1 であり、ラウンドは予算に達するずっと前に 100 ファイルに達します。ソースのチェックアウトから、デフォルトの最も密なラウンドの読み取りを測定するには、次のようにします。

    ```bash
    .venv/bin/python -c "import resource, tempfile, pathlib; from server.settings import settings; from server.code.app.index.delta_file import open_delta; p = pathlib.Path(tempfile.mkdtemp()) / 'delta.json'; n = settings.code_scip_delta_round_max_nodes // 2 - 10; p.write_text('{\"commit\":\"c\",\"files\":[{\"path\":\"a\",\"symbols\":[' + ','.join(['{\"qualified_name\":\"\"}'] * n) + ']}]}'); list(open_delta(p).rounds(100, lambda x: True)); print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss >> 10, 'MiB peak RSS')"
    ```

  無効な差分は、pydantic による不正項目の一覧ではなく、最初の問題を示す短い `delta invalid: <reason>`（エントリの場合は `file entry <i>: …`）でパスを失敗させます。`--keep-delta` は、ハーネスが書き出した状態の差分を、パースされる前に保持します（バイト上限は引き続き適用されます）。ファイルは、コマンドを実行したユーザーだけが読み取れます（モード 0600）。

- **C/C++ フルインデックス。** `code_scip_full` は、1 回のパスにつき 1 つのエントリを再インデックス化します。対象は、`scip_commit` がまだ設定されていないエントリ（差分は "watermarks not seeded" としてスキップします）と、最後に適用されたフル実行が `LOOM_CODE_SCIP_FULL_EVERY_HOURS`（168）時間前になったすべてのエントリで、これにより上限で切り詰められた差分が修復されます。失敗した実行は失敗の 6 時間後に、Pod とともに強制終了された実行（ロールアウト）は開始の 6 時間後に再試行されます。base-unknown の差分は `scip_commit` をクリアするため、そのエントリは次にフルインデックスを受けます。パスはエントリロックの下で、ハーネスに `LOOM_CODE_SCIP_FULL_TIMEOUT_SECONDS`（8 時間）、チェックアウトの移動、適用、repack にさらに 6 時間を割り当てて実行されます。`loom code scip-full` は同じインデックスを手動で実行し（`--keep-delta` は差分を保存するため、中断された適用を `--apply-delta` で再実行できます）、`loom code scip-delta` は差分パスを 1 回実行して `RESULT:` 行を出力します。
- **C/C++ サブモジュール。** このティアは、エントリのアクセストークンでサブモジュールのピンを取得します。そのため、`.gitmodules` の URL がリモートのオーナー配下の絶対 https URL であるプライベートサブモジュールには、ほかの認証情報は不要です。オーナーは大文字小文字を含め書かれたとおりに照合されるため、別の大文字小文字で綴られたオーナーはトークンなしで取得されます。相対 URL はチェックアウト内のローカルパスに解決され、リモートがありません。このティアは `file` トランスポートを拒否するため、そのサブモジュールの更新は失敗します。リポジトリの `.gitmodules` にコミットできる人は、トークンが読み取れるそのオーナーの任意のリポジトリをサブモジュールに指定できます。そのため、エントリには、サブモジュールが必要とするリポジトリに限定したトークンを与えてください。
- **SQL ↔ C++ クロスリファレンス。** `code_crossref` は、前回の実行以降に `git_head` が動いた C/C++ エントリごとに、`code_test_map` のコンテンツ参照行（テストクエリから、それが使う関数や設定を登録するファイルへ）を再構築します。読み取るのはパーサティアのチャンクのみです。
- **チェックアウトの保守。** フルの C/C++ パス（エントリロックの下）と Python、TypeScript、Go のパスは、ミラーと同じ規則（後述）の下で、`LOOM_CODE_MIRROR_GC_INTERVAL_H` ごとに `git gc --no-prune` でエントリのチェックアウトを repack します。ティアがチェックアウト内で実行する git コマンドはすべて auto-gc とバックグラウンドメンテナンスが無効なので、fetch によって repack が始まることはありません。
- **エントリロック。** フルパスと差分が同じエントリを同時に処理することはありません。それぞれが `LOOM_CODE_SCIP_LOCK_DIR`（`var/cron/locks`）内のエントリのロックファイル `scip.<org>.<namespace>.lock`（両方の名前はパーセントエンコードされます）を取得します（`loom code scip-lock-path --org ORG --namespace NS` で表示できます）。フルパスは、そのエントリで実行中の差分を待ちます。差分は、フルパスが保持しているエントリをスキップし、`skipped`（"entry lock held (full pass running)"）にその名前を記載します。異なるエントリは並行して実行されます。どのパスも ClickHouse 上のエントリのキー（`loom._migration_lock` 内の `scip_entry:<org>/<namespace>`）も取得するため、2 つのホスト上のパスも互いに排他されます。死んだ保持者のキーは 120 秒で失効します。キーを取得できないパス（ClickHouse または Keeper に到達できない）は、ホストのファイルだけで実行せず、失敗します。保持者は、更新なしで 60 秒（一時停止を含む）が経つと、別のホストがキーを取得できるようになる前に、自分のキーを失ったとみなし、以降のインデックス行もウォーターマークも書き込みません。書き込みはすべて事前にキーを確認し、パスは `EntryLockLost` で終了します。ハーネスの手動実行は `loom code entry-lock --org ORG --namespace NS -- <command>` の下で行います。このコマンドは両方のロックを取得し、別のパスがそのエントリを保持している場合は 75 で終了し、キーを失った場合はコマンドを強制終了します（終了コード 3）。また、キーの保持者（`LOOM_ENTRY_LEASE_HOLDER`）をコマンドに渡し、ハーネスのアップロードはこれを伴います。`code.scip_ingest` は、その保持者がもうキーを持っていない場合、パートを拒否する（409 `ENTRY_LEASE_NOT_HELD`）ため、エントリを失ったパスは `scip_apply` のバッチを完了させません。`--tier <producer>` を付けると、コマンドの終了結果を、そのコンパイラティアのヘルス（`code.repos` の `tier_health`）としてエントリに記録し、stderr を含むコマンドの出力を stdout に中継します。
- **TypeScript の依存関係は自動です。** 新しいチェックアウトでは、コンパイラによるインデックス化の前に、型定義を含む開発用依存関係を含めて依存関係がインストールされます。Loom はロックファイルに応じて npm、pnpm、Yarn（クラシックまたはモダン）、Bun を使い、既存のロックファイルを強制し、ライフサイクルスクリプトと実行可能なパッケージマネージャのフックを無効にします。パッケージマニフェストのない単純なスクリプトにはインストールは不要です。依存関係のマニフェストやロックファイルの変更は、次のインデックスの前に更新を引き起こします。失敗または中断されたインストールは、コンパイラのウォーターマークを進めずに再試行されます。ダウンロードには、リポジトリのパッケージレジストリへのネットワークアクセスが必要です。プライベートパッケージレジストリ、互換性のないロックファイル、ビルドを必要とする生成型は、プロジェクト固有の設定が必要になることがあります。`workers.status` は失敗したインストーラとその終了コードを示し、エントリのティアヘルスレコード（`code.index_status`）にはインストーラの出力の末尾が記録されます。サーバーの認証情報は、インストーラの環境には渡されません。サーバーは、チェックアウトの `package.json`（最大 1 MiB）と各インストールルートの `yarn.lock`（最大 64 MiB）を、通常ファイルとしてのみ、リンク経由では決して読み取りません。シンボリックリンクのファイルは、リンクがリポジトリ内を指していても TypeScript のパスを失敗させます。
- **リポジトリのファイルは読み取る前にサイズが確認されます。** 取り込みはまずミラーにファイルのサイズを問い合わせます。`LOOM_CODE_INGEST_MAX_FILE_BYTES` を超えるファイルは読み取られず、その `code_files` 行に理由が記録されます（`code.index_errors`）。0 にすると上限が解除されます。インポートリゾルバが読み取るマニフェスト（TypeScript、Python、Go のプロジェクトおよびワークスペースのファイル）はすべて、ミラーから最大 1 MiB まで、`go.mod` は最大 64 KiB まで、いずれも最大 200 個まで読み取られます。それより大きいものは、コミットごとに 1 回、org と名前空間を示す警告とともにスキップされ、それによって解決されたはずのインポートは未解決のままになります。ワーカーのパス以外では、リゾルバはファイルの代わりにインデックス化されたチャンクから読み取ります。チャンクには `LOOM_CODE_INGEST_MAX_FILE_BYTES` 以内のファイルしか含まれません（0 の場合は任意のサイズ）。バレルの再エクスポートも同じファイル上限の下で読み取られます。上限を超えるバイナリまたは拒否リストのファイルは、上限以下のものと同様に、そのパスと先頭 8 KiB によって墓標化されます。生成されたファイルはスキップ行となり、宣言は記録されません。
- **リポジトリ独自の除外は上限があり、フェイルオープンです。** `.loomignore` と `.gitattributes` は最大 1 MiB まで読み取られ、合わせて最大 1,000 パターンを与えます。それより大きいファイルはパターンを与えず、1,000 番目を超えるパターンは破棄されるため、それらが除外するはずだったパスはインデックス化されます。ログにはそのファイルまたは件数が記載されます。エントリ自身の `exclude` glob は切り詰められないため、除外しなければならないパスはそこに指定してください。
- **適用の失敗** は、そのバッチをエラーとともに `failed` のままにします。`loom code scip-retry <batch_id>` でバッチを pending に戻します。
- **手動および CI のプロデューサ** は `scripts/scip_harness/`（`run_delta.sh`、`run_typescript_delta.sh`、`run_python_delta.sh`、`run_go_monorepo_delta.sh`、`scip_to_delta.py`。詳細はその README を参照）にあります。

<a id="repository-job-sandbox"></a>

### リポジトリジョブのサンドボックス

**ビルドを伴うティアはリポジトリのコードを実行します。** C/C++ ティアはリポジトリ自身の `cmake` configure を実行し（`CMakeLists.txt` はプログラムです）、Go ティアはリポジトリのモジュールグラフに対して `go mod download` を実行し、TypeScript ティアはロックファイルに記載されたパッケージをインストールします。コンパイラティアの実行は、リポジトリの作者が書いたコードを、実行するホスト上でそのまま走らせることだと考えてください。

ホストが対応している場合、Loom はすべてのリポジトリジョブ（clone、fetch と `ls-remote`、`git submodule update`、依存関係のインストール、コンパイラティアのハーネス）を、[バブルラップ](https://github.com/containers/bubblewrap)（`server/code/app/index/sandbox.py`）上に構築した 1 つの境界の内側で実行します。

- 専用の user、pid、network、mount、ipc、uts 名前空間を持ち、capability はなく、新しい user 名前空間も作れません。seccomp フィルタは `ptrace`、`mount`、`unshare`、`setns`、`CLONE_NEWUSER` 付きの `clone`、`bpf`、`perf_event_open`、カーネルキーリング、モジュールと kexec のロード、`userfaultfd`、`open_by_handle_at`、`process_vm_readv`/`writev` を拒否します（`clone3` は `ENOSYS` を返すため、C ライブラリは `clone` にフォールバックします）。
- マウントされるのは、システムディレクトリ、ツールチェーン、ジョブ自身のパスだけです。ツールチェーンは、Loom のデータを一切公開しない場合にのみマウントされます。`var/` そのもの、またはそれを含むディレクトリ、ミラーとチェックアウト、env ファイル、インストールルート、`HOME` は決してマウントされず、`var/` の内側や `HOME` の下にあるものもマウントされません（既定の `var/bin/scip-clang` は、そのファイル 1 つだけがマウントされます）。これらのパスのいずれかが、マウントされる別のディレクトリの内側にある場合（サーバーの Python プレフィックス内のミラー、システムパス下の `var/` など）は、サンドボックス内で空のマウントがそのパスを覆います。インストールルートは例外で、サーバーの Python がそこから動作している可能性があるためです。Python プレフィックスやシステムパスが Loom のデータパスのいずれかである、またはその内側にあるホストでは、サンドボックス化されたジョブは一切実行されません。空のマウントではそのデータを隠せないためで、`/v1/health` と `loom doctor` は `repo_sandbox_layout_refused` を報告します。チェックアウト（fetch の場合はベアミラー）と出力ディレクトリは書き込み可能で、チェックアウトの `.git` とハーネススクリプトは読み取り専用です。`/tmp` と `HOME` はジョブ終了後に削除される新規ディレクトリです。サーバーの環境変数、認証情報、ホストの `HOME`、Docker ソケット、その他のホストパスは見えません。
- rlimit は `prlimit` で設定します。`LOOM_CODE_SANDBOX_CPU_SECONDS`（7200）、`LOOM_CODE_SANDBOX_MEMORY_MB`（16384）、`LOOM_CODE_SANDBOX_MAX_FILE_MB`（32768）は bwrap の起動前に設定され、ジョブ全体の合計ではなく、ジョブの**各プロセス**（CPU 時間、データメモリ、ファイルサイズ）を制限します。`LOOM_CODE_SANDBOX_MAX_PIDS`（2048）は**1 つのジョブ**のプロセスとスレッドの数を制限します。これはジョブの user 名前空間の内側で設定され、カーネルはそのジョブのタスクだけを数えます（Linux 5.14 以降。それより古いカーネルは uid のタスクをホスト全体で数えます）。その外側では、RLIMIT_NPROC がホスト上の同じ uid のタスクすべて（他のコンテナのものを含む）を数えてしまいます。サンドボックスはサーバーの `PATH` にある `prlimit` を実行するため、それは `/usr` などのマウントされるシステムパスの下になければなりません。Loom は cgroup を設定しません。合計に厳密な上限が必要な場合は、コンテナ自体にメモリと pids の制限を与えてください。colour はそのバックストップとして、各コンテナに `pids_limit: 8192` を設定しています。各ジョブにはウォールクロックのタイムアウトもあり、ジョブのツリー全体を kill します。ジョブの pid 名前空間により、ジョブが起動したプロセスがジョブより長く生き残ることはありません。
- サーバー側で出力を制限します。ジョブの stderr と、ビルド、インストール、clone の stdout は、最後の 1 MiB だけを保持します。Loom が stdout を解析するコマンド（`ls-remote`、fetch）は最大 64 MiB まで出力でき、それを超えるとジョブ全体が kill されて失敗します。サーバーがジョブやチェックアウトから読み戻すファイルは通常ファイルであり、リンクは決して読みません。対象は、コンパイラティアの delta（`LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` と `LOOM_CODE_SCIP_DELTA_MAX_NODES` まで）、チェックアウトの `package.json`（1 MiB まで）、各インストールルートの `yarn.lock`（64 MiB まで）です。
- ネットワークはありません。ただし clone、fetch、サブモジュール、依存関係のダウンロードは例外で、その場合もサンドボックス外のエグレスプロキシ経由に限ります。プロキシは、443、22、およびリモート自身のポートへの `CONNECT` を受け付け、名前を解決し（`/etc/resolv.conf` だけを使います。`/etc/hosts` や nsswitch は使わず、search ドメインもないため、リモートには完全な DNS 名が必要です）、**すべての**アドレスが公開アドレスでない限り拒否し（ループバック、プライベート範囲、リンクローカル、クラウドメタデータアドレスは、リダイレクトや `.gitmodules` の URL 経由の場合も含めて拒否されます）、検証したアドレスに接続します。平文の HTTP にはルートがありません。1 つのジョブが同時に保持できるトンネルは最大 32 本です。サンドボックス内のクライアントはローカルリレー（その HTTPS プロキシ）経由でプロキシに到達し、リレーは最大 32 接続をブリッジします。それを超える接続は、ブリッジが 1 つ終わるまで待たされます。プロキシで上限を超えたその他の接続は、`LOOM_CODE_EGRESS_SLOT_WAIT_S`（既定は 5 秒）の後に拒否されます。ssh トンネルがスロットの一部を保持している間のリレー接続も同様です。プロキシは最初の拒否をログに記録し、拒否が 2 回以上あった場合はジョブ終了時にその合計を記録します。

  どちらの方向にも 1 バイトも流れないトンネルは、`LOOM_CODE_EGRESS_IDLE_TIMEOUT_S`（既定は 5 分）で閉じられます。クライアントが EOF を送った後は、サーバーからのバイトがないまま `LOOM_CODE_EGRESS_HALF_CLOSE_TIMEOUT_S`（既定は 10 秒）が経過すると閉じられます。低速なクライアントのためにバイトがまだ待機している間は、代わりにアイドルタイムアウトが適用されます。バイトが 1 つも流れる前にクライアントが EOF を送ったトンネル（クライアントが諦めた接続）は、ただちに閉じられます。プロキシ側のトンネルが終了した後（サーバーが閉じた、リセットした、またはプロキシが閉じた場合）、クライアントからのバイトが少なくとも 1 秒なければ、リレーは自身の接続を終了します。送られたデータを一切読まないクライアントは、アイドルタイムアウトまでリレー接続を、つまりリレーのスロットを保持し続けます。

リポジトリジョブが実行する git はすべて、システムやグローバルの git 設定を読まず、`https` と `ssh` のトランスポートだけを許可します（`protocol.allow=never`）。`ext::` と `file://` は拒否され、URL の書き換えも同様です。エントリのアクセストークンが渡されるのは、そのエントリの clone、fetch、C/C++ ティアの `git submodule update` だけで、ビルドや `go mod download` には渡されません。また git がトークンを送るのは、リモートのオーナー配下の URL（`https://host/owner/`。オーナーのパスがないリモートではホスト全体）に限られます。サンドボックス内の `ssh` リモートにはホストの SSH 鍵がありません。アクセストークン付きの HTTPS リモートを使ってください。

**サンドボックスを実行するスタック。** `redeploy.sh` がデプロイする blue/green の colour（`dev/docker-compose.colour.yml`）は、後述のコンテナオプションを設定します。他の Compose ファイル（`compose.yaml`、`dev/docker-compose.cloud.yml`）は設定しません。ホストが読み込んでいない AppArmor プロファイルはコンテナの起動を妨げるため、これらは Docker の既定のままで、プローブは失敗し、すべてのリポジトリジョブは通常のプロセスとして実行されます（_サンドボックスなし_ を参照）。ホストにプロファイルがある状態でそこでも実行するには、次のようにします。

- colour ファイルの `security_opt` ブロックと `pids_limit`（コンテナのバックストップ）をコピーします。`seccomp=` のパスはプロジェクトディレクトリからの相対パスです（`--project-directory` で変更しない限り、compose ファイルのあるフォルダ）。`dev/` 内のファイルでは `./seccomp/…` で動作し、`compose.yaml` では `./dev/seccomp/loom-repo-sandbox.json` が必要です。
- リリースのスタンドアロンバンドル（`loom-stack.tar.gz`）には、seccomp ファイルも AppArmor プロファイルも含まれていません。同じリリースのソースツリーから両方を取得し、`seccomp=` を JSON を置いた場所に向けてください。
- `redeploy.sh` が確認するのは colour の compose だけです。別のスタックを起動する前に、`dev/apparmor_check.sh <compose file>` と、後述の使い捨てコンテナでの確認を手動で実行してください。

ソースからの実行（`make server`）では、ホストが同じ要件を満たす場合にのみサンドボックスが使えます。

**プロファイルを読み込めるホスト。** 同梱のプロファイルは AppArmor 4.0 以降（`abi <abi/4.0>`）を対象としています。テスト済みなのは Ubuntu 24.04 だけで、CI の `sandbox-profile` ジョブが `kernel.apparmor_restrict_unprivileged_userns=1` の状態で確認しています。Debian 13 の 4.x パーサーはこの ABI を受け付けますが、そこでのプロファイルは未テストです。Docker Desktop（どの OS でも）や、Fedora や RHEL のような SELinux ディストリビューションなど、AppArmor のないホストでは、colour のオプションを安全に使えません。そこでは Docker が `apparmor=` を無視するため、`systempaths=unconfined` が外す `/proc` のマスクの代わりになるものがありません（後述の _`/proc`_ を参照）。古いパーサー（Ubuntu 22.04、Debian 12）はプロファイルを拒否します。それらのホストでサンドボックスを実行できるのは `seccomp=unconfined` と `apparmor=unconfined` を指定した場合だけで（_サンドボックスなし_ の直前の段落）、その場合は同じ `/proc` の保護が失われます。リリース版のスタックは、これらすべてのホストでサンドボックスなしで動作します。

`LOOM_CODE_SANDBOX_REQUIRED` が設定されていなければ、どのホストでも `LOOM_CODE_REPOS_JSON` に列挙されたリポジトリはインデックス化されます。サンドボックスが必要なのは、ページから追加されたリポジトリだけです。その場合に何が実行されるかは _サンドボックスなし_ に書かれており、リリース版スタックのコマンドは[クイックスタート](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)にあります。

**要件。** `PATH` 上の `bwrap` と `prlimit`（util-linux）（イメージには両方あります）と、非特権の user 名前空間が必要です。Docker の既定設定は bwrap を 3 通りの方法で妨げ、colour の compose はそれぞれを可能な限り狭く緩和します。

```yaml
security_opt:
  - seccomp=./seccomp/loom-repo-sandbox.json
  - apparmor=loom-repo-sandbox
  - systempaths=unconfined
```

- **seccomp。** Docker の既定プロファイルは、名前空間フラグ付きの `clone`、`unshare`、`mount`、`umount2` を `CAP_SYS_ADMIN` を持つコンテナにだけ許可し、`pivot_root` はどのコンテナにも許可しません。`dev/seccomp/loom-repo-sandbox.json` は Docker の既定プロファイル（moby/profiles `seccomp/v0.2.3`、Docker 29 が同梱するもの）に、これら 5 つをコンテナ内のすべてのプロセスに許可するルールを 1 つ加えたものです。bwrap にはその 5 つすべてが必要です。サンドボックスの内側では、Loom 独自のフィルタがジョブに対して `unshare`、`mount`、`umount2`、`pivot_root`、`CLONE_NEWUSER` 付きの `clone` を拒否します。他の新しい名前空間を要求する `clone` はフィルタを通過しますが、bwrap がジョブを `--cap-drop ALL` で実行するため、capability がなく失敗します。
- **AppArmor。** `docker-default` には `deny mount` があります。unprivileged user 名前空間を制限するホスト（Ubuntu 23.10 以降、`kernel.apparmor_restrict_unprivileged_userns=1`）では `apparmor=unconfined` は役に立ちません。そこでは、user 名前空間を作る unconfined プロセスは `unprivileged_userns` プロファイルに移され、マウントが拒否されます。`deploy/apparmor/loom-repo-sandbox` では、コンテナ内のすべてのプロセスが名前を変更した `docker-default` の下で動作し、`deny mount` は維持され、ABI 4.0 では `userns` ルールがないため user 名前空間は拒否されます。`/usr/bin/bwrap` の exec だけが子プロファイル `loom-repo-sandbox//bwrap` に移ります。これは同じ内容に `userns, mount, pivot_root` を加えたものです。bwrap とそれが起動するすべてのものは子プロファイルに留まるため、リポジトリジョブは AppArmor ではそれらを許可され、上述の Loom の seccomp フィルタによって拒否されます。境界は bwrap の exec です。サーバーと、サンドボックス外でサーバーが実行するツールは unshare も mount もできませんが、コンテナ内で `/usr/bin/bwrap` を exec できるプロセスなら、Loom のフィルタなしで user 名前空間を作り、その中でマウントできます。
- **`/proc`。** Docker は `/proc` と `/sys` 配下のパスをマスクし、そのためカーネルはサンドボックスが行う新しい procfs のマウントを拒否します。`systempaths=unconfined` はそのマスクを外します。これにより、`/proc/sys`、`/proc/sysrq-trigger`、`/proc/irq`、`/proc/bus`、`/proc/fs` の読み取り専用マウントや、`/proc/kcore`、`/proc/keys`、`/sys/firmware` などへのマスクも失われます。どちらのプロファイルでも、`docker-default` のルールが `/proc/sys`、`sysrq-trigger`、その他 `/proc` と `/sys` 配下のシステムファイルへの書き込み、および `kcore` と `/sys/firmware` へのあらゆるアクセスを引き続き拒否します。AppArmor のないホストでは、Docker は `apparmor=` オプションを無視し、マウントの代わりになるものがないため、`redeploy.sh` はそのようなホストを拒否します。

**`--disable-userns`。** 子プロファイルは `docker-default` の `/proc/sys` への書き込み拒否を維持しており、これが bwrap の `--disable-userns` が行う sysctl の書き込みを拒否します。許可ルールは拒否を上書きできず、パスが取りうる両方の形式について拒否の glob を絞ると、レビューが難しくなります。そのため、本番を含むこのプロファイルでは、プローブが bwrap による `max_user_namespaces` への言及を検出し、それを INFO でログに記録し、フラグなしで bwrap を実行することが想定されています。Loom のフィルタが単独で新しい user 名前空間を拒否します。それ以外のプローブ失敗は WARNING でログに記録され、サンドボックスは無効のままになります。

**プロファイルの読み込み。** Docker ホストで一度読み込んでください。`/etc/apparmor.d` は起動のたびに再読み込みされます。

```bash
sudo install -m644 deploy/apparmor/loom-repo-sandbox /etc/apparmor.d/loom-repo-sandbox
sudo apparmor_parser -r -W /etc/apparmor.d/loom-repo-sandbox
```

**確認方法。** `apparmor_parser -Q -K` はプロファイルを受け付けますが、bwrap がそのプロファイルの下で動くかどうかは、ホストが読み込んで初めて分かります。それが最初に行われるのは CI です。`.github/workflows/docker-smoke.yml` の `sandbox-profile` ジョブは、Ubuntu 24.04 で `kernel.apparmor_restrict_unprivileged_userns=1` を有効のままプロファイルを読み込み、本番イメージで colour のオプションを使ってプローブと hostile スイートを実行します。デプロイ用ホストでは、稼働中のものではなく、これからデプロイするビルドから作った使い捨てコンテナで確認してください（`True` はサンドボックスが動作することを意味します）。

```bash
docker build -q -t loom:sandbox-check .
docker run --rm --network none --user loom -w /app --entrypoint python \
  --security-opt seccomp=dev/seccomp/loom-repo-sandbox.json \
  --security-opt apparmor=loom-repo-sandbox --security-opt systempaths=unconfined \
  loom:sandbox-check -c 'from server.code.app.index import sandbox as s; print(s.sandbox_available())'
docker image rm loom:sandbox-check
```

**`redeploy.sh` が確認すること。** colour をビルドする前に、プロファイルが読み込まれていない場合と、AppArmor のないホストでは停止します。新しい colour の起動後、プロキシをそちらに切り替える前に、`dev/sandbox_probe.sh` が、その colour のコンテナのうちプロファイルの下で動くもの（`loom-<colour>` と `loom-<colour>-workers`）それぞれで同じプローブを実行します。いずれかが失敗した場合、または検査できなかった場合は、その理由を出力し、新しい colour のコンテナを削除して、古い colour が稼働したまま終了します。停止ではなく削除するのは、ビルドがその colour の以前のビルドをすでに置き換えているため、それはロールバック先にならず、そのワーカーがリースを取得してサンドボックスなしでジョブを実行する可能性があるからです。これは、上記の確認を省略したデプロイで、実際のプロファイルが最初に確認される場所です。

**サンドボックスを必須にする。** プローブは切り替えを制御しますが、新しいワーカーはその前に起動し、スーパーバイザーのリースを取得して（初回デプロイ時、または古いリースが空いているとき）リポジトリジョブを開始する可能性があります。colour が設定する `LOOM_CODE_SANDBOX_REQUIRED=true` はこの隙間を塞ぎます。サンドボックスが動作できない場合、リポジトリジョブ（clone、fetch、依存関係のインストール、コンパイラティア、`loom code scip-full` のハーネス）は通常のプロセスとして実行されません。ingest パスとコンパイラティアは、オペレーターのエントリを含むすべてのエントリをスキップし、そのスキップをパスのノートに記録します。`loom code scip-full` は 1 で終了し、それでもランナーに到達したジョブは `SandboxRequiredError` で失敗します。`/v1/health` は `repo_jobs_unsandboxed` をエラーとして報告します。`loom code init` の最初のベア clone はワーカーの clone であり、他と同じリポジトリジョブなので、同様に拒否されて 1 で終了します。

**無効にする。** colour をサンドボックスなしで再び実行するには、`dev/docker-compose.colour.yml` の両方のサービスから `security_opt` ブロックと `LOOM_CODE_SANDBOX_REQUIRED` の両方を削除し、再デプロイします。`security_opt` だけを削除すると、すべてのリポジトリジョブが拒否され、`/v1/health` は degraded になります。

`no-new-privileges` は設定しません。これを設定すると、カーネルが子プロファイルへの exec 遷移を拒否し、bwrap が起動しなくなります。そのため、イメージの setuid バイナリ（`su`、`mount`、`passwd` など）は、Docker の既定と同様にコンテナ内で引き続き動作します。それらを `/proc/sys` から遠ざけているのは、読み取り専用マウントではなく、プロファイルのルールです。

これらのオプションでも、コンテナ自体の閉じ込めは弱まります。seccomp は、コンテナ内のすべてのプロセスに 5 つの呼び出しを許可します（bwrap の外では、AppArmor と `CAP_SYS_ADMIN` の欠如が引き続きそれらを拒否します）。AppArmor は、bwrap とその配下で実行されるものにユーザー名前空間の作成とその内部でのマウントを許可します。これは bwrap を exec できるすべてのプロセスに及びますが、リポジトリジョブは例外です（Loom のフィルタ）。そして `/proc` と `/sys` は、Docker のマスクではなくプロファイルのルールに依存します。これを、サンドボックスなしでリポジトリのコードを実行する場合と比較して検討してください。`seccomp=unconfined` と `apparmor=unconfined` も、ホストがユーザー名前空間を制限していない環境では機能しますが、プロファイル全体が無効になります。

**サンドボックスがない場合**、Loom はプロセスごとに 1 回プローブを行い、警告をログに記録し、`/v1/health` と `loom doctor` に `repo_jobs_unsandboxed` を報告します。`LOOM_CODE_SANDBOX_REQUIRED=true` の場合、すべてのリポジトリジョブが拒否されます（上記）。それ以外の場合は次のとおりです。

- オペレーターのリポジトリ（`LOOM_CODE_REPOS_JSON`）はすべてのティアを維持し、ビルドステップはこれまでどおりホスト上の通常のプロセスとして実行されます。
- リモートがネットワーク URL である自己登録のリポジトリは、クローンもフェッチもされず、コンパイラティアも使われません。egress プロキシがなければ、ジョブが接続するホストとポートを検証するものがないためです。取り込みパスのノートと `workers.status` がこのスキップを示します。サンドボックスが利用可能になると、そのエントリは再開されます。
- ローカルパスの自己登録リポジトリは、パーサーティアと静的コンパイラティアを維持しますが、ビルドベースのティア（C/C++）は使われません。
- ページからのリポジトリまたはブランチの追加は、`SANDBOX_UNAVAILABLE` で拒否されます。

各 Go エントリは、`LOOM_CODE_SCIP_GO_MODCACHE` の下（`<root>/<org>/<namespace>`）に独自のモジュールキャッシュを持ちます。`go` はキャッシュ済みのモジュールのチェックサムを再検証しないため、あるリポジトリのジョブが書き込んだキャッシュが別のリポジトリのジョブに使われることはありません。

<a id="mirror-housekeeping"></a>

### ミラーのハウスキーピング

各サービングプロセスは、`LOOM_CODE_MIRROR_GC_INTERVAL_H` ごとに、そのホストのミラーに対して `git gc --no-prune` を実行します（CPU と IO の優先度は低く、2 つ目のコピーと `LOOM_CODE_MIRROR_GC_MARGIN_GB`（デフォルトは 5 GB）を置く余裕がある場合のみ。余裕がない場合、gc は次の間隔まで待ち、警告をログに記録します）。また、`LOOM_CODE_REPOS_ROOT` から、有効なエントリのどれにも参照されていないミラー、`checkout-*` ツリー、ミラーのロックファイルを削除します。これにより、削除されたリポジトリは、`code.repo.remove` が到達したホストだけでなく、すべてのホストから消えます。`loom.host.disk.free_bytes` / `loom.host.disk.total_bytes` ゲージは、ディスクアラームの入力です。

<a id="measured-coverage"></a>

## 計測されたカバレッジ

CI で行単位のテストごとのカバレッジを取る計装ビルドを実行しているリポジトリでは、テストが名前で参照しているものだけでなく、各テストが実際に実行したものをテストマップに反映できます。インポートは、CI の `checks_coverage_lines` テーブルと `checks` テーブルを保持する ClickHouse サーバーから最新の完全な実行を読み取ります。読み取りは、ファイルハッシュによる `LOOM_CODE_COVERAGE_SHARDS` 個のスライスに分けて行います。1 つのスライスは、そのサーバーのクエリごとのメモリに収まる必要があります。ステートメントの形ごとに 1 時間あたりのクォータがあるサーバーでは、インポート 1 回分（スライスごとに 2 つのステートメント）に加えて再試行 1 回分も収まる必要があります。公開プレイグラウンドでは、ステートメントの形ごとに 1 時間あたり 100 件まで許可されます。デフォルトの 16 スライスでは各形で 16 件を使うため、ドライランと本番実行を同じ 1 時間内に行えます。

インポートは次を書き込みます。

- (ソースファイル, テスト) ごとに 1 行の `code_test_map` 行を、`method = coverage` で書き込みます。対象は、テストが、到達するテスト数が `LOOM_CODE_COVERAGE_SPECIFIC_MAX_TESTS` 以下の行を実行した場合です（すべてのクエリが実行する行は、どのテストも指名しません）。`confidence` は、そのペアの最も特定的な行を共有するテストの数が増えるほど下がり、`evidence` には実行のコミットと特定的な行の範囲が入ります。
- ソースファイルごとに 1 行の `code_coverage_files` 行を、いずれかのテストが実行したすべての範囲とともに書き込みます。これにより `code.coverage_claim` は、「多くのテストが実行した」ものと「どのテストも実行していない」ものを区別できます。

最新の実行に含まれなくなった行は、すべてのスライスを読み終えた後にのみ、トゥームストーンとして処理されます。`code.coverage_claim` は、計測された行の範囲が対象のスパンと重なる場合に、その行を他のどのティアよりも上位の `executed` として順位付けします。実行がファイルをカバーしているのに、どのテストもその行を実行していない対象は `uncovered` となり、計測結果は `measured` に入ります。

```bash
LOOM_CODE_COVERAGE_SOURCE_URL=https://<coverage server>
LOOM_CODE_COVERAGE_SOURCE_USER=<reader>
LOOM_CODE_COVERAGE_NAMESPACES=<namespace>[,<namespace>]
loom code coverage-import --namespace <namespace> --dry-run   # counts only, writes nothing
loom code coverage-import --namespace <namespace>
LOOM_CODE_COVERAGE_IMPORT_ENABLED=true                         # then daily, in the workers
```

<a id="another-branch"></a>

## 別のブランチ

名前空間は1つのブランチを追跡し、ブランチがインデックスされるのは、エントリでそのブランチが指定されている場合だけです。同じ `repo` で、`namespace` と `git_branch` が異なるエントリを追加します。このエントリは bare クローンを共有します。`embed_reuse_from` にメインブランチの名前空間を設定すると、内容が同一のチャンクは埋め込みを再利用します。その後、最初のインデックスのときと同様に、新しい名前空間に対して `loom code init` と `loom code backfill` を実行します。

<a id="repairs"></a>

## 修復

`loom code reconcile --namespace NS` は、チャンクがブランチ先頭での最終変更より古いインデックス済みパスを一覧表示します。`--heal` を指定すると、ウォーターマークを動かさずにそれらを再取り込みします。

**エクストラクタの変更後。** `EXTRACTOR_MODULES`（`server/code/domain/extract/extractor_stamp.py`）にあるモジュールのコードとグラマーのバージョンが、インデックスのバージョンスタンプを構成します。スタンプはコードトークンのみをハッシュするため、コメントや docstring の編集ではスタンプは変わりません。デプロイでスタンプが変わると、`code_ingest` は変更が影響する言語ファミリーに属する全名前空間のライブファイルを、1 パスあたり `LOOM_CODE_EXTRACTOR_RECHUNK_PER_PASS` 件ずつ再導出します。このとき、スタンプが変わったステージ（chunk、symbol、edge、guard）のみを実行します。進捗は `code.index_status` の `extractor_stamp*` 状態キーで確認できます。大規模なインデックスでは数時間かかります。

**スタンプ外の変更後**（言語レーン、ファセット、新しいファイル種別）は、既存の行は自動的には修復されません。影響を受けるパスを、その名前空間自身のウォーターマークで再取り込みしてください。

```bash
HEAD=$(docker exec loom-workers loom code stats --namespace "$NS" | awk '$1 == "git_head" {print $2}')
git --git-dir var/code_repos/<name>.git ls-tree -r --name-only "$HEAD" | grep -E '\.(sql|sh)$' > /tmp/$NS-paths.txt
docker cp /tmp/$NS-paths.txt loom-workers:/tmp/
docker exec loom-workers loom code backfill --namespace "$NS" --commit "$HEAD" --no-fetch --embed \
  --paths-file /tmp/$NS-paths.txt
```

`--paths-file` は修復モードです。列挙されたパスのみを再取り込みし、ウォーターマークには触れません。内容が変わっていないファイルは、チャンクとエンベディングがそのまま保持されます。シンボル、ファセット、エッジは再導出されます。多数のファイルで同時にシンボルが増えた場合は、`--edges-second-pass` を追加してください。

オプションのレーンは設定で切り替えます。有効にした後は、該当するパスに対して修復を実行してください。

| 設定                              | デフォルト | レーン                                                                                |
| --------------------------------- | ---------- | ------------------------------------------------------------------------------------- |
| `LOOM_CODE_SQL_STATEMENT_RECORDS` | off        | `.sql` ファイルを、ステートメントごとに 1 つの `statement` シンボルとして扱い、ダイアレクトとテーブルのファセットを付与します |
| `LOOM_CODE_SHELL_ISLANDS`         | off        | シェルスクリプトから発行される SQL をステートメントレコードとして扱います               |
| `LOOM_CODE_PY_SQL_ISLANDS`        | off        | Python の呼び出し内の SQL 文字列をステートメントレコードとして扱います                 |
| `LOOM_CODE_HOST_ISLANDS`          | off        | CI ワークフロー、Dockerfile、Makefile のジョブ、ステージ、ターゲットをシンボルとして扱います |

`scripts/ops/code_edge_audit.py` は、パーサティアのコールグラフにある 2 種類の見えない欠陥（呼び出し先に定義が存在するようになったのに拒否されたままの呼び出し箇所と、ターゲットが消えた低品質のエッジ）を調べる読み取り専用の集計ツールです。ステージング環境のコピーに対して `LOOM_BACKGROUND_WORKERS=off` を指定して実行してください。

<a id="maintenance-commands"></a>

## メンテナンスコマンド

| コマンド                                     | 動作                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom code compact [--table T] [--apply]`    | コードベクトルテーブルのキーごとの物理コピー数が `LOOM_CODE_COMPACT_MIN_RATIO`（1.5）に達した時点で `OPTIMIZE … FINAL` を測定し、`--apply` を付けると実行してマージの完了を待ちます。バルク処理は `LOOM_CODE_COMPACT_AFTER_BULK_ROWS` 行の後に自動で実行します（[メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#compaction-and-optimize)） |
| `loom code warm <questions.txt>`             | 各質問を一度ずつ実行し、`code.ask` の回答を全員が使えるようキャッシュします（テキスト行、または `question` を持つ JSONL）。ヒットは無料のため、再実行しても安全です                                                                                                                                                                       |
| `loom code answers-purge`                    | 合成モデルの変更後に、キャッシュ済みの `code.ask` の回答を破棄します（それ以外では、引用されたファイルが変更されたときにのみキャッシュが無効になります）。`--apply` を付けるまではドライランです                                                                                                                                                              |
| `loom code repack`                           | 最近マージされた PR のコンテキストパックを、現在の形式で再構築します                                                                                                                                                                                                                                                     |
| `loom code crossref`                         | SQL／シェルのテストから、それを実装する C++ への参照付けを行うパスです（冪等）                                                                                                                                                                                                                                                       |
| `loom code coverage-import [--dry-run]`      | 設定されたソースにある、テストごとの行カバレッジの最新の実行結果をテストマップにインポートします（[計測されたカバレッジ](#measured-coverage)）                                                                                                                                                                                      |
| `loom code reembed`, `loom code vector-twin` | 名前空間のチャンクを 2 つ目の埋め込みモデルのラベルで書き込む、または ClickHouse 内でベクトルの Matryoshka プレフィックス版を埋めます。再開可能です                                                                                                                                                                        |
| `loom code lint-instructions`                | `CLAUDE.md`、`AGENTS.md`、`.claude/**` およびスキル内にある、head で解決できなくなったパスと `Qualified::symbol` 参照を一覧表示します。CI 向けに `--fail-on-stale` を使えます                                                                                                                                                            |
| `loom code export-matrix`                    | マージ済み PR の共変更から、「このファイルが変わると、通常これらも変わる」という表を、指示ファイル向けに書き出します                                                                                                                                                                                                    |
| `loom code export-pairs`                     | 実際のトラフィックから（クエリ → 操作対象のユニット、ハードネガティブ）の検索ペアをエクスポートします。エンベッダーやリランカーのチューニング向けです                                                                                                                                                                       |

`make bench-code-intel LOOM_BASE_URL=…` は、正解の質問セットを持つリポジトリを対象に、実際のプロバイダーを使うサーバーに対してコードインデックスの品質ゲート（`bench/code_intel_qa.py`）を実行します。CI の埋め込みはダミーのため、CI では実行しません（[ベンチマーク](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。

<a id="rollout-and-shutdown"></a>

## ロールアウトとシャットダウン

- `LOOM_MCP_CODE_TOOLS`（デフォルトはオフ）を設定すると、デフォルトの MCP ツールセットに `code.*` ツールが一覧表示されます。`coding` ツールセットでは設定に関係なく一覧表示され、HTTP と SDK からも常に呼び出せるため、チャットクライアントに公開する前にインデックスを評価できます。
- `LOOM_CODE_INTEL_ENABLED=false` にすると、`code.repo.remove` を除くすべての `code.*` オペレーションが事前条件エラーを返します。あわせて、カバレッジのインポートワーカー、通知ワーカー、およびミラーのホストループも停止します。それ以外のコードワーカーは、それぞれ独自のスイッチを持ちます（[ワーカー](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)）。
- **リポジトリを廃止する。** メンテナーは、リポジトリの Access タブで **Retire…** を使います（`code.repo.remove` の `confirm` に名前空間を指定します）。エントリは非アクティブになり、消去サガがその名前空間の `loom.code_*` テーブルをすべて削除します。ベアミラーは、他のブランチが使用していなければ削除されます。検索テレメトリは保持されます。オペレーターが設定したエントリは `REPO_FROM_CONFIG` で拒否されるため、代わりに `LOOM_CODE_REPOS_JSON` から削除してください。`LOOM_CODE_INTEL_ENABLED=false` の場合、ページはリポジトリを一覧表示できませんが、HTTP、MCP、SDK 経由の `code.repo.remove` は引き続き処理されます。その場合、他のレプリカは、コードインテリジェンスが再びオンになってから、自分が持つミラーのコピーを削除します。
- **インデックスを消去する。** コード名前空間に対して `scope_type = namespace` で `erasure.request` を実行すると、`loom.code_*` テーブルも削除されます。まだ設定に残っているエントリは、次のパスで最初から再インデックスされるため、先に廃止してください。

<a id="when-a-tier-fails"></a>

## ティアが失敗したとき

エントリを最新に保つ各ティア（ミラーの fetch、パーサーの ingest、およびすべてのコンパイラティア）は、直近の結果をエントリに記録します。失敗した場合は、認証情報を伏せ字にしたエラー、エラーの種類、その種類に対する唯一の対処方法、`failing_since`、連続して失敗したパスの回数が記録されます。次に成功したパスがこの記録を消去します。cron から実行するティアは、`loom code entry-lock --tier <tier>` の下で実行された場合に同じように記録します。`loom code scip-delta` と `scip-full` は自分で記録します。

このホストが依存関係を取得できない Go モジュール（Go は、多くの場合 vanity import path を通じたリポジトリ自身のモジュールについて、プライベートリポジトリであると報告します）は、失敗ではありません。Go ティアは残りをインデックスし、ティアを `unavailable`、種類を `dependency_unavailable` として、対処方法とともに記録します（`LOOM_CODE_SCIP_GO_PRIVATE` を設定し、ティアのユーザーにそのホスト向けの git 認証情報を与えます。パスやバージョンが誤っている、または存在しない場合も、Go は同じことを報告します）。この注記は一度だけ書き込まれ、パスとして数えられず、`code_tier_failing` 問題になることもなく、`as_of.failing_tiers` にも含まれないため、エージェントのコード回答はこれらのモジュールにフラグを立てません。この注記は、モジュールをビルドした直近の実行を反映します。何もビルドしないパスはこの注記をそのまま残し、そのような失敗なしにビルドした次の実行がこれを消去します。したがって、依存関係を取得できないままのモジュールが次に変更されたときに、再び現れます。コマンドの終了ステータスと出力だけを見る `entry-lock --tier go` の下では、そのような失敗のない正常終了がこれを消去します。それ以外の理由で失敗したモジュールは引き続きティアを失敗させ、その記録には到達できないモジュールも挙げられます。

記録は、各読み手がすでに参照している場所で読めます。

- エージェントのコード回答は、これを `as_of.failing_tiers` に含めます（[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#the-as_of-block)）。
- `code.index_status` と `code.repos` は、これを `tier_health` で返します。ダッシュボードのリポジトリカードには、失敗しているティア、失敗し始めた時刻、どれだけ遅れているか、対処方法が表示され、`unavailable` のティアがあればその対処方法も表示されます。
- ティアが `LOOM_CODE_TIER_FAILING_WARN_PASSES` 回連続で、または `LOOM_CODE_TIER_FAILING_WARN_HOURS` 時間にわたって失敗すると、`/v1/health`、ダッシュボードのバナー、`loom doctor` に `code_tier_failing` 問題として表示されます（[モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#v1health)）。失敗しているエントリとティアごとに最大5行まで、理由とともに1行ずつ表示され、異なる対処方法はそれぞれ一度だけ表示されます。デプロイ自身の認証情報で読み取る `loom doctor` は、挙げた各エントリも最大5件まで、repository@branch (org/namespace) の形式で、理由を示すエラー行とともに名前で示します。残りは `code.repos` が名前を示します。プラットフォーム管理者のトークンは、[制限付き](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#restricted-namespaces)ではないすべてのエントリについて同じ内容を読み取れます。制限付きのエントリは「a restricted entry」と表示され、分類を読み取れないエントリも同様です。それ以外の呼び出し元は、`/v1/health` がトークンなしで応答するため、どのエントリも読み取れません。
- プッシュ型のアラートには、`loom.code.tier_failing` と `loom.code.tier_lag_commits` のゲージが、`code_tier_failing` 問題が報告する各ティアを示します（[テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)）。アラートは `deploy/dashboards/code_plane.json` にあり、Prometheus の場合はチャートのルールがあります（[アラートルール](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md#alert-rules)）。

種類（`_KINDS`）とその対処方法（`fix_for`）は、[`server/code/domain/index/tier_health.py`](https://github.com/ClickHouse/loom/blob/main/server/code/domain/index/tier_health.py) にあります。どの種類にも一致しない失敗は「unclassified error」と表示され、読むべき出力の末尾が付きます。

<a id="failure-model"></a>

## 障害モデル

| 障害 | 挙動 | 復旧 |
| --- | --- | --- |
| `git fetch` が失敗する | ウォーターマークは動かない。エントリの `fetch` ティアがエラーとその種別、対処方法を記録する（`tier_health`） | 次回のパス（スーパーバイザのバックオフ） |
| コミットの途中でクラッシュする | コミットを再実行する | 冪等なアップサート |
| ファイルのパースに失敗する | シンボルなしのチャンク。`code_files.index_error` が設定される（`code_parse_timeout_s` または `code_parse_memory_mb` を超えたパースと、そのプロセスで実行できなかったパースを含む） | `code.index_errors` が一覧を返す。次の変更または修復で解消される（どちらかの上限を引き上げても、再読み込みは行われない） |
| すべてのファイルが `LookupError` "... is loaded from outside the interpreter's site-packages" でパースに失敗する | すべてのファイルでシンボルなしのチャンクになる。tree-sitter またはグラマーが、パースプロセスから見えない場所にインストールされている | tree-sitter のパッケージを、`PYTHONPATH` やユーザーサイトではなく、サーバーのインタプリタ自身の環境にインストールする |
| 埋め込みプロバイダがダウンしている | ベクトルなしでチャンクを保存する。字句検索は引き続き利用できる | `code_embed_recovery` |
| コンパイラティアの適用に失敗する | バッチが `failed` になり、エラーが記録される | `loom code scip-retry <batch_id>` |
| コンパイラティアのパスが失敗またはハングする | ハングはハーネスのタイムアウトで終了し、それ以外はパスの期限（そのタイムアウトに git の各ステップと適用のための余裕を加えたもの）で取り消される。エントリのロックは解放される。パスが報告した失敗（ハーネス自身のタイムアウトを含む）は、種別と対処方法とともに `tier_health` に記録される | 次回のパス。`workers.status` がタイムアウトを示す |
| コンパイラティアのウィンドウの基点が不明（履歴の書き換え、プルーニングされたミラー） | `scripts/scip_harness/` のすべてのティアが、空のデルタではなく終了コード 3 で終了する。Python、TypeScript、Go のワーカーはそのティアのウォーターマークをクリアする | 次回の実行はそのティアの完全なインデックス作成になる（C/C++ では `code_scip_full`） |
| コンパイラティアのデルタがリンクである、通常ファイルでない、サイズ超過である、または Go ハーネスがモジュール数より多くのデルタを書き出す | デルタは拒否され（`FileRefused`、または `Go harness wrote more deltas than its N module(s)`）、エントリのパスは失敗する。ウォーターマークは動かない | サイズ超過の場合：`LOOM_CODE_SCIP_DELTA_FILE_MAX_MB` と `LOOM_CODE_SCIP_DELTA_MAX_NODES` を引き上げる（これらは作業量と保持されるパス文字列を制限する）。ラウンドの予算を超えたファイルエントリの場合：ワーカーのメモリの範囲内で `LOOM_CODE_SCIP_DELTA_ROUND_MAX_NODES` を引き上げる。固定の文字数上限を超えたファイルエントリは名前が示されるが、その上限を変更する設定はない。分割すべきなのはインデックス対象のファイルである。リンク、通常ファイルでないもの、余分なデルタは、リポジトリが変更されるかエントリが削除されるまで、パスごとに繰り返し発生する |
| GitHub のレート制限 | パスは終了し、カーソルは動かない | 次回のパス |
| ブランチが強制プッシュされた（ウォーターマークがもはや祖先ではない） | ウォーターマークが新しい head にリセットされ、ログに記録される | `loom code backfill` で再導出する |
| 週次のグラフ統計が失敗する | 先週のランクが引き続き使われる | 次回の週次パス |

コードインデックスのゲージ（`loom.code.index_lag_commits`、`loom.code.stale_files`、`loom.code.tier_lag_seconds`、`loom.code.embed_backlog`、`loom.code.files_errored`）は[テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)に掲載されています。アラートの対象は[モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#what-to-alert-on)を参照してください。「Loom — Code Plane」ダッシュボードは `deploy/dashboards/code_plane.json` で定義されています。

<a id="related"></a>

## 関連ドキュメント

- [リポジトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)：セルフサービスのリポジトリ、メンテナー、リーダー
- [鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)：クライアントがインデックスの遅延を読み取る方法
- [コードインデックスのアーキテクチャ](../architecture/code-index-architecture.md)
