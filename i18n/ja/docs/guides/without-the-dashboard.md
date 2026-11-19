<!-- i18n: source=docs/guides/without-the-dashboard.md blob=d950ac1fe252e9d585139b1da802185c47ab7aa9 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md), which is authoritative.

<a id="use-loom-without-the-dashboard"></a>

# ダッシュボードを使わずに Loom を使う

`/me` ダッシュボードでできることは、すべてターミナル、スクリプト、またはエージェントのシェルから実行できます。`loom login` で一度サインインしたら、`loom call` または Python SDK を使います。このページでは、ダッシュボードの各ビューに対応するコマンドと SDK 呼び出しを示します。ここに載っていないビューも、すべて通常のオペレーションとして実行できます（`loom call --list`）。

<a id="sign-in"></a>

## サインイン

```console
$ loom login --server https://loom.example.com   # approve on any browser: password or SSO
$ loom whoami
$ loom logout
```

`loom login` は、自動的に更新されるセッションを保存します。[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)のルーターの背後では、`loom login --org <org>` はその組織専用のセルにサインインします。ルーターの `GET /cells/lookup` に問い合わせ、ルーターまたはセルがまだ存在しない場合は指定されたサーバーをそのまま使います。429 が返った場合は最大 `--wait` 秒（デフォルトは 120）待機し、それ以外の場合はサインインを開始せずに拒否します。その後のセッションはセルのホストのものになるため、`--server` または `LOOM_BASE_URL` に、表示されたホストを指定してください。CI やサービスにはコードを承認する人がいないため、ボット（後述）としてサインインするか、`LOOM_API_KEY` にスコープ付きトークンを設定します。[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook)を参照してください。SDK のみがインストールされている場合は、同じコマンドを `python -m loom login` のように実行します。

<a id="bots-and-ci-jobs"></a>

## ボットと CI ジョブ

