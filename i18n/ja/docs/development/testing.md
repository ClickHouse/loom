<!-- i18n: source=docs/development/testing.md blob=abc5f046c927795cb7b9c4d6a81dc5bc34b9cc85 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md), which is authoritative.

<a id="testing"></a>

# テスト

テストをどのスイートに置くか、各スイートの実行に必要なもの、環境が揃っていない場合にテストがどのようにスキップされるか、CI が強制するゲート、そしてテストが固定している内容の再生成方法について説明します。

<a id="the-suites"></a>

## スイート

| スイート                                        | パス                                                                 | 必要なもの                                                                    | ローカルでの実行                                               | CI での実行                                                    |
| ----------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------- |
| ユニット                                        | `tests/unit/`                                                        | 外部依存なし。ClickHouse は不要                                               | `make test-unit`（`pytest bench -q` も実行されます）           | `unit` は分割してランダム順で実行。`unit-coverage` はカバレッジ 40% 以上      |
| ベンチハーネス                                  | `bench/`（その `test_*` ファイル）                                   | 外部依存なし                                                                  | `pytest bench -q`                                              | `unit-coverage`                                                |
| 統合                                            | `tests/integration/`                                                 | マイグレーション済みの ClickHouse と稼働中のサーバー                          | `LOOM_BASE_URL` を指定して `make test-integration`            | `integration`                                                  |
| アナライザーコーパス                            | `tests/analyzer/`                                                    | マイグレーション済みの ClickHouse（サーバーは不要）                           | `pytest tests/analyzer -q`                                     | `integration`                                                  |
| コードレーンコーパス                            | `tests/code_lanes/`、ゲートは `tests/unit/test_code_lanes_corpus.py` | 外部依存なし                                                                  | `python -m tests.code_lanes.corpus`                            | `unit`                                                         |
| ダッシュボード                                  | `frontend/src/**/*.test.tsx`                                         | Node                                                                          | `npm --prefix frontend run test`（axe を使う vitest）          | `frontend-vitest`                                              |
| ダッシュボードのエンドツーエンド                | `frontend/e2e/*.spec.ts`                                             | Node、Chromium（`npx playwright install chromium`）                           | `npm --prefix frontend run e2e`                                | `frontend-e2e`                                                 |
| プラグインフック                                | `integrations/*/tests/*.test.mjs`                                    | Node                                                                          | `node --test integrations/claude-code-plugin/tests/*.test.mjs` | `agent-plugins`                                                |
| フレームワークが extra である Python SDK アダプター | `sdk/python/tests/`                                              | `uv`。各アダプターの extra を `.venv-sdk-adapters/` 配下の専用 venv にインストール | `make test-sdk-adapters`                                  | `sdk-python-adapters`                                          |
| TypeScript SDK                                  | `sdk/typescript/`                                                    | Node                                                                          | `npm --prefix sdk/typescript run check`                        | `sdk-typescript`                                               |
| ベンチマークとゲート                            | `bench/*.py`、`make bench-*`                                         | **実際の**プロバイダーを使うサーバー                                          | [ベンチマークの実行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)                 | 実行しない（CI のエンベッダーはフェイクのため）                |

**ベンチマークを CI に含めない理由。** CI は `LOOM_EMBEDDING_MODEL=fake-ngram-1024` と `LOOM_LLM_PROVIDER=fake` で実行されるため、そこでの再現率の数値は実際の検索について何も測定しません。フェイクのエンベディングで品質ゲートが通っても、ゲートがないより悪い結果になります。CI で実行するには、有料プロバイダーのシークレットをリポジトリに置く必要もあります。ゲート（`make bench-quick`、`bench-quality-gate`、`bench-code-intel`）は、抽出プロンプト、デフォルトモデル、エンベッダーのいずれかを変更する前に、実際のプロバイダーを使うデプロイメントに対して手動で実行します。

<a id="unit-tests"></a>

## ユニットテスト

