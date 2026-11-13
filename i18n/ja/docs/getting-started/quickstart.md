<!-- i18n: source=docs/getting-started/quickstart.md blob=4175c372ea5183fffd31de1d59658aae09018c47 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# クイックスタート

このページでは、ノート PC 上で Loom を起動し、HTTP、Python、MCP で最初のメモリを保存します。API キーもアカウントも不要です。リリース版スタックを新規インストールした場合は、イメージに含まれる小さなローカルモデルで埋め込みを行うため、ネットワークがなくても意味のある検索結果が返ります（アップグレードした環境では、`.env` で指定されている embedder がそのまま使われます）。LLM はフェイクなので、抽出と `memory.ask` の回答は何も生成しません。チェックアウトした環境の `.env` では、どちらもフェイクプロバイダーを使います。

**標準の Docker インストールはシングルユーザー向けです。** ログインはなく、アクセスは localhost のみに限られ、名前空間は既定で `dev/local` です。マルチユーザーモードを有効にしなくても、1 人で複数のエージェントを接続できます。**OSS ではチーム向けのデプロイもサポートしています。** 認証付きアカウントと名前空間のアクセス制御を利用できます。[マルチユーザーモードで起動する](#start-in-multi-user-mode)か、[デプロイ → ID モード](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)を参照してください。どちらのモードでも Loom の機能は同じです。実際の LLM と埋め込みを使うには、どちらのモードでもプロバイダーの設定が必要です。

<a id="prerequisites"></a>

## 前提条件

| 必要なもの                     | 用途                                                                                       |
| ------------------------------ | ------------------------------------------------------------------------------------------ |
| compose プラグイン付きの Docker | スタック（ClickHouse と Loom サーバー）の実行。                                             |
| Python 3.12 以降               | コントリビューター専用の `loom` CLI。SDK は 3.11 以降をサポートします。                    |
| Node.js 20 以降                | TypeScript SDK（任意）。                                                                   |
| 空きポート `7777`              | Loom 用。チェックアウトした状態での開発では、ClickHouse も `8123` と `9000` で公開されます。 |
| `jq`                           | 手順 4 の `curl` の出力を読むため（任意）。                                                |

<a id="1-boot-the-stack"></a>

## 1. スタックを起動する

<a id="fast-path-released-image-no-clone"></a>

### 最短手順：リリース済みイメージを使う（clone 不要）

前提条件：Docker Desktop、または Compose 2.20 以降を備えた Docker Engine。Windows では、`docker` が動作する WSL 2 のターミナルでコマンドを実行します。つまり、そのディストリビューションで WSL 連携を有効にした Docker Desktop、またはそのディストリビューションにインストールした Docker Engine のいずれかが必要です。インストーラは POSIX シェルスクリプトです。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

このコマンドはインストーラを現在のディレクトリに保存してから、そのファイルを実行します。そのため、ホームディレクトリ、またはあなただけが書き込めるほかのディレクトリで実行してください。共有ディレクトリでは、実行前に他のユーザーが `loom-install.sh` を差し替えたり、別のファイルを指すようにしたりできてしまいます。ダウンロードに失敗した場合は、curl のエラーと 0 以外の終了コードで停止します。インストールに失敗した場合は、報告された問題を修正してから、コマンド全体をもう一度実行してください。インストーラは最新の GitHub Release から `loom-stack.tar.gz` をダウンロードして SHA-256 を検証し、Compose と ClickHouse の設定を `~/.loom` にインストールします。続いて、対応する `ghcr.io/clickhouse/loom:X.Y.Z` と `ghcr.io/clickhouse/loom-extractor:X.Y.Z`（ドキュメント抽出サービス）のイメージを pull し、`http://localhost:7777/v1/health` が応答するまで待ちます。サーバイメージには、API、ワーカー、マイグレーション、そしてサーバのルートで配信するビルド済みの React ダッシュボードが含まれており、Python や Node のインストールは不要です。Python、TypeScript、Go のコンパイラインデクサが同梱され、有効になっています。`amd64` では C++ のインデクサ（`scip-clang`）も同様です。TypeScript インデクサでインデックスしたリポジトリでは、依存関係が自動的にインストールされます。どちらのイメージも Linux の `amd64` と `arm64` に対応しており、Apple Silicon 上の Docker Desktop も含まれます。

Loom が応答すると、インストーラは次の処理を行います。

1. **メモリを確認する。** 新規インストールでは、テスト用のメモリを保存し、読み戻し、削除します。これにより書き込みと読み取りが動作することを確認でき、有効なメモリは残りません（削除されたバージョンは「Recently deleted」に表示されます）。この確認に通過した後の再実行やアップグレードでは、この処理は行いません。
2. **サインインが有効かどうかを調べる。** サインインが有効な場合、エージェントは接続せず、トークンの入手先を案内します。
3. **コーディングエージェントの接続を提案する。** ターミナルでは、見つかったエージェントごとに確認します。Claude Code と Codex はそれぞれのプラグイン経由、VS Code は `code --add-mcp` で接続します。`LOOM_CONNECT` を指定すると、確認の代わりにその値で動作し、ターミナルがない実行でも使えます。調べるのは指定したクライアントだけで、値は `claude-code`、`codex`、`vscode` のカンマ区切りリスト、`all`、または単独の `none` です。それ以外の値ではインストールが停止します。ターミナルがなく `LOOM_CONNECT` もない場合、どのエージェントも調べず、変更もしません。以前に設定したプラグインや MCP サーバ（プラグイン、`claude mcp add`、`codex mcp add`、`loom install` によるもの）はそのまま残します。Claude Code のプラグインについては、このインストールを指すようにするコマンドを表示します。Claude Code のプラグインは PATH 上の Node.js 18 以降を使うため、Node.js がない場合、インストーラはプラグインをインストールせず、MCP サーバだけを追加する `claude mcp add` の行を表示します。Codex のプラグインは nvm などで入れた Node.js も見つけられるため、インストールされ、`codex mcp add` の行も併せて表示されます。Codex のプラグインは常に `http://localhost:7777` を使うので、別の場所へのインストールでは、代わりにそのセットアップページへのリンクが表示されます。VS Code は、リモートセッションと WSL では提案されません。`LOOM_CONNECT` で指定したクライアントが PATH にない場合は、その名前が表示されます。
4. **ダッシュボードを開く。** 新規インストールでデスクトップ環境の場合に開きます。`LOOM_NO_OPEN=1` で開かないようにできます。

WSL から実行した場合、これらの手順は WSL 内で作用します。Windows 上で動作するエージェントは、[接続ガイド](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)に従って Windows から接続し、インストーラが表示するダッシュボードのアドレスは Windows のブラウザで開いてください。

Ctrl-C はどの手順でも実行を中断します。ただし、エージェント CLI がプラグイン用に開始した clone は、バックグラウンドで完了することがあります。

スタックは専用の ClickHouse コンテナを実行します。すでに運用している ClickHouse（セルフマネージドのサーバ、または 26.4 以降で Keeper を備えた ClickHouse Cloud サービス）を使うには、その URL を渡します。URL にパスワードが含まれていない場合、インストーラはパスワードを尋ねます。そのうえで `loom` データベースと読み取り専用のリーダーユーザーを作成するか、管理者が実行すべき SQL を表示します（[ClickHouse の選び方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)）。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

各リリースの `install.sh` はそのリリースをインストールします。したがって `latest` が配信するコピーは最新リリースをインストールし、下記のリリース固有の URL は常にそのリリースをインストールします。新しいインストールの上に古いリリースのコピーを実行すると、新しいデータの上で古い Loom と ClickHouse を起動しないよう、インストールを拒否します。意図的にダウングレードする場合は、[インストーラによるインストールのダウングレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install)に従ってください。リリースを固定するには、そのリリース固有の URL を使います。

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

出力内容に問題がなければ、検証済みのバンドルからインストールします。

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

インストーラに渡した `LOOM_PORT`、`LOOM_BIND_HOST`、`LOOM_IMAGE_REPOSITORY`、`LOOM_EXTRACTOR_IMAGE_REPOSITORY`、`LOOM_CLICKHOUSE_IMAGE` は、インストールディレクトリの `.env` に書き込まれます。そのため、以降そこで実行する `docker compose` コマンドや、以降のインストーラ実行でも同じポートとイメージが使われます。`LOOM_BIND_HOST` にループバック以外を指定すると Loom がこのホストの外へ公開されるため、`LOOM_PUBLIC_BASE_URL` とサインインがなければ、サーバはその状態での起動を拒否します。その場合は `LOOM_SKIP_START=1` を付けてインストールし、`.env`（後述）にそれらを設定してから、`LOOM_SKIP_START=1` なしでインストールコマンドをもう一度実行してください。

各リリースは、ビルド時点で最新だった ClickHouse リリースを実行します。そのため、同じ Loom バージョンに対してインストールコマンドを再実行しても ClickHouse は変わらず、より新しい Loom バージョンをインストールすると ClickHouse も更新されます。ただし `.env` で `LOOM_CLICKHOUSE_IMAGE` を固定している場合は更新されません（その行を削除すると、再びリリースに追従します）。インストールコマンドを再実行すると、管理対象の Compose／設定ファイルが更新され、コンテナが調整されます。`~/.loom/.env` と名前付き Docker ボリュームはそのまま保持され、抽出サービスの URL とトークンがない場合は追加されます。データを削除せずに停止するには、次のようにします。

```bash
docker compose --project-directory "$HOME/.loom" down
```

`--volumes` は、元に戻せないリセットを意図して行う場合にのみ付けてください。

<a id="try-it-in-github-codespaces"></a>

### GitHub Codespaces で試す

[![GitHub Codespaces で開く](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

リポジトリの dev container は、手元のマシンには何も入れずに、codespace 内で最新リリースを実行します。上記のインストーラで Loom をインストールして起動し、Claude Code を接続します。初回の起動では約 3 GB のイメージをダウンロードするため、数分かかります。ダッシュボードは、作成ログの最後の数行に表示されるアドレス（`Loom is running in the codespace. Dashboard: https://<codespace name>-7777.app.github.dev`。これは **Ports** タブにあるポート 7777 の転送アドレスです）で開きます。その上に表示されるインストーラの `http://127.0.0.1:7777/` は、codespace 自身のアドレスです。codespace のターミナルで `cd ~ && claude` を実行し（Loom 自体の開発向けの指示を含むチェックアウトの外で実行します）、サインインして「Remember that I never deploy on Friday」と伝えます。新しい `claude` セッションで「When can I deploy?」と尋ねてください。

この試用環境はサインインなしのシングルユーザーで動作します。ブラウザではポート 7777 が非公開で転送されており、そのアドレスはあなたの GitHub アカウントにのみ応答します。非公開のままにしてください。公開にしたり、組織内限定にしたりすると、サインインのないサーバが共有先の全員に開放されます。codespace 内のエージェント（codespace の `claude` など）は `http://localhost:7777` を使います。ダッシュボードの接続行には転送アドレスが表示されますが、そのアドレスには GitHub のサインインが必要で、codespace 内のエージェントにはそれがありません。デスクトップ版 VS Code で開いた場合、codespace はこのポートをノート PC の `localhost:7777` にも転送します。そこはローカルインストールと同様に、ノート PC 上のすべてのプログラムに開かれており、ノート PC 上のエージェントも到達できます。この転送にはそのローカルポートが空いている必要があるため、ローカルの Loom は先に停止してください。ダッシュボードはブラウザのアドレスから使ってください。ノート PC の `localhost:7777` ではページは開きますが、サーバがそのリクエストを拒否するためデータは表示されません。このサーバ自身のページといえるのは、転送アドレスだけです。

インストールは `/workspaces/.loom` にあるため、codespace を再構築しても `.env` と Compose プロジェクトは保持されます。ただしメモリは Docker ボリュームにあり、再構築で保持されない場合があります。また、再構築のたびにその上で最新リリースのインストーラが実行され、そのリリースに更新されます。このドキュメントで `~/.loom` や `"$HOME/.loom"` と書かれている箇所は、codespace では `/workspaces/.loom` を指します。Codespaces は、ポート 7777 が待ち受けを始めるとすぐに開くかどうかを尋ねます。ダッシュボードは、作成ログに「Loom is running in the codespace」と表示されてから応答します。ログがエラーで終わっている場合は、codespace のターミナルで `sh .devcontainer/start-loom.sh` を実行してください。初回の実行で設定された内容は保持されます。

<a id="start-in-multi-user-mode"></a>

### マルチユーザーモードで起動する

リリース済みイメージを新規にインストールし、初回起動からアカウントとサインインを有効にするには、コンテナを起動せずにスタックのファイルだけを用意します。

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

生成された `COMPOSE_PROJECT_NAME` と `LOOM_JWT_SECRET` はそのまま残してください。続いて、スタックを pull して起動します（独自の ClickHouse を使う場合は、インストーラの案内どおり、先に `ch-setup` を実行します）。

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

[ヘルスチェック](#check-the-stack)に通ったら、`http://localhost:7777/` を開いてサインアップします。SDK と MCP のクライアントにはトークンが必要です。以下の例にあるシングルユーザー用の `dev/local` のデフォルト値ではなく、あなたのアカウントに付与された org と名前空間を使ってください。

これが有効にするのはマルチユーザー認証であり、公開ネットワークへの接続ではありません。スタックは引き続き localhost にバインドされます。チームと共有する前に、[オペレーターのアクセス](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)と [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls) を設定してください。[実際のモデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)も設定してください。マルチユーザーモードを選んでも、フェイク LLM は置き換わりません。

すでに稼働中のシングルユーザーのスタックについては、代わりに[切り替え手順](#2-open-the-dashboard)に従ってください。コンテナを再作成しても、既存のデータボリュームは保持されます。

<a id="contributor-path-build-a-checkout"></a>

### コントリビューター向け：チェックアウトをビルドする

前提条件：Docker Compose 2.34 以降。チェックアウトの compose ファイルが ClickHouse を毎日 pull するために必要です。

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` は次の 3 つを行います。

1. git が管理しない 2 つのファイルを作成します。`.env.example` からの `.env` と、テンプレートからの `deploy/clickhouse-config/named_collections.xml` です。どちらも上書きしません。抽出サービスのトークンがない既存の `.env` には、トークンを追加します。
2. dev の compose スタックを `--build` 付きで実行します。
3. サーバの準備ができるまで `http://localhost:7777/v1/health` をポーリングし、保留中のマイグレーション数を表示します。`--timeout`（300 秒）が経過すると、最後のヘルス応答の本文とログの行を表示して終了します。

コントリビューターの初回起動ではイメージのビルドと全スキーママイグレーションの適用が行われるため、数分かかります。その後 `loom up` は、ダッシュボードの URL（`http://localhost:7777/`）、最初のメモリを保存する `curl` の呼び出しと Python スニペット、そして Claude Code を接続する行を表示します。認証が有効な場合は、writer トークンも発行し、それらのスニペットに埋め込みます。

| フラグ       | 動作                           |
| ------------ | ------------------------------ |
| `--rebuild`  | コード変更後に再ビルドします。 |
| `--no-build` | キャッシュ済みイメージを再利用します。 |
| `--open`     | ダッシュボードを開きます。     |
| `--port`     | 別の Loom ポートを使います。   |
| `--env-file` | 別の env ファイルを指定します。 |

稼働中のスタックで `loom up` を再実行すると、ヘルスの再確認だけを行います。`make dev` は CLI を使わずに同じことを行い、次を実行します。

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

`compose.yaml` を先に指定すると、Compose は `${VAR:-default}` の置換のためにリポジトリルートの `.env` を読み込みます。`--env-file .env` はそれを明示しているだけです。`loom down` はスタックを停止し、ボリュームを保持します。`loom down --delete-data` は、生成された Compose プロジェクト名を入力した場合にのみ、このインストールの名前付きボリュームだけを完全に削除します。`--yes` は対話なしの確認です。Makefile のターゲットも同様で、`make down` はデータを保持し、`make down-all` は削除します。

<a id="what-the-stock-env-boots"></a>

### 標準の `.env` で起動するもの

チェックアウトとリリースバンドルの `.env` テンプレートは、どちらもシングルユーザーモードを選択します。リリースバンドルは `LOOM_IDENTITY_PROVIDER=single-user` を明示的に設定します。チェックアウトでは空のままにし、`LOOM_AUTH_REQUIRED=false` から同じモードを推定します。

| 設定                                                                         | 値            | 効果                                                                      |
| ---------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user`（または空） | `single-user` | ログインなし。デフォルトの名前空間は `dev/local`。SDK と MCP の呼び出しにトークンは不要です。 |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | フェイク LLM  | 抽出、エンティティのタグ付け、`memory.ask` の合成は何も生成しません。     |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384`（リリースバンドル）   | bge-small     | イメージに含まれるモデルファイルによる、CPU 上のセマンティック検索。      |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072`（チェックアウト） | フェイクエンベッダ | 検索は文字 n-gram で順位付けします。配線の確認には十分ですが、再現率の確認には不十分です。 |

どちらのインストーラも、一意の `COMPOSE_PROJECT_NAME`、ランダムな JWT 署名シークレット、ランダムな抽出サービスのトークン、`LOOM_BIND_HOST=127.0.0.1` を生成します。そのため、別々のクローンが ClickHouse のボリュームを意図せず共有することはなく、Loom と ClickHouse はこのコンピュータからのみ到達できます。

実際のプロバイダを使うには、**Settings → Models & keys** を開き、プロバイダとモデルを選んで API キーを入力し、**Test & apply** をクリックします。1 つの OpenAI API キーで LLM とエンベディングの両方を賄えます。Docker の再起動は不要です。エンベディングを変更すると検証付きの再構築が実行され、進捗と再試行はダッシュボードに表示されます。

環境変数ベースでデプロイする場合は、代わりに `~/.loom/.env`（リリース版スタック）または `.env`（チェックアウト）のプロバイダ行を置き換えます。`OPENAI_API_KEY` を設定するだけでは何も変わりません。標準のプロバイダ行は、プロバイダを明示的に指定しているためです。ローカルまたはフェイクのエンベッダから別のものへ切り替えると、保存済みのデータが再エンベディングされます。

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Claude、OpenAI、ホスト型エンベディング、サブスクリプション専用のクライアント、LLM プロバイダを 1 つだけ使うデプロイについては、[モデルと API キーの設定](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)に従ってください。リリース版スタックの `.env` を編集した後は、`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom` で反映します。通常の再起動では、コンテナの環境変数は再読み込みされません。詳細な設定は[デプロイ → プロバイダ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)にあります。

<a id="check-the-stack"></a>

### スタックを確認する

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` は、ClickHouse に到達でき、`code_tier_failing`（リポジトリのコードインデックスのティアの失敗）以外にエラーとなる問題がない場合に `status: ok` を返します。`fake_embedding`、`fake_llm`、`providers_degraded` は現在どのモードかを示します。`problems` は各問題とその対処を列挙し、`schema.pending` は未適用のマイグレーションを列挙します。
- `loom doctor` は、サーバ側のチェックごとに 1 行を出力します。`--json` を付けると JSON で出力します。ソースのチェックアウトから起動したスタックでは、`docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor` を実行してください。

<a id="2-open-the-dashboard"></a>

## 2. ダッシュボードを開く

`single-user` モードでは、`http://localhost:7777/` を開くとそのままダッシュボードが表示されます。マルチユーザーモードでは、先にサインアップまたはサインインしてください。`single-user` モードでは、ダッシュボードとエージェントのデフォルトに `dev/local` が使われます（`LOOM_DEFAULT_NAMESPACE` で名前空間の名前を変更できます）。これは利便性のためのモードであり、ユーザー分離の境界ではありません。サーバーに到達できる人は誰でも、そのデータと管理機能にアクセスできます。localhost のみで使用してください。

代わりにサインアップを試す場合（`trust-network`：メールアドレスとパスワードを使い、名前空間の名前を最初に取得した人がその管理者になります）は、`~/.loom/.env`（リリース版スタック）または `.env`（チェックアウト）の次のエントリを置き換えます。

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

`single-user` を明示的に選択している場合、`LOOM_AUTH_REQUIRED=true` だけを設定しても不十分です。このモードでは認証がオフのままになります。インストーラーが生成した `LOOM_JWT_SECRET` と既存のボリュームはそのまま維持し、サーバーを再作成して環境変数を読み込ませます。

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

その後、ダッシュボードはサインインページで開き、**Sign up** タブが表示されます。すべての SDK と MCP の呼び出しにトークンが必要になります。ID モードについては [ID と名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes)を参照してください。`trust-network` を指定すると、サインアップを開放したまま、ループバック以外の `LOOM_BIND_HOST` で起動することもできます。その場合、ポートに到達できる人は誰でもサインアップできます。チームがサーバーに到達できるようにする前に、オペレーターのアクセスについては[デプロイガイド](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)に、[TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls) については該当セクションに従ってください。

<a id="3-get-a-token"></a>

## 3. トークンを取得する

シングルユーザーモードではトークンは任意です。それでもトークンを発行しておくと、認証を有効にしたときにもスクリプトがそのまま動きます。ただし、スクリプトが `org`（または空欄）、`namespace`、`agent` を明示していることが条件です。SDK が認証情報を見つけて送るときのように（[Python SDK → 認証情報](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)）、認証情報を送る呼び出しでは、これらのどれも自動では補われません。空欄の `org` はトークン自身の org になります。

- **ダッシュボード：** **Settings → Tokens → New token**。デフォルトでは、トークンは所属するすべての名前空間とリポジトリを読み取れます（制限付きリポジトリは追加した場合のみ）。コレクションは読み取れず、保存先は現在の名前空間です。_Only what you choose_ を選ぶと、チェックを入れた名前空間、コレクション、リポジトリだけに制限されます（初期状態では何も選択されていません）。チェックを入れたものは一覧の順に検索されます。順序は行の矢印で変更でき、デスクトップでは行のグリップをドラッグしても変更できます。結果画面にはトークンが一度だけ表示され、MCP クライアントまたはスクリプト向けのセットアップも示されます。
- **CLI（オペレーターとして）：** 署名シークレットを保持しているサーバーコンテナ内で実行します。

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  マシンの外に出すトークンには `--ttl-days 30` を追加してください。`loom mint-token --help` で、スコープ指定のオプション（`--namespace-role`、`--read-scope`、`--traffic-class`）を確認できます。

- **自分でサインアップするボット**（トラストネットワークのみ）：
  `loom init --agent nightly-triage --server-url http://localhost:7777` を実行すると、名前空間 `agent-nightly-triage` が作成され、writer トークンが `~/.loom/token` に、URL が `~/.loom/config.json` に保存され、クレームコードが表示されます。担当者は `/oauth/agent-claim` でそのコードを一度入力し、自分のパスワードを設定して所有権を取得します。

SDK、CLI、プラグインが参照する場所にトークンを保存します。

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

クレーム、ロール、グラントについては [トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) を参照してください。

<a id="4-first-memory-over-http"></a>

## 4. HTTP での最初のメモリ

すべての操作は JSON ボディを持つ `POST /v1/<op>` です。インストーラーまたはチェックアウトから得た、標準の `.env` のままの新規インストールはシングルユーザーです。トークンは不要で、`org` や `namespace` を指定しない日常的なメモリ呼び出しや検索呼び出し（`memory.set`、`memory.get`、`memory.search` ほか数個）は、そのインストール自身の org `dev`、名前空間 `local` で実行されます。これは MCP とダッシュボードが使うものと同じです。`agent` を指定しない `memory.set` はエージェント `http` として記録され、検索はすべてのエージェントを対象に読み取ります。そのため、以下の呼び出しに必要なのは各呼び出し固有のフィールドだけです（[対象となる op](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)）。トークンなしの呼び出しが 401 `AUTH_INVALID` を返す場合は、認証が有効になっています。`-H "Authorization: Bearer $LOOM_API_KEY"` を追加し、アカウントの `org`、`namespace`、`agent` を指定してください。トークンが空のヘッダーは拒否されます。

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

`memory.set` のレスポンスには `memory_id`、`content_sha256`、`status` が含まれます。`memory.get` は 1 つのキーを読み戻します。`found` はそのキーが有効な値を保持しているかどうかを示し、`value` は書き込まれたままの値です。検索レスポンスには `results` と `abstain` が含まれ、後者は最上位のヒットが回答の根拠として十分に強いかどうかを示します。検索は名前空間内のすべてのエージェントのメモリを対象とし、ダッシュボードで追加されたものも含みます。特定のエージェントのメモリだけを読むには、`agent` を指定してください。
`GET /v1/operations` はすべての操作を一覧表示し、<http://localhost:7777/docs> ではブラウザから各操作を試せます。リクエストとレスポンスの形式は [HTTP API リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)を参照してください。

<a id="5-first-memory-from-python"></a>

## 5. Python からの最初のメモリ

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

まだ PyPI にない リリースは、そのアセットからインストールします（[GitHub Release からのインストール](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)）。

この例では `org` と `namespace` を指定しているため、手順 3 のトークンがあってもなくても動作します。SDK はトークンを見つけると、それを送信します。シングルユーザーのサーバーでは、トークンなしの呼び出しでこれらを省略できます。`Agent(agent="quickstart", base_url=...)` も `dev`/`local` で動作します。

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

SDK がサーバーと認証情報をどこから見つけるかについては、[Python SDK → 認証情報](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)を参照してください。TypeScript SDK は `npm install @clickhouse/loom` でインストールします（[クローンからのインストール](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)）。

クライアントのインターフェースについては、[Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) と [TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) のリファレンスを参照してください。

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. コーディングエージェントでの最初のメモリ

インストーラーがエージェントを接続済みの場合は、このセクションの最後の段落に進んでください。

Claude Code では、Loom プラグインをインストールします。MCP ツールに加えて、フック（セッション開始時のコンテキスト、終了時のキャプチャ）とスキルが追加されます。

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

このマシン上のシングルユーザーサーバーでは、デフォルトのサーバー URL をそのまま使い、Claude Code がプラグインの設定を尋ねてきたらトークンは空のままにします。

プラグインを使わない場合は、MCP エントリを直接追加します。

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

どちらの方法でも、このマシン上のシングルユーザーサーバーには自分のトークンなしで接続できます。

認証が有効な場合は、**Settings → Tokens** でトークンを作成し、**Connect** の下の **Claude Code** を選択します。ダイアログには、プラグイン用の設定と、トークン入りの `claude mcp add` エントリ用の設定が表示されます。その後、次の手順を行います。

1. `~/.loom/config.json` を、ダイアログの `config.json` ブロックと、トークンの保存先の名前空間（`"namespace": "<namespace>"`）で上書きします。シングルユーザーのセッションがファイルに残した `org` や `namespace` はトークンより優先されます。これらがない場合、プラグインは `*` 以外でトークンの最初の名前空間、または `coding` に書き込みます。設定していた `code_repo` や `agent` は元に戻して追加してください。
2. `LOOM_API_KEY`（手順 3 でエクスポートした可能性があります：`unset LOOM_API_KEY`）とプラグインの `token` オプションは空のままにするか、新しいトークンを設定します。プラグインはそのファイルより前に、この両方を読み込みます（[設定](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)）。
3. トークンなしで作成した `claude mcp add` エントリは、ダイアログのコマンドを実行する前に削除します（`claude mcp remove loom -s user`）。ダイアログのコマンドは、既に存在する名前を拒否します。

`loom install --client <name>` は、Claude Desktop、Claude Code、Codex、Cursor、VS Code、Cline、Windsurf、Zed 向けの MCP エントリを書き込みます。フラグを指定しない場合は、見つかったすべてのクライアントを設定します。Codex と Cursor については、[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)を参照してください。

新しいセッションを開始し、_「金曜日には絶対にデプロイしない」ことを覚えておいて_ と伝えます。エージェントは `loom_memory_set` を呼び出します。新しいセッションで _「いつデプロイできる？」_ と尋ねると、エージェントは `loom_search` を呼び出し、保存された事実に基づいて回答します。モデルはツールの説明から、保存や検索のタイミングを判断します。トリガーワードはありません。

<a id="7-see-it-in-the-dashboard"></a>

## 7. ダッシュボードで確認する

| ページ            | 表示内容                                                     |
| ----------------- | ------------------------------------------------------------ |
| `/memories`       | 名前空間のメモリ。書き込んだエージェントと時刻も表示します。 |
| `/admin/activity` | 監査ログ。どのエージェントがいつ、どのキーを書き込んだかを示します。 |
| `/admin/usage`    | 操作ごと、エージェントごとのコスト。                         |

その他の機能については、[ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)を参照してください。

<a id="optional-index-a-repository-on-the-released-stack"></a>

## オプション：リリース済みスタックでリポジトリをインデックスする

このスタックでは、ダッシュボードからのリポジトリ追加は `SANDBOX_UNAVAILABLE` で拒否されます。追加には[リポジトリジョブのサンドボックス](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox)が必要ですが、Docker のデフォルトの閉じ込め設定がこれをブロックするため、このスタックではサンドボックスを無効にしています。信頼できるリポジトリだけを登録してください。ビルド手順（TypeScript のインストール、Go モジュールのダウンロード、CMake）はサンドボックスなしで `loom` コンテナ内で実行され、そのコンテナの環境には JWT シークレットとプロバイダーキーが含まれています。

`LOOM_CODE_REPOS_JSON` は、すべてのリポジトリを JSON 配列として保持する 1 行です（[エントリの設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)）。`~/.loom/.env` で、この行のコメントを解除し（無ければ追加し）、編集します。`dev` は、シングルユーザースタックが動作する org であり、マルチユーザーのアカウントがサインアップする org（`LOOM_SELF_SIGNUP_ORG`）でもあります。これを変更した場合は、ここと下の `--org` に、自分のアカウントの org を使ってください。

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom は、リポジトリのビルドファイル（`go.mod`、`package.json`、`pyproject.toml`、ルートの `CMakeLists.txt` など）からコンパイラティアのインデクサーを選びます。代わりに `scip_producer` で 1 つを指定することもできます（[コンパイラティア](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)）。C/C++ には設定済みのビルドツリーも必要なため、それが無い C/C++ リポジトリではパーサーティアのみが使われます。
- プライベートリポジトリの場合は、GitHub トークン（`.env` の `LOOM_CODE_GITHUB_TOKEN`、またはエントリの `github_token`）を追加し、`"public":true` を外してください。この設定は、ホスト上で公開されているリポジトリ向けで、org 内の誰もが **Repositories** からすぐにリーダーとしてインデックスに参加できるようになります。最初の `loom code init` の前に決めてください。名前空間の参加ポリシーは `public` から設定され、後から `public` を編集しても変わりません（メンテナーは `code.repo.set_access` で変更できます）。

その後、`loom` コンテナを再作成し、最初のインデックスを実行します。

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` は、リリース済みスタックのローカルエンベッダーで動作します。フェイクエンベッダーの下（チェックアウトの `.env`、または `fake-ngram-*` を指定したままのアップグレード済みインストール）では、拒否されて終了します。それでも配線を試したい場合は、`--allow-fake-embedder` を追加してください。後から **Settings → Models & keys** でモデルを選ぶとコードインデックスが再エンベッドされます。`.env` を変更した場合は、[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)に記載のとおり、別途再エンベッドが必要です。

マルチユーザーモードでは、登録されたリポジトリにメンテナーがいないため、`"public":true` が無いと、リーダーの参加リクエストは承認する人がいないまま待機します。`loom code init` の後、オペレーターとして、コンテナ内で発行したトークンを使って誰かにアクセス権を付与してください。`admin` はその人をメンテナーにし、以降はその人がリーダーを承認します。`viewer` は読み取りを許可します。subject は、そのアカウントがサインアップ時に得た名前空間（ログインハンドルで、そのトークンの subject）です。セルフサインアップのメールアドレスは検証されないため、それに対する付与では誰も入れません。

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## オプション：ドキュメントに質問する

```bash
loom demo
```

`loom demo` は、このリポジトリの `AGENTS.md` をアップロードし、`knowledge.ingest` で取り込み（スタックのリファレンス抽出器が解析します）、`knowledge.synthesize` でその内容について質問し、回答と引用されたチャンクを出力します。フェイク LLM を使う場合は、代わりに `knowledge.search` の上位ヒットを出力します。`--file` と `--question` を指定すると、自分のドキュメントと質問を使えます。

<a id="common-problems"></a>

## よくある問題

| 症状                                                                          | 原因と対処                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` が fact を書き込まない                                    | LLM がフェイクのものです。[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)に従って設定してください。                                                                                                                                                                                    |
| 検索結果が無作為に見える                                                      | エンベッダーがフェイクのものです（`.env` の `fake-ngram-*`）。[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)の説明どおりに切り替えると、保存済みのデータが再埋め込みされます。モデルを変更するためにボリュームを削除しないでください。                                                                               |
| 認証を有効にした後に 401 `AUTH_INVALID` が返る                                | トークンがないか、別の `LOOM_JWT_SECRET` で署名されたトークンです。[トークンを取得する](#3-get-a-token)の Compose コマンドで再発行してください。                                                                                                                                                   |
| ダッシュボードが 503 を返す                                                   | サーバーをソースから（`make server`）、フロントエンドをビルドせずに実行しています。`make frontend-build` を実行してください。                                                                                                                                                                             |
| リリース版のスタックが healthy にならない                                     | `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse` を実行してください。[トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)も参照してください。                                                                                                                             |
| リポジトリを追加すると `SANDBOX_UNAVAILABLE` が返る                           | スタックがリポジトリジョブのサンドボックスなしで動作しています。`LOOM_CODE_REPOS_JSON` を使ってリポジトリをインデックスしてください（[リポジトリをインデックスする](#optional-index-a-repository-on-the-released-stack)）。                                                                                           |
| インストーラーがポート `7777` は使用中だと表示する                            | 別のプログラム、または別の Loom がそのポートで待ち受けています。それを停止するか、`sh loom-install.sh` の前に `LOOM_PORT=7778` を付けてインストールコマンドを再実行してください。`.env` にポートが保持されます。                                                                    |
| インストーラーがポート `7777` は応答しないと表示する                          | 応答しないままポートを占有しているプログラムがあるか、`LOOM_BIND_HOST`、`LOOM_PORT` またはファイアウォールのためにこのホストからポートに到達できません。                                                                                                                                                       |
| インストーラーがポート `7777` を確認できなかったと表示する                    | curl が、終了コードが示す理由で失敗しました。`LOOM_BIND_HOST` と `LOOM_PORT` を確認してください。                                                                                                                                                                                      |
| WSL 上の Docker Desktop で、インストールが `Ports are not available` で停止する | Windows がポートを使用または予約しており、WSL 内のインストーラーのチェックでは検出できません。`netsh interface ipv4 show excludedportrange protocol=tcp` で予約済みの範囲を確認し、その範囲外の `LOOM_PORT` を `sh loom-install.sh` の前に付けてインストールコマンドを再実行してください。 |
| クローンでポート `8123` または `9000` が使用中                                | ClickHouse のホストポートを再マップしてください（[ローカルスタック](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)）。                                                                                                                                                                       |
| インストーラーがイメージをプルできない                                        | Docker 自身がレジストリに到達できません。プロキシの内側にある場合は、Docker デーモンに[プロキシ設定](https://docs.docker.com/engine/daemon/proxy/)を行ってください。                                                                                                                               |

<a id="next"></a>

## 次のステップ

- [Loom を評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)：このスモークテストを、ワークロード固有の品質、レイテンシ、コストの評価に発展させます。
- [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)：このスタックに対して Claude Code、Codex、Cursor を使います。
- [最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)：SDK からのアイデンティティ、abstain の処理、セッション、コンテキストパック、実行を扱います。
- [デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)：チーム向けに Loom を運用します。