ボットには `loom login` は不要です。一度登録しておき（`loom bot create`、またはプラットフォーム管理者による `loom call /v1/admin/bots.create`）、`LOOM_CLIENT_ID` と、秘密鍵（`LOOM_CLIENT_KEY_FILE`、`LOOM_CLIENT_KEY`、または `LOOM_CLIENT_KEY_SECRET` に指定した AWS Secrets Manager のシークレット）のいずれか、CI ジョブの場合は `LOOM_WORKLOAD_AUDIENCE` を指定して実行します。
SDK はトークンの取得と更新を自動で行います。有効期間は 1 時間で、ボットの上限がそれより短い場合はその値になります。詳細は[シングルサインオン](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md#bots-and-ci)を参照してください。

<a id="loom-call-the-whole-api"></a>

## `loom call`: API 全体を呼び出す

```console
$ loom call --list                          # every operation and the role it needs
$ loom call memory.search query="deploy window" top_k:=5
$ loom call grants.approve namespace=support subject=bob@example.com decision=approve
$ loom call /v1/me/service_accounts         # a route outside the operation registry
```

CI やエージェントで使う自分用のトークンには、専用のコマンドがあります。

```console
$ loom token create --name ci --namespace-role support=writer --namespace docs --ttl-days 0
$ loom token list
$ loom token revoke --name ci
```

- **オペレーション名** は `/v1/<op>` に POST されます。`org` と `namespace` はトークンから取得されます。`--org` と `--namespace` を指定すると、それらを上書きできます。
- **パス**（`/` で始まるもの）は、指定されたとおりに送信されます。本文がなければ GET、あれば POST です。
- **フィールド** は、テキストの場合は `key=value` で、入力したとおりに送信されます。それ以外の値には `key:=json` を使います（`top_k:=5`、`pinned:=true`、`tags:='["a","b"]'`）。`--data` は JSON オブジェクト全体を受け取り、`@file` でファイルを、`-` で標準入力を指定できます。
- **出力：** 応答は JSON として出力されます。サーバーからのエラーは、コードを含む 1 行が stderr に出力され、終了コード 1 で終了します。コマンド自体の誤り（未知のオペレーションや不正な形式のフィールドなど）は、終了コード 2 で終了します。

各オペレーションのフィールドは、[オペレーションリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)に一覧があります。

Python では、`agent.call("<op>", {...})` ですべてのオペレーションを呼び出せます。また、すべてのオペレーションは `agent.client` のメソッドとしても利用できます。レジストリ外のルートは、`agent.account` と `agent.deployment` にあります。

<a id="dashboard-view--command-and-sdk-call"></a>

## ダッシュボードのビューとコマンド・SDK 呼び出しの対応

| ダッシュボード                    | `loom call`                                                                                                                    | SDK (`agent = loom.Agent(org, namespace, name)`)                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| メモリ：検索、閲覧、編集          | `memory.search`, `memory.list`, `memory.set`, `memory.update_by_id`, `memory.delete`                                           | `agent.memory.search(...)`, `.list(...)`, `.set(...)`                                                                 |
| ナレッジ：アップロード、閲覧、ダウンロード | `knowledge.add`, `knowledge.list`, `knowledge.search`, `/v1/knowledge.download?org=…&namespace=…&source_uri=…`                 | `agent.knowledge.add(...)`, `.list()`, `.download(source_uri)`                                                        |
| 名前空間：作成、設定              | `namespace.register`, `namespace.get`, `namespace.list_mine`, `policy.set`, `blacklist.add`                                    | `agent.namespaces.*`, `agent.policy.*`, `agent.blacklist.*`                                                           |
| 名前空間への参加                  | `namespace.browse`, `namespace.join`, `namespace.leave`                                                                        | `agent.client.namespace.join(...)`                                                                                    |
| 参加リクエストの承認              | `grants.pending_for_me`、続けて `grants.approve namespace=… subject=… decision=approve`（または `reject`）                     | `agent.client.grants.pending_for_me()`, `agent.client.grants.approve(...)`                                            |
| メンバーと権限付与                | `grants.list`, `grants.create`, `grants.revoke`                                                                                | `agent.grants.*`                                                                                                      |
| レビューキュー（競合）            | `conflicts.list`, `conflicts.resolve`, `learnings.review`                                                                      | `agent.conflicts.list()`, `.resolve(...)`                                                                             |
| アクティビティ、監査ログ          | `profile.activity`, `audit.list`, `agents.connected`, `mentor.brief`, `memory.exposure_summary`, `memory.owner_report`         | `agent.audit.list(...)`, `agent.profile.*`                                                                            |
| コストと予算                      | `cost.summary`, `cost.budget_status`                                                                                           | `agent.cost.summary(...)`                                                                                             |
| 実行                              | `run.list`, `run.get`                                                                                                          | `agent.run.*`                                                                                                         |
| コードインデックス                | `code.index_status`, `code.repo.add`, …                                                                                        | `agent.code.*`                                                                                                        |
| 消去、忘却                        | `erasure.request`, `erasure.status`, `erasure.list`, `erasure.export`, `memory.clear`                                          | `agent.erasure.*`                                                                                                     |
| タスクと受信トレイ                | `task.create`, `task.pull`, `task.updates`, `inbox.*`                                                                          | `agent.tasks.*`                                                                                                       |
| トラッカーソース                  | `tracker.source.add`, `tracker.source.list`, `tracker.item`, …                                                                 | `agent.tracker.*`                                                                                                     |
| グラフ（エンティティとファクト）  | `facts.traverse`, `facts.*`                                                                                                    | `agent.facts.*`                                                                                                       |
| 会話とセッションブリーフ          | `conversation.*`, `session.brief_*`                                                                                            | `agent.conversations.*`, `agent.briefs.*`                                                                             |
| 設定：トークン                    | `/v1/me/service_accounts`, `/v1/me/service_accounts.create`, `/v1/me/service_accounts.revoke`, `loom token create`             | `agent.account.tokens()`, `.create_token(...)`, `.revoke_token(name)`                                                 |
| デフォルトの名前空間              | `/v1/me/set-default-namespace namespace=…`                                                                                     | `agent.account.set_default_namespace(...)`                                                                            |
| 共有の概要                        | `/v1/me/sharing-summary`                                                                                                       | `agent.account.sharing_summary()`                                                                                     |
| 管理：ヘルス                      | `/v1/health`, `ops.health`                                                                                                     | `agent.deployment.health()`                                                                                           |
| 管理：バックグラウンドワーカー    | `/v1/admin/workers.status`                                                                                                     | `agent.deployment.workers_status()`                                                                                   |
| 管理：モデルとプロバイダー        | `/v1/admin/models`, `/v1/admin/models/validate`, `/v1/admin/models` (POST), `/v1/admin/models/reset`, `/v1/admin/models/retry` | `agent.deployment.models()`, `.validate_models(...)`, `.save_models(...)`, `.reset_models(...)`, `.retry_models(...)` |
| 管理：ユーザー、クレームの解除    | `admin.users`, `/v1/admin/claim_release`                                                                                       | `agent.client.admin.users()`, `agent.deployment.release_claim(...)`                                                   |

ダッシュボードと同じロールが適用されます。参加の承認には、その名前空間の admin 権限が必要です。管理ビューには、組織またはプラットフォームの admin が必要です。
