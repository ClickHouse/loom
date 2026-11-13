<!-- i18n: source=docs/getting-started/quickstart.md blob=2dcc795de559021866a2affe24bef8e4e8f41f81 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# クイックスタート

このページでは、ノート PC 上で Loom を起動し、HTTP、Python、MCP の 3 つの方法で最初のメモリを保存します。API キーもアカウントも不要です。リリース版スタックを新規インストールした場合、イメージに含まれる小さなローカルモデルで埋め込みを行うため、ネットワークがなくても検索は意味のあるヒットを返します（アップグレードした環境では、`.env` で指定されている embedder がそのまま使われます）。一方、LLM は偽物（fake）なので、抽出や `memory.ask` の回答は何も生成しません。チェックアウトした リポジトリの `.env` では、両方とも fake プロバイダーを使います。

**標準の Docker インストールはシングルユーザー向けです。** ログインはなく、アクセスは localhost のみに限定され、名前空間は既定で `dev/local` になります。マルチユーザーモードを有効にしなくても、1 人で複数のエージェントを接続できます。**チーム向けのデプロイも OSS でサポートされており**、認証付きアカウントと名前空間のアクセス制御を利用できます。[マルチユーザーモードで起動する](#start-in-multi-user-mode)か、[Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) を参照してください。
どちらのモードでも Loom の機能は同じです。実際の LLM や埋め込みを使うには、どちらのモードでもプロバイダーの設定が必要です。

<a id="prerequisites"></a>

## 前提条件

| 必要なもの                     | 用途                                                                                     |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| compose プラグイン付きの Docker | スタック（ClickHouse と Loom サーバー）の実行。                                          |
| Python 3.12 以降               | コントリビューター専用の `loom` CLI。SDK は 3.11 以降をサポートしています。              |
| Node.js 20 以降                | TypeScript SDK（任意）。                                                                 |
| 空いているポート `7777`        | Loom 用。チェックアウトした状態での開発では、ClickHouse も `8123` と `9000` で公開されます。 |
| `jq`                           | ステップ 4 の `curl` の出力を読むため（任意）。                                          |

<a id="1-boot-the-stack"></a>

## 1. スタックを起動する

<a id="fast-path-released-image-no-clone"></a>

### 最短手順：リリース済みイメージを使う（clone 不要）

前提条件：Docker Desktop、または Compose 2.20 以降を含む Docker Engine。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

このコマンドはインストーラーをカレントディレクトリに保存してから、そのファイルを実行します。そのため、ホームディレクトリ、または自分だけが書き込めるディレクトリで実行してください。共有ディレクトリでは、実行前に他のユーザーが `loom-install.sh` を差し替えたり、別のファイルを指すようにしたりできます。ダウンロードに失敗した場合は、curl のエラーと 0 以外の終了コードで停止します。インストールに失敗した場合は、報告された問題を修正してから、コマンド全体をもう一度実行してください。インストーラーは、最新の GitHub Release から `loom-stack.tar.gz` をダウンロードして SHA-256 を検証し、Compose と ClickHouse の設定を `~/.loom` にインストールします。続いて、対応する `ghcr.io/clickhouse/loom:X.Y.Z` と `ghcr.io/clickhouse/loom-extractor:X.Y.Z`（ドキュメント抽出サービス）のイメージを pull し、`http://localhost:7777/v1/health` が応答するまで待ちます。サーバーイメージには、API、ワーカー、マイグレーション、そしてサーバーのルートで配信されるビルド済みの React ダッシュボードが含まれており、Python や Node のインストールは不要です。Python、TypeScript、Go のコンパイラインデクサーも含まれ、有効になっています。TypeScript インデクサーでインデックスしたリポジトリは、依存関係が自動的にインストールされます。どちらのイメージも Linux の `amd64` と `arm64` に対応しており、Apple Silicon 上の Docker Desktop でも動作します。

このスタックは専用の ClickHouse コンテナを実行します。すでに運用している ClickHouse（セルフマネージドのサーバー、または Keeper 付きの ClickHouse Cloud サービス、26.4 以降）を使うには、その URL を渡します。URL にパスワードが含まれていない場合、インストーラーがパスワードを尋ね、`loom` データベースと読み取り専用のリーダーユーザーを作成します。あるいは、管理者が実行すべき SQL を表示します（[ClickHouse の選び方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)）。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

各リリースの `install.sh` は、そのリリース自身をインストールします。そのため、`latest` が配信するコピーは最新リリースをインストールし、下記のようにリリース固有の URL を使えば、毎回そのリリースがインストールされます。新しいバージョンがインストールされている環境に対して古いリリースのコピーを実行すると、新しいデータの上で古い Loom と ClickHouse を起動してしまわないよう、インストールを拒否します。意図的にダウングレードする場合は、[インストーラーでインストールした環境のダウングレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install)に従ってください。リリースを固定するには、そのリリース固有の URL を使います。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

起動前にすべてを確認したい場合は、ファイルをダウンロードして検証します。

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

出力内容に問題がなければ、検証済みのバンドルからインストールします。

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

インストーラーに渡した `LOOM_PORT`、`LOOM_BIND_HOST`、`LOOM_IMAGE_REPOSITORY`、`LOOM_EXTRACTOR_IMAGE_REPOSITORY`、`LOOM_CLICKHOUSE_IMAGE` は、インストールディレクトリの `.env` に書き込まれます。そのため、以後そこで実行する `docker compose` コマンドや、以後のインストーラーの実行でも、同じポートとイメージが使われます。ループバック以外の `LOOM_BIND_HOST` を指定すると、Loom がこのホストの外部に公開されます。その場合、`LOOM_PUBLIC_BASE_URL` とサインインを設定しないと、サーバーは起動を拒否します。`LOOM_SKIP_START=1` を付けてインストールし、`.env` にそれらを設定したうえで（下記参照）、`LOOM_SKIP_START=1` なしでインストールコマンドをもう一度実行してください。

各リリースは、ビルド時点で最新だった ClickHouse リリースを実行します。そのため、同じ Loom バージョンに対してインストールコマンドを再実行しても ClickHouse は変わりませんが、より新しい Loom バージョンをインストールすると ClickHouse も更新されます。ただし、`.env` で `LOOM_CLICKHOUSE_IMAGE` を固定している場合は更新されません（その行を削除すると、再びリリースに追従します）。インストールコマンドを再実行すると、管理対象の Compose／設定ファイルが更新され、コンテナが再調整されます。`~/.loom/.env` と名前付き Docker ボリュームは保持されます。エクストラクターの URL とトークンがない場合は、それらが追加されます。データを削除せずに停止するには、次を実行します。

```bash
docker compose --project-directory "$HOME/.loom" down
```

`--volumes` は、元に戻せないリセットを意図的に行う場合にのみ付けてください。

<a id="try-it-in-github-codespaces"></a>

### GitHub Codespaces で試す

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

リポジトリの dev container は、手元のマシンに何もインストールせずに、codespace 内で最新リリースを実行します。上記のインストーラーで Loom をインストールして起動し、Claude Code を接続します。初回の起動では約 3 GB のイメージをダウンロードするため、数分かかります。ダッシュボードは、作成ログの最後の数行に表示されるアドレスで開きます（`Loom is running in the codespace. Dashboard: https://<codespace name>-7777.app.github.dev`。これは **Ports** タブにある、ポート 7777 の転送先アドレスです）。その上に表示されるインストーラーの `http://127.0.0.1:7777/` は、codespace 自身のアドレスです。codespace のターミナルで `cd ~ && claude` を実行し（チェックアウトしたディレクトリの外で実行します。そこにある指示は Loom 自体の開発向けです）、サインインして「Remember that I deploy only on Tuesdays」と伝えます。新しい `claude` セッションでは「When do I deploy?」と尋ねてください。

この試用環境は、サインインなしのシングルユーザーで動作します。ブラウザーでは、ポート 7777 は非公開で転送されます。そのアドレスに応答するのは、自分の GitHub アカウントだけです。非公開のままにしてください。公開にしたり、組織内限定にしたりすると、サインインのないサーバーが、共有された全員に開かれてしまいます。codespace 内のエージェント（たとえばその `claude`）は `http://localhost:7777` を使います。ダッシュボードの接続行には転送先アドレスが表示されますが、そのアドレスには GitHub のサインインが必要で、codespace 内のエージェントにはそれがありません。デスクトップ版 VS Code で codespace を開くと、ポートはノート PC の `localhost:7777` にも転送されます。そこはローカルインストールと同様に、ノート PC 上のすべてのプログラムに対して開かれており、ノート PC 上のエージェントからも到達できます。この転送にはローカルのそのポートが空いている必要があるため、ローカルの Loom は先に停止してください。ダッシュボードはブラウザーのアドレスから使ってください。ノート PC の `localhost:7777` ではページは開きますが、サーバーがそのリクエストを拒否するためデータは表示されません。このサーバー自身のページとして使えるのは、転送先アドレスだけです。

インストール先は `/workspaces/.loom` なので、codespace を再ビルドしても `.env` と Compose プロジェクトは保持されます。ただし、メモリが保存されている Docker ボリュームは、再ビルドで保持されない場合があります。また、再ビルド時には最新リリースのインストーラーがその上で実行されるため、そのリリースに更新されます。これらのドキュメントで `~/.loom` や `"$HOME/.loom"` と書かれている箇所は、codespace では `/workspaces/.loom` に読み替えてください。Codespaces は、ポート 7777 が待ち受けを開始するとすぐに、それを開くよう提案します。ダッシュボードが応答するのは、作成ログに "Loom is running in the codespace" と表示されてからです。代わりにログの末尾がエラーで終わっている場合は、codespace のターミナルで `sh .devcontainer/start-loom.sh` を実行してください。最初の実行で設定された内容は保持されます。

<a id="start-in-multi-user-mode"></a>

### マルチユーザーモードで起動する

初回起動からアカウントとサインインを有効にして、リリース済みイメージを新規インストールするには、コンテナを起動せずにスタックのファイルだけを準備します。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Docker を起動する前に `~/.loom/.env` を編集します。既存の identity と auth のエントリを置き換え、サインアップの設定を追加します。

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

生成された `COMPOSE_PROJECT_NAME` と `LOOM_JWT_SECRET` はそのまま残してください。続いて、スタックを pull して起動します（独自の ClickHouse を使う場合は、インストーラーの案内どおり、先に `ch-setup` を実行します）。

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

[ヘルスチェック](#check-the-stack)に通ったら、`http://localhost:7777/` を開いてサインアップします。SDK や MCP のクライアントにはトークンが必要です。以下の例にあるシングルユーザー用の `dev/local` のデフォルトではなく、自分のアカウントに付与された org と名前空間を使ってください。

これが有効にするのはマルチユーザー認証であり、ネットワークの公開ではありません。スタックは引き続き localhost にバインドされます。チームと共有する前に、[オペレーターのアクセス](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)と [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls) を設定してください。あわせて、[実際のモデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)も設定してください。マルチユーザーモードを選んでも、fake LLM は置き換わりません。

すでに稼働中のシングルユーザーのスタックについては、代わりに[切り替え手順](#2-open-the-dashboard)に従ってください。コンテナを再作成しても、既存のデータボリュームは保持されます。

<a id="contributor-path-build-a-checkout"></a>

### コントリビューター向け：チェックアウトをビルドする

前提条件：Docker Compose 2.34 以降。チェックアウトに含まれる compose ファイルが、ClickHouse を毎日 pull するために必要とします。

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` は次の 3 つを行います。

1. git が追跡しない 2 つのファイルを作成します。`.env.example` から `.env` を、テンプレートから `deploy/clickhouse-config/named_collections.xml` を作ります。どちらも上書きはしません。エクストラクターのトークンがない既存の `.env` には、トークンを追加します。
2. dev の compose スタックを `--build` 付きで実行します。
3. サーバーの準備ができるまで `http://localhost:7777/v1/health` をポーリングし、未適用のマイグレーション数を表示します。`--timeout`（300 秒）が過ぎると、最後のヘルスレスポンスの本文とログ行を表示して終了します。

コントリビューターとしての初回起動では、イメージのビルドとすべてのスキーママイグレーションの適用が行われるため、数分かかります。その後 `loom up` は、ダッシュボードの URL（`http://localhost:7777/`）、最初のメモリを保存する `curl` 呼び出しと Python のスニペット、そして Claude Code を接続する行を表示します。認証が有効な場合は、writer トークンも発行し、それらのスニペットに埋め込みます。

| フラグ       | 動作                               |
| ------------ | ---------------------------------- |
| `--rebuild`  | コードの変更後に再ビルドする。     |
| `--no-build` | キャッシュ済みのイメージを再利用する。 |
| `--open`     | ダッシュボードを開く。             |
| `--port`     | 別の Loom ポートを使う。           |
| `--env-file` | 別の env ファイルを指定する。      |

稼働中のスタックに対して `loom up` を再実行すると、ヘルスの再確認だけが行われます。`make dev` は CLI を使わずに同じことを行い、次を実行します。

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

`compose.yaml` を先に指定すると、Compose は `${VAR:-default}` の置換のためにリポジトリルートの `.env` を読み込みます。`--env-file .env` は、それを明示しているだけです。`loom down` はスタックを停止し、ボリュームは保持します。`loom down --delete-data` は、生成された Compose プロジェクト名を入力した場合にのみ、このインストールの名前付きボリュームだけを完全に削除します。`--yes` は、対話なしで実行するための確認です。Makefile のターゲットも同様で、`make down` はデータを保持し、`make down-all` はデータを削除します。

<a id="what-the-stock-env-boots"></a>

### 標準の `.env` で起動するもの

チェックアウトとリリースバンドルの `.env` テンプレートは、どちらもシングルユーザーモードを選択します。リリースバンドルは `LOOM_IDENTITY_PROVIDER=single-user` を明示的に設定します。チェックアウトでは空のままにし、`LOOM_AUTH_REQUIRED=false` から同じモードを推定します。

| 設定                                                                         | 値            | 効果                                                                                     |
| ---------------------------------------------------------------------------- | ------------- | ---------------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user`（または空） | `single-user` | ログインなし。デフォルトの名前空間は `dev/local`。SDK と MCP の呼び出しにトークンは不要。 |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | fake LLM      | 抽出、エンティティのタグ付け、`memory.ask` の合成は何も生成しない。                      |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384`（リリースバンドル）   | bge-small     | イメージに含まれるモデルファイルを使い、CPU でセマンティック検索を行う。                 |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072`（チェックアウト） | fake embedder | 検索は文字 n-gram でランク付けされる。配線の確認には十分だが、再現率の確認には不十分。   |

どちらのインストーラーも、一意の `COMPOSE_PROJECT_NAME`、ランダムな JWT 署名シークレット、ランダムなエクストラクタートークン、そして `LOOM_BIND_HOST=127.0.0.1` を生成します。そのため、別々の clone が ClickHouse のボリュームを知らないうちに共有することはなく、Loom と ClickHouse にはこのコンピューターからのみ到達できます。

実際のプロバイダーを使うには、**Settings → Models & keys** を開き、プロバイダーとモデルを選び、API キーを入力して、**Test & apply** をクリックします。1 つの OpenAI API キーで、LLM と埋め込みの両方を賄えます。Docker の再起動は不要です。埋め込みを変更すると検証付きの再構築が実行され、進捗と再試行はダッシュボードに表示されます。

代わりに環境変数でデプロイする場合は、`~/.loom/.env`（リリース済みスタック）または `.env`（チェックアウト）のプロバイダー行を置き換えます。`OPENAI_API_KEY` を設定するだけでは何も変わりません。標準のプロバイダー行が、プロバイダーを明示的に指定しているためです。local や fake の embedder から切り替えると、保存済みのデータが再埋め込みされます。

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Claude、OpenAI、ホスト型の埋め込み、サブスクリプション専用のクライアント、LLM プロバイダーを 1 つだけ使うデプロイについては、[モデルと API キーの設定](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)に従ってください。リリース済みスタックの `.env` を編集した後にのみ、`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom` で反映します。通常の再起動では、コンテナの環境変数は再読み込みされません。詳細な設定は [Deployment → Providers](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers) にあります。

<a id="check-the-stack"></a>

### スタックを確認する

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` は、ClickHouse に到達でき、かつ `code_tier_failing`（リポジトリのコードインデックス階層の失敗）以外にエラーとなる問題がない場合に `status: ok` を返します。`fake_embedding`、`fake_llm`、`providers_degraded` は、現在どのモードで動作しているかを示します。`problems` は各問題とその対処法を列挙し、`schema.pending` は未適用のマイグレーションを列挙します。
- `loom doctor` は、サーバー側のチェックごとに 1 行を表示します。`--json` を付けると JSON で出力します。ソースのチェックアウトから起動したスタックでは、`docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor` を実行してください。

<a id="2-open-the-dashboard"></a>

## 2. ダッシュボードを開く

`single-user` モードでは、`http://localhost:7777/` を開くとそのままダッシュボードが表示されます。
マルチユーザーモードでは、先にサインアップまたはサインインしてください。`single-user` モードでは、
ダッシュボードとエージェントのデフォルトは `dev/local` を使います（`LOOM_DEFAULT_NAMESPACE` で
名前空間の名前を変更できます）。これは利便性のためのモードであり、ユーザー分離の境界ではありません。サーバーに
到達できる人は誰でも、そのデータと管理機能にアクセスできます。localhost のみで使用してください。

代わりにサインアップを試すには（`trust-network`：メールアドレスとパスワードを使い、名前空間名を最初に
確保した人がその管理者になります）、`~/.loom/.env`（リリース版スタック）または `.env`（チェックアウト）の
次のエントリを置き換えます。

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

`single-user` を明示的に選択している場合、`LOOM_AUTH_REQUIRED=true` だけを設定しても不十分です。
このモードでは認証がオフのままになります。インストーラーが生成した
`LOOM_JWT_SECRET` と既存のボリュームはそのまま維持し、サーバーを再作成して環境変数を読み込ませてください。

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

これ以降、ダッシュボードはサインインページで開き、**Sign up** タブが表示されます。また、すべての SDK
と MCP の呼び出しにトークンが必要になります。ID モードについては [Identities and
namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes) を参照してください。
`trust-network` を指定すると、ループバック以外の `LOOM_BIND_HOST` でもサインアップを開放した状態で起動できます。
そのため、ポートに到達できる人は誰でもサインアップできます。サーバーをチームから到達可能にする前に、
オペレーターのアクセスについては[デプロイガイド](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)、
[TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls) については該当セクションに従ってください。

<a id="3-get-a-token"></a>

## 3. トークンを取得する

シングルユーザーモードではトークンは任意です。それでも発行しておくと、認証を有効にしたあとでもスクリプトが動き続けます。ただし、スクリプトが `org`（または空の値）、`namespace`、`agent` を明示していることが条件です。SDK が認証情報を見つけて送るときのように、認証情報を送る呼び出しでは、これらのどれも自動では補完されません（[Python
SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)）。空の `org` はトークン自身のものになります。

- **ダッシュボード：** **Settings → Tokens → New token**。デフォルトでは、トークンは自分が所属するすべての名前空間とリポジトリを読み取れます（制限付きリポジトリは追加した場合のみ）。コレクションは読み取れず、現在いる名前空間に保存します。_Only what you choose_ を選ぶと、チェックを入れた名前空間、コレクション、リポジトリに限定されます（最初は何もありません）。チェックを入れたものは、一覧の順に検索されます。順序は行の矢印で変更でき、デスクトップでは行のグリップをドラッグしても変更できます。結果画面にはトークンが一度だけ表示され、MCP クライアントやスクリプト向けのセットアップも示されます。
- **CLI（オペレーターとして）：** 署名シークレットを保持しているサーバーコンテナの中で実行します。

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  マシンの外に出るトークンには `--ttl-days 30` を付けてください。`loom mint-token --help` で、スコープ指定のオプション（`--namespace-role`、`--read-scope`、`--traffic-class`）を確認できます。

- **自分でサインアップするボット**（trust-network のみ）：
  `loom init --agent nightly-triage --server-url http://localhost:7777` は、名前空間 `agent-nightly-triage` を作成し、writer トークンを `~/.loom/token` に、URL を `~/.loom/config.json` に保存して、クレームコードを表示します。担当者がそのコードを `/oauth/agent-claim` で一度入力し、自分のパスワードを設定すると、所有権を引き継げます。

SDK、CLI、プラグインが参照する場所にトークンを保存します。

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

クレーム、ロール、グラントについては [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) を参照してください。

<a id="4-first-memory-over-http"></a>

## 4. HTTP での最初のメモリ

すべての操作は、JSON ボディを付けた `POST /v1/<op>` です。インストーラーまたはチェックアウトから入れた、標準の `.env` のままの新規インストールはシングルユーザーです。トークンは不要で、日常的なメモリ呼び出しや検索呼び出し（`memory.set`、`memory.get`、`memory.search` など）は、`org` や `namespace` を指定しなければ、そのインストール自身の org `dev`、名前空間 `local` で実行されます。これは MCP とダッシュボードが使うものと同じです。`agent` を指定しない `memory.set` はエージェント `http` として記録され、検索はすべてのエージェントを読み取ります。そのため、以下の呼び出しに必要なのは各呼び出し固有のフィールドだけです（[対象の op](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)）。トークンなしの呼び出しが 401 `AUTH_INVALID` を返す場合は、認証が有効になっています。`-H "Authorization: Bearer $LOOM_API_KEY"` を追加し、アカウントの `org`、`namespace`、`agent` を指定してください。空のトークンを持つヘッダーは拒否されます。

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

`memory.set` のレスポンスには `memory_id`、`content_sha256`、`status` が含まれます。
`memory.get` は 1 つのキーを読み戻します。`found` はそのキーが有効な値を保持しているかどうかを示し、`value` は書き込まれたとおりの値です。検索レスポンスには `results` と `abstain` が含まれ、後者は最上位のヒットが回答の根拠にできるほど強いかどうかを示します。検索は、ダッシュボードで追加されたものも含め、名前空間内のすべてのエージェントのメモリを読み取ります。特定のエージェントのメモリだけを読むには、`agent` を指定します。
`GET /v1/operations` はすべての操作を一覧表示し、<http://localhost:7777/docs> ではブラウザーから各操作を試せます。リクエストとレスポンスの形式は [HTTP API リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)を参照してください。

<a id="5-first-memory-from-python"></a>

## 5. Python から最初のメモリを作る

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

まだ PyPI に公開されていないリリースは、リリースのアセットからインストールします
（[GitHub Release からのインストール](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)）。

この例では `org` と `namespace` を指定しているため、ステップ 3 のトークンがあってもなくても動作します。SDK はトークンを見つけると、それを送信します。シングルユーザーのサーバーでは、トークンなしの呼び出しでこれらを省略することもできます。`Agent(agent="quickstart", base_url=...)` も `dev`/`local` で動作します。

```python
import asyncio

from loom import Agent


async def main():
    async with Agent(
        org="dev",
        namespace="local",
        agent="quickstart",  # names the writer in the audit log
        base_url="http://localhost:7777",
    ) as a:  # sends a credential it finds; none needed in single-user mode
        await a.memory.set("standup_time", "Daily standup is at 9:30.")
        row = await a.memory.get("standup_time")  # None when the key is unset
        print(row["value"] if row else "unset")
        for hit in await a.memory.search("when is standup?"):
            print(hit.content_excerpt)


asyncio.run(main())
```

SDK がサーバーと認証情報をどこから見つけるかについては、
[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials) を参照してください。
TypeScript SDK は `npm install @clickhouse/loom` でインストールできます
（[クローンからのインストール](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)）。

クライアントのインターフェースについては、[Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) と
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) のリファレンスを参照してください。

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. コーディングエージェントでの最初のメモリ

Claude Code では、Loom プラグインをインストールします。MCP ツールに加えて、フック（セッション開始時のコンテキスト取得、終了時のキャプチャ）とスキルが追加されます。

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

このマシン上のシングルユーザーサーバーでは、デフォルトのサーバー URL をそのまま使い、Claude Code がプラグインの設定を尋ねたときにトークンを空のままにします。

プラグインを使わない場合は、MCP エントリを直接追加します。

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

どちらの方法でも、このマシン上のシングルユーザーサーバーには、自分のトークンなしで接続できます。

認証を有効にしている場合は、**Settings → Tokens** でトークンを作成し、**Connect** の下の **Claude Code** を選びます。ダイアログには、プラグイン用の設定と `claude mcp add` エントリ用の設定が、トークン入りで表示されます。その後、次の手順を行います。

1. `~/.loom/config.json` を、ダイアログの `config.json` ブロックと、トークンの保存先の名前空間（`"namespace": "<namespace>"`）で上書きします。シングルユーザーのセッションがファイルに残した `org` または `namespace` はトークンより優先されます。これらがない場合、プラグインは `*` 以外でトークンの最初の名前空間、または `coding` に書き込みます。設定していた `code_repo` や `agent` があれば、元に戻します。
2. `LOOM_API_KEY`（手順 3 でエクスポートしている場合があります：`unset LOOM_API_KEY`）とプラグインの `token` オプションは、空のままにするか、新しいトークンに設定します。プラグインは、そのファイルより先にこの両方を読み込みます
   （[configuration](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)）。
3. トークンなしで作成した `claude mcp add` エントリは、ダイアログのコマンドを実行する前に削除します（`claude mcp remove loom -s user`）。ダイアログのコマンドは、既存の名前を拒否するためです。

`loom install --client <name>` は、Claude Desktop、Claude Code、Codex、Cursor、VS Code、Cline、Windsurf、Zed の MCP エントリを書き込みます。フラグを指定しない場合は、見つかったすべてのクライアントを設定します。Codex と Cursor については、[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) を参照してください。

新しいセッションを開始し、_「Remember that I never deploy on Friday.」_ と伝えます。エージェントは `loom_memory_set` を呼び出します。新しいセッションで _「When can I deploy?」_ と尋ねると、エージェントは `loom_search` を呼び出し、保存された事実に基づいて回答します。モデルは、ツールの説明に基づいて保存と検索のタイミングを判断します。トリガーとなるキーワードはありません。

<a id="7-see-it-in-the-dashboard"></a>

## 7. ダッシュボードで確認する

| ページ              | 表示内容                                                   |
| ----------------- | ---------------------------------------------------------- |
| `/memories`       | 名前空間のメモリを、書き込んだエージェントと時刻とともに表示します。 |
| `/admin/activity` | 監査ログ：どのエージェントが、どのキーを、いつ書き込んだか。       |
| `/admin/usage`    | 操作ごと、エージェントごとのコスト。                               |

残りは [ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md) を参照してください。

<a id="optional-index-a-repository-on-the-released-stack"></a>

## オプション：リリース版スタックでリポジトリをインデックスする

このスタックでは、ダッシュボードからのリポジトリ追加は `SANDBOX_UNAVAILABLE` で拒否されます。
追加には[リポジトリジョブのサンドボックス](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)が必要ですが、Docker のデフォルトの隔離がサンドボックスをブロックするため、このスタックではオフにしています。信頼できるリポジトリだけを登録してください。ビルド手順（TypeScript のインストール、Go モジュールのダウンロード、CMake）はサンドボックスなしで `loom` コンテナ内で実行され、そのコンテナの環境には JWT シークレットとプロバイダーキーが入っています。

`LOOM_CODE_REPOS_JSON` は、すべてのリポジトリを JSON 配列として 1 行で保持する設定です
（[エントリを設定する](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)）。
`~/.loom/.env` でこの行のコメントを外し（なければ追加し）、編集します。
`dev` は、シングルユーザースタックが動作する org であり、マルチユーザーのアカウントがサインアップする org でもあります（`LOOM_SELF_SIGNUP_ORG`）。これを変更した場合は、ここと以下の `--org` に、自分のアカウントの org を使ってください。

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom は、リポジトリのビルドファイル（`go.mod`、`package.json`、`pyproject.toml`、ルートの `CMakeLists.txt` など）から、コンパイラ層のインデクサーを選びます。代わりに `scip_producer` でインデクサーを指定することもできます（[コンパイラ層](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)）。
  C/C++ では設定済みのビルドツリーも必要です。ない場合、C/C++ リポジトリではパーサー層のみが使われます。
- プライベートリポジトリの場合は、GitHub トークン（`.env` の `LOOM_CODE_GITHUB_TOKEN`、またはエントリの `github_token`）を追加し、`"public":true` を削除します。これはホスト上で公開されているリポジトリ向けの設定で、org の誰もが **Repositories** からすぐにリーダーとしてインデックスに参加できるようになります。最初の `loom code init` の前に決めてください。初期化時に `public` をもとに名前空間の参加ポリシーが設定され、後から `public` を編集してもポリシーは変わりません（メンテナーは `code.repo.set_access` で変更できます）。

その後、`loom` コンテナを再作成し、最初のインデックスを実行します。

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` は、リリース版スタックのローカル embedder で動作します。フェイク embedder の下（チェックアウトした `.env`、または `fake-ngram-*` を指定したままのアップグレード済みインストール）では、拒否されて終了します。それでも動作を試したい場合は、`--allow-fake-embedder` を追加してください。後から **Settings → Models & keys** でモデルを選ぶと、コードインデックスが再埋め込みされます。`.env` を変更した場合は、独自に再埋め込みが必要です。詳しくは[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)を参照してください。

マルチユーザーモードでは、登録したリポジトリにメンテナーがいないため、`"public":true` がないと、リーダーの参加リクエストは承認者がいないまま待機します。`loom code init` の後、コンテナ内で発行したトークンを使い、オペレーターとして誰かにアクセスを付与します。`admin` ならその人がメンテナーとなり、以後はリーダーを承認します。`viewer` なら読み取りができます。subject には、そのアカウントがサインアップ時に得た名前空間（ログインハンドルで、トークンの subject）を指定します。セルフサインアップのメールアドレスは検証されないため、メールアドレスへの付与では誰も参加できません。

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## オプション：ドキュメントについて質問する

```bash
loom demo
```

`loom demo` は、このリポジトリの `AGENTS.md` をアップロードし、`knowledge.ingest` で取り込み（スタックのリファレンス抽出器が解析します）、`knowledge.synthesize` でその内容について質問します。回答と、回答が引用したチャンクが出力されます。偽の LLM を使う場合は、代わりに `knowledge.search` の上位ヒットが出力されます。`--file` と `--question` を使うと、自分のドキュメントと質問を指定できます。

<a id="common-problems"></a>

## よくある問題

| 症状                                              | 原因と対処                                                                                                                                                                                     |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` がファクトを書き込まない      | LLM がフェイクのものになっています。[Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) に従って設定してください。                                                                                                      |
| 検索結果が無作為に見える                          | エンベッダーがフェイクのものです（`.env` の `fake-ngram-*`）。[Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) の説明どおりに切り替えると、保存済みのデータが再埋め込みされます。モデルを変えるためにボリュームを削除しないでください。 |
| 認証を有効にしたあとに 401 `AUTH_INVALID` になる  | トークンがないか、別の `LOOM_JWT_SECRET` で署名されたトークンです。[Get a token](#3-get-a-token) の Compose コマンドで発行し直してください。                                                                     |
| ダッシュボードが 503 を返す                       | サーバーがソースから（`make server`）、ビルド済みのフロントエンドなしで動いています。`make frontend-build` を実行してください。                                                                                               |
| リリース版のスタックが healthy にならない         | `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse` を実行してください。[Troubleshooting](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md) も参照してください。                                               |
| リポジトリを追加すると `SANDBOX_UNAVAILABLE` が返る | スタックがリポジトリジョブのサンドボックスなしで動いています。`LOOM_CODE_REPOS_JSON` を使ってリポジトリをインデックスしてください（[Index a repository](#optional-index-a-repository-on-the-released-stack)）。             |
| インストーラーがポート `7777` は使用中だと表示する | 別のプログラムまたは別の Loom がそのポートで待ち受けています。それを停止するか、`sh loom-install.sh` の前に `LOOM_PORT=7778` を付けてインストールコマンドを再実行してください。`.env` にポートが保持されます。                      |
| インストーラーがポート `7777` が応答しないと表示する | 応答しないままプログラムがポートを占有しているか、`LOOM_BIND_HOST`、`LOOM_PORT` またはファイアウォールのためにこのホストから到達できません。                                                                         |
| インストーラーがポート `7777` を確認できなかった  | curl が、その終了コードが示す理由で失敗しました。`LOOM_BIND_HOST` と `LOOM_PORT` を確認してください。                                                                                                        |
| クローンのポート `8123` または `9000` が使用中    | ClickHouse のホストポートを再割り当てしてください（[local stack](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)）。                                                                                         |
| インストーラーがイメージを pull できなかった      | Docker 自身がレジストリに到達できていません。プロキシの内側にある場合は、Docker デーモンに[プロキシ設定](https://docs.docker.com/engine/daemon/proxy/)を行ってください。                                                 |

<a id="next"></a>

## 次のステップ

- [Evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)：このスモークテストを、ワークロード固有の品質・レイテンシ・コストの評価に発展させます。
- [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)：このスタックに Claude Code、Codex、Cursor を接続します。
- [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：SDK を使った ID、abstain の処理、セッション、コンテキストパック、実行（run）を扱います。
- [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)：チーム向けに Loom を運用します。
