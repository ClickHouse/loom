<!-- i18n: source=README.md blob=fa96e2a9b6d3a241f92954a1b800e537f520209a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/README.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>AI エージェントのための共有メモリ、ドキュメント検索、コードインテリジェンス。</b><br>
  Claude Code、Codex、Cursor、そして独自のエージェント向け。
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="Python SDK on PyPI" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="TypeScript SDK on npm" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="https://github.com/ClickHouse/loom/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP server score" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
</p>

<p align="center">
  <!-- languages -->
  <a href="../../README.md">English</a> · <a href="../zh-CN/README.md">简体中文</a> · 日本語 · <a href="../ko/README.md">한국어</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · <a href="../ru/README.md">Русский</a>
  <!-- /languages -->
</p>

<p align="center">
  <a href="#what-loom-is-for">Loom の用途</a> ·
  <a href="#measured-results">ベンチマーク</a> ·
  <a href="#get-started">はじめに</a> ·
  <a href="#why-loom">機能</a> ·
  <a href="#integrations">連携</a> ·
  <a href="https://clickhouse.github.io/loom/">ドキュメント</a>
</p>

![共有されたエージェントのメモリ、ピン留めされたファクト、フラグが付いた競合を表示する Loom ダッシュボード](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom は、エージェントに長期的な共有メモリを提供する、オープンソースでセルフホスト型の MCP サーバーです。あるエージェントがセッションで学んだことを、次のエージェントやツールが見つけられます。

エージェントは MCP、HTTP、Python、TypeScript から接続します。Docker、または [Helm チャートを使った Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) でセルフホストできます。エージェントのコンテキストとインデックスデータは [ClickHouse](docs/use-cases/self-hosted-clickhouse.md) に保存されます。また、デプロイ時に、ライブのメモリとナレッジの状態に [初回起動時に Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) を選択することもできます。ダッシュボードでは、メモリ、リポジトリ、タスク、実行を一か所にまとめて確認できます。

<a id="what-loom-is-for"></a>

## Loom の用途

- **コーディングエージェント間で共有される長期メモリ**。セッション開始時にコンテキストを読み込み、実行中のセッションを記録するプラグインを備えています。
  [エージェントメモリ](docs/use-cases/agent-memory.md) ·
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **エージェントアプリケーション向けのメモリ**。Python または TypeScript SDK からユーザーごとに利用できます。[最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **名前空間、ロール、トークンを備えたマルチユーザー対応の MCP メモリサーバー**。
  [マルチユーザーモード](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **リポジトリのコードインデックス**。コーディングエージェントが定義、呼び出し元、変更の影響範囲を問い合わせます。[コードインデックス](docs/use-cases/code-intelligence.md)
- **コーディングエージェントのモデルコスト削減**。以下の[測定結果](#measured-results)を参照してください。
  [コーディングエージェントのコスト](docs/use-cases/coding-agent-cost.md)
- **引用付きのドキュメント検索（RAG）**。extractor サービスが変換する PDF、`.docx`、`.pptx` ファイルにも対応します。[ドキュメント検索](docs/use-cases/document-search.md)
- **マルチエージェントワークフロー向けの共有コンテキスト**：共有メモリ、タスクインボックス、決定事項。[マルチエージェント](docs/use-cases/multi-agent.md)
- **自律型エージェント向けのメモリ、リコール、監査、消去。**
  [自律型エージェント](docs/use-cases/autonomous-agents.md)
- **エージェントの作業記録**。検索可能な会話（記録を有効にした場合）、再開用のセッションブリーフ、目標・結果・モデルコスト（提供された場合）を含む実行履歴、PR とトラッカーの履歴を保持します。
  [実行履歴と PR 履歴](docs/use-cases/runs-and-pr-history.md)

すべてのユースケースと、他のツールの方が適している場合については、[ユースケース](docs/use-cases/index.md)を参照してください。

Loom は、1 つのエージェントの短期的なメモリのためのインプロセスライブラリではなく、エージェントフレームワークでもありません。[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## 測定結果

| 評価                           | Loom あり | Loom なし | 効率の向上                         | 対象                               |
| ------------------------------ | --------- | --------- | ---------------------------------- | ---------------------------------- |
| コーディングエージェントの回答品質     | **0.84**  | 0.78      | モデルコストが **20% 低下**            | 24 問 × 3 回のペア実行       |
| コーディングエージェントのファイル特定 | **96.4%** | 91.4%     | モデルコストが **21% 低下**           | 36 問 × 10 回のペア実行      |
| メモリと全履歴での回答の比較 | **94%**   | 90%       | リーダーのプロンプトトークンが **91% 減少** | LongMemEval-S の 100 問（ペア比較） |

どちらのコーディング設定も、ClickHouse のコードベースで Claude Code（`claude-sonnet-5`）を使用しています。コストはセッション全体のモデル利用料金で、Loom のサーバー側モデル呼び出し、ホスティング、インデックス作成は含みません。回答品質は、検証済みの回答の要点をどれだけカバーしているかを測定したもので、矛盾があれば減点します。ファイル特定は、エージェントが回答の最後に挙げるよう求められた最大 3 つのファイルの中に、正しいファイルが含まれているかを数えたものです。

メモリの行では、同じ `gpt-5` リーダーを使い、取得したメモリと会話の全履歴を比較しています。トークンの削減量にはメモリの構築は含まれていません。94% と 90% の精度の差は記述的なものであり、統計的に確立されたものではありません。

**これとは別に、Loom の `memory.ask` 構成は、LongMemEval-S の全 500 問で回答精度 96.6% を記録しました。** これは独自に設定された回答モデルを使ったプロダクト評価であり、上記の固定リーダーによる比較とは異なります。

結果は、テストしたワークロードに固有のものです。

[プロトコルと再現可能なアーティファクト](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[詳細な結果](#benchmark-details) ·
[ご自身のワークロードで評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## はじめに

<a id="1-choose-your-docker-setup"></a>

### 1. Docker のセットアップを選ぶ

- **個人利用（デフォルト）：** ログイン不要で、アクセスは localhost のみ、名前空間は `dev/local` です。1 人で複数のエージェントを接続できます。
- **チーム利用：** 認証付きアカウントと名前空間のアクセス制御を備えます。[初回起動からマルチユーザーモードで開始します](docs/getting-started/quickstart.md#start-in-multi-user-mode)。

どちらのモードも、設定済みの LLM や埋め込みを含め、Loom の機能は同じです。シングルユーザーモードは機能を削減したエディションではありません。[既存のインストールをデータを削除せずに切り替える](docs/getting-started/quickstart.md#2-open-the-dashboard)こともできます。その場合は、新しいアカウントに既存の名前空間へのアクセス権を付与します。

何もインストールせずに試すには、codespace で開きます。

[![GitHub Codespaces で開く](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
（[実行内容とアクセスできる人](docs/getting-started/quickstart.md#try-it-in-github-codespaces)）

デフォルトの個人用インストールは、Docker をインストールした状態で行います（Windows では、`docker` が動作する WSL 2 ターミナルで行います。WSL 統合を有効にした Docker Desktop、またはディストリビューション内にインストールした Docker Engine を使います）。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

ホームディレクトリ、またはご自身だけが書き込める[別のディレクトリ](docs/getting-started/quickstart.md#fast-path-released-image-no-clone)から実行してください。インストーラーは Loom と ClickHouse を起動し、マイグレーションを適用して、ダッシュボードの URL **http://localhost:7777/** を表示します。新規インストールの場合は、続けてメモリの保存と読み取りが正常に行えるかを確認します。ターミナルでは、検出したコーディングエージェント（Claude Code、Codex、VS Code）の接続も提案し、新規インストールの場合はダッシュボードを開きます。イメージには、サーバー、ワーカー、ダッシュボード、および Python、TypeScript、Go のコンパイラインデクサーが含まれます。TypeScript の依存関係は自動的に準備されます。[クイックスタートとバージョン固定リリース](docs/getting-started/quickstart.md)

インストール先は `~/.loom` です（`LOOM_INSTALL_DIR` で別のディレクトリを指定できます）。Compose ファイルと、起動時の設定を記述した `~/.loom/.env` が置かれます。インストールコマンドを再度実行すると、その場でアップグレードされ、`.env` の設定は保持されます。

シングルユーザーモードは localhost のみで使ってください。共有アクセスが必要な場合は、TLS を含め、[チーム向けデプロイガイド](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)に従ってください。

<a id="2-configure-models-and-api-keys"></a>

### 2. モデルと API キーを設定する

**標準のインストールは、キーもネットワークもなしで起動します。** ストレージ、ダッシュボード、構造的なコードインデックス、セマンティック検索は動作します。セマンティック検索には、イメージに含まれる小さなローカル埋め込みモデルを使います。fake LLM は AI による回答を生成しません。

| コンポーネント | 実運用での設定 | 未設定の場合 |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| 埋め込み | プロバイダー、埋め込みモデル、次元数、API キー。 | `local`（CPU 上の bge-small）：セマンティック検索は使えますが、ホスト型モデルより精度は劣ります。 |
| LLM | プロバイダー、モデル、API キー。 | `fake-echo-1`：意味のある抽出、要約、回答は行われません。 |

**Settings → Models & keys** を開き、両方のプロバイダーとモデルを選んでキーを入力し、**Test & apply** をクリックします。Docker の再起動は不要です。埋め込み空間を変更すると、既存のインデックスが再構築されます。ダッシュボードで選べる埋め込みはホスト型プロバイダーです。`local` を維持したまま LLM を追加するには、代わりに `~/.loom/.env` に LLM の行を設定してください（[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)）。

**1 つの OpenAI API キーで両方の役割をまかなえます。** Claude には別の埋め込みプロバイダーが必要です。OpenAI、Gemini、OpenAI 互換の埋め込みエンドポイントに対応しています。エージェントの CLI ログインやサブスクリプションは、Loom のプロバイダーキーの代わりにはなりません。

環境変数での設定を希望する場合は、`~/.loom/.env` を編集してからサーバーを再作成します。

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

キーだけでは固定されたモデル（ローカル埋め込みと fake LLM）は変わりません。プロバイダーとモデルも設定してください。ダッシュボードで保存した設定は `.env` より優先されます。プロバイダー別の手順とヘルスチェックは、[モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)を参照してください。

**そのほかの設定はすべて 1 つのファイルにあります。** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) には、すべての設定がデフォルト値と説明付きで、ClickHouse 接続、タイムアウトと予算、レート制限、ワーカー、機能を切り替える `*_enabled` スイッチなどの分野別に並んでいます。どの設定も `~/.loom/.env` に `LOOM_<NAME>` として設定してサーバーを再作成すれば反映されます。設定ファイルをマウントするデプロイでは、このファイルのコピーに設定できます（[設定の読み込み方法](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)）。`live` と記された設定は、Loom の実行中にも変更できます。`loom settings set <name> <value>` か、Admin → Deployment settings を使います。`loom settings list --changed` を実行すると、デフォルトと異なる設定と、それぞれの値の取得元が分かります（[ランタイム設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)）。

<a id="3-connect-your-agents"></a>

### 3. エージェントを接続する

インストーラーがエージェントを接続した場合は、以下のコマンドは不要です。個人用インストールを実行しているマシンでは、プラグインにトークンは不要です。

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

新しいセッションを開始すると、最初のセッションが `http://localhost:7777` に接続します。Codex では、`codex` を一度対話的に実行し、プラグインのフックを承認してください。承認するまでは、ツールだけが動作します。残しておきたい内容をエージェントに伝え、次のセッションでそれについて質問してみてください。

個人用インストールを実行しているマシンの Cursor では、下のボタンで、トークン不要の[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools)を追加できます。

[![Cursor に Loom を追加](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

個人用インストールを実行しているマシンの VS Code エージェントモードでは、下のボタンで、トークン不要の[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)を追加できます。

[![VS Code に Loom を追加](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![VS Code Insiders に Loom を追加](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

チームサーバーの場合は、Claude Code に `--config server_url=… --config token=…` を渡すか、[コーディングエージェントの接続](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)の説明に従って Codex の接続先を指定します。トークンは **Settings → Tokens** で作成します。Cursor やその他の MCP クライアント、および [SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) についても、そちらで説明しています。

<a id="why-loom"></a>

## Loom を選ぶ理由

- **引き継がれるメモリ。** エージェントやセッションをまたいで、決定事項や調査結果を共有できます。各ファクトには書き込んだ主体が記録され、バージョン履歴と競合レビューによって修正を追跡でき、名前空間は任意の時点までロールバックできます。
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **根拠を伴うコードインテリジェンス。** 定義、呼び出し元、変更の影響範囲、関連するテストを検索でき、コードをコミット、プルリクエスト、issue に結びつけられます。結果には、リビジョン、ソース上の位置、網羅性が示されます。 [Code index](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **根拠に基づく検索。** アクセスが許可されているメモリ、ドキュメント、会話、トラッカーのレコードを検索します。回答には出典が示され、信頼度の低い検索では回答を控えることもできます。
  [Retrieval](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **マルチエージェントのハンドオフ。** リース、リトライ、レビュールール、結果を備えた永続的なタスクインボックスを通じて、作業を割り当てます。ワークフローを実行するのはお使いのエージェントで、Loom はハンドオフを保持します。 [Task queues](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **エージェントのデータの管理。** 名前空間のアクセス、来歴、保持期間、監査レコード、検証済みの消去を管理できます。コンテキストは ClickHouse 上でクエリ可能なまま残ります。
  [Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## インテグレーション

| インテグレーション                                                                                                                                                                                                                                                                                   | 接続先                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)、[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)、[その他の MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)（[すべてのインテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)） | 既存のエージェントのワークフローにメモリとコードインテリジェンスを組み込みます。 |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)、[TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)、[HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | 独自のエージェントやアプリケーション。                                             |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | リクエストトレース、モデルコスト、検索スコア、プロンプトバージョンの帰属。 |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | ClickHouse 上の OpenTelemetry のトレース、ログ、メトリクス。                         |

Langfuse と ClickStack は任意のインテグレーションであり、Docker スタックにはバンドルされていません。

<a id="benchmark-details"></a>

## ベンチマークの詳細

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### 検索、リーダー、コード検索の詳細な結果

| 評価                                                  | 結果                                                                                               |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S の根拠検索（500 問）                    | 最初の 30 件のメモリに、裏付けとなるセッションが少なくとも 1 つ含まれる質問の割合は **98.6%**。    |
| LongMemEval-S の固定 `gpt-5` リーダー（500 問）       | 上位 200 件のメモリからの回答精度は **92.0%**。                                                    |
| LoCoMo（全 10 会話）                                  | 回答精度は **89.6%**。敵対的な質問に対する正しい回答拒否は **90.8%**。                             |
| MemoryAgentBench の事実質問サンプル（500 問）         | SubEM は **99.0%**。生成された回答のトークン重複 F1 は **95.7%**。                                 |
| 概念的コード検索（36 問）                             | 正解ファイルの再現率は 5 件で **91.2%**、10 件で **97.2%**。MRR は **0.91**。                      |
| SWE-bench Verified のファイル特定（200 件の issue）   | 最初の 10 件に変更されたファイルが含まれる issue：**82%**（BM25 は **57%**）。最初の 5 件では **76.5%**。 |
| トラッカーの重複検出（104 ペア）                      | 記録されている古い方の重複を最上位に順位付けできた割合は **87.5%**。                               |

これらは異なるタスクを測定したものであり、単一の総合スコアではありません。SWE-bench が測定するのはファイル特定であり、issue の解決ではありません。[results ledger](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) には、スコアの定義、小規模サンプルの評価、より弱い結果や否定的な結果、完全なプロトコル、モデル、成果物が含まれています。

[Methodology](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[coverage and known gaps](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[run the benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## ドキュメントとコントリビューション

共有エージェントコンテキスト、コードインテリジェンス、権限付き検索については、まず[ドキュメント](https://clickhouse.github.io/loom/)をご覧ください。デプロイの要件については、[前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites)を参照してください。

[Deployment](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [FAQ](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[Contributing](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [Security reporting](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [License](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [Notice](https://github.com/ClickHouse/loom/blob/main/NOTICE)
