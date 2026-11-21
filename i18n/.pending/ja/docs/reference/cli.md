<!-- i18n: source=docs/reference/cli.md blob=ff91f4468115d6d9b0cb51b887479b6922e69b72 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md), which is authoritative.

<a id="cli-reference"></a>

# CLI リファレンス

`loom` は、Loom のデプロイメントを実行、保守、接続するための唯一のコマンドラインのエントリポイントです。サーバーパッケージの `loom` コンソールスクリプト（`pyproject.toml`、`[project.scripts]` → `server.cli:main`）であり、パーサーツリーは `server/cli/app.py` と、`server/cli/` 配下のコマンドファミリーごとのモジュールにあります。このページ末尾の表は、そのツリーから生成されています。

<a id="where-it-runs"></a>

## 実行場所

| 場所                                               | 方法                                                               | 用途                                                                                                   |
| -------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| サーバーイメージ内                                 | `docker exec loom-server loom <command>`（web コンテナ）           | デプロイ自体の設定に対して実行するオペレーターコマンド：`doctor`、`migrate`、`mint-token`、`reembed`。 |
| 分割デプロイの workers コンテナ内                  | `docker exec loom-workers loom <command>`                          | web ティアから切り離したい長時間のメンテナンス：`code backfill`、`code stats`。                        |
| チェックアウト                                     | リポジトリのルートで `pip install -e .` を実行し、`loom <command>` | `up`、`install`、`demo`、およびローカルスタックに対する開発。                                          |

コンテナ内では、設定はコンテナの環境変数です。チェックアウトから実行する場合、`loom` はまず作業ディレクトリの `.env`（または `LOOM_ENV_FILE` が指すファイル。空にすると読み込みません）を、compose の `env_file` と同じ方法で環境に読み込みます。シェルがすでにエクスポートしている変数は上書きしません。これはプロバイダーキーで重要です。キーなしでチェックアウトから埋め込みコマンドを実行すると、フェイクの埋め込みにフォールバックし、使えないベクトルを書き込んでしまいます。

コマンドは 2 種類に分かれます。

- **オペレーターコマンド**は `server/settings/` を読み、ClickHouse と直接通信します（`migrate`、`reembed`、`code backfill`、`revoke-token`、各種のバックフィルと修復）。デプロイの認証情報で実行されるため、その認証情報がある場所で実行してください。
- **クライアントコマンド**は URL とトークンを使い、稼働中のサーバーと HTTP で通信します（`install`、`init`、`demo`、`memory-search`、`ingest-transcript`、`export-okf`、`import-okf`）。`--server-url`（`demo` では `--base-url`）と、`init` を除いて `--token` を受け取ります。解決順はプラグインフックと同じです。URL は `LOOM_BASE_URL`、次に `~/.loom/config.json` の `url`、いずれもなければ `http://localhost:7777` です。トークンは `LOOM_API_KEY`、次に `~/.loom/config.json` の `token`、次に `~/.loom/token`（`LOOM_API_KEY_FILE` で変更可能）です。`LOOM_CONFIG_FILE` は設定ファイルの場所を変更します。エージェント（ワークロードトークンを伴う `LOOM_CLIENT_ID`）の場合は `--token` か `LOOM_API_KEY` だけを使い、保存済みのトークンは使いません。どちらもなければ拒否します（終了コード 2）。`loom install` は `--token` の指定にかかわらず拒否し、`loom init` はサインアップを行う前に拒否します。`loom call` はワークロードトークンの交換を自分で行います。

`loom mint-token` に必要なのは `LOOM_JWT_SECRET` だけです。トークンをローカルで署名します。

**セッションコマンド**（`login`、`logout`、`whoami`、`call`、`token`）は Python SDK 独自のもの（`loom.cli`。SDK だけをインストールした場合は `python -m loom …`）なので、これらを使うには `loom` に `pip install clickhouse-loom` が必要です。`loom login [--server URL] [--profile NAME] [--no-browser] [--org ORG [--wait S]]` はワンタイムコードでサインインします。`--org` を付けると、ルーターの参照が示すその組織の[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)にサインインします。`loom call <op|/path> [key=text|key:=json …] [--data JSON] [--org] [--namespace]` は、そのセッションで任意のオペレーションやルートにアクセスします。`loom token create|list|revoke` は、CI やエージェント向けの自分のトークンを作成、一覧表示、失効します。詳しくは[ダッシュボードなしで Loom を使う](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md)を参照してください。

<a id="conventions"></a>

## 規約

**書き換えはデフォルトでドライラン。** 保存済みの行を書き換えるコマンドは、`--apply` を指定しない限り、書き込む内容を報告するだけです。`--dry-run` も指定でき、これはデフォルトの動作を明示するものです。この 2 つのフラグは同時に指定できません（`server/cli/_apply.py`、`add_apply_flags`）。ドライランが成功すると、stderr に `dry run: nothing was written; re-run with --apply to write` が出力されて終わります。そのため、コマンドの `--json` 出力だけが stdout に残ります。コマンドによっては同じ考え方を独自のフラグで表します（`loom entity-edges-invalidate-junk` は `--dry-run`（デフォルト）または `--apply` を取ります。`loom reembed --dry-run` は書き込まずに行数を数えます）。各コマンドのフラグは下の表に一覧があります。

**すべての書き込みはプリンシパルを明示します。** コマンドによる書き込みには、アクター `process:cli:<command>` が記録されます（`server/memory/domain/provenance.py`、`cli_actor`）。このため、来歴と監査ログから、どのコマンドがその行を書き込んだかが分かります。

