<!-- i18n: source=README.md blob=fa96e2a9b6d3a241f92954a1b800e537f520209a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/README.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>AI エージェントのための、共有メモリ、ドキュメント検索、コードインテリジェンス。</b><br>
  Claude Code、Codex、Cursor、そして独自のエージェントに対応します。
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

![Loom dashboard showing shared agent memories, a pinned fact and flagged conflicts](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom は、エージェントに長期的な共有メモリを提供する、オープンソースのセルフホスト型 MCP サーバーです。あるエージェントがセッション中に学んだことを、次のエージェントやツールが見つけられます。

エージェントは MCP、HTTP、Python、TypeScript を通じて接続します。Docker、または [Helm チャートを使った Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) でセルフホストできます。エージェントのコンテキストとインデックスデータは [ClickHouse](docs/use-cases/self-hosted-clickhouse.md) に保存されます。また、デプロイ時には、ライブのメモリとナレッジの状態に [初回起動時に Postgres を選択](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) することもできます。ダッシュボードでは、メモリ、リポジトリ、タスク、実行を 1 か所にまとめて確認できます。

<a id="what-loom-is-for"></a>

## Loom の用途

- **コーディングエージェント間で共有される長期メモリ**。セッション開始時にコンテキストを読み込み、実行中のセッションを記録するプラグインを備えています。
  [エージェントメモリ](docs/use-cases/agent-memory.md) ·
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **エージェントアプリケーション向けのメモリ**。ユーザーごとに、Python または TypeScript SDK から利用できます。[最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **名前空間、ロール、トークンを備えたマルチユーザー対応の MCP メモリサーバー**。
  [マルチユーザーモード](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **リポジトリのコードインデックス**。コーディングエージェントが定義、呼び出し元、変更の影響範囲を問い合わせます。[コードインデックス](docs/use-cases/code-intelligence.md)
- **コーディングエージェントのモデルコストの削減**。下記の[測定結果](#measured-results)を参照してください。
  [コーディングエージェントのコスト](docs/use-cases/coding-agent-cost.md)
- **引用付きのドキュメント検索（RAG）**。extractor サービスが変換する PDF、`.docx`、`.pptx` ファイルにも対応します。[ドキュメント検索](docs/use-cases/document-search.md)
- **マルチエージェントワークフロー向けの共有コンテキスト**：共有メモリ、タスクインボックス、意思決定。[マルチエージェント](docs/use-cases/multi-agent.md)
- **自律エージェント向けのメモリ、リコール、監査、消去。**
  [自律エージェント](docs/use-cases/autonomous-agents.md)
- **エージェントの作業記録**。検索可能な会話（記録を有効にした場合）、再開用のセッションブリーフ、目標・結果・モデルコスト（提供された場合）を含む実行履歴、PR とトラッカーの履歴を保持します。
  [実行履歴と PR 履歴](docs/use-cases/runs-and-pr-history.md)

すべてのユースケースと、他のツールの方が適している場合については、[ユースケース](docs/use-cases/index.md)を参照してください。

Loom は、1 つのエージェントの短期的なメモリのためのインプロセスライブラリではなく、エージェントフレームワークでもありません。[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## 測定結果

| 評価                           | Loom あり | Loom なし | 効率向上                           | 範囲                               |
| ------------------------------ | --------- | --------- | ---------------------------------- | ---------------------------------- |
| コーディングエージェントの回答品質 | **0.84**  | 0.78      | モデルコストが **20% 低下**        | 24 問 × 3 回のペア実行             |
| コーディングエージェントのファイル特定 | **96.4%** | 91.4%     | モデルコストが **21% 低下**        | 36 問 × 10 回のペア実行            |
| メモリと全履歴による回答の比較 | **94%**   | 90%       | リーダーのプロンプトトークンが **91% 減少** | LongMemEval-S のペア 100 問 |

どちらのコーディング設定も、ClickHouse のコードベースで Claude Code（`claude-sonnet-5`）を使用しています。コストはセッション全体のモデル利用料であり、Loom のサーバー側モデル呼び出し、ホスティング、インデックス作成は含みません。回答品質は、検証済みの回答の要点をどれだけカバーしているかを測定し、矛盾にはペナルティを課します。ファイル特定は、エージェントが回答の最後に挙げるよう求められる最大 3 つのファイルの中に、正しいファイルが含まれているかどうかを数えます。

メモリの行は、取得したメモリと会話の全履歴を、同じ `gpt-5` リーダーで比較したものです。トークンの削減量にはメモリの構築は含まれません。94% と 90% の精度差は記述的なものであり、統計的に確立されたものではありません。

**これとは別に、Loom の `memory.ask` 構成は、LongMemEval-S の全 500 問で 96.6% の回答精度を記録しました。** これは独自に設定した回答モデルを使うプロダクト評価であり、上記の固定リーダーによる比較ではありません。

結果は、テストしたワークロードに固有のものです。

[プロトコルと再現可能な成果物](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[詳細な結果](#benchmark-details) ·
[自分のワークロードで評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## はじめに

<a id="1-choose-your-docker-setup"></a>

### 1. Docker のセットアップを選ぶ

- **個人利用（デフォルト）：** ログイン不要、localhost のみのアクセス、`dev/local` 名前空間。1 人で複数のエージェントを接続できます。
- **チーム利用：** 認証付きアカウントと名前空間のアクセス制御。[初回起動からマルチユーザーモードで開始します](docs/getting-started/quickstart.md#start-in-multi-user-mode)。

どちらのモードでも、設定済みの LLM や埋め込みを含め、Loom の同じ機能を利用できます。シングルユーザーモードは機能を削った版ではありません。[データを削除せずに既存のインストールを切り替える](docs/getting-started/quickstart.md#2-open-the-dashboard)こともできます。その場合は、新しいアカウントに既存の名前空間へのアクセス権を付与してください。

何もインストールせずに試すには、codespace で開きます。

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
（[実行される内容とアクセスできるユーザー](docs/getting-started/quickstart.md#try-it-in-github-codespaces)）

デフォルトの個人用インストールでは、Docker をインストールしておいてください（Windows では、`docker` が動作する WSL 2 ターミナルで行います。WSL 統合を有効にした Docker Desktop、またはディストリビューション内にインストールした Docker Engine を使います）。

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

ホームディレクトリ、またはあなただけが書き込める[別のディレクトリ](docs/getting-started/quickstart.md#fast-path-released-image-no-clone)から実行してください。インストーラーは Loom と ClickHouse を起動し、マイグレーションを適用して、ダッシュボードの URL **http://localhost:7777/** を表示します。新規インストールの場合は、続けてメモリが保存され、読み戻せることを確認します。ターミナルでは、検出したコーディングエージェント（Claude Code、Codex、VS Code）の接続も提案し、新規インストールではダッシュボードを開きます。イメージにはサーバー、ワーカー、ダッシュボード、および Python、TypeScript、Go のコンパイラインデクサーが含まれています。TypeScript の依存関係は自動的に準備されます。[クイックスタートとバージョン固定のリリース](docs/getting-started/quickstart.md)

インストールは `~/.loom` に置かれます（`LOOM_INSTALL_DIR` で別のディレクトリを指定できます）。ここには Compose ファイルと、`~/.loom/.env` の起動設定が入ります。インストールコマンドを再実行すると、その場でアップグレードされ、`.env` の設定は保持されます。

シングルユーザーモードは localhost のみで使ってください。共有アクセスが必要な場合は、TLS を含め、[チーム向けデプロイガイド](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)に従ってください。

<a id="2-configure-models-and-api-keys"></a>

### 2. モデルと API キーを設定する

**標準のインストールは、キーもネットワークもなしで起動します。** ストレージ、ダッシュボード、構造的なコードインデックス、セマンティック検索は動作します。セマンティック検索には、イメージに含まれる小さなローカル埋め込みモデルが使われます。fake LLM は AI による回答を生成しません。

| コンポーネント | 実運用での設定 | 未設定の場合 |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| 埋め込み | プロバイダー、埋め込みモデル、次元数、API キー。 | `local`（CPU 上の bge-small）：セマンティック検索は可能ですが、ホスト型モデルより精度は劣ります。 |
| LLM | プロバイダー、モデル、API キー。 | `fake-echo-1`：意味のある抽出、要約、回答は得られません。 |

**Settings → Models & keys** を開き、両方のプロバイダーとモデルを選び、キーを入力して **Test & apply** をクリックします。Docker の再起動は不要です。埋め込み空間を変更すると、既存のインデックスが再構築されます。ダッシュボードで選べる埋め込みはホスト型プロバイダーです。`local` を維持したまま LLM を追加するには、代わりに `~/.loom/.env` の LLM 関連の行を設定してください（[モデルとキー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)）。

**1 つの OpenAI API キーで両方の役割をまかなえます。** Claude には別の埋め込みプロバイダーが必要です。OpenAI、Gemini、OpenAI 互換の埋め込みエンドポイントに対応しています。エージェントの CLI ログインやサブスクリプションは、Loom のプロバイダーキーの代わりにはなりません。

環境変数で設定したい場合は、`~/.loom/.env` を編集してからサーバーを再作成します。

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

キーだけでは、固定されたモデル（ローカル埋め込みと fake LLM）は変わりません。プロバイダーとモデルも設定してください。ダッシュボードで保存した設定は `.env` より優先されます。プロバイダー別の手順とヘルスチェックについては、[モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)を参照してください。

**それ以外の設定は 1 つのファイルにまとまっています。** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) には、すべての設定がデフォルト値と説明付きで、領域ごとに記載されています。ClickHouse 接続、タイムアウトと予算、レート制限、ワーカー、機能のオン・オフを切り替える `*_enabled` スイッチなどです。どの設定も `~/.loom/.env` に `LOOM_<NAME>` として指定し、サーバーを再作成すれば反映されます。設定ファイルをマウントするデプロイでは、このファイルのコピーに設定することもできます（[設定の読み込み方法](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)）。`live` と記された設定は、Loom の実行中にも変更できます。`loom settings set <name> <value>` か、Admin → Deployment settings を使います。`loom settings list --changed` を実行すると、デフォルトと異なる設定と、各値の取得元が表示されます（[ランタイム設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)）。

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

新しいセッションを開始すると、最初のセッションが `http://localhost:7777` に接続します。Codex では、`codex` を一度対話的に実行し、プラグインのフックを承認してください。承認するまでは、ツールだけが動作します。残しておきたいことをエージェントに伝え、次のセッションでそれについて尋ねてみてください。

個人用インストールを実行しているマシンの Cursor では、下のボタンで[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools)を追加できます。トークンは不要です。

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

個人用インストールを実行しているマシンの VS Code エージェントモードでは、下のボタンで[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)を追加できます。トークンは不要です。

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

チームサーバーの場合は、Claude Code に `--config server_url=… --config token=…` を渡すか、[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)の説明に従って Codex の接続先をそのサーバーに向けてください。トークンは **Settings → Tokens** で作成します。Cursor やその他の MCP クライアント、および [SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) についても、そちらで説明しています。

<a id="why-loom"></a>

## Loom を選ぶ理由

- **引き継がれるメモリ。** 決定事項や調査結果を、エージェントやセッションをまたいで共有できます。各ファクトには書き込んだ主体が記録され、バージョン履歴と競合レビューによって修正を追跡でき、名前空間は任意の時点にロールバックできます。
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **根拠を伴うコードインテリジェンス。** 定義、呼び出し元、変更の影響範囲、関連するテストを見つけられます。コードをコミット、プルリクエスト、イシューと結び付けることもできます。結果には、そのリビジョン、ソースの位置、網羅性が示されます。[Code index](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **根拠に基づく検索。** アクセスが許可されたメモリ、ドキュメント、会話、トラッカーのレコードを検索します。回答には出典が示され、信頼度の低い検索結果の場合は回答を控えることができます。
  [Retrieval](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **マルチエージェントのハンドオフ。** リース、リトライ、レビュールール、結果を備えた永続的なタスクインボックスを通じて作業を割り当てます。ワークフローを実行するのはお使いのエージェントで、Loom はハンドオフを保持します。[Task queues](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **エージェントデータの管理。** 名前空間へのアクセス、来歴、保持期間、監査レコード、検証付きの消去を管理できます。コンテキストは ClickHouse で引き続きクエリできます。
  [Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## インテグレーション

| インテグレーション                                                                                                                                                                                                                                                                                   | 接続先                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)、[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)、[その他の MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)（[すべてのインテグレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)） | 既存のエージェントのワークフローにメモリとコードインテリジェンスを組み込みます。                 |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)、[TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)、[HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | 独自のエージェントやアプリケーション。                                             |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | リクエストのトレース、モデルのコスト、検索スコア、プロンプトバージョンの帰属。 |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | ClickHouse 上の OpenTelemetry のトレース、ログ、メトリクス。                         |

Langfuse と ClickStack はオプションのインテグレーションであり、Docker スタックには同梱されていません。

<a id="benchmark-details"></a>

## ベンチマークの詳細

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### 検索、リーダー、コード検索の詳細な結果

| 評価                                                  | 結果                                                                                               |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S エビデンス検索（500問）                 | 最初の30件のメモリに、裏付けとなるセッションが少なくとも1つ含まれていた質問の割合は **98.6%**。    |
| LongMemEval-S 固定 `gpt-5` リーダー（500問）          | 上位200件のメモリからの回答精度は **92.0%**。                                                      |
| LoCoMo（全10件の会話）                                | 回答精度は **89.6%**。敵対的な質問に対する正しい回答拒否は **90.8%**。                             |
| MemoryAgentBench 事実質問サンプル（500問）            | SubEM は **99.0%**。生成された回答のトークン重複 F1 は **95.7%**。                                 |
| 概念的コード検索（36問）                              | 正解ファイルの再現率は 5件で **91.2%**、10件で **97.2%**。MRR は **0.91**。                        |
| SWE-bench Verified ファイル特定（200件の issue）      | 最初の10件に変更対象ファイルが含まれた issue の割合は、BM25 の **57%** に対して **82%**。最初の5件では **76.5%**。 |
| トラッカーの重複検出（104ペア）                       | 記録されている古い重複が1位にランクされた割合は **87.5%**。                                        |

これらは異なるタスクを測定したものであり、単一の総合スコアではありません。SWE-bench が測定するのはファイルの特定であり、issue の解決ではありません。[results ledger](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) には、スコアの定義、小規模サンプルの評価、より低い結果や否定的な結果、完全なプロトコル、モデル、成果物が記載されています。

[Methodology](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[coverage and known gaps](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[run the benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## ドキュメントとコントリビューション

共有エージェントコンテキスト、コードインテリジェンス、権限付き検索については、まず[ドキュメント](https://clickhouse.github.io/loom/)をご覧ください。デプロイの要件については、[前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites)を参照してください。

[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [FAQ](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[コントリビューション](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [セキュリティ報告](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [ライセンス](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [Notice](https://github.com/ClickHouse/loom/blob/main/NOTICE)
