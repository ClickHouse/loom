<!-- i18n: source=docs/architecture/server-layout.md blob=a488f9e3096ee9ff0f09303b9a382a9ce950bac7 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md), which is authoritative.

<a id="server-layout"></a>

# サーバーのレイアウト

`server/` はドメインごとに構成されています。各プロダクトプレーンは 1 つのパッケージを持ち、そのパッケージにワイヤ宣言、ハンドラー、純粋なロジック、ClickHouse へのアクセス、バックグラウンドループがまとまっています。import が向けられる方向はテストによって固定されています。このページは、新しいモジュールの配置先を決めるとき、およびコントラクトテストが出す import エラーを読み解くときの地図となります。

<a id="packages"></a>

## パッケージ

| パッケージ                    | 担当                                                                                                                                                                                                        | インポートできるもの                                                                                        |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/main.py`              | アプリの配線：lifespan、ルーター、ワーカー、トランスポート。`server/boot.py`、`server/health.py`、`server/wiring.py`、`server/worker_main.py`、`server/__init__.py` とともに、ルート直下にある唯一のモジュール群 | すべて                                                                                                      |
| `server/boot.py`              | lifespan の起動処理と停止処理：直列化された起動時マイグレーション、リーダーのトラッカー名前空間の行、失効スナップショットとキーリング、スーパーバイザーのリースループ、書き込みのドレイン                    | すべて                                                                                                      |
| `server/health.py`            | `GET /v1/health`：複数ティアのヘルス応答（ClickHouse、プロバイダー、起動スナップショット、キャリブレーション、レートリミッター、要求時にはワーカーとオブザーバビリティ）                                    | すべて                                                                                                      |
| `server/wiring.py`            | `wire()`：各プラットフォームの接合部にドメイン側の部品を渡す（認証ゲートの名前空間ディレクトリ、状態ゲージの事実情報）。`main.py`、CLI、テストスイートから実行される                                        | すべて                                                                                                      |
| `server/worker_main.py`       | ワーカー子プロセスのコンポジションルート（`python -m server.worker_main`）：スーパーバイザーとそのワーカーが使うもの。サービス提供用の lifespan は含まない                                                | すべて                                                                                                      |
| `server/core/`                | 横断的なインフラ：ClickHouse クライアント（`ch`）、`errors`、バジェット、日時、ハッシュ、命名、カーソル、冪等性、レート制限、ベクトルストレージポリシー、`schema/`（マイグレーションと DDL ツール）            | `core`、`settings`                                                                                          |
| `server/settings/`            | `catalog.py` で宣言されるすべての設定。領域ごとに 1 セクションで、`Settings` に合成される                                                                                                                  | `core`、`settings`                                                                                          |
| `server/operations/`          | レジストリの中核：`Operation` と `Role`（`base.py`）、フラット化されたレジストリ（`registry.py`：`ALL_OPERATIONS`、`OPERATIONS_BY_NAME`）、`toolsets.py`、読み取りプロジェクション、リーチ集計                | `core`、プラットフォームパッケージ、各ドメインの `api`                                                      |
| `server/transport/`           | HTTP と MCP：アドミッション、リクエストのバインド、一覧、プライマー                                                                                                                                         | `core`、プラットフォームパッケージ。ドメインの `api`、`app`、`domain`（`store` と `workers` は不可）。`cli` は不可 |
| `server/cli/`                 | `loom` コマンド。外側のアダプター                                                                                                                                                                           | すべて                                                                                                      |
| `server/workers/`             | ワーカーレジストリ（`registry.py`）、スーパーバイザー、パスコンテキスト、ステータスとそのストア、初回パスのスケジューリング、コストイベント。ワーカーのループは各ドメインにある                            | `core`、プラットフォームパッケージ。ドメインの `workers`（レジストリのモジュール文字列）                    |
| `server/observability/`       | 台帳、イベント、監査行、メトリクス、トレーシングのエクスポート                                                                                                                                              | `core`、プラットフォームパッケージ                                                                          |
| `server/auth/`                | トークンの発行と検証、ロールと名前空間のゲート、リクエストごとのアイデンティティ                                                                                                                            | `core`、プラットフォームパッケージ                                                                          |
| `server/embedding/`、`llm/`   | 差し替え可能な埋め込みプロバイダーと LLM プロバイダー                                                                                                                                                       | `core`、プラットフォームパッケージ                                                                          |
| `server/clients/`、`storage/` | 外向きの HTTP（共有プール、GitHub）、保持期間の句                                                                                                                                                           | `core`、プラットフォームパッケージ                                                                          |
| `server/memory/`              | メモリプレーン：`memory.*`、検索、ask、抽出、ファクト、プロシージャ、プロファイル、ワーキングメモリ、コンテキストパック、競合、ブラックリスト、`loom_search`                                               | [レイヤー](#layers)を参照                                                                                   |
| `server/code/`                | コードプレーン：`code.*`、インデックス（`app/index/`、`domain/index/`、`store/index/`）、そのパーサー（`domain/extract/`）、取り込みとコンパイラティアのワーカー                                           | [レイヤー](#layers)を参照                                                                                   |
| `server/tracker/`             | トラッカー名前空間：`tracker.*`、重複チェック、ライブソースのリーダーとそのクライアント、loom 自身のトラッカーテーブル                                                                                      | [レイヤー](#layers)を参照                                                                                   |
| `server/knowledge/`           | ナレッジドキュメントとチャンク、会話、リポジトリドキュメント                                                                                                                                                | [レイヤー](#layers)を参照                                                                                   |
| `server/erasure/`             | 消去 saga、その集計、連携バックエンド、リカバリーワーカー                                                                                                                                                   | [レイヤー](#layers)を参照                                                                                   |
| `server/tasks/`               | タスクインボックス、実行台帳、それらのウェイクアップループと webhook ループ                                                                                                                                 | [レイヤー](#layers)を参照                                                                                   |
| `server/identity/`            | 名前空間、グラント、履歴ポリシー、読み取りスコープ、制限付きアクセス、ダッシュボードのページ、呼び出し元 API（`/v1/me/*`）                                                                                | [レイヤー](#layers)を参照                                                                                   |
| `server/insights/`            | 運用者向けサーフェス：`ops.*`、`cost.*`、`agents.*`、`audit.list`、`ui.vitals`、`mentor.brief`、`/v1/admin/*` ルート                                                                                        | [レイヤー](#layers)を参照                                                                                   |
| `server/assistant/`           | アシスタント：その LibreChat サービスユーザーとエージェント定義。起動時にセットアップされる                                                                                                                 | [レイヤー](#layers)を参照                                                                                   |

プラットフォームパッケージとは、`core` と上記のその他の非ドメインパッケージ（`settings`、`operations`、`transport`、`cli`、`workers`、`observability`、`auth`、`embedding`、`llm`、`clients`、`storage`）です。`core` と `settings` を除き、各パッケージは他のどのプラットフォームパッケージもインポートできます。表に記載がある場合を除き、ドメインパッケージをインポートするものはありません。

<a id="layers"></a>

## レイヤー

ドメインパッケージは最大で五つのサブパッケージを持ちます。モジュールは主な責務のある場所に置かれ、小さなドメインではサブパッケージが少なくなります。

| レイヤー   | 内容                                                                                                                             | インポートできるもの                                                                                                                                                    |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api/`     | ワイヤー：Pydantic の入力モデルと出力モデル（`models*.py`）、およびドメインの op グループの `Operation` 宣言（`ops*.py`）          | `core`、`operations`、`settings`、任意のドメインの `domain` と `api`、`Operation` がバインドするハンドラのための自身の `app`                                             |
| `app/`     | ハンドラとオーケストレーション：op やワーカーの処理が I/O を伴って行う内容を順に記述する                                           | 自身の `api`、`app`、`domain`、`store`、他ドメインの `app`、`domain` とワイヤーモデル（`api/models*.py`）、`core` と、`transport` および `cli` を除くすべてのプラットフォームパッケージ |
| `domain/`  | 純粋なロジック、型付きの値、ポリシー：ClickHouse もネットワークも使わない                                                        | `core`（その I/O モジュール `ch`、`schema`、`idempotency`、`ratelimit_shared`、`namespace_write_marks` を除く）、`settings`、任意のドメインの `domain`                    |
| `store/`   | ClickHouse のリポジトリと行ビルダー：ドメインが自身のテーブルに対して実行するすべてのステートメント                               | `core`、`settings`、`observability`、`storage`、自身の `store`、任意のドメインの `domain`                                                                               |
| `workers/` | バックグラウンドループ。レジストリの 1 行につき 1 モジュール                                                                     | `app` と同じ。ただし `api` の代わりに `workers` を使う。他ドメインの `app`、`domain` とワイヤーモデル                                                                   |

`domain/` のモジュールのうち一つだけがプロセスを起動します。`server/code/domain/extract/bounded_parse.py` で、`domain/` 内のすべての tree-sitter パーサーがこれを呼び出し、各ソースをまずリソース制限付きのローカル子プロセスでパースします。

意図的に存在しない依存の向きが二つあります。一つ目は、`store/` は `app/` を決して呼び出さないことです。リポジトリは値を受け取って行を返すため、app はフェイクのストアに対してテストできます。二つ目は、どのドメインも他のドメインの `store/` を読まないことです。そのドメインの `app/` を呼び出すか、`domain/` の型を読みます。これにより、テーブルの所有者だけがそのテーブルに対するステートメントの書き込み元になります。他のドメインのハンドラを呼び出すために、`app/` と `workers/` はそのドメインのワイヤーモデル（`api/models*.py`）をインポートできますが、`api/ops*.py` は決してインポートできません。`app/` はワイヤーモデルをインポートし、`ops` モジュールは決してインポートしないので、`Operation` 宣言は循環インポートなしにハンドラをバインドできます。

<a id="the-import-contract"></a>

## インポートの規約

`tests/unit/test_package_contracts.py` は、`server/` 配下のすべてのモジュールを `ast` で解析し、各インポート（トップレベル、`if`/`try` の内側にネストされたもの、関数内のもの、`TYPE_CHECKING` 配下のもの）を、両端のパッケージとレイヤーによって分類します。上記のルールで許可されていないインポートがあると、インポートする側とされる側のモジュールを示してテストが失敗します。

現時点で規約に違反しているインポートは、`tests/unit/package_contract_allowlist.txt` に、モジュールパスで `importer -> imported` の形式のキーとして列挙されています。このリストは縮小する一方です。許可されているペアが実際には発生しなくなった場合もテストが失敗するため、インポートを修正する際は、その行を削除します。

これと並行して、もう 1 つのチェックが実行されます。`server.transport` をインポートしてよいのは、`server/transport/`、`server/cli/`、`server/main.py` だけです（`tests/unit/test_transport_layering.py`）。ドメインモジュールが `server.cli` をインポートすることはありません。

<a id="adding-a-module"></a>

## モジュールの追加

1. モジュールが扱うテーブル、op グループ、またはプレーンに対応するドメインを選びます。すべてのプレーンで使われるモジュール（ClickHouse のヘルパーやハッシュ規則など）は `core/` に置きます。2 つのドメインで使われるモジュールは、読み取る行を持つ側のドメインに置き、もう一方のドメインがそれを呼び出します。
2. レイヤーは、誰が呼び出すかではなく、モジュールが何をするかで選びます。I/O を行うもの、または何かを await する `async def` は `app/` か `store/` に置きます。引数だけで決まる関数は `domain/` に置きます。
3. 名前は、パスを繰り返さずに、パッケージ内で何を保持しているかで付けます。`server/erasure/app/erasure_saga.py` ではなく、`server/erasure/app/saga.py` とします。
4. パッケージの `__init__.py` には、そのパッケージが何を所有するかを説明する 1 行の docstring を書きます。名前を再エクスポートするのは、そのパッケージの公開インターフェースである場合に限ります（`server/memory/store/`、`server/code/store/index/`）。
5. バックグラウンドループは、`<domain>/workers/` 内のモジュールと、そのモジュールの完全なパスを記した `server/workers/registry.py` の 1 行で構成します。
6. モック、monkeypatch の対象、レジストリの行、カバレッジの下限値では、モジュールをパスで指定します。再エクスポートを経由せず、モジュールの実際のパスで書きます。
7. サーバーが同梱するファイル（マイグレーション、`schema.sql`、フロントエンドのビルド）は、`__file__` の親ディレクトリを数えるのではなく、`server.core.paths` を通じて探します（`tests/unit/test_repo_paths.py`）。

<a id="extractor-modules"></a>

## Extractor モジュール

`extractor_stamp.STAGE_MODULES` に列挙されたファイルが、コードインデックスのバージョンスタンプです。スタンプは、各ファイルの名前とコードトークンをハッシュ化したものです。import は、取り込み元のパッケージではなく、束縛される名前として数えます。スタンプが変わると、すべてのコード名前空間のインデックスが再導出されます。これらのファイルは `server/code/domain/extract/` に `extractor_stamp.py` とともに置かれます。スタンプを計算する側は自身のディレクトリからこれらを読み込むため、ファイル名は変更できません。言語レーンのモジュールはその隣に置かれます。モジュールをパッケージ間で移動してもスタンプは変わりません。一方、名前の変更、整形（`ruff format` の対象外です）、コードの変更を行うとスタンプが変わります。

<a id="related"></a>

## 関連情報

- [システム概要](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) — コントロールプレーンのコンポーネントとリクエストの流れ。
- [コードインデックスのアーキテクチャ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md) — extractor スタンプと再インデックス。
- [リクエストパス](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md#request-path) — リクエストがトランスポート、レジストリ、ハンドラを通る経路。