**すべてのクエリには帰属が付きます。** コマンドが発行する ClickHouse のクエリには `log_comment` タグ `cli.<command>[.<subcommand>]` が付きます。コマンドが独自の `--traffic-class` を取らない限り、読み取りは `background` トラフィッククラスで実行されます。`loom serve` は例外です。これはサーバー自体であり、そのリクエストとワーカーの処理は自分でタグを付けます。

**`--json`** を提供するコマンドでは、機械可読な結果を 1 つだけ stdout に出力し、人間向けのレポートは stderr に出力します。

<a id="common-commands"></a>

## よく使うコマンド

<a id="run-and-check-a-deployment"></a>

### デプロイの実行と確認

```bash
loom up                          # from a checkout: create .env, boot Loom, its extractor and ClickHouse, print how to connect
docker exec loom-server loom doctor          # pre-boot checks + the /v1/health problem list, each with its fix
docker exec loom-server loom doctor --json   # the same, machine-readable
loom doctor --client             # also examine this machine as a coding-agent client
docker exec loom-server loom migrate --status              # applied and pending migrations
docker exec loom-server loom migrate --dry-run --strict    # what a boot would apply; exit 1 on ledger drift
```

`loom up` は `/v1/health` が応答するまで待機し（上限は `--timeout`、デフォルトは 300 秒）、認証が有効な場合は writer トークンを発行して、ダッシュボードの URL と最初の呼び出し用スニペットを表示します。`--no-build` はキャッシュ済みのイメージを再利用します。すでにヘルスチェックに応答しているスタックは、`--rebuild` を指定しない限りそのまま残されます。`loom doctor` は、いずれかのチェックが失敗すると終了コード 1 で終了します。`loom serve` はローカル用途で uvicorn 上にコントロールプレーンを起動します（イメージでは `scripts/serve.sh` 経由で起動します）。マイグレーションとアップグレードについては[アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)、ヘルスチェックについては[モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)を参照してください。

<a id="tokens"></a>

### トークン

```bash
# A writer token for one namespace, valid 30 days, printed on stdout
docker exec loom-server loom mint-token --subject my-bot --org dev \
  --namespaces local --roles writer --ttl-days 30

# A scoped token: per-namespace roles, in read-priority order
loom mint-token --subject triage --org acme \
  --namespace-role priv=admin --namespace-role pub=viewer --json

# Retire one token, or every token of a subject issued before an instant
docker exec loom-server loom revoke-token --jti <jti> --reason "leaked"
docker exec loom-server loom revoke-token --subject my-bot --issued-before 2030-01-01T00:00:00Z
```

恒久的な失効には、有効期限を設定することも、それ以前に失効させたトークンを復元するよう範囲を狭めることもできません。既存のカットオフ以降の時刻、または恒久的な失効時刻にトークンのクロックスキュー許容値を加えた時刻のいずれかを、カットオフとして選んでください。

