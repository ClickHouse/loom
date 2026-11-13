<!-- i18n: source=README.md blob=e1db34acfd382a3c93ed63eca0125785ac13f76f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/README.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>AI エージェントのための、共有メモリ、ドキュメント検索、コードインテリジェンス。</b><br>
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
  <a href="https://github.com/ClickHouse/loom/blob/main/docs/index.md">ドキュメント</a>
</p>

![Loom dashboard showing shared agent memories, a pinned fact and flagged conflicts](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom は、エージェントに長期的な共有メモリを提供する、オープンソースのセルフホスト型 MCP サーバーです。あるエージェントがセッション中に学んだことを、次のエージェントやツールが見つけられます。

エージェントは MCP、HTTP、Python、TypeScript で接続します。Docker でセルフホストするか、[Helm chart を使って Kubernetes 上](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)で運用できます。エージェントのコンテキストとインデックスデータは [ClickHouse](docs/use-cases/self-hosted-clickhouse.md) に保存されます。デプロイ時には、ライブのメモリとナレッジの状態に [初回起動時に Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) を選択することもできます。ダッシュボードでは、メモリ、リポジトリ、タスク、実行を一か所にまとめて確認できます。

<a id="what-loom-is-for"></a>

## Loom の用途

- **複数のコーディングエージェントで共有する長期メモリ**。セッション開始時にコンテキストを読み込み、実行中のセッションを記録するプラグインを備えています。
  [Agent memory](docs/use-cases/agent-memory.md) ·
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **エージェントアプリケーション向けのメモリ**。Python または TypeScript SDK からユーザーごとに利用できます。[最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **ネームスペース、ロール、トークンを備えたマルチユーザー対応の MCP メモリサーバー**。
  [マルチユーザーモード](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [ネームスペースと共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **リポジトリのコードインデックス**。コーディングエージェントが定義、呼び出し元、変更の影響範囲を問い合わせられます。[コードインデックス](docs/use-cases/code-intelligence.md)
- **コーディングエージェントのモデルコスト削減**。下記の[計測結果](#measured-results)を参照してください。
  [コーディングエージェントのコスト](docs/use-cases/coding-agent-cost.md)
- **引用付きのドキュメント検索 (RAG)**。extractor サービスが変換する PDF、`.docx`、`.pptx` ファイルにも対応します。[ドキュメント検索](docs/use-cases/document-search.md)
- **マルチエージェントワークフロー向けの共有コンテキスト**: 共有メモリ、タスクインボックス、意思決定。[マルチエージェント](docs/use-cases/multi-agent.md)
- **自律型エージェント向けのメモリ、リコール、監査、消去**。
  [自律型エージェント](docs/use-cases/autonomous-agents.md)
- **エージェント作業の記録**。検索可能な会話 (記録を有効にした場合)、再開用のセッション概要、目標・結果・モデルコスト (提供された場合) を含む実行履歴、PR とトラッカーの履歴を保持します。
  [実行履歴と PR 履歴](docs/use-cases/runs-and-pr-history.md)

すべてのユースケースと、他のツールの方が適している場合については、[ユースケース](docs/use-cases/index.md)を参照してください。

Loom は、1 つのエージェントの短期的なメモリのためのインプロセスライブラリでも、エージェントフレームワークでもありません。[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## 測定結果

| 評価                           | Loom あり | Loom なし | 効率の向上                         | 対象範囲                           |
| ------------------------------ | --------- | --------- | ---------------------------------- | ---------------------------------- |
| コーディングエージェントの回答品質 | **0.84**  | 0.78      | モデルコストが **20% 低下**        | 24 問 × 3 回のペア実行             |
| コーディングエージェントのファイル特定 | **96.4%** | 91.4%     | モデルコストが **21% 低下**        | 36 問 × 10 回のペア実行            |
| メモリと全履歴での回答の比較   | **94%**   | 90%       | リーダーのプロンプトトークンが **91% 減少** | LongMemEval-S のペア 100 問        |

どちらのコーディング評価も、ClickHouse のコードベースで Claude Code (`claude-sonnet-5`) を使用しています。
コストはセッション全体のモデル利用料金であり、Loom のサーバー側のモデル呼び出し、ホスティング、インデックス作成は含みません。回答品質は、検証済みの回答の要点をどれだけカバーしているかを測定し、矛盾にはペナルティを課します。
ファイル特定は、名前が挙げられた 3 つのファイルの中に正しいファイルが含まれているかを数えます。

メモリの行では、取得したメモリと会話の全履歴を、同じ `gpt-5` リーダーで比較しています。トークンの削減量にはメモリの構築を含みません。94% と 90% の精度差は記述的なものであり、統計的に確立されたものではありません。

**これとは別に、Loom の `memory.ask` 構成は、LongMemEval-S の全 500 問で 96.6% の回答精度を記録しました。** これは、独自に設定した回答モデルを用いたプロダクト評価であり、上記の固定リーダーによる比較とは異なります。

結果は、テストしたワークロードに固有のものです。

[プロトコルと再現可能な成果物](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[詳細な結果](#benchmark-details) ·
[自分のワークロードで評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## はじめに

<a id="1-choose-your-docker-setup"></a>

### 1. Docker のセットアップを選ぶ

- **個人利用（デフォルト）:** ログインなし、localhost のみのアクセス、`dev/local`
  namespace。1 人で複数のエージェントを接続できます。
- **チーム利用:** 認証付きアカウントと namespace のアクセス制御。
  [初回起動からマルチユーザーモードで始める](docs/getting-started/quickstart.md#start-in-multi-user-mode)。

どちらのモードでも、設定済みの LLM と embedding を含め、Loom の機能は同じように使えます。
シングルユーザーモードは機能を絞ったエディションではありません。
[既存のインストールをデータを削除せずに切り替える](docs/getting-started/quickstart.md#2-open-the-dashboard)こともできます。
その場合は、新しいアカウントに既存の namespace へのアクセス権を付与します。

何もインストールせずに試すには、codespace で開きます。

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
（[何が実行され、誰がアクセスできるか](docs/getting-started/quickstart.md#try-it-in-github-codespaces)）

Docker がインストールされている環境でデフォルトの個人向けインストールを行うには:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

ホームディレクトリ、または
[自分だけが書き込めるその他のディレクトリ](docs/getting-started/quickstart.md#fast-path-released-image-no-clone)から実行してください。
インストーラは Loom と ClickHouse を起動し、マイグレーションを適用して、ダッシュボードの URL
**http://localhost:7777/** を表示します。イメージにはサーバー、ワーカー、ダッシュボード、
Python・TypeScript・Go のコンパイラインデクサが含まれ、TypeScript の依存関係は自動的に準備されます。
[クイックスタートとリリースの固定](docs/getting-started/quickstart.md)

インストール先は `~/.loom` です（`LOOM_INSTALL_DIR` で別のディレクトリを指定できます）。
Compose ファイルと、起動時の設定を記した `~/.loom/.env` が置かれます。
インストールコマンドをもう一度実行すると、`.env` の設定を保持したままその場でアップグレードされます。

シングルユーザーモードは localhost のみで使ってください。共有アクセスが必要な場合は、TLS を含め
[チームデプロイガイド](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)に従ってください。

<a id="2-configure-models-and-api-keys"></a>

### 2. モデルと API キーを設定する

**標準のインストールは、キーやネットワークがなくても起動します。** ストレージ、ダッシュボード、
構造的なコードインデックス、セマンティック検索は動作します。セマンティック検索には、イメージに含まれる
小さなローカル embedding モデルが使われます。fake LLM は AI が生成する回答を返しません。

| コンポーネント | 実運用向けの設定                                         | 未設定の場合                                                                          |
| -------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Embeddings     | プロバイダ、embedding モデル、次元数、API キー。         | `local`（CPU 上の bge-small）: セマンティック検索は可能ですが、ホスト型モデルより精度は劣ります。 |
| LLM            | プロバイダ、モデル、API キー。                           | `fake-echo-1`: 意味のある抽出、要約、回答は得られません。                             |

**Settings → Models & keys** を開き、両方のプロバイダとモデルを選んでキーを入力し、
**Test & apply** をクリックします。Docker の再起動は不要です。embedding 空間を変更すると、
既存のインデックスが再構築されます。ダッシュボードで選べる embedding はホスト型プロバイダです。
`local` を維持したまま LLM を追加するには、代わりに `~/.loom/.env` の LLM 関連の行を設定してください
（[Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)）。

**1 つの OpenAI API キーで両方の役割をまかなえます。** Claude には別の embedding プロバイダが必要です。
OpenAI、Gemini、OpenAI 互換の embedding エンドポイントに対応しています。エージェントの CLI ログインや
サブスクリプションは、Loom のプロバイダキーの代わりにはなりません。

環境変数で設定したい場合は、`~/.loom/.env` を編集してからサーバーを再作成します。

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

キーだけでは、固定されているモデル（ローカル embedder と fake LLM）は変わりません。
プロバイダとモデルも設定してください。
ダッシュボードで保存した設定は `.env` より優先されます。プロバイダごとの手順とヘルスチェックは
[モデルと API キー](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)を参照してください。

**それ以外の設定は 1 つのファイルにまとまっています。** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) には、
すべての設定がデフォルト値と説明付きで分野別に並んでいます。ClickHouse 接続、タイムアウトと予算、
レート制限、ワーカー、機能のオン・オフを切り替える `*_enabled` スイッチなどです。
どの設定も `~/.loom/.env` に `LOOM_<NAME>` として指定してサーバーを再作成すれば反映されます。
設定ファイルをマウントするデプロイでは、このファイルのコピーに記述することもできます
（[設定の読み込み方](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)）。
`live` と記された設定は、Loom の実行中にも変更できます。`loom settings set <name> <value>` か、
Admin → Deployment settings を使います。`loom settings list --changed` を実行すると、デフォルトと
異なる設定と、各値の取得元が表示されます
（[ランタイム設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)）。

<a id="3-connect-your-agents"></a>

### 3. エージェントを接続する

個人向けインストールを実行しているマシンでは、プラグインにトークンは不要です。

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

新しいセッションを開始すると、最初のセッションが `http://localhost:7777` に接続します。Codex では、
`codex` を一度対話モードで実行し、プラグインのフックを承認してください。承認するまではツールだけが動作します。
残しておきたい内容をエージェントに伝え、次のセッションでそれについて尋ねてみてください。

個人向けインストールを実行しているマシンの Cursor では、下のボタンで
[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools)を追加できます。トークンは不要です。

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

個人向けインストールを実行しているマシンの VS Code エージェントモードでは、下のボタンで
[ローカル MCP エントリ](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server)を追加できます。
トークンは不要です。

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

チームサーバーの場合は、Claude Code に `--config server_url=… --config token=…` を渡すか、
[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)の説明に従って
Codex をサーバーに向けてください。トークンは **Settings → Tokens** で作成します。
Cursor やその他の MCP クライアント、および
[SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)についても、そこで説明しています。

<a id="why-loom"></a>

## Loomを選ぶ理由

- **引き継がれるメモリ。** 決定事項や調査結果を、エージェントやセッションをまたいで共有できます。各ファクトには書き込んだ主体が記録され、バージョン履歴と競合レビューによって修正を追跡でき、namespace は任意の時点までロールバックできます。
  [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **根拠を伴うコードインテリジェンス。** 定義、呼び出し元、変更の影響範囲、関連するテストを見つけられます。コードをコミット、プルリクエスト、issue と結び付けることもできます。結果には、リビジョン、ソース上の位置、網羅性が示されます。 [コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **根拠に基づく検索。** アクセスが許可されたメモリ、ドキュメント、会話、トラッカーのレコードを検索します。回答には出典が示され、信頼度の低い検索では回答を控えることもできます。
  [検索](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **マルチエージェントの引き継ぎ。** リース、リトライ、レビュールール、結果を備えた永続的なタスクインボックスを通じて、作業を割り当てます。ワークフローを実行するのはエージェントで、Loom は引き継ぎを保持します。 [タスクキュー](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **エージェントデータの管理。** namespace のアクセス、来歴、保持期間、監査記録、検証済みの消去を管理できます。コンテキストは ClickHouse 上でクエリ可能なまま保たれます。
  [セキュリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## 連携

| 連携                                                                                                                                                                                                                                                                                   | 接続対象                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)、[Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md)、[その他の MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)（[すべての連携](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)） | 既存のエージェントワークフローにメモリとコードインテリジェンスを組み込みます。                 |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)、[TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)、[HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | 独自のエージェントやアプリケーション。                                             |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | リクエストトレース、モデルコスト、検索スコア、プロンプトバージョンの紐付け。 |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | ClickHouse 上の OpenTelemetry のトレース、ログ、メトリクス。                         |

Langfuse と ClickStack は任意の連携であり、Docker スタックには同梱されていません。

<a id="benchmark-details"></a>

## ベンチマークの詳細

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### 検索、リーダー、コード検索の詳細な結果

| 評価                                                  | 結果                                                                                               |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S 根拠検索 (500 問)                       | 上位 30 件のメモリに、根拠となるセッションが少なくとも 1 つ含まれていた質問の割合は **98.6%**。    |
| LongMemEval-S 固定 `gpt-5` リーダー (500 問)          | 上位 200 件のメモリからの回答正解率は **92.0%**。                                                  |
| LoCoMo (全 10 会話)                                   | 回答正解率は **89.6%**。敵対的質問に対する正しい回答拒否は **90.8%**。                             |
| MemoryAgentBench 事実質問サンプル (500 問)            | SubEM は **99.0%**。生成された回答のトークン重複 F1 は **95.7%**。                                 |
| 概念的コード検索 (36 問)                              | 正解ファイル再現率は 5 件で **91.2%**、10 件で **97.2%**。MRR は **0.91**。                        |
| SWE-bench Verified ファイル特定 (200 件の issue)      | 上位 10 件に変更ファイルを含む issue の割合は **82%** (BM25 は **57%**)。上位 5 件では **76.5%**。 |
| トラッカーの重複検出 (104 ペア)                       | 記録済みの古い重複を 1 位にランク付けできた割合は **87.5%**。                                      |

これらは異なるタスクの測定であり、単一の総合スコアではありません。SWE-bench が測定するのはファイル特定であり、issue の解決ではありません。[結果台帳](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)には、スコアの定義、小規模サンプルの評価、より弱い結果や否定的な結果、完全なプロトコル、モデル、成果物が記載されています。

[方法論](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[カバレッジと既知のギャップ](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[ベンチマークの実行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## ドキュメントとコントリビューション

共有エージェントコンテキスト、コードインテリジェンス、権限付きリトリーバルについては、まず[ドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/index.md)をご覧ください。デプロイの要件については、[前提条件](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites)を参照してください。

[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [FAQ](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[コントリビューション](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [セキュリティ報告](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [ライセンス](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [Notice](https://github.com/ClickHouse/loom/blob/main/NOTICE)
