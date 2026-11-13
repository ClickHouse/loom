<!-- i18n: source=README.md blob=7d127b3e0da860afac15179ad31d456d9592dc96 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/README.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>AI エージェントのための、共有メモリ、ドキュメント検索、コードインテリジェンス。</b><br>
  Claude Code、Codex、Cursor、そして独自のエージェントに対応します。
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="PyPI の Python SDK" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="npm の TypeScript SDK" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="https://github.com/ClickHouse/loom/blob/main/LICENSE"><img alt="ライセンス: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP サーバースコア" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
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
  <a href="https://github.com/ClickHouse/loom/blob/main/docs/index.md">ドキュメント</a>
</p>

![共有されたエージェントのメモリ、固定されたファクト、フラグ付きの競合を表示する Loom ダッシュボード](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom は、エージェントに長期的な共有メモリを提供する、オープンソースでセルフホスト型の MCP サーバーです。あるエージェントがセッションで学んだことを、次のエージェントやツールが見つけられます。

エージェントは MCP、HTTP、Python、TypeScript から接続します。Docker または [Helm チャートを使った Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) でセルフホストできます。エージェントのコンテキストとインデックスデータは [ClickHouse](docs/use-cases/self-hosted-clickhouse.md) に保存されます。ライブのメモリとナレッジの状態については、デプロイ時に [初回起動時に Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) を選択することもできます。ダッシュボードでは、メモリ、リポジトリ、タスク、実行を一か所にまとめて確認できます。

<a id="what-loom-is-for"></a>

## Loom の用途

- **複数のコーディングエージェントで共有する長期メモリ**。セッション開始時にコンテキストを読み込み、実行中のセッションを記録するプラグインを備えています。
  [エージェントメモリ](docs/use-cases/agent-memory.md) ·
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **エージェントアプリケーション向けのメモリ**。Python または TypeScript の SDK から、ユーザーごとに利用できます。[最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **名前空間、ロール、トークンを備えたマルチユーザー対応の MCP メモリサーバー**。
  [マルチユーザーモード](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **リポジトリのコードインデックス**。コーディングエージェントが定義、呼び出し元、変更の影響範囲を問い合わせます。[コードインデックス](docs/use-cases/code-intelligence.md)
- **コーディングエージェントのモデルコストの削減**。以下の[測定結果](#measured-results)を参照してください。
  [コーディングエージェントのコスト](docs/use-cases/coding-agent-cost.md)
- **引用付きのドキュメント検索（RAG）**。extractor サービスが変換する PDF、`.docx`、`.pptx` ファイルにも対応します。[ドキュメント検索](docs/use-cases/document-search.md)
- **マルチエージェントワークフロー向けの共有コンテキスト**：共有メモリ、タスクインボックス、決定事項。[マルチエージェント](docs/use-cases/multi-agent.md)
- **自律エージェント向けのメモリ、リコール、監査、消去。**
  [自律エージェント](docs/use-cases/autonomous-agents.md)
- **エージェント作業の記録**。検索可能な会話（記録を有効にしている場合）、再開に使えるセッションブリーフ、目標・結果・モデルコスト（提供された場合）を含む実行履歴、PR とトラッカーの履歴を残します。
  [実行履歴と PR 履歴](docs/use-cases/runs-and-pr-history.md)

すべてのユースケースと、他のツールの方が適している場合については、[ユースケース](docs/use-cases/index.md)を参照してください。

Loom は、1 つのエージェントの短期的なメモリのためのインプロセスライブラリでも、エージェントフレームワークでもありません。[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## 測定結果

| 評価                           | Loom あり | Loom なし | 効率の向上                         | 対象                               |
| ------------------------------ | --------- | --------- | ---------------------------------- | ---------------------------------- |
| コーディングエージェントの回答品質 | **0.84**  | 0.78      | モデルコストが **20% 低下**        | 24 問 × 3 回のペア実行             |
| コーディングエージェントのファイル特定 | **96.4%** | 91.4%     | モデルコストが **21% 低下**        | 36 問 × 10 回のペア実行            |
| メモリと全履歴での回答の比較   | **94%**   | 90%       | リーダーのプロンプトトークンが **91% 減少** | LongMemEval-S のペア 100 問        |

どちらのコーディング設定も、ClickHouse のコードベースに対して Claude Code（`claude-sonnet-5`）を使用しています。コストはセッション全体のモデル料金であり、Loom のサーバー側のモデル呼び出し、ホスティング、インデックス作成は含みません。回答品質は、検証済みの回答の要点をどの程度カバーしているかを、矛盾に対するペナルティ付きで測定したものです。ファイル特定は、指定された 3 つのファイルの中に正しいファイルが含まれているかを数えます。

メモリの行は、取得したメモリと会話の全履歴を、同じ `gpt-5` リーダーで比較しています。トークンの削減量にはメモリの構築は含まれません。94% と 90% の精度差は記述的なものであり、統計的に確立されたものではありません。

**これとは別に、Loom の `memory.ask` 構成は、LongMemEval-S の全 500 問で 96.6% の回答精度を記録しました。** これは、独自に設定された回答モデルを使う製品評価であり、上記の固定リーダーによる比較とは異なります。

結果は、テストしたワークロードに固有のものです。

[プロトコルと再現可能な成果物](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[詳細な結果](#benchmark-details) ·
[自分のワークロードで評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## はじめに

<a id="1-choose-your-docker-setup"></a>

### 1. Docker のセットアップを選ぶ

- **個人利用（デフォルト）：** ログイン不要、localhost のみのアクセス、`dev/local` 名前空間を使用します。1 人で複数のエージェントを接続できます。
- **チーム利用：** 認証付きアカウントと名前空間のアクセス制御を使用します。[初回起動からマルチユーザーモードで開始します](docs/getting-started/quickstart.md#start-in-multi-user-mode)。

どちらのモードでも、設定済みの LLM や埋め込みを含め、Loom の機能は同じように使えます。シングルユーザーモードは機能を絞ったエディションではありません。[データを削除せずに既存のインストールを切り替える](docs/getting-started/quickstart.md#2-open-the-dashboard)こともできます。その場合は、新しいアカウントに既存の名前空間へのアクセス権を付与します。

何もインストールせずに試すには、codespace で開きます。

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
（[実行される内容とアクセスできるユーザー](docs/getting-started/quickstart.md#try-it-in-github-codespaces)）

Docker がインストールされている環境で、デフォルトの個人用インストールを行うには次を実行します。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

ホームディレクトリ、またはご自身だけが書き込める[別のディレクトリ](docs/getting-started/quickstart.md#fast-path-released-image-no-clone)から実行してください。インストーラーは Loom と ClickHouse を起動し、マイグレーションを適用して、ダッシュボードの URL **http://localhost:7777/** を表示します。新規インストールでは、続いてメモリの保存と読み取りが正しく動作することを確認します。ターミナルでは、検出したコーディングエージェント（Claude Code、Codex、VS Code）を接続するかどうかも尋ね、新規インストールではダッシュボードを開きます。イメージには、サーバー、ワーカー、ダッシュボード、および Python、TypeScript、Go のコンパイラインデクサーが含まれています。TypeScript の依存関係は自動的に準備されます。[クイックスタートとバージョン固定のリリース](docs/getting-started/quickstart.md)

インストール先は `~/.loom` です（`LOOM_INSTALL_DIR` で別のディレクトリを指定できます）。Compose ファイルと、`~/.loom/.env` の起動設定が置かれます。インストールコマンドをもう一度実行すると、その場でアップグレードされ、`.env` の設定は保持されます。

シングルユーザーモードは localhost のみで使用してください。共有アクセスが必要な場合は、TLS を含め、[チーム向けデプロイガイド](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)に従ってください。

<a id="2-configure-models-and-api-keys"></a>

### 2. モデルと API キーを設定する

**標準のインストールは、キーやネットワークがなくても起動します。** ストレージ、ダッシュボード、構造的なコードインデックス作成、セマンティック検索は、イメージに含まれる小さなローカル埋め込みモデルで動作します。fake LLM は AI による回答を生成しません。

| コンポーネント | 実運用での設定 | 未設定の場合 |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| 埋め込み | プロバイダー、埋め込みモデル、次元数、API キー。 | `local`（CPU 上の bge-small）：セマンティック検索は使えますが、ホスト型モデルより精度は劣ります。 |
| LLM | プロバイダー、モデル、API キー。 | `fake-echo-1`：意味のある抽出、要約、回答は得られません。 |

**Settings → Models & keys** を開き、両方のプロバイダーとモデルを選択し、キーを入力して、**Test & apply** をクリックします。Docker の再起動は不要です。埋め込み空間を変更すると、既存のインデックスが再構築されます。ダッシュボードで選べる埋め込みはホスト型プロバイダーです。`local` を維持したまま LLM を追加するには、代わりに `~/.loom/.env` に LLM の行を設定してください（[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)）。

**1 つの OpenAI API キーで両方の役割をまかなえます。** Claude には別の埋め込みプロバイダーが必要です。OpenAI、Gemini、OpenAI 互換の埋め込みエンドポイントに対応しています。エージェントの CLI ログインやサブスクリプションは、Loom のプロバイダーキーの代わりにはなりません。

環境変数で設定したい場合は、`~/.loom/.env` を編集してから、サーバーを再作成します。

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

キーだけでは固定されたモデル（ローカル埋め込みと fake LLM）は変わりません。プロバイダーとモデルも設定してください。ダッシュボードで保存した設定は `.env` より優先されます。プロバイダーごとの設定例とヘルスチェックについては、[モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)を参照してください。

**そのほかの設定はすべて 1 つのファイルにあります。** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) には、すべての設定がデフォルト値と説明つきで、ClickHouse 接続、タイムアウトとバジェット、レート制限、ワーカー、機能のオン／オフを切り替える `*_enabled` スイッチなどの分野別に並んでいます。いずれも `~/.loom/.env` に `LOOM_<NAME>` として設定し、サーバーを再作成すれば反映されます。設定ファイルをマウントするデプロイでは、このファイルのコピーに設定することもできます（[設定の読み込み方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)）。`live` と記された設定は、Loom の実行中にも変更できます。`loom settings set <name> <value>` を使うか、Admin → Deployment settings から変更します。`loom settings list --changed` を実行すると、デフォルトと異なる設定と、各値の取得元が表示されます（[ランタイム設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)）。

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

新しいセッションを開始してください。最初のセッションは `http://localhost:7777` に接続します。Codex では、一度 `codex` を対話モードで実行し、プラグインのフックを承認してください。承認するまではツールのみが動作します。残しておく価値のある内容をエージェントに伝え、次のセッションでそれについて質問してみてください。

個人用インストールを実行しているマシンの Cursor では、下のボタンで[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools)を追加できます。トークンは不要です。

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

個人用インストールを実行しているマシンの VS Code エージェントモードでは、下のボタンで[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)を追加できます。トークンは不要です。

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

チームサーバーの場合は、Claude Code に `--config server_url=… --config token=…` を渡すか、[コーディングエージェントの接続](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)の説明に従って Codex をサーバーに向けてください。トークンは **Settings → Tokens** で作成します。Cursor などの MCP クライアントや[SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)についても、そちらで説明しています。

<a id="why-loom"></a>

## Loom を使う理由

- **引き継がれるメモリ。** エージェントやセッションをまたいで、決定事項や調査結果を共有できます。各ファクトには書き込んだ主体が記録され、バージョン履歴と競合レビューによって修正を追跡でき、名前空間は任意の時点にロールバックできます。
  [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **根拠を伴うコードインテリジェンス。** 定義、呼び出し元、変更の影響範囲、関連テストを見つけられ、コードをコミット、プルリクエスト、issue に結び付けられます。結果にはリビジョン、ソースの位置、網羅性が示されます。 [コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **根拠に基づく検索。** アクセスが許可されたメモリ、ドキュメント、会話、トラッカーのレコードを検索します。回答は出典を引用し、信頼度の低い検索では回答を控えることもできます。
  [検索](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **マルチエージェントのハンドオフ。** リース、リトライ、レビュールール、結果を備えた永続的なタスク受信箱を通じて作業を割り当てます。ワークフローを実行するのはお使いのエージェントで、Loom はハンドオフを保持します。 [タスクキュー](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **エージェントデータの管理。** 名前空間のアクセス、来歴、保持期間、監査記録、検証済みの消去を管理します。コンテキストは ClickHouse 上でクエリ可能なまま残ります。
  [セキュリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## インテグレーション

| インテグレーション | 接続対象 |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)、[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)、[その他の MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)（[すべてのインテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)） | 既存のエージェントのワークフローで、メモリとコードインテリジェンスを利用できます。 |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)、[TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)、[HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md) | 独自のエージェントやアプリケーション。 |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md) | リクエストのトレース、モデルのコスト、検索スコア、プロンプトバージョンごとの帰属。 |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md) | ClickHouse 上の OpenTelemetry のトレース、ログ、メトリクス。 |

Langfuse と ClickStack は任意のインテグレーションであり、Docker スタックには同梱されていません。

<a id="benchmark-details"></a>

## ベンチマークの詳細

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### 検索、リーダー、コード検索の詳細な結果

| 評価                                                  | 結果                                                                                               |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S の根拠検索（500 問）                    | 上位 30 件のメモリに、裏付けとなるセッションが少なくとも 1 つ含まれた質問の割合は **98.6%**。      |
| LongMemEval-S の固定 `gpt-5` リーダー（500 問）       | 上位 200 件のメモリからの回答正解率は **92.0%**。                                                  |
| LoCoMo（全 10 件の会話）                              | 回答正解率は **89.6%**。敵対的な質問に対する正しい回答保留は **90.8%**。                           |
| MemoryAgentBench の事実質問サンプル（500 問）         | SubEM は **99.0%**。生成された回答のトークン重複 F1 は **95.7%**。                                 |
| 概念的なコード検索（36 問）                           | 正解ファイルの再現率は 5 件で **91.2%**、10 件で **97.2%**。MRR は **0.91**。                      |
| SWE-bench Verified のファイル特定（200 件の issue）   | 上位 10 件に変更ファイルが含まれた issue：**82%**（BM25 は **57%**）。上位 5 件では **76.5%**。    |
| トラッカーの重複検出（104 ペア）                      | 記録されている古い方の重複を 1 位にランク付けできた割合は **87.5%**。                              |

これらは異なるタスクを測定したものであり、1 つの総合スコアではありません。SWE-bench が測定するのはファイルの特定であり、issue の解決ではありません。[結果台帳](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)には、スコアの定義、小規模サンプルの評価、より弱い結果や否定的な結果、完全なプロトコル、モデル、成果物が含まれています。

[方法論](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[カバレッジと既知の不足](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[ベンチマークの実行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## ドキュメントとコントリビュート

共有エージェントコンテキスト、コードインテリジェンス、権限付き検索を使い始めるには、[ドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/index.md)を参照してください。デプロイの要件については、[前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites)を参照してください。

[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [FAQ](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[コントリビュート](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [セキュリティ報告](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [ライセンス](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [Notice](https://github.com/ClickHouse/loom/blob/main/NOTICE)