`--save` を指定すると、トークンを表示する代わりに `~/.loom/token`（または指定したパス）に書き込みます。`--ttl-days` を指定しない場合、トークンに有効期限はなく、`loom revoke-token` でのみ失効させられます。`--read-scope own_user` は、読み取りを各リクエストが指定する `user_id` に限定します。`--consumer NAME` は[コンシューマータグ](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#the-consumer-tag)を認証情報に紐づけます。詳しくは[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)を参照してください。

<a id="connect-a-client"></a>

### クライアントの接続

```bash
loom install --client cursor --token "$LOOM_API_KEY" --server-url https://loom.example.com
loom install --client claude-code --toolset coding+tracker --dry-run
loom init --agent triage-bot --server-url https://loom.example.com   # self-signup: a namespace + token
```

`loom install` は、クライアントの設定に MCP サーバーのエントリを書き込みます（または `claude mcp add` / `codex mcp add` を実行します）。`--client` を省略すると、見つかったすべての対応クライアントを設定します。詳しくは [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)を参照してください。`loom init` は、トークンを `~/.loom/token` に、サーバー、組織、名前空間を `~/.loom/config.json` に保存します。このファイルは、クライアントのコマンドとプラグインのフックが読み取ります。

<a id="re-embed-after-changing-the-embedder"></a>

### エンベッダー変更後の再埋め込み

```bash
docker exec loom-server loom reembed --dry-run          # count rows whose embedding_model is not the configured one
docker exec loom-server loom reembed --namespace support
docker exec loom-server loom reembed --delete-old        # re-embed, then delete the off-model rows
```

Loom が自動的に再埋め込みを行うことはありません。`LOOM_EMBEDDING_MODEL` を変更したら、`loom reembed` を実行してください。保存済みのコンテンツからベクトルを再生成し、それ以外のカラムはすべて保持します。`--in-database <named collection>` は、`aiEmbed`（ClickHouse 26.7 以降）を使って ClickHouse 内で埋め込みを行い、テーブルごとに 1 つの `INSERT … SELECT` を実行します。顧客ヘッダーまたは ID ヘッダーが設定されている間、および推論ゲートウェイが必須の間は拒否されます。`--resume` は、前回の実行の `resume:` 行が示す行の続きから再開します。詳しくは[メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)を参照してください。

<a id="index-a-repository"></a>

### リポジトリのインデックス作成

```bash
docker exec loom-workers loom code init --org acme --namespace code-myrepo
docker exec loom-workers loom code backfill --org acme --namespace code-myrepo --embed
docker exec loom-workers loom code stats --org acme --namespace code-myrepo
```

`code backfill` は再開可能です。`--phase index` はパース、チャンク分割、シンボルとエッジの導出を行い、`--phase embed` は未処理のベクトルを埋め、`--embed` は両方を 1 回の実行で行います。フェイクエンベッダーでの埋め込みは、`--allow-fake-embedder`（開発専用）を指定しない限り拒否されます。`--org` と `--namespace` のデフォルトは、それぞれ `LOOM_CODE_ORG` と `LOOM_CODE_NAMESPACE` です。完全な手順、コンパイラティア、PR 履歴のバックフィルについては、[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)を参照してください。

<a id="export-and-import"></a>

### エクスポートとインポート

```bash
loom export-okf --namespace support --out ./support-okf            # a namespace as a Markdown bundle
loom okf-lint ./support-okf --strict                               # offline lint; exit 2 on a critical finding or, with --strict, a warning
loom import-okf ./support-okf --namespace support-copy
loom export-actions --format cedar -o loom.cedarschema             # the registry as a policy schema
```

`export-okf` は、`sensitivity=high` の行、および機密度が不明な行を、`--include-high` を指定しない限り出力から除外します。ミラーリポジトリは、名前空間のグラントよりも広い読者に公開されるためです。そのような行にリンクされた raw チャンクとダイジェスト、および high の形式（認証情報、カード番号、SSN、記載された PIN）を含む raw テキストも、その行と同様に除外されます。`--include-high` を指定するとこれらが出力に含まれ、サーバーは出力された各項目を監査します。詳しくは [OKF](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md) と[アクションスキーマのエクスポート](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#exporting-the-action-schema)を参照してください。

<a id="diagnose-and-repair"></a>

### 診断と修復

`loom report` は、バグ報告フォームの Environment フィールドを `loom doctor` の出力とともに表示します。`--open` を指定すると、バージョン情報を入力済みのフォームを開きます。`loom drops` は、名前空間のメモリ取り込みドロップ台帳を表示します。`loom erasure-re-verify` は、失敗した消去サーガを再検証します。バックフィルと修復のコマンド（`integrity-repair`、`reconcile-status`、`repair-value-bytes`、`entity-edges-invalidate-junk`、`rechunk`、`lexical-stats-rebuild` など）と、それぞれを実行すべきタイミングについては、[メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)と[トラブルシューティング](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)を参照してください。

<a id="reading-the-table"></a>

## 表の読み方

各行は 1 つのコマンドまたはサブコマンドを表し、完全な呼び出し形式、ヘルプテキストの最初の一文、引数を示します。位置引数は `<name>` の形式で、オプションは長い形式で表示しています。`loom <command> --help` を実行すると、ヘルプの全文、デフォルト値、選択肢を表示できます。ジェネレータ（`scripts/codegen/docs_reference.py`、`render_cli`）は、コミット済みのパーサースナップショット `tests/unit/snapshots/cli_tree.json` を読み込みます。`tests/unit/test_cli_surface_snapshot.py` は、このスナップショットと `tests/unit/snapshots/cli_help.json` を実際のパーサーと一致させ続けます。そのため、新しいフラグを追加すると、両方のスナップショットとこのページを再生成するまで CI が失敗します。

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
python scripts/codegen/docs_reference.py
```

<a id="commands"></a>

## コマンド

<!-- BEGIN GENERATED: cli -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

| コマンド | 概要 | 引数とフラグ |
| --- | --- | --- |
| `loom code` | コードインテリジェンスプレーンの操作。 |  |
| `loom code init` | bare クローンとシード状態を作成する。 | `--org`, `--namespace` |
| `loom code backfill` | ツリー全体のインデックス作成（再開可能）。 | `--org`, `--namespace`, `--phase`, `--embed`, `--include-poisoned`, `--allow-fake-embedder`, `--no-fetch`, `--limit`, `--commit`, `--paths-file`, `--concurrency`, `--edges-second-pass`, `--no-edges-second-pass`, `--dialect-folder-pass`, `--embedding-model-suffix`, `--context-callees`, `--callers-from` |
| `loom code reconcile` | 内容に基づく陳腐化チェック。チャンクがブランチ先頭での最終変更より古いパスを検出する。--heal を指定すると再取り込みする（ウォーターマークは変更しない）。 | `--org`, `--namespace`, `--heal`, `--limit`, `--max-commits`, `--no-fetch`, `--show` |
| `loom code compact` | コードベクターテーブルのキーあたりの物理コピー数が --min-ratio に達したときに OPTIMIZE ... FINAL を実行し、マージの完了を待つ。 | `--table`, `--min-ratio`, `--apply`, `--dry-run`, `--timeout-s` |
| `loom code reembed` | 名前空間の有効なチャンクを、2 つ目の埋め込みモデルのラベルで書き込む。データベース内で完結し、再開可能で、チェックアウトは不要。 | `--org`, `--namespace`, `--embedding-model`, `--source-model`, `--prefix`, `--batch` |
| `loom code vector-twin` | チャンクベクターの Matryoshka プレフィックス版を ClickHouse 内で名前空間ごとに作成する。再埋め込みは不要で、再開可能。 | `--org`, `--namespace`, `--embedding-model`, `--all-namespaces` |
| `loom code crossref` | SQL／シェルのテストから実装している C++ への参照パス（code_test_map の method=content_reference を書き込む。冪等）。 | `--org`, `--namespace` |
| `loom code coverage-import` | 設定されたソースの最新のテスト単位の行カバレッジ実行結果をインポートする（code_test_map の method=coverage と code_coverage_files を書き込む）。 | `--org`, `--namespace`, `--dry-run` |
| `loom code export-pairs` | 実トラフィック（トレースと結果の結合）から、埋め込みモデルやリランカーのファインチューニング用に（クエリ → 操作対象ユニット、ハードネガティブ）の検索ペアをエクスポートする。 | `--org`, `--namespace`, `--days`, `--out` |
| `loom code backfill-prs` | GitHub からマージ済み PR の履歴を取り込む。 | `--org`, `--namespace`, `--months`, `--max-prs` |
| `loom code repack` | 最近マージされた PR のコンテキストパックを、現在のパック形式で再生成する。 | `--org`, `--namespace`, `--days`, `--limit`, `--dry-run` |
| `loom code describe` | PageRank 上位のチャンクに対し、LLM でコンテキスト付きの一行説明を作成する。 | `--org`, `--namespace`, `--top-pct`, `--limit`, `--suffix`, `--concurrency` |
| `loom code answers-purge` | キャッシュされた code.ask の回答を墓石化する（合成モデルのアップグレード後、古い低性能モデルの回答は、引用ファイルが変更されたときにしか無効化されないため、そのままでは永久に返され続ける）。 | `--llm-model`, `--all`, `--apply`, `--dry-run`, `--org`, `--namespace` |
| `loom code scip-full` | コンパイラティアの完全な再インデックスを手動で実行する（code_scip_full ワーカーはデフォルトで毎週実行する。マージごとの差分で TU 上限により劣化した部分やドリフトを修復する）。 | `--checkout`, `--jobs`, `--keep-delta`, `--apply-delta`, `--org`, `--namespace` |
| `loom code scip-lock-path` | このホスト上のエントリのコンパイラティア用ロックファイルを表示する。 | `--org`, `--namespace` |
| `loom code entry-lock` | エントリのコンパイラティアロック（このホストのファイルとデプロイメントのリース）を保持してコマンドを実行する。別のパスがロックを保持している場合は終了コード 75、実行中にリースを失った場合は 3（コマンドは停止される）。 | `--org`, `--namespace`, `--tier`, `<command>` |
| `loom code scip-delta` | コンパイラティアの差分パスを 1 回手動で実行する（code_scip_delta ワーカーと同じコード）。設定済みの各リポジトリで git_head と scip_commit を比較し、変更された TU に対して scip-clang を実行して適用する。 |  |
| `loom code entries` | 設定済みのリポジトリごとに 1 行を表示する：org namespace checkout lock-file（ロックパスは解決済み。手動実行では `loom code entry-lock` 経由でロックを取得する）。 |  |
| `loom code warm` | 質問リスト（1 行 1 質問、または 'question' フィールドを持つ JSONL）から code.ask の回答キャッシュをあらかじめ作成する。 | `<file>`, `--org`, `--namespace` |
| `loom code scip-retry` | 失敗した SCIP バッチを pending に戻す。 | `--org`, `--namespace`, `<batch_id>` |
| `loom code backfill-reviews` | code_pr_history にあるマージ済み PR のうち、インラインレビューコメントがまだ保存されていないものを取り込む（新しい順、PR ごとに GitHub へ 1 リクエスト）。 | `--org`, `--namespace`, `--prs` |
| `loom code backfill-assignees` | すでに保存されているマージ済み PR について code_pr_history.assignees（リポジトリがレビューを割り当てた相手）を埋める。クローズ済みの新しい PR から 1 リクエストあたり 100 件ずつたどる。 | `--org`, `--namespace`, `--pages` |
| `loom code backfill-items` | 指定日以降に更新された code_gh_items（Issue と PR、状態を問わない）を、更新の古い順に 1 リクエストあたり 100 件ずつ埋める。gh_items_cursor を進める。 | `--org`, `--namespace`, `--since`, `--pages` |
| `loom code stats` | ウォーターマークと現在の行数。 | `--org`, `--namespace` |
| `loom code export-matrix` | マージ済み PR の同時変更から保守マトリクスを作成する：「このファイルを変更するとき、通常これらも変更される」を根拠付きで、指示ファイル（AGENTS.md / copilot-instructions.md）向けの Markdown として出力する。 | `--org`, `--namespace`, `--prs`, `--min-together`, `--min-conf`, `--max-partners`, `--max-rows`, `--out`, `--json` |
| `loom code lint-instructions` | クローンの先頭でパスやシンボルの参照がもう解決できないエージェント向け指示ファイル（CLAUDE.md、AGENTS.md、copilot-instructions、.claude/.md、skills）を検出する。 | `--org`, `--namespace`, `--file`, `--glob`, `--symbols`, `--all`, `--json`, `--fail-on-stale` |
| `loom decisions` | ドキュメントから抽出した決定記録。 |  |
| `loom decisions extract` | 1 つのナレッジドキュメントから決定記録を抽出する。モデルはドキュメント全体を読む（LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS を超える場合はセクション単位のウィンドウで読む。画像のみの見出しは… | `--org`, `--namespace`, `--source-uri`, `--repo`, `--into`, `--max-per-section`, `--window-chars`, `--apply`, `--dry-run` |
| `loom knowledge` | ナレッジドキュメントとそのソース。 |  |
| `loom knowledge sync` | リポジトリ名前空間のドキュメントソースに対して同期パスを今すぐ 1 回実行する。各ソースの変更ファイルを一覧し（--full ですべてのファイル）、変更されたものをドキュメントとして書き込み、完全な一覧に存在しなくなったものを廃止する… | `--org`, `--namespace`, `--source`, `--full` |
| `loom knowledge source` | 実行中のサーバー経由で操作する、名前空間のドキュメントソース。 |  |
| `loom knowledge source list` | 名前空間のソース、アイテム、同期状態を一覧表示する。 | `--namespace`, `--org`, `--server-url`, `--token` |
| `loom knowledge source add` | リンクまたはアイテムからソースを登録する。何かを保存する前に認証情報でプローブする（knowledge.source.add）。 | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--kind`, `--link`, `--item-json`, `--include`, `--credential-file`, `--dry-run` |
| `loom knowledge source update` | ソースのアイテムを追加・削除したり、ファイル種別を変更したりする。追加したアイテムは保存済みの認証情報でプローブされる（knowledge.source.update）。 | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--link`, `--item-json`, `--include`, `--remove-link`, `--remove-item-json` |
| `loom knowledge migrate-originals` | ドキュメント、アップロード、または取り込みが必要とする、まだ ClickHouse に保持されているドキュメントの原本を LOOM_DOCUMENT_S3_BUCKET に移動する。各原本は個別のオブジェクトにコピーされ、ハッシュ検証のうえ切り替えられ、… で保護される | `--org`, `--namespace`, `--dry-run` |
| `loom export-okf` | 名前空間を OKF v0.2 バンドル（Markdown + フロントマター）として出力する。Loom メモリを git でレビューできる形にした投影。 | `--server-url`, `--token`, `--org`, `--namespace`, `--out`, `--title`, `--include-tombstoned`, `--limit`, `--dry-run`, `--no-evidence`, `--no-links`, `--include-high` |
| `loom import-okf` | OKF v0.2 バンドルのコンセプトを memory.set 経由で名前空間に書き込む。 | `<path>`, `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--session-id`, `--keep-sessions`, `--verify`, `--dry-run`, `--no-evidence`, `--no-lint`, `--force-import`, `--no-links`, `--prune` |
| `loom okf-lint` | OKF バンドルのディレクトリをオフラインで lint する。ミラーリポジトリの CI が実行するコンパイルゲート。 | `<path>`, `--json`, `--strict` |
| `loom up` | ローカルスタック（Loom、そのドキュメント抽出器、ClickHouse）を起動し、接続方法を表示する。テンプレートから .env と ClickHouse の設定を作成し、docker compose up --build を実行し、待機する… | `--root`, `--file`, `--env-file`, `--no-build`, `--rebuild`, `--port`, `--timeout`, `--subject`, `--org`, `--namespace`, `--open` |
| `loom down` | データを保持したままローカルスタックを停止する。--delete-data は確認付きのリセットを行う。 | `--root`, `--file`, `--env-file`, `--delete-data`, `-y` |
| `loom ch-setup` | セルフマネージドの ClickHouse または ClickHouse Cloud サービスを Loom 用に準備する。loom データベースと読み取り専用のリーダーユーザーを作成するか、管理者向けに SQL を表示する。 | `--print-sql`, `--print-analyst-sql` |
| `loom participant` | プロバイダーの変更が待機する Loom プロセス。 |  |
| `loom participant retire` | シャットダウンせずに終了したプロセスのプロバイダー参加者を退役させる（その ID はそのプロセスの /v1/health の `participant_id`）。 | `--participant`, `--yes` |
| `loom deployment` | すべてのレプリカが自身を照合する設定。 |  |
| `loom deployment publish` | この env の設定をデプロイメントの設定として公開する。意図的にルートシークレットや LOOM_EMBEDDING_MODEL/DIM を置き換える場合に使う。 | `--yes`, `--check` |
| `loom settings` | 有効な各設定の値と、ライブ設定の上書き。 |  |
| `loom settings list` | 各設定の値、その出所、ライブかどうかを表示する。 | `--prefix`, `--changed`, `--json` |
| `loom settings set` | ライブ設定をデプロイメント全体で上書きする。再起動後も維持される。 | `<name>`, `<value>`, `--expect` |
| `loom settings reset` | 上書きを削除し、env またはデフォルトに戻す。 | `<name>`, `--expect` |
| `loom settings history` | 記録された変更を新しい順に表示する：誰が、変更前後の値。 | `<name>`, `--limit`, `--json` |
| `loom secrets` | ClickHouse に封印されて保存された認証情報と署名キー。 |  |
| `loom secrets reseal` | LOOM_JWT_SECRET_PREVIOUS でしか開けないものを、LOOM_JWT_SECRET で封印し直す。 | `--dry-run`, `--check` |
| `loom signing-key` | LOOM_JWT_SECRET と並んで ClickHouse に保持されるトークン署名キー。 |  |
| `loom signing-key rotate` | キーを追加する。すべてのプロセスが読み込んだ時点で署名に使われる。 | `--algorithm`, `--activate-in` |
| `loom signing-key retire` | キー（または 'default'）によるトークン検証を停止する。 | `<kid>` |
| `loom signing-key record-root` | この LOOM_JWT_SECRET を、意図的に置き換えた後のデプロイメントのルートシークレットとして記録する（旧シークレットで封印されたキーは使用不能のままになる）。 |  |
| `loom signing-key list` | キーを一覧表示する。 |  |
| `loom provider-lock` | プロバイダー設定ロック。 |  |
| `loom provider-lock clear` | プロセスが停止したホルダーのプロバイダー設定ロックを解放する（更新、ハートビート、クエリ実行中の間は拒否される）。 | `--holder` |
| `loom export-analytics` | UTC の 1 日分の利用状況と名前空間のスナップショットを Parquet として S3 に書き出す。名前は LOOM_ANALYTICS_EXPORT_NAME_KEY でハッシュ化される。 | `--bucket`, `--prefix`, `--day`, `--role-arn`, `--region`, `--expected-bucket-owner` |
| `loom sql-access` | sql_v1_* ビューを通じてテナントのメモリ、会話、リクエスト台帳を読み取る ClickHouse ユーザーを作成、スコープ変更、削除する。 |  |
| `loom sql-access grant` | ユーザーがこのスコープだけを読み取れるようにする。ユーザーがいなければ作成する（パスワードは一度だけ表示される）。 | `--user`, `--org`, `--namespaces`, `--all`, `--rotate` |
| `loom sql-access revoke` | ユーザーのスコープを取り消し、その後ユーザーを削除する。 | `--user` |
| `loom sql-access list` | 付与済みのスコープ行を SQL ユーザーごとにすべて表示する。 |  |
| `loom reembed` | embedding_model が設定済みの埋め込みモデルと異なるベクターを再埋め込みする（LOOM_EMBEDDING_MODEL を変更した後に実行する。Loom は自動で再埋め込みしない）。 | `--org`, `--namespace`, `--batch-size`, `--in-database`, `--dry-run`, `--delete-old`, `--key-only`, `--resume` |
| `loom reextract` | 名前空間の生のエビデンス（memory.set_from_messages のターン）を、実行中のサーバーの書き込みパス経由で別の名前空間に再生する。保存済みのトランスクリプトに対して現在の抽出器を適用する。ソースは… | `--org`, `--namespace`, `--into`, `--base-url`, `--token`, `--dates`, `--concurrency`, `--limit-sessions`, `--dry-run` |
| `loom deploy-delta` | カラースワップで何が変わったか：デプロイメントの台帳行（操作ごとの量、エラー、p50/p95、アップストリームの費用）と ClickHouse のステートメント形状（実行回数、バイト数、秒数）を、前後それぞれのウィンドウで比較する… | `--swap-at`, `--window-min`, `--ch-os-user`, `--cluster`, `--top`, `--json` |
| `loom entity-export` | 名前空間のエンティティグラフ（共起エッジとトリプル）を、外部のグラフツールや RDF ツール向けに GraphML または JSON-LD でエクスポートする。 | `--org`, `--namespace`, `--format`, `--output`, `--all` |
| `loom audit-namespace` | 名前空間がどのように使われているか（namespace.audit）：完全一致キーの読み取りと検索、ハッシュキー、二重エンコードされた値や過大な値、手作りのインデックス行、メモリとして保存されたリース、バージョンの増加。 | `--org`, `--namespace`, `--window-days`, `--samples`, `--json` |
| `loom mentor` | ストアから 1 つのエージェントへのアドバイス（mentor.brief）：呼び出した内容、名前空間が提供するプレーン、根拠付きのヒント、過去のヒントがどれだけ従われたか。 | `--org`, `--namespace`, `--agent`, `--window-days`, `--json` |
| `loom serve` | コントロールプレーンを起動する（uvicorn）。 | `--host`, `--port`, `--log-level`, `--reload` |
| `loom migrate` | 設定された CH に対してスキーママイグレーションを適用する。 | `--status`, `--dry-run`, `--strict`, `--target`, `--migrations-dir` |
| `loom reconcile-status` | status が loom.memory と食い違った memory_embeddings の行（プールが認識しなかった superseded / conflicted / duplicate のマーク）を、識別行（org, namespace, agent… | `--org`, `--namespace`, `--all-namespaces`, `--since-days`, `--exclude`, `--limit`, `--apply`, `--dry-run` |
| `loom period-digest` | 期間ダイジェストを書き込む。1 つの名前空間内の各識別子について、連続する K 個のセッションダイジェストから導出された 1 つのセマンティックメモリ。 | `--org`, `--namespace`, `--sessions`, `--max-identities`, `--max-periods`, `--apply`, `--dry-run`, `--rewrite` |
| `loom demote-activity-narration` | ツールの動作を語るだけで持続的な成果を伴わない、名前空間内の抽出済みファクト（「User read lines 1-1000 of X」「Claude ran a command on …」）を見つけ、格下げ（信頼度 x0.25）またはラベル付けする… | `--org`, `--namespace`, `--action`, `--classifier`, `--days`, `--limit`, `--apply`, `--dry-run` |
| `loom recalibrate` | 埋め込みモデルの再プローブと名前空間ごとのキャリブレーションを今すぐ実行する（通常は再キャリブレーションワーカーが週次で実行する）。 |  |
| `loom import-clickgap` | ClickGap（Detector→Validator→Reviewer）の SQLite エクスポートを Loom にインポートする（lessons、型付きエンティティ、カテゴリ）。 | `--sqlite`, `--org`, `--namespace`, `--agent`, `--dry-run`, `--batch-size`, `--seed-recall-events` |
| `loom import-recall` | 外部のベクターリコール型エージェントメモリの SQLite エクスポートを Loom にインポートする。 | `--sqlite`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom import-brain` | Markdown のエージェント「brain」ディレクトリを Loom にインポートする：MEMORY.md（常に注入される HOT レイヤー）→ profile.set のセクション、memory/contexts/*.md（オンデマンドで読む WARM レイヤー）→ knowledge.add のドキュメント。 | `--dir`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom claim-release` | セルフサインアップのクレームを解放する：付与を取り消し、オーナーをクリアし、無効化する。 | `--org`, `--namespace`, `-y` |
| `loom mint-token` | JWT をローカルで発行する（LOOM_JWT_SECRET が必要）。 | `--subject`, `--org`, `--namespaces`, `--roles`, `--platform-admin`, `--namespace-role`, `--ttl-days`, `--save`, `--read-scope`, `--read-user-id`, `--traffic-class`, `--consumer`, `--service-account`, `--user-token`, `--json` |
| `loom revoke-token` | jti または subject でトークンを 1 つ失効させる（失効モジュール経由で loom.revoked_tokens に書き込む。設定済みの ClickHouse が必要）。 | `--jti`, `--subject`, `--list`, `--issued-before`, `--scim`, `--reason`, `--expires-at`, `--json` |
| `loom sso` | パスワードアカウントをシングルサインオンに移行する。 |  |
| `loom sso adopt` | SSO でのサインインを、メールアドレスが --domain に属するパスワードアカウントに届くようにします。これにより、各ユーザーは ID プロバイダーで自分のアカウント、その名前空間とデータにサインインできます。 | `--domain`, `--org`, `--skip`, `--keep-tokens`, `--apply`, `--dry-run` |
| `loom sso link` | ある人の SSO でのサインインを、別のアドレス（個人用のアドレスや +tag 付きのアドレス）で登録されたパスワードアカウントに届くようにします。 | `--email`, `--org`, `--namespace`, `--apply`, `--dry-run` |
| `loom sso unlink` | 1 つのメールアドレスのリンクを元に戻します。そのグラントはアカウントを受け入れなくなります。 | `--email`, `--apply`, `--dry-run` |
| `loom sso password-link` | シングルサインオンを無効にした後のために、メールアドレスの SSO アカウントのパスワードを設定するための 1 回限りのリンク（LOOM_PASSWORD_LINK_TTL_SECONDS の間有効、デフォルトは 1 日）を出力します。 | `--email`, `--org` |
| `loom bot` | キーまたは CI の ID でサインインするボットと CI ジョブ。 |  |
| `loom bot create` | ボットを登録します。その名前空間と、キー（--generate-key または --public-key）や CI の信頼設定（--issuer、--audience、--claim）を指定します。 | `--name`, `--org`, `--namespace-role`, `--generate-key`, `--public-key`, `--issuer`, `--audience`, `--claim`, `--jwks-uri`, `--max-token-age`, `--max-access-token-ttl`, `--max-agents`, `--agent-claim`, `--platform-admin`, `--replace` |
| `loom bot list` | 有効か無効かを問わず、すべてのボット。 |  |
| `loom bot disable` | ボットの証明を拒否し、ボットが保持するトークンを失効させます。 | `--name` |
| `loom doctor` | 起動前の設定ミス（Python のバージョン、設定、JWT、CH への到達性）を診断し、/v1/health が返す問題一覧を、それぞれの対処法とともに出力します。 | `--json`, `--client`, `--server-url`, `--token` |
| `loom report` | バグ報告フォームの Environment 欄（バージョンと `loom doctor` のチェック結果）を出力します。 | `--open` |
| `loom drops` | 名前空間のメモリ取り込みドロップ台帳（loom.memory_drops）を、ステージと理由ごとに出力します。パイプラインが破棄、切り詰め、または断念したものが分かります。 | `--namespace`, `--org`, `--hours` |
| `loom namespaces-gc` | 使い捨ての名前空間（ベンチのプレフィックス配下にあり、誰も登録しておらず、--idle-hours の間誰も書き込んでいない、完了済みのベンチ実行）を一覧表示（デフォルト）または消去します。消去は、リスト単位の消去 saga ごとに行います… | `--org`, `--prefix`, `--namespace`, `--exclude-prefix`, `--idle-hours`, `--include-registered`, `--erase`, `--no-purge`, `--skip-external`, `--batch`, `--show`, `--requester`, `--worker-pass` |
| `loom erasure-re-verify` | 失敗した消去 saga を再検証します。それぞれの最後のステップ名付きの理由と、新たな残留データの集計を出力します（デフォルト、読み取り専用）。--resume を指定すると、リカバリワーカーの範囲を超えて、チェックポイントから再実行します… | `--org`, `--id`, `--resume`, `--skip-external`, `--completed-since`, `--orphan-graph`, `--erase`, `--batch`, `--show`, `--requester` |
| `loom reconcile` | ClickHouse または Postgres のリストア後、もう一方のストアがリストア時点以降に記録した消去と、機密コンテンツを削除またはマスクするプライバシー操作であるリダクションを、リストアされたストアで再度実行します。ClickHouse のリストア後は、すべてを再度公開します… | `--restored`, `--point`, `--allow-live`, `--apply`, `--dry-run` |
| `loom router` | デプロイメントのセルの前段に置かれるルーター。 |  |
| `loom router serve` | 配置、ルックアップ、オペレーターの API を提供し、その他のリクエストをセルにルーティングします。 | `--host`, `--port`, `--log-level` |
| `loom router migrate` | ディレクトリの保留中のマイグレーションを適用します。 |  |
| `loom cell` | ルーターのディレクトリを通じて、デプロイメントのセルを操作します。 |  |
| `loom cell credential` | ルーターの認証情報を作成、またはセルの認証情報をローテーションします。 | `<action>`, `--cell`, `--next`, `--finish`, `--revert`, `--operator`, `--router` |
| `loom cell limit` | セルへの新しい org の受け入れを閉じる、再開する、または上限を設ける。 | `--cell`, `--closed`, `--open`, `--placements-max`, `--router` |
| `loom cell which` | org に対するルックアップのセルとホストを出力します（配置されていない場合は最初のセル）。 | `--org`, `--router` |
| `loom cell remove` | 配置のない状態で接続しているセルのディレクトリ行を削除します。 | `--cell`, `--router` |
| `loom cell prepare` | このセルをルーターのディレクトリに登録し、その行を書き込みます。 | `--cell`, `--host`, `--credential-sha256`, `--probe-url`, `--wait`, `--router` |
| `loom tracker-probe` | トラッカーソースへの読み取り専用の初回接続。認証情報ごと、アップストリームのテーブルごとの件数と最新のタイムスタンプ、ブレーカーの状態、設定済みのトラッカー名前空間を表示します。 |  |
| `loom tracker-contract-check` | 読み取り専用：各トラッカーソースのスキーマがレコード契約（docs/features/tracker/record-contract.md）を満たしているかを確認します。 | `--source`, `--namespace`, `--json` |
| `loom tracker-dupcheck-fit` | 1 つのトラッカー名前空間について、tracker.duplicate_check の較正済み信頼度を今すぐ再フィットします。起票時点の重複のゴールドデータを再生し、フィットして、結果を保存します。 | `--namespace`, `--org`, `--negatives`, `--dry-run`, `--out` |
| `loom tracker-embed-reset` | 1 つのトラッカー名前空間の tracker_embed パスのカーソルをクリアし、次のパスでウィンドウを再走査して、テキストのレシピが変わったすべてのアイテムを再埋め込みします。 | `--namespace`, `--org`, `--kind`, `--apply`, `--dry-run` |
| `loom namespaces-adopt` | ライブのメモリを持ちレジストリ行のない名前空間に、オーナー行を付与します。ベンチのプレフィックス配下は kind=bench、それ以外は memory とし、retention_days は 0、TTL なしです。 | `--org`, `--owner`, `--prefix`, `--namespace`, `--kind`, `--apply`, `--dry-run`, `--show` |
| `loom sensitivity-sweep` | 書き込み時に機微性検出器がラベルを付けるはずだったものの、あるパスがスキップした有効な行にラベルを付けます。 | `--org`, `--namespace`, `--since-days`, `--limit`, `--show`, `--only-shape`, `--apply`, `--dry-run` |
| `loom integrity-repair` | 行レベルの整合性ゲージ（ベクトルを保持する dim-0 行、tombstoned_at/status のずれ、valid_to を過ぎた有効な行）を読み取り、--apply を指定すると、dim-0 行を本来の次元で再出力して検索が… | `--org`, `--namespace`, `--limit`, `--show`, `--apply`, `--dry-run` |
| `loom rechunk` | 名前空間のナレッジドキュメントを、保存済みのコンテンツから現在のチャンカーで再チャンク化して再埋め込みします。これにより、すべてのドキュメントがセクションのメタデータ、チャンクのコンテキスト、ドキュメント頻度を持つようになります。 | `--org`, `--namespace`, `--source-uri`, `--apply`, `--dry-run` |
| `loom export-actions` | オペレーションレジストリから導出したアクションスキーマをエクスポートします。Loom の拡張を含む MCP の tools/list マニフェスト（json）、またはポリシーゲートウェイが操作対象の統制に使う Cedar スキーマ（cedar）です。 | `--format`, `--output` |
| `loom lexical-stats-rebuild` | 名前空間の BM25 の文書頻度を、有効な行から再計算します（語頻度ビューは挿入のみを反映するため、置き換えや tombstone により古い語彙が残ります）。 | `--org`, `--namespace`, `--no-knowledge`, `--no-memory` |
| `loom version` | インストール済みの Loom のバージョンを出力します。 |  |
| `loom install` | ローカルの AI クライアントの設定に Loom を組み込みます。 | `--client`, `--server-url`, `--token`, `--name`, `--toolset`, `--dry-run`, `--force` |
| `loom demo` | 最初の 5 分間：このリポジトリの AGENTS.md（または --file）を `make ingest` と同じ方法でナレッジプレーンに取り込み、質問をして、根拠の行とともに回答を出力します。 | `--file`, `--question`, `--tags`, `--base-url`, `--token`, `--org`, `--namespace`, `--root`, `--search-only` |
| `loom init` | Loom サーバー上に ID を作成します。 | `--agent`, `--server-url`, `--org`, `--purpose`, `--ttl-days`, `--token-path`, `--config-path`, `--no-files`, `--json` |
| `loom ingest-transcript` | チャット履歴ファイル（markdown / JSON）を、1 つのスレッドとして Loom の conversation テーブルにパイプします。 | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--thread-id`, `--file`, `--dry-run` |
| `loom memory-search` | シェルから memory.search を実行します。 | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--query`, `--top-k`, `--search-mode`, `--json-agent` |
| `loom set-host` | このデプロイメントの Loom ホストを指定します。 | `<emails>`, `--env-file`, `--append`, `--dry-run` |
| `loom login` | ワンタイムコードを使って、任意のブラウザから Loom サーバーにサインインします。 |  |
| `loom logout` | サーバー上の保存済みセッションを終了し、ローカルからも削除します。 |  |
| `loom whoami` | 保存済みのセッションでサインインしている ID を表示します。 |  |
| `loom call` | 保存済みのセッションで、任意のオペレーションまたは API ルートを呼び出します（`loom call --help`）。 |  |
| `loom token` | CI やエージェント向けに、自分のトークンを作成、一覧表示、失効します（`loom token --help`）。 |  |

<!-- prettier-ignore-end -->
<!-- END GENERATED: cli -->

<a id="related"></a>

## 関連ドキュメント

- [オペレーションリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)：クライアントコマンドが呼び出す op。
- [メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md)：再埋め込み、バックフィル、修復。
- [コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)。
- [アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md)：アップグレード手順における `loom migrate`。
- [ローカル開発](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md)。