- **ClickHouse は使わない。** ユニットテストではクライアントをスタブに置き換える。実際の SQL セマンティクスが必要なものは、統合テストまたはアナライザのスイートに置く。
- **ランダムな順序で、並列に実行する。** CI は `-n auto -p randomly` を付けて、コアごとに 1 つのワーカープロセスでスイートを実行し、最初にシードを出力する。そのため、テストは他のどのテストの後でも、またどのテストと並行しても成功する必要がある。ルールと、シードとログから失敗を再現する方法は、[`tests/README.md`](https://github.com/ClickHouse/loom/blob/main/tests/README.md#rules-every-test-keeps) にある。
- **非同期テストでは `await asyncio.sleep(...)` で待機する。** `time.sleep` は使わない。
- **実時間の日付。** 固定の日付を持つフィクスチャを、陳腐化チェックが `now()` と比較する場合、その日付がしきい値を超えて古くなるまではテストが成功する。チェックが実時間と照らし合わせて読む日付をハードコードせず、コードの時刻のシームを通してクロックを固定する。

<a id="integration-tests"></a>

## 統合テスト

統合テストスイートは、CI の `integration` ジョブと同じように、稼働中のサーバーに対して実行します。

```bash
bash dev/ci_start_clickhouse.sh                      # or the dev stack's ClickHouse
make migrate
LOOM_INSTANCE_LABEL=dev:$USER:tests LOOM_BACKGROUND_WORKERS=off make server &
LOOM_BASE_URL=http://localhost:7777 make test-integration
```

- `tests/conftest.py` はリポジトリの `.env` をテストプロセスに読み込みます（未設定の変数だけを補います）。これにより、テストとサーバーは同じプロバイダーを選びます。あるエンベッダーで書き込まれた行は、別のエンベッダーでの読み取りからは見えません。
- トークンを自分でエクスポートしていない限り、各テストにはワイルドカード org のライタートークンが `LOOM_API_KEY` に設定されます（`agent` フィクスチャが使用します）。また、`mint_test_token` フィクスチャはテスト自身の org 用のトークンを発行します。どちらもテストプロセスの `LOOM_JWT_SECRET` で署名するため、サーバーのものと一致している必要があります。
- サーバーは `LOOM_BACKGROUND_WORKERS=off` で実行してください。ワーカーを検証するテストは、自身の名前空間に対して `run_pass` をインプロセスで呼び出します。稼働中のスーパーバイザーのワーカーには名前空間フィルターがないため、それらのテストの行を消費してしまいます。`knowledge_ingest` のように、全テナントが共有するキューから 1 件を取り出す pass を持つワーカーは、pass が実行するのと同じ方法でテスト自身のアイテムを実行して検証します（`ingest_run.take_and_run`）。
- `tests/integration/test_document_object_store_live.py` には、`LOOM_TEST_S3_ENDPOINT` に S3 互換ストアも必要です（CI はローカルのストアを起動します。`.github/workflows/ci.yml` を参照）。なければスキップされます。
- `tests/integration/test_knowledge_ingest_live.py` のパブリッシュ系テストは独自のジョブを実行し、原本をドキュメントエクストラクターに送信します。SQL と TOML にルーティングされたエクストラクターが必要で（`LOOM_DOCUMENT_EXTRACTOR_URL` または `LOOM_DOCUMENT_EXTRACTORS_JSON` とそのトークン。[設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)を参照）、なければスキップされます。`tests/integration/test_knowledge_inspect_ingest.py` も、`knowledge.inspect` を通じてサーバーにエクストラクターが設定されていることを検証するため、サーバーにも同じ設定を渡してください。CI は各パートのサーバーを起動する前にリファレンスエクストラクターを起動します（`.github/workflows/ci.yml`、[起動方法](https://github.com/ClickHouse/loom/blob/main/extractor/README.md)）。
- `tests/integration/test_pg_authority.py` には Postgres オーソリティのサーバーが必要です。`dev/ci_start_postgres.sh` で Postgres を起動し、`make migrate` を実行し、サーバーと pytest の両方に `LOOM_STORAGE_AUTHORITY=postgres` と `LOOM_PG_DSN=postgres://loom:loomdev@localhost:5432/loom`（スクリプトのデータベース。ポートは `LOOM_PG_HOST_PORT` で変更できます）を設定します。このテストはオーソリティのコードをテストプロセス内でも実行するためです。`LOOM_PG_STANDBY=0`（同期スタンバイなし）で起動した Postgres を使う場合は、pytest の環境にも `LOOM_PG_STANDBY=0` が必要で、これによりスタンバイのヘルステストがスキップされます。ClickHouse オーソリティのサーバーに対しては、API だけを使うテスト以外はすべてスキップされます。
- `tests/integration/test_pg_clickhouse_live_ch.py` は、`pg_clickhouse` を備えた Postgres から [SQL サーフェス](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)を読み取ります。`dev/ci_start_pg_clickhouse.sh` で起動し（ホストネットワーク、ポート 5433 を使うため Linux の Docker ホストが前提です。コンテナは 127.0.0.1 に公開された ClickHouse に接続します。イメージは `LOOM_PGCH_POSTGRES` と `LOOM_PGCH_VERSION` で選びます）、その ClickHouse の `LOOM_CH_*` と併せて `LOOM_PGCH_DSN=postgres://app:appdev@127.0.0.1:5433/app` を設定します。これらがなければスキップされます。終了後はコンテナを削除してください（`docker rm -f loom-pgch`、または `LOOM_PGCH_CONTAINER` で指定した名前）。
- CI は `LOOM_LATENCY_BUDGET_MULTIPLIER=10` を設定します。共有ランナーはノイズが大きく、デフォルトの op ごとの予算では違反が発生するためです。
- セルフサインアップに依存するテストは、`LOOM_SELF_SIGNUP=open` のない CI ではスキップされます。発行したトークンを使うことを推奨します。
- **ティア。** `tests/integration/conftest.py` は、手元の簡易実行で除外できる 4 つのマーカーを登録します（`-m "not slow and not destructive"`）。CI はディレクトリ全体をパートに分けて実行します（後述）。`slow` は意図的に実時間を消費します（リースの失効、リトライの使い切り）。`destructive` はマージを停止したり、`OPTIMIZE` を実行したり、スコープ全体を削除したりします。`provider` は実際のエンベッダーまたは LLM を必要とし、フェイクではスキップされます。`recovery` は障害を起こし、そこからの復旧経路を検証します。
- **パート。** `LOOM_TEST_SHARD=k/n` は、n 分割のうち k 番目のパートのテストファイルだけを収集します。パートはファイル単位で構成されるため、モジュールフィクスチャは 1 つのパートでのみ構築されます。ファイルは `tests/shard_weights.json` に記録された各ファイルの秒数で割り振られます。記載のないファイルは、テスト関数の数に記録された 1 関数あたりの秒数を掛けた値で、何も記録がなければ、すべての統合ファイルと同様に 1 件あたり 1 秒として扱われます（`tests/_shards.py`）。CI はスイートを 4 つのパートに分け、それぞれ独自のスタックで実行します。分離ゲートのパートは別にあります。パート k の失敗は `LOOM_TEST_SHARD=k/4 make test-integration` で再現できます。
- **失敗時の診断。** 失敗したテストのレポートには、直近 25 件の Loom リクエスト（メソッド、URL、ステータス、`X-Request-Id`、レイテンシ）が含まれます。JWT、ベアラートークン、シークレットは伏せ字になっており、リクエスト ID から対応するサーバーのログ行を探せます。
- **クリーンアップ。** `unique_org(label)`（`tests/integration/_resources.py`）は `it-<label>-<8 hex>` を発行します。セッション終了時に、発行されたすべての org の行を `org` 列を持つ各テーブルから削除し、残った行を数えます。残りがあれば実行は失敗します。`LOOM_IT_KEEP_ROWS=1` を設定すると、デバッグ用に行が残ります。

<a id="framework-adapters"></a>

## フレームワークアダプター

フレームワークが SDK extra であるSDKアダプターは、そのフレームワークを `uv.lock` に含めません。そのため `make test-sdk-adapters` は、各アダプターを `.venv-sdk-adapters/` 配下のそれぞれ専用の venv にインストールして（2つのフレームワークが同じパッケージを互換性のない形で固定することがあるため）、ユニットテスト（`sdk/python/tests/test_<module>.py`）を実行します。1つだけ実行するには `make test-sdk-adapter-<extra>` を使います。統合テスト（`tests/integration/test_sdk_<framework>_*.py`）は、フレームワークがない場合はスキップされます。実行するには、その extra の依存関係を `.venv` にインストールし（`uv pip install --python .venv/bin/python -r <(python scripts/gates/sdk_extra_requirements.py langchain)`）、[統合テスト](#integration-tests)と同様にサーバーに対して実行します。`uv sync` を実行すると、フレームワークは再び削除されます。

<a id="how-tests-skip"></a>

## テストをスキップする条件

環境が欠けているテストは、理由を付けてスキップします。変数が無いことを理由に失敗させてはいけません。`os.environ.get(...) or pytest.skip(...)` を使い、`os.environ[...]` は使わないでください。共有フィクスチャもこのルールに従います。`control_plane_url` は `LOOM_BASE_URL` が無い場合にスキップします。`LOOM_BASE_URL` が設定されているのに `/v1/health` が 200 を返さない場合は、サーバーが存在しないのではなく壊れているということなので、URL とエラーを示してテストを失敗させます。`require_real_embedder` は、フェイクの embedder で動作しているサーバーでは、検索品質のテストをスキップします。アナライザースイート（`migrated_ch`）は、`LOOM_CH_*` のアドレスで ClickHouse が応答しない場合、または対象のマイグレーションが遅れている場合に、アドレスと対処方法を理由に示してスキップします。`CI` が設定されている環境では、ジョブが ClickHouse を起動して先にマイグレーションを実行するため、スキップせずに失敗します。

<a id="the-analyzer-statement-corpus"></a>

## アナライザのステートメントコーパス

Loom の ClickHouse ステートメントは、多数のビルダーが多数のフラグのもとで組み立てる Python 文字列です。ユニットテストのスタブはどんな文字列でも受け入れるため、フィルタ対象の列を隠すエイリアス、`WITH` なしで読まれる CTE、サブクエリ内で使われる外側のエイリアス、ドライバがバインドできないパラメータ型といった問題は、エラーにならずに、失敗しても検知されない分岐としてサーバーに到達します。その結果、行が欠落します。`tests/analyzer/` はすべてのビルダーをあらゆる軸で実行し、ステートメントを形状ごとに重複排除して、マイグレーション済みの空のテーブルに対して `EXPLAIN QUERY TREE` を実行します。これにより、上記の問題はすべて数ミリ秒で拒否されます。`shapes.txt` はコミットされたレビュー対象です。ステートメントの形状を変更するプルリクエストでは、新しい先頭部分が差分に表示されます。

```bash
pytest tests/analyzer -q                              # what CI runs
python -m tests.analyzer.corpus --write               # regenerate shapes.txt (review the diff)
python -m tests.analyzer.corpus --case erasure --errors
```

[アナライザの README](https://github.com/ClickHouse/loom/blob/main/tests/analyzer/README.md) では、ケースの形式、マイグレーション済みターゲットの要件、`LOOM_CH_CAPTURE_STATEMENTS` で取得した本番環境の形状のインポートについて説明しています。

<a id="ci-gates"></a>

## CI ゲート

ほとんどのゲートはユニットテストなので `make test-unit` で実行されます。残りは `make lint` のステップです。失敗したときに何を実行すべきかは、各ゲートに示しています。

| ゲート                          | 場所                                                                                  | 失敗する条件                                                                                                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 公開コピー                      | `tests/unit/test_public_copy.py`                                                      | ツリー内に隣接製品、人物、内部ホスト、内部名前空間の名前がある、論文や設計記録を引用している、または公開ページに競合製品の名前がある                                                           |
| ドキュメントリンク              | `tests/unit/test_doc_links.py`                                                        | ルートの Markdown、`docs/`、インテグレーションの README にある相対リンクまたは `#anchor` が解決できない                                                                                        |
| 設定リファレンス                | `tests/unit/test_settings_reference.py`, `settings_reference.py --check`              | 生成された設定リファレンスが設定のソースと異なる、またはフィールドに対応する行がない                                                                                                           |
| ドキュメントリファレンス        | `tests/unit/test_docs_reference.py`, `docs_reference.py --check` (`make lint`)        | `docs/reference/` 配下の生成領域が古い、または op やレンダラーに対応する行がない                                                                                                               |
| 標準 env とドキュメントの一致   | `tests/unit/test_stock_env_matches_docs.py`                                           | クイックスタートに記載された標準の `.env` の値が `.env.example` とずれている、または記載された compose コマンドに `--env-file .env` がない                                                       |
| 関数の長さ                      | `scripts/gates/function_length_gate.py` (lint)                                        | ファイルに長すぎる関数が増える、または一覧にある関数がベースラインを超えて長くなる                                                                                                             |
| mypy ラチェット                 | `scripts/gates/mypy_gate.py` (`make typecheck`)                                       | いずれかのファイルの strict mypy エラー数が `scripts/gates/mypy_baseline.json` を超える                                                                                                        |
| シークレット                    | `scripts/gates/check_no_secrets.py` (lint), `tests/unit/test_no_secrets_guard.py`     | 追跡対象のファイルに認証情報の形をした値がある                                                                                                                                                 |
| ダッシュボードのアクセシビリティ | `biome lint frontend/src` (`make lint`、CI ジョブ `format`)                           | ダッシュボードの `.tsx` にアクセシビリティ違反が追加される                                                                                                                                     |
| プリンシパル間リーク            | `tests/integration/test_cross_principal_leakage.py`                                   | 別のテナントとして実行した読み取り op が、カナリア nonce を返す、または 500 を返す。ペイロードは各 op の入力モデルから合成されるため、新しい読み取り op は追加された日からプローブされる         |
| ワーカーレジストリのスナップショット | `tests/unit/test_worker_registry_snapshot.py`, `snapshots/worker_registry.json`  | `defaults`、`all_on`、`production` プロファイルで、有効なワーカーの集合、順序、実行間隔、デッドラインが変わる                                                                                   |
| ワーカーレジストリの整合性      | `tests/unit/test_worker_registry_parity.py`                                           | `/v1/admin/workers.status` が一覧するワーカーと、スーパーバイザーが実行するワーカーが異なる                                                                                                    |
| マイグレーションの加算性        | `tests/unit/test_migrations_additive.py`                                              | マイグレーションがテーブルやカラムを削除またはリネームする、または宣言された再構築以外でテーブルを交換する                                                                                     |
| マイグレーション番号            | `tests/unit/test_migration_numeric_order.py`                                          | 2 つのマイグレーションファイルが同じ番号を持つ                                                                                                                                                 |
| スキーマチェック                | `schema-check` ジョブ：strict ドライラン、続いて `make schema-check`                  | 台帳にリポジトリに存在しないファイルがある、または `schemas/schema.sql` が全マイグレーションのリプレイ結果と異なる                                                                             |
| CLI サーフェスのスナップショット | `tests/unit/test_cli_surface_snapshot.py`, `snapshots/cli_tree.json`, `cli_help.json` | `loom` のパーサー、引数、デフォルト値、`--help` のテキストのいずれかが変わる                                                                                                                   |
| op 到達性の集計                 | `tests/unit/test_op_reach_census.py`, `op_reach_census.py --check`                    | 生の HTTP 以外に到達経路のない op が experimental ではない、ダッシュボードの呼び出し箇所も `server/operations/dashboard.py` のエントリもない op がある、または `reach_manifest.py` が古い        |
| op のサーフェス                 | `tests/unit/test_op_surfaces.py`                                                      | op の MCP ルーティング、非表示 op の拒否、`server/operations/dashboard.py` のエントリが、その op の到達性と一致しない                                                                           |
| MCP 一覧のバジェット            | `tests/unit/test_mcp_listing_budget.py`                                               | デフォルトの `tools/list` がトークン上限を超える、またはモデルが呼び出せないツールを指し示す                                                                                                   |
| 設定へのアクセス                | `tests/unit/test_settings_attribute_access.py`                                        | コードが `getattr(settings, "<field>", default)` で設定を読み取る                                                                                                                              |
| 生成された SDK とプラグインのコピー | `sdk_py_gen.py`, `sdk_ts_gen.py`, `agent_plugin_sync.py` の `--check` (lint)      | 生成されたファイルがそのソースと異なる（[コード生成](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)）                                                       |
| フォーマット                    | `format` ジョブ、および `lint` 内の `ruff format --check`（ローカルでは `make format-check`） | ruff、Biome、Prettier、Taplo、shfmt がファイルを変更することになる                                                                                                                             |

意図した変更に対してスナップショットを再生成するには、次を実行します。

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
LOOM_UPDATE_WORKER_SNAPSHOT=1 python -m pytest tests/unit/test_worker_registry_snapshot.py
python scripts/gates/function_length_gate.py --update   # only ever lowers the baselines
python scripts/gates/mypy_gate.py --update
```

ラチェットのベースラインは上がりません。`--update` はより低い数値のみを書き込むため、負債がファイル間で移動することはありません。

<a id="the-ci-workflow"></a>

## CI ワークフロー

`.github/workflows/ci.yml` は、`main` または `release/X.Y` ラインへのすべてのプルリクエストと、いずれかへのプッシュで実行されます。Python 3.12（`sdk-python-package` はほかのバージョンも追加します）と Node 22 を使います。プルリクエストへ新しくプッシュすると、そのプルリクエストで実行中のワークフローはキャンセルされます。`main` またはリリースラインへのプッシュは、それぞれ専用の concurrency グループで最後まで実行されるため、あるマージコミットでの失敗が次のコミットに隠れることはありません。すべてのアクションは完全なコミット SHA に固定され（バージョンはコメントに記載）、ワークフローはジョブにリポジトリへの読み取り専用アクセス（`permissions: contents: read`）だけを与えます。

| ジョブ | 実行内容 |
| ---------------------- | ---------------------------------------- |
| `clickhouse` | `dev/clickhouse_release.sh` が指定する ClickHouse リリースを解決し、`unit`、`schema-check`、`integration` が同じものをテストするようにします |
| `lint` | `ruff check`、`ruff format --check`、`sdk_py_gen.py`、`sdk_py_client_gen.py`、`sdk_py_admin_gen.py`、`agent_plugin_sync.py`、`settings_reference.py`、`settings_schema.py`、`docs_reference.py`、`op_reach_census.py` の `--check`、SDK に対する `mypy --strict`、mypy ラチェット、関数長ゲートとシークレットゲート |
| `unit` | `pytest tests/unit` を 6 つのパート（`LOOM_TEST_SHARD`）に分け、それぞれ専用のランナーで、ランダム順、全コア、カバレッジ付きで実行します |
| `unit-coverage` | 各パートのカバレッジを結合して（下限 40%）モジュールごとの下限を確認し、そのあと `pytest bench` と汎用インテグレーションのサンプル（`integrations/generic/test.sh`）を実行します。`shard-weights` アーティファクトは単体テストファイルごとの実行秒数で、パート間の実行時間がずれてきたら `tests/shard_weights.json` としてコミットします |
| `sandbox` | 実際の bubblewrap 上で、悪意あるリポジトリジョブに対するサンドボックスのテストスイートを実行します。スキップは失敗になります（`LOOM_TEST_REQUIRE_SANDBOX=1`） |
| `schema-check` | 新規の ClickHouse（`dev/ci_start_clickhouse.sh`）、すべてのマイグレーション、strict ドライラン、`make schema-check` |
| `ts-typecheck` | ダッシュボードのビルド（`tsc -b` + Vite）とバンドルバジェット（`npm run budget`） |
| `format` | 固定バージョンの Biome、Prettier、Taplo、shfmt によるチェックと、`frontend/src` に対する Biome のアクセシビリティ lint |
| `helm` | すべてのオーバーレイに対する `helm lint --strict` と kubeconform、`deploy/okf-sync/cronjob.yaml` に対する kubeconform、チャートが拒否すべき values の確認、`scripts/gates/helm_extractor_isolation.py`、`helm_metadata_egress.py`、`helm_assistant_isolation.py` |
| `agent-plugins` | プラグインのフックと grep シムに対する `node --test`、Cursor フックのサンプル（`integrations/cursor/test.sh`）、両プラグインとシムのすべてのフック、ライブラリ、`bin/` ヘルパースクリプトの構文チェック、grep シムのエントリポイントが実行可能であることの確認 |
| `frontend-vitest` | vitest（ユニット、コンポーネント、axe）を、別々のランナーでパートに分けて実行します |
| `frontend-e2e` | Playwright を、別々のランナーでパートに分けて実行します |
| `integration` | 各パートが専用の ClickHouse、マイグレーション、リファレンス抽出器、起動済みサーバー（フェイクプロバイダー、ワーカーはオフ）を持ちます。番号付きのパートは `pytest tests/integration`（`LOOM_TEST_SHARD`）を分割し、分離ゲートのパートはプロバイダーのステージングテスト、認証オンのゲート、`pytest tests/analyzer` を扱います |
| `integration-postgres` | `LOOM_STORAGE_AUTHORITY=postgres` のサーバーに対し、メモリ、ナレッジ、分離、冪等性、消去、復元 ID のスイートと認証オンのゲートを、同期スタンバイ付きの Postgres 16 と 18 で実行します（[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)） |
| `mixed-version` | `scripts/gates/mixed_version.py`：N-1 と N を 1 つの ClickHouse 上に並べてビルドします。N-1 は `HEAD~1` で、プルリクエストでは、そのマージコミットの第 1 親（マージ時点での対象ブランチ）、プッシュでは直前のコミットです |
| `sdk-typescript` | `sdk_ts_gen.py --check`、`npm run check`（lint、型、バージョン、README の例、テスト）、`npm run check:dist` |
| `sdk-python-adapters` | `make test-sdk-adapters`：フレームワークが SDK の extra であるアダプターごとに、そのフレームワークを入れた専用の venv（`uv.lock` の外）で、テストと、そのモジュールおよび extra を必要としないアダプター（`__init__.py`、`langgraph.py`、`strands.py`）に対する `mypy --strict` を実行します。Python 3.12 と、直接の要件（SDK 自身、extra、pytest）をすべて下限バージョンにした 3.11 で実行します |
| `sdk-python-package` | uv で SDK の wheel をビルドし、クリーンな venv にインストールして `scripts/gates/sdk_wheel_smoke.py` を実行します。Python 3.11、3.12、3.14 に加え、宣言されたすべての依存関係を下限バージョンにした 3.11 と 3.14 でも実行します |

`.github/workflows/plugin-platforms.yml` は、変更が `integrations/`、プラグインマーケットプレイス、`.gitattributes`、またはこのワークフロー自体に及ぶときに実行されます。Linux（x86_64、arm64、Alpine）、macOS（Apple silicon、Intel）、Windows（x86_64、arm64）、WSL 2（Ubuntu、Node 18）でプラグインと grep シムのスイートを実行し、続いてチェックアウトからプラグインをインストールした実際の Claude Code と Codex のセッションを実行します（Claude Code プラグインでの `tests/e2e/agent-session.mjs`：ローカルのフェイクがモデル API と Loom の両方に応答します）。また、Ubuntu と macOS のランナーではエージェントの `PATH` から `node` を外した状態で、Ubuntu、macOS、Windows のランナーでは `node` がまったくない状態（すべてのフックがターンをブロックせずに失敗します）で、Windows では Git Bash なしでも実行します。

`.github/workflows/codeql.yml` は、`main` または `release/X.Y` ラインへのプルリクエスト、それらへのプッシュ、毎週、および手動で CodeQL を実行します。Python、JavaScript/TypeScript、ワークフローそれぞれに `codeql (<language>)` チェックが 1 つあります。検出結果はコードスキャンアラート（リポジトリの Security タブ）になり、メンテナーが振り分けます。このチェックはマージをブロックしません。対象から除外するパスとその理由は、ワークフローのヘッダーに記載されています。

`.github/workflows/scorecard.yml` は、`main` へのプッシュ、毎週、および手動で OpenSSF Scorecard のチェックを実行します。README のバッジに表示されるスコアを公開し、検出結果をコードスキャンに追加します。プルリクエストでは実行されず、マージもブロックしません。

`.github/workflows/docs-site.yml` は、`docs/`、`mkdocs.yml`、`llms.txt`、`llms-install.md`、`scripts/docs_site/`、`scripts/codegen/page_summary.py`、またはこのワークフロー自体に触れる `main` へのプルリクエストと、`main` へのすべてのプッシュで `mkdocs build --strict` によりドキュメントサイトをビルドします。そのため、壊れたリンクやアンカーは `docs site / build` チェックで失敗します。リポジトリ変数 `DOCS_SITE_ENABLED` が `true` の間だけ、`main` から GitHub Pages にサイトを公開します。

別のブランチに積み重ねたプルリクエストは、`main` または `release/X.Y` ラインに付け替えるまでチェックが実行されません。マージコンフリクトのあるプルリクエストも同様です。プッシュ後に「no checks reported」と表示されたら、たいていはこのどちらかが原因です。

<a id="related"></a>

## 関連情報

- [ローカル開発](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md)、[コード生成](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md)、[マイグレーション](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)
- [ベンチマークの方法論](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)
