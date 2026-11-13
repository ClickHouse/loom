<!-- i18n: source=docs/getting-started/quickstart.md blob=b7af76d7843e0c8a0e3cb4146bb5e3a9f57ba395 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# クイックスタート

このページでは、ノート PC 上で Loom を起動し、HTTP、Python、MCP の 3 通りで最初のメモリを保存します。API キーもアカウントも不要です。リリース版スタックを新規インストールした場合、イメージに同梱された小さなローカルモデルで埋め込みを行うため、ネットワークがなくても検索は意味のあるヒットを返します（アップグレードした環境では、`.env` に指定されている embedder がそのまま使われます）。一方、LLM はダミーなので、抽出と `memory.ask` の回答は何も生成しません。チェックアウトした リポジトリの `.env` では、どちらもダミーのプロバイダーを使います。

**標準の Docker インストールはシングルユーザー向けです。** ログインはなく、アクセスは localhost のみで、デフォルトの名前空間は `dev/local` です。マルチユーザーモードを有効にしなくても、1 人で複数のエージェントを接続できます。**OSS ではチーム向けのデプロイもサポートしており**、認証付きアカウントと名前空間のアクセス制御を利用できます。[マルチユーザーモードで起動する](#start-in-multi-user-mode)か、[Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) を参照してください。どちらのモードでも Loom の機能は同じです。実際の LLM や埋め込みを使うには、いずれのモードでもプロバイダーの設定が必要です。

<a id="prerequisites"></a>

## 前提条件

| 必要なもの                      | 用途                                                                                |
| ------------------------------- | ----------------------------------------------------------------------------------- |
| compose プラグイン付きの Docker | スタック（ClickHouse と Loom サーバー）の実行。                                      |
| Python 3.12 以降                | コントリビューター専用の `loom` CLI。SDK は 3.11 以降をサポートします。             |
| Node.js 20 以降                 | TypeScript SDK（任意）。                                                            |
| 空きポート `7777`               | Loom 用。チェックアウトからの開発では、ClickHouse も `8123` と `9000` で公開されます。 |
| `jq`                            | ステップ 4 で `curl` の出力を読むために使用（任意）。                               |

<a id="1-boot-the-stack"></a>

## 1. スタックを起動する

<a id="fast-path-released-image-no-clone"></a>

### 最短手順：リリース済みイメージを使う（clone 不要）

前提条件：Docker Desktop、または Compose 2.20 以降を備えた Docker Engine。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

このコマンドはインストーラーをカレントディレクトリに保存し、そのファイルを実行します。そのため、ホームディレクトリ、またはあなただけが書き込めるディレクトリで実行してください。共有ディレクトリでは、実行前に他のユーザーが `loom-install.sh` を差し替えたり、別のファイルを指すように変更したりできます。ダウンロードに失敗した場合は curl のエラーを表示し、ゼロ以外の終了コードで停止します。インストールに失敗した場合は、表示された内容に対処してから、コマンド全体をもう一度実行してください。インストーラーは、最新の GitHub Release から `loom-stack.tar.gz` をダウンロードして SHA-256 を検証し、Compose と ClickHouse の設定を `~/.loom` 配下にインストールします。続いて、対応する `ghcr.io/clickhouse/loom:X.Y.Z` と `ghcr.io/clickhouse/loom-extractor:X.Y.Z`（ドキュメント抽出サービス）のイメージを pull し、`http://localhost:7777/v1/health` が応答するまで待ちます。サーバーイメージには、API、ワーカー、マイグレーション、そしてサーバーのルートで配信されるビルド済みの React ダッシュボードが含まれており、Python や Node のインストールは不要です。Python、TypeScript、Go のコンパイラインデクサーは同梱されており、有効になっています。TypeScript インデクサーでインデックスしたリポジトリでは、依存関係が自動的にインストールされます。どちらのイメージも Linux の `amd64` と `arm64` に対応しており、Apple Silicon 上の Docker Desktop でも動作します。

Loom が応答すると、インストーラーは次の処理を行います。

1. **メモリを確認する。** 新規インストールでは、テスト用のメモリを保存し、読み戻して削除します。これにより書き込みと読み取りが動作することを確認でき、有効なメモリは残りません（削除されたバージョンは「Recently deleted」に表示されます）。この確認に成功した後の再実行や、アップグレードでは、この確認を省略します。
2. **サインインが有効かどうかを調べる。** サインインが有効な場合、エージェントは接続せず、トークンの入手先を案内します。
3. **コーディングエージェントの接続を提案する。** 端末上では、見つかったエージェントごとに確認します。Claude Code と Codex はプラグイン経由、VS Code は `code --add-mcp` 経由です。`LOOM_CONNECT` を指定すると、確認の代わりにその値が使われ、端末がない実行でも有効です。対象になるのは指定したクライアントだけです。値には、`claude-code`、`codex`、`vscode` のカンマ区切りリスト、`all`、または単独の `none` を指定でき、それ以外の値ではインストールが停止します。端末がなく `LOOM_CONNECT` も指定されていない場合、エージェントの確認も変更も行いません。以前に設定済みのプラグインや MCP サーバー（プラグイン、`claude mcp add`、`codex mcp add`、`loom install` のいずれかによるもの）はそのまま残します。Claude Code のプラグインについては、このインストールを指すようにするコマンドを表示します。Claude Code のプラグインは、PATH 上の Node.js 18 以降で動作します。そのため、Node.js がない場合、インストーラーはプラグインをインストールせず、MCP サーバーだけを設定する `claude mcp add` の行を表示します。Codex のプラグインは nvm などで入れた Node.js も見つけられるため、インストールされ、隣に `codex mcp add` の行も表示されます。Codex のプラグインは常に `http://localhost:7777` を使うため、別の場所へのインストールでは、代わりにセットアップページへのリンクが表示されます。VS Code は、リモートセッションと WSL では提案されません。`LOOM_CONNECT` で指定したクライアントが PATH にない場合は、そのクライアント名が表示されます。
4. **ダッシュボードを開く。** 新規インストールでデスクトップ環境の場合に開きます。`LOOM_NO_OPEN=1` を指定すると開きません。

Ctrl-C でどの段階でも実行を止められます。ただし、エージェント CLI がプラグインのために開始した clone は、バックグラウンドで完了することがあります。

このスタックは、専用の ClickHouse コンテナを実行します。すでに運用している ClickHouse（セルフマネージドのサーバー、または ClickHouse Cloud のサービス。26.4 以降で Keeper 付き）を使うには、その URL を渡してください。URL にパスワードが含まれていない場合、インストーラーがパスワードを尋ね、`loom` データベースと読み取り専用のリーダーユーザーを作成します。あるいは、管理者が実行すべき SQL を表示します（[ClickHouse の選び方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)）。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

各リリースの `install.sh` は、そのリリースをインストールします。したがって `latest` が配信するコピーは最新リリースをインストールし、下記のリリース固有の URL は常にそのリリースをインストールします。より新しいインストールの上に、古いリリースのコピーを実行すると、新しいデータの上で古い Loom と ClickHouse を起動してしまわないよう、インストールを拒否します。意図的にダウングレードする場合は、[インストーラーによるインストールのダウングレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install)に従ってください。リリースを固定するには、そのリリース固有の URL を使います。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

起動前にすべてを確認したい場合は、ファイルをダウンロードして検査します。

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

出力内容に問題がなければ、検査済みのバンドルからインストールします。

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

インストーラーに渡した `LOOM_PORT`、`LOOM_BIND_HOST`、`LOOM_IMAGE_REPOSITORY`、`LOOM_EXTRACTOR_IMAGE_REPOSITORY`、`LOOM_CLICKHOUSE_IMAGE` は、インストールディレクトリの `.env` に書き込まれます。そのため、以降そこで実行する `docker compose` コマンドや、以降のインストーラー実行でも、同じポートとイメージが使われます。ループバック以外の `LOOM_BIND_HOST` を指定すると、Loom はこのホストの外に公開されます。その場合、`LOOM_PUBLIC_BASE_URL` とサインインがなければサーバーは起動を拒否します。`LOOM_SKIP_START=1` を付けてインストールし、`.env` にそれらを設定してから（後述）、`LOOM_SKIP_START=1` を付けずにインストールコマンドをもう一度実行してください。

各リリースは、ビルド時点で最新だった ClickHouse リリースを実行します。そのため、同じ Loom バージョンに対してインストールコマンドを再実行しても ClickHouse は変わりません。より新しい Loom バージョンをインストールすると ClickHouse も更新されますが、`.env` で `LOOM_CLICKHOUSE_IMAGE` を固定している場合は更新されません（その行を削除すると、再びリリースに追従します）。インストールコマンドを再実行すると、管理対象の Compose／設定ファイルが更新され、コンテナが整合されます。`~/.loom/.env` は保持され、extractor の URL とトークンがない場合はそれらが追加されます。名前付き Docker ボリュームも保持されます。データを削除せずに停止するには、次のようにします。

```bash
docker compose --project-directory "$HOME/.loom" down
```

`--volumes` は、元に戻せないリセットを意図して行う場合にのみ追加してください。

<a id="try-it-in-github-codespaces"></a>

### GitHub Codespaces で試す

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

リポジトリの dev container は、お使いのマシンには何も入れずに、codespace 内で最新リリースを実行します。上記のインストーラーで Loom をインストールして起動し、Claude Code を接続します。初回の起動では約 3 GB のイメージをダウンロードするため、数分かかります。ダッシュボードは、作成ログの最後の行に表示されるアドレスで開いてください（`Loom is running in the codespace. Dashboard: https://<codespace name>-7777.app.github.dev`。これは **Ports** タブにある、ポート 7777 の転送先アドレスです）。その上に表示されるインストーラーの `http://127.0.0.1:7777/` は、codespace 自身のアドレスです。codespace のターミナルで `cd ~ && claude` を実行します（チェックアウトしたディレクトリの外で実行します。そこにある指示は Loom 自体の開発用だからです）。サインインしたら「Remember that I never deploy on Friday」と伝え、新しい `claude` セッションで「When can I deploy?」と尋ねてみてください。

この試用環境は、サインインなしのシングルユーザーで動作します。ブラウザーでは、ポート 7777 は非公開で転送されます。そのアドレスはあなたの GitHub アカウントにだけ応答します。非公開のままにしてください。公開にしたり、組織内限定にしたりすると、サインインのないサーバーが、共有された全員に開かれてしまいます。codespace 内のエージェント（たとえばその `claude`）は `http://localhost:7777` を使います。ダッシュボードの接続用の行には、代わりに転送先アドレスが表示されますが、これには GitHub サインインが必要で、codespace 内のエージェントにはそれがありません。デスクトップ版 VS Code で codespace を開くと、ポートはお使いのノート PC の `localhost:7777` にも転送されます。そこはローカルインストールと同様に、ノート PC 上のすべてのプログラムに開かれており、ノート PC 上のエージェントからも到達できます。この転送にはそのローカルポートが空いている必要があるため、ローカルの Loom は先に停止してください。ダッシュボードはブラウザーのアドレスから使ってください。ノート PC の `localhost:7777` ではページは開きますが、サーバーがリクエストを拒否するためデータは表示されません。このサーバー自身のページになるのは、転送先アドレスだけです。

インストールは `/workspaces/.loom` にあるため、codespace を再構築しても `.env` と Compose プロジェクトは残ります。ただし、メモリは Docker ボリュームにあり、再構築で残らない場合があります。また、再構築時には最新リリースのインストーラーがその上で実行され、そのリリースに更新されます。これらのドキュメントで `~/.loom` や `"$HOME/.loom"` と書かれている場所は、codespace では `/workspaces/.loom` を使います。Codespaces は、ポート 7777 が待ち受けを開始するとすぐに、そのポートを開くよう提案します。ダッシュボードは、作成ログに「Loom is running in the codespace」と表示されてから応答します。代わりにログがエラーで終わった場合は、codespace のターミナルで `sh .devcontainer/start-loom.sh` を実行してください。最初の実行で設定された内容は保持されます。

<a id="start-in-multi-user-mode"></a>

### マルチユーザーモードで起動する

最初の起動からアカウントとサインインを有効にした、リリース済みイメージの新規インストールを行うには、コンテナを起動せずにスタックのファイルを準備します。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Docker を起動する前に `~/.loom/.env` を編集します。既存の identity と auth のエントリを置き換え、サインアップの設定を追加してください。

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

生成された `COMPOSE_PROJECT_NAME` と `LOOM_JWT_SECRET` はそのまま残してください。その後、スタックを pull して起動します（独自の ClickHouse を使う場合は、インストーラーの案内どおり、先に `ch-setup` を実行します）。

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

[ヘルスチェック](#check-the-stack)に合格したら、`http://localhost:7777/` を開いてサインアップします。SDK と MCP のクライアントにはトークンが必要です。以下の例にあるシングルユーザー用の `dev/local` というデフォルト値ではなく、あなたのアカウントに付与された org と名前空間を使ってください。

これが有効にするのはマルチユーザー認証であり、公開ネットワークではありません。スタックは引き続き localhost にバインドされます。チームと共有する前に、[オペレーターのアクセス](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)と [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls) を設定してください。あわせて、[実際のモデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)も設定してください。マルチユーザーモードを選んでも、偽の LLM は置き換わりません。

すでに稼働中のシングルユーザーのスタックについては、代わりに[切り替え手順](#2-open-the-dashboard)に従ってください。コンテナを再作成しても、既存のデータボリュームは保持されます。

<a id="contributor-path-build-a-checkout"></a>

### コントリビューター向け：チェックアウトをビルドする

前提条件：Docker Compose 2.34 以降。チェックアウトの compose ファイルが、ClickHouse を毎日 pull するために必要とします。

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` は次の 3 つを行います。

1. git が追跡しない 2 つのファイルを作成します。`.env.example` から `.env` を、テンプレートから `deploy/clickhouse-config/named_collections.xml` を作ります。どちらも上書きはしません。extractor トークンのない既存の `.env` には、トークンを追加します。
2. 開発用の compose スタックを `--build` 付きで実行します。
3. サーバーの準備ができるまで `http://localhost:7777/v1/health` をポーリングし、未適用のマイグレーション数を表示します。`--timeout`（300 秒）を過ぎると、最後のヘルスレスポンスの本文とログの行を表示して終了します。

コントリビューター向けの初回起動では、イメージをビルドし、すべてのスキーママイグレーションを適用するため、数分かかります。その後 `loom up` は、ダッシュボードの URL（`http://localhost:7777/`）、最初のメモリを保存する `curl` の呼び出しと Python のスニペット、そして Claude Code を接続する行を表示します。認証が有効な場合は、writer トークンも発行して、それらのスニペットに埋め込みます。

| フラグ       | 動作                               |
| ------------ | ---------------------------------- |
| `--rebuild`  | コードの変更後に再ビルドします。   |
| `--no-build` | キャッシュ済みのイメージを再利用します。 |
| `--open`     | ダッシュボードを開きます。         |
| `--port`     | 別の Loom ポートを使います。       |
| `--env-file` | 別の env ファイルを指定します。    |

稼働中のスタックで `loom up` を再実行すると、ヘルスの再確認だけを行います。`make dev` は CLI を使わずに同じことを行い、次を実行します。

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

`compose.yaml` を先に指定すると、Compose は `${VAR:-default}` の置換にリポジトリルートの `.env` を読み込みます。`--env-file .env` は、それを明示しているだけです。`loom down` はスタックを停止し、ボリュームは保持します。`loom down --delete-data` は、生成された Compose プロジェクト名を入力して初めて、このインストールの名前付きボリュームだけを完全に削除します。`--yes` は、非対話での確認です。Makefile のターゲットも同様で、`make down` はデータを保持し、`make down-all` はデータを削除します。

<a id="what-the-stock-env-boots"></a>

### 標準の `.env` で起動するもの

チェックアウトとリリースバンドルのどちらの `.env` テンプレートも、シングルユーザーモードを選択します。リリースバンドルは `LOOM_IDENTITY_PROVIDER=single-user` を明示的に設定します。チェックアウトでは空のままにして、`LOOM_AUTH_REQUIRED=false` から同じモードを推定します。

| 設定                                                                         | 値            | 効果                                                                                  |
| ---------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user`（または空） | `single-user` | ログインなし。デフォルトの名前空間は `dev/local`。SDK と MCP の呼び出しにトークンは不要。 |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | 偽の LLM      | 抽出、エンティティのタグ付け、`memory.ask` の合成は何も生成しません。                 |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384`（リリースバンドル）   | bge-small     | イメージに含まれるモデルファイルを使い、CPU でセマンティック検索を行います。          |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072`（チェックアウト） | 偽のエンベッダー | 検索は文字 n-gram で順位付けします。配線の確認には十分ですが、再現率の確認には使えません。 |

どちらのインストーラーも、一意の `COMPOSE_PROJECT_NAME`、ランダムな JWT 署名シークレット、ランダムな extractor トークン、そして `LOOM_BIND_HOST=127.0.0.1` を生成します。そのため、別々の clone が ClickHouse のボリュームを知らないうちに共有することはなく、Loom と ClickHouse はこのコンピューターからのみ到達できます。

実際のプロバイダーを使うには、**Settings → Models & keys** を開き、プロバイダーとモデルを選んで API キーを入力し、**Test & apply** をクリックします。1 つの OpenAI API キーで、LLM と埋め込みの両方を賄えます。Docker の再起動は不要です。埋め込みを変更すると検証付きの再構築が実行され、進行状況と再試行がダッシュボードに表示されます。

代わりに環境変数でデプロイする場合は、`~/.loom/.env`（リリース済みスタック）または `.env`（チェックアウト）のプロバイダーの行を置き換えます。`OPENAI_API_KEY` だけを設定しても何も変わりません。標準のプロバイダーの行が、それぞれのプロバイダーを明示的に指定しているためです。ローカルまたは偽のエンベッダーから切り替えると、保存済みのデータは再度埋め込まれます。

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Claude、OpenAI、ホスト型の埋め込み、サブスクリプション専用のクライアント、LLM プロバイダーを 1 つだけ使うデプロイについては、[モデルと API キーの設定](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)に従ってください。リリース済みスタックの `.env` を編集した後にのみ、`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom` で反映します。通常の再起動では、コンテナの環境は再読み込みされません。詳細な設定は [Deployment → Providers](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers) にあります。

<a id="check-the-stack"></a>

### スタックを確認する

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` は、ClickHouse に到達でき、`code_tier_failing`（リポジトリのコードインデックス層の失敗）以外にエラーとなる問題がない場合に、`status: ok` を返します。`fake_embedding`、`fake_llm`、`providers_degraded` は、現在どのモードで動いているかを示します。`problems` は各問題とその対処方法を一覧にし、`schema.pending` は未適用のマイグレーションを一覧にします。
- `loom doctor` は、サーバー側のチェックごとに 1 行を出力します。`--json` を付けると JSON で出力します。ソースのチェックアウトから起動したスタックでは、`docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor` を実行してください。

<a id="2-open-the-dashboard"></a>

## 2. ダッシュボードを開く

`http://localhost:7777/` は、`single-user` モードではそのままダッシュボードが開きます。マルチユーザーモードでは、先にサインアップまたはサインインしてください。`single-user` モードでは、ダッシュボードとエージェントのデフォルトに `dev/local` が使われます（`LOOM_DEFAULT_NAMESPACE` で名前空間の名前を変更できます）。これは利便性のためのモードであり、ユーザー分離の境界ではありません。サーバーに到達できる人は誰でも、そのデータと管理機能にアクセスできます。localhost のみで使用してください。

代わりにサインアップを試すには（`trust-network`：メールアドレスとパスワードを使い、名前空間の名前を最初に取得した人がその管理者になります）、`~/.loom/.env`（リリース版スタック）または `.env`（チェックアウト）の該当する項目を次のように置き換えます。

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

`single-user` を明示的に選択している場合、`LOOM_AUTH_REQUIRED=true` を設定するだけでは不十分です。このモードでは認証がオフのままになります。インストーラーが生成した `LOOM_JWT_SECRET` と既存のボリュームはそのまま残し、サーバーを再作成して環境変数を読み込ませます。

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

これ以降、ダッシュボードはサインインページで開き、**Sign up** タブが表示されます。また、すべての SDK と MCP の呼び出しにトークンが必要になります。ID モードについては、[Identities and namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes) を参照してください。`trust-network` を指定すると、非ループバックの `LOOM_BIND_HOST` でもサインアップを開放した状態で起動できます。その場合、ポートに到達できる人は誰でもサインアップできます。チームがサーバーに到達できるようにする前に、オペレーターのアクセスについては[デプロイガイド](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)、[TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls) については該当の節に従ってください。

<a id="3-get-a-token"></a>

## 3. トークンを取得する

シングルユーザーモードではトークンは任意です。それでもトークンを発行しておけば、認証を有効にしたあとも、スクリプトが `org`（または空欄）、`namespace`、`agent` を指定している限り、そのまま動作します。SDK が認証情報を見つけて送る場合（[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)）のように、認証情報を送る呼び出しではこれらのいずれも自動では補完されず、空欄の `org` にはトークン自身の org が使われます。

- **ダッシュボード：** **Settings → Tokens → New token**。デフォルトでは、トークンは所属するすべての名前空間とリポジトリを読み取れます（制限付きリポジトリは追加した場合のみ）。コレクションは読み取れず、保存先は現在の名前空間です。_Only what you choose_ を選ぶと、チェックした名前空間、コレクション、リポジトリだけに制限されます（初期状態では何も選択されていません）。チェックしたものは一覧の順に検索されます。順序は各行の矢印で変更でき、デスクトップでは行のグリップをドラッグしても変更できます。結果画面にはトークンが一度だけ表示され、MCP クライアントやスクリプト向けのセットアップも示されます。
- **CLI（オペレーターとして）：** 署名シークレットを保持しているサーバーコンテナ内で実行します。

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  マシンの外に出すトークンには `--ttl-days 30` を付けてください。`loom mint-token --help` で、スコープ指定のオプション（`--namespace-role`、`--read-scope`、`--traffic-class`）を確認できます。

- **自分でサインアップするボット**（trust-network のみ）：
  `loom init --agent nightly-triage --server-url http://localhost:7777` を実行すると、名前空間 `agent-nightly-triage` が作成され、writer トークンが `~/.loom/token` に、URL が `~/.loom/config.json` に保存され、クレームコードが表示されます。人間が `/oauth/agent-claim` でそのコードを一度入力し、自分のパスワードを設定すると、所有権を引き継げます。

SDK、CLI、プラグインが参照する場所にトークンを保存します。

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

クレーム、ロール、グラントについては [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) を参照してください。

<a id="4-first-memory-over-http"></a>

## 4. HTTP で最初のメモリを作る

すべての操作は、JSON ボディを付けた `POST /v1/<op>` です。インストーラまたはチェックアウトで入れた直後の、標準の `.env` のままの環境はシングルユーザーで、トークンは不要です。`org` も `namespace` も指定しない日常的なメモリ呼び出しや検索呼び出し（`memory.set`、`memory.get`、`memory.search` ほか）は、インストール自身の org `dev` と namespace `local` で実行されます。これは MCP とダッシュボードが使うものと同じです。`agent` を指定しない `memory.set` は agent `http` として記録され、検索はすべての agent を対象に読み取ります。そのため、以下の呼び出しに必要なのはそれぞれのフィールドだけです（[対象の op](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)）。トークンなしの呼び出しが 401 `AUTH_INVALID` を返す場合は、認証が有効になっています。`-H "Authorization: Bearer $LOOM_API_KEY"` を追加し、アカウントの `org`、`namespace`、`agent` を指定してください。トークンが空のヘッダーは拒否されます。

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

`memory.set` のレスポンスには `memory_id`、`content_sha256`、`status` が含まれます。`memory.get` は 1 つのキーを読み戻します。`found` はそのキーが有効な値を保持しているかどうかを示し、`value` は書き込まれたままの値です。検索のレスポンスには `results` と `abstain` が含まれ、後者は最上位のヒットが回答の根拠にできるほど強いかどうかを示します。検索は名前空間内のすべての agent のメモリを読み取り、ダッシュボードで追加されたものも含まれます。特定の agent のメモリだけを読むには、`agent` を指定してください。
`GET /v1/operations` はすべての操作を一覧表示し、<http://localhost:7777/docs> ではブラウザから各操作を試せます。リクエストとレスポンスの形式は [HTTP API リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)を参照してください。

<a id="5-first-memory-from-python"></a>

## 5. Python から最初のメモリを作成する

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

まだ PyPI に公開されていないリリースは、リリースのアセットからインストールします（[installing from a GitHub Release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)）。

この例では `org` と `namespace` を指定しているため、手順 3 のトークンの有無にかかわらず動作します。SDK はトークンが見つかればそれを送信します。シングルユーザーのサーバーでは、トークンなしの呼び出しでこれらを省略できます。`Agent(agent="quickstart", base_url=...)` も `dev`/`local` で動作します。

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

SDK がサーバーと認証情報をどこから見つけるかについては、[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials) を参照してください。TypeScript SDK は `npm install @clickhouse/loom` でインストールします（[クローンから](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)インストールする場合も可能です）。

クライアントのインターフェースについては、[Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) と [TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) のリファレンスを参照してください。

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. コーディングエージェントでの最初のメモリ

インストーラーがエージェントに接続済みの場合は、このセクションの最後の段落に進んでください。

Claude Code では、Loom プラグインをインストールします。MCP ツールに加えて、フック（セッション開始時のコンテキスト、終了時のキャプチャ）とスキルが追加されます。

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

このマシン上のシングルユーザーサーバーでは、Claude Code がプラグインの設定を求めてきたときに、デフォルトのサーバー URL をそのまま使い、トークンは空のままにします。

プラグインを使わない場合は、MCP エントリを直接追加します。

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

どちらの方法でも、このマシン上のシングルユーザーサーバーには、ユーザー自身のトークンなしで接続できます。

認証が有効な場合は、**Settings → Tokens** でトークンを作成し、**Connect** の下の **Claude Code** を選択します。ダイアログには、プラグイン用の設定と、トークン入りの `claude mcp add` エントリ用の設定が表示されます。その後、次の手順を行います。

1. `~/.loom/config.json` を、ダイアログの `config.json` ブロックと、トークンの保存先の名前空間（`"namespace": "<namespace>"`）で上書きします。シングルユーザーのセッションがファイルに残した `org` や `namespace` はトークンより優先されます。これらがない場合、プラグインはトークンの最初の名前空間（`*` 以外）か `coding` に書き込みます。設定済みだった `code_repo` や `agent` は元に戻してください。
2. `LOOM_API_KEY`（手順 3 でエクスポートしている場合があります。`unset LOOM_API_KEY` を実行してください）とプラグインの `token` オプションは、空のままにするか、新しいトークンに設定します。プラグインはこのファイルより先にこの両方を読み取ります（[configuration](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)）。
3. トークンなしで作成した `claude mcp add` エントリは、ダイアログのコマンドを実行する前に削除します（`claude mcp remove loom -s user`）。ダイアログのコマンドは、既に存在する名前を拒否します。

`loom install --client <name>` は、Claude Desktop、Claude Code、Codex、Cursor、VS Code、Cline、Windsurf、Zed 用の MCP エントリを書き込みます。フラグを指定しない場合は、見つかったすべてのクライアントに設定します。Codex と Cursor については [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) を参照してください。

新しいセッションを開始し、_「金曜日には絶対にデプロイしないことを覚えておいて」_ と伝えます。エージェントは `loom_memory_set` を呼び出します。新しいセッションで _「いつデプロイできる？」_ と尋ねると、エージェントは `loom_search` を呼び出し、保存された事実をもとに回答します。モデルは、ツールの説明に基づいて保存と検索のタイミングを判断します。トリガーとなる語はありません。

<a id="7-see-it-in-the-dashboard"></a>

## 7. ダッシュボードで確認する

| ページ            | 表示内容                                                         |
| ----------------- | ---------------------------------------------------------------- |
| `/memories`       | 名前空間のメモリを、書き込んだエージェントと時刻とともに表示します。 |
| `/admin/activity` | 監査ログ：どのエージェントがいつどのキーを書き込んだかを表示します。 |
| `/admin/usage`    | 操作ごと、エージェントごとのコストを表示します。                 |

その他の機能については [ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md) を参照してください。

<a id="optional-index-a-repository-on-the-released-stack"></a>

## オプション：リリース版スタックでリポジトリをインデックスする

このスタックでは、ダッシュボードからのリポジトリ追加は `SANDBOX_UNAVAILABLE` で拒否されます。追加には[リポジトリジョブのサンドボックス](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)が必要ですが、Docker のデフォルトの隔離がこれをブロックするため、このスタックでは無効にしています。信頼できるリポジトリだけを列挙してください。ビルド手順（TypeScript のインストール、Go モジュールのダウンロード、CMake）はサンドボックスなしで `loom` コンテナ内で実行され、このコンテナの環境には JWT シークレットとプロバイダーキーが含まれています。

`LOOM_CODE_REPOS_JSON` は、すべてのリポジトリを JSON 配列として 1 行で保持する変数です（[エントリの設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)）。`~/.loom/.env` でこの行のコメントを解除し（なければ追加し）、編集します。`dev` は、シングルユーザーのスタックが動作する org であり、マルチユーザーのアカウントがサインアップする org でもあります（`LOOM_SELF_SIGNUP_ORG`）。これを変更している場合は、ここと下の `--org` に自分のアカウントの org を使ってください。

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom は、リポジトリのビルドファイル（`go.mod`、`package.json`、`pyproject.toml`、ルートの `CMakeLists.txt` など）からコンパイラ層のインデクサーを選びます。代わりに `scip_producer` で 1 つを指定することもできます（[コンパイラ層](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)）。C/C++ は設定済みのビルドツリーも必要なため、それがない C/C++ リポジトリではパーサー層のみになります。
- プライベートリポジトリの場合は、GitHub トークン（`.env` の `LOOM_CODE_GITHUB_TOKEN`、またはエントリの `github_token`）を追加し、`"public":true` を外してください。これはホスト上で公開されているリポジトリ向けの設定で、org の誰もが **Repositories** からすぐにリーダーとしてインデックスに参加できるようになります。最初の `loom code init` の前に決めてください。これは `public` から名前空間の参加ポリシーを設定し、後から `public` を編集してもポリシーは変わりません（メンテナーは `code.repo.set_access` で変更できます）。

その後、`loom` コンテナを再作成し、最初のインデックスを実行します。

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` は、リリース版スタックのローカルエンベッダーで動作します。偽のエンベッダー（チェックアウトした場合の `.env`、または `fake-ngram-*` を指定したままのアップグレード済みインストール）では、拒否して終了します。それでも動作を試したい場合は、`--allow-fake-embedder` を追加してください。後から **Settings → Models & keys** でモデルを選ぶと、コードインデックスは再エンベディングされます。`.env` を変更した場合は、別途再エンベディングが必要です。詳しくは[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)を参照してください。

マルチユーザーモードでは、列挙したリポジトリにメンテナーがいないため、`"public":true` がないと、リーダーの参加リクエストは承認者がいないまま待機することになります。`loom code init` の後で、コンテナ内で発行したトークンを使い、オペレーターとして誰かにアクセス権を付与してください。`admin` は、その人をメンテナーにします。メンテナーはそれ以降、リーダーを承認します。`viewer` は読み取りを許可します。subject は、そのアカウントがサインアップ時に使った名前空間（ログインハンドル、つまりそのトークンの subject）です。セルフサインアップのメールアドレスは検証されないため、メールアドレスへの付与では誰も参加できません。

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

`loom demo` は、このリポジトリの `AGENTS.md` をアップロードし、`knowledge.ingest` で取り込み（スタックのリファレンス抽出器が解析します）、`knowledge.synthesize` でその内容について質問し、回答と引用されたチャンクを表示します。フェイク LLM を使う場合は、代わりに `knowledge.search` の上位ヒットを表示します。`--file` と `--question` を指定すると、自分のドキュメントと質問を使えます。

<a id="common-problems"></a>

## よくある問題

| 症状                                              | 原因と対処                                                                                                                                                                                     |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` が fact を書き込まない        | LLM がダミーのものになっています。[Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) に従って設定してください。                                                                                                      |
| 検索結果が無作為に見える                          | embedder がダミーのものです（`.env` の `fake-ngram-*`）。[Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) の手順で切り替えると、保存済みのデータが再埋め込みされます。モデルを変えるためにボリュームを削除しないでください。 |
| 認証を有効にした後に 401 `AUTH_INVALID` になる    | トークンがないか、別の `LOOM_JWT_SECRET` で署名されたトークンです。[トークンを取得する](#3-get-a-token)の Compose コマンドで再発行してください。                                                                     |
| ダッシュボードが 503 を返す                       | サーバーがソースから実行されており（`make server`）、フロントエンドがビルドされていません。`make frontend-build` を実行してください。                                                                                               |
| リリース版のスタックが healthy にならない         | `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse` を実行してください。[Troubleshooting](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md) も参照してください。                                               |
| リポジトリを追加すると `SANDBOX_UNAVAILABLE` が返る | スタックがリポジトリジョブ用のサンドボックスなしで動いています。`LOOM_CODE_REPOS_JSON` でリポジトリをインデックスしてください（[リポジトリをインデックスする](#optional-index-a-repository-on-the-released-stack)）。             |
| インストーラーがポート `7777` は使用中だと表示する | 別のプログラムまたは別の Loom がそのポートで待ち受けています。それを停止するか、`sh loom-install.sh` の前に `LOOM_PORT=7778` を付けてインストールコマンドを再実行してください。`.env` にポートが保持されます。                      |
| インストーラーがポート `7777` が応答しないと表示する | 応答しないままポートを占有しているプログラムがあるか、`LOOM_BIND_HOST`、`LOOM_PORT` またはファイアウォールのために、このホストからポートに到達できません。                                                                         |
| インストーラーがポート `7777` を確認できなかった  | curl が、終了コードが示す理由で失敗しました。`LOOM_BIND_HOST` と `LOOM_PORT` を確認してください。                                                                                                        |
| クローンのポート `8123` または `9000` が使用中    | ClickHouse のホストポートを再マップしてください（[local stack](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)）。                                                                                         |
| インストーラーがイメージを pull できなかった      | Docker がレジストリに到達できていません。プロキシ配下の場合は、Docker デーモンに[プロキシ設定](https://docs.docker.com/engine/daemon/proxy/)を行ってください。                                                 |

<a id="next"></a>

## 次のステップ

- [Evaluate Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)：このスモークテストを、ワークロードに合わせた品質、レイテンシ、コストの評価へと発展させます。
- [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)：このスタックに対して Claude Code、Codex、Cursor を接続します。
- [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：SDK から、ID、abstain の扱い、セッション、コンテキストパック、実行（run）を扱います。
- [Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)：チーム向けに Loom を運用します。
