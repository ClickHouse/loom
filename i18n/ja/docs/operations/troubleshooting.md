<!-- i18n: source=docs/operations/troubleshooting.md blob=3ede5f4702a909ff463f5c011fadcc17a512eb56 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md), which is authoritative.

<a id="troubleshooting"></a>

# トラブルシューティング

各項目では、症状、原因を確認するためのチェック、そして対処方法を示します。1 件のリクエストを台帳、クエリログ、トレースにわたって追跡するには、[リクエストのデバッグ](https://github.com/ClickHouse/loom/blob/main/docs/guides/debugging.md)を参照してください。

<a id="start-here"></a>

## はじめに

ログを読む前に、次の 3 つのコマンドでほとんどの疑問に答えが出ます。

```bash
curl -s http://localhost:7777/v1/health | jq '{status, ch_ok, ch_error, schema, problems}'
docker exec loom-server loom doctor              # the same problems, plus pre-boot checks
curl -s -XPOST http://localhost:7777/v1/ops.health -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"org":"<org>"}' | jq   # ClickHouse parts, workers, stubs (ORG_ADMIN)
```

`problems` の各エントリには `fix` が含まれています。まずそれを読んでください。クライアントマシンでは、`loom doctor --client` が、SDK とフックが解決するサーバー URL とトークンを確認します。各フィールドの意味については、[モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#health-checks)を参照してください。

<a id="server-and-clickhouse"></a>

## サーバーと ClickHouse

<a id="every-operation-fails-and-v1health-returns-503"></a>

### すべての操作が失敗し、/v1/health が 503 を返す

- **原因：** サーバーから ClickHouse に到達できません。起動も最初のマイグレーションで停止します。
- **確認：** `curl -s localhost:7777/v1/health | jq .ch_error` を実行し、続けて `docker exec loom-server loom doctor`（`clickhouse_reachable` の行）を実行します。
- **対処：** `LOOM_CH_HOST` / `LOOM_CH_PORT` / `LOOM_CH_USER` / `LOOM_CH_PASSWORD` を修正します。ポート 8443 は TLS を前提とします。それ以外のポートで TLS を使う場合は `LOOM_CH_SECURE=true` を設定します。ClickHouse Cloud では、サービスが稼働していること（アイドル状態のサービスは一時停止します）と、IP 許可リストに Loom のホストが含まれていることを確認します。

<a id="loom-up-times-out-or-the-container-exits-at-boot"></a>

### loom up がタイムアウトする、またはコンテナーが起動時に終了する

- **原因：** 多くの場合、マイグレーションまたは ClickHouse のエラー、`.env` の値の不正、あるいは compose ファイル外のプロセスで `LOOM_INSTANCE_LABEL` が未設定であることが原因です。
- **確認：** `docker logs loom-server` を実行します。失敗したマイグレーションファイルや設定名がログに記載されています。env ファイルは `docker compose --env-file .env -f dev/docker-compose.cloud.yml config >/dev/null` で検証します（起動に使う compose ファイルを指定してください）。
- **対処：** ログに出ているエラーに対処します。認証が有効で、リポジトリに含まれる開発用の `LOOM_JWT_SECRET` を使った状態のデプロイ（`LOOM_DEPLOYMENT=1`）は、仕様として起動を拒否します。実際のシークレットを設定してください（[デプロイ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#required-environment)）。

<a id="writes-fail-with-too_many_parts-or-merges-are-failing"></a>

### 書き込みが TOO_MANY_PARTS で失敗する、またはマージが失敗している

- **症状：** 書き込みが ClickHouse エラー 252 `TOO_MANY_PARTS` で失敗する、または `ops.health` が `clickhouse_parts: fail` を報告します。
- **確認：** マージが失敗していないかを確認します。原因は書き込み頻度ではなく、ほとんどの場合マージの失敗です。まず `system.part_log` のマージエラーを読みます。

  ```sql
  SELECT table, error, count() AS failures, any(exception)
  FROM system.part_log
  WHERE database = 'loom' AND event_type = 'MergeParts' AND error != 0
    AND event_time > now() - INTERVAL 1 HOUR
  GROUP BY table, error;
  ```

- **対処：** 例外メッセージでサーバーがマージできないスキップインデックスが示されている場合は、そのインデックスをマージ時のマテリアライズから除外します。この設定はメタデータのみの変更で、元に戻せます。挿入されたパートでは引き続きインデックスが構築されます。

  ```sql
  ALTER TABLE loom.<table> MODIFY SETTING
    exclude_materialize_skip_indexes_on_merge = '<existing list>,<index name>';
  ```

  マージが成功するようになれば、パート数は数分で減少します。先にインデックスを削除しないでください（後で再構築するとテーブルが書き換えられます）。また、`parts_to_throw_insert` を引き上げるだけにしないでください。マージが止まっている間はパート数が増え続けます。マージが失敗していない場合は、1 ステートメントにつき 1 行ずつ挿入しているライターを探します（`system.query_log` を `log_comment` で集計します。[クエリの帰属](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#logs-traces-and-query-attribution)を参照）。

<a id="writes-take-seconds-after-a-clickhouse-cloud-replica-restart"></a>

### ClickHouse Cloud のレプリカ再起動後に書き込みに数秒かかる

- **症状：** 読み取りは高速なまま、書き込みの多い操作（`memory.set`、`run.*`、`conversation.append`）に数秒かかります。エラーは出ません。デプロイ直後に起きることが多いものの、デプロイは原因ではありません。
- **確認：** レプリカの稼働時間と、システムログの再アタッチが進行中かどうかを確認します。

  ```sql
  SELECT hostName(), uptime() FROM clusterAllReplicas(default, system.one);
  SELECT count() FROM system.query_log
  WHERE event_time > now() - INTERVAL 15 MINUTE AND query ILIKE 'ATTACH TABLE system.%';
  ```

- **対処：** Loom 側で行うことはありません。再起動後しばらくの間、サービスはシステムテーブルを再アタッチしており、それが完了すると挿入は回復します（通常は 1 時間以内）。この件を理由にデプロイをロールバックしないでください。解消しない場合は ClickHouse Cloud のサポートに問い合わせてください。

<a id="requests-fail-with-backend_timeout-or-overloaded"></a>

### リクエストが BACKEND_TIMEOUT または OVERLOADED で失敗する

- **原因：** ClickHouse のステートメントがクラスごとのデッドラインを超えた（504 `BACKEND_TIMEOUT`）か、プロセスのプール済み ClickHouse 接続がすべて使用中のままだった（503 `OVERLOADED`）場合です。書き込みが持続的に多いと、バッチ挿入（監査行、トランスクリプトのターン、agent_events のフラッシュ）は最大 `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS` の間、さらにフラッシュの間も接続を保持します。バックグラウンドのバッチ挿入（usage-event と write-mark のフラッシュ）は最大 `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` の間、さらにフラッシュの間も接続を保持します（[バッチ挿入](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#batched-inserts)）。`OVERLOADED` は、ステートメントが、サーバーまたは ClickHouse ユーザーのメモリ不足に遭遇した場合にも発生します（コード 241。すべてのクエリで共通です。サーバーログに ClickHouse の行を伴う `ClickHouse is short of memory` が 5 秒に 1 回出力されます）。`conversation.delete` または `conversation.compact` では、`OVERLOADED` は、スレッドの埋め込みへの書き込みが 8 回のリコンサイルラウンドにわたって続いたことも意味します。スレッドの書き込みが落ち着いてから再送してください。
- **確認：** `system.query_log` で `log_comment` ごとに最も遅い発行元を確認します（[モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#logs-traces-and-query-attribution)）。パートの圧迫は `ops.health` で確認します。メモリについては、`system.query_log` の `exception_code = 241` のステートメントと、その近くにある最大の `memory_usage` を、サーバーの `max_server_memory_usage` およびユーザーの `max_memory_usage_for_user` と照らし合わせます。
- **対処：** 支配的になっているクエリまたはワーカーに対処します。メモリの場合は、そのステートメントに対処するか、ClickHouse サービスのメモリを増やします。ClickHouse から遠い Loom（WAN 越し）では、レイテンシ予算が呼び出しのたびに発動しないよう、`LOOM_LATENCY_BUDGET_MULTIPLIER` を 20 前後にする必要があります。書き込みが持続的に多い状況での `OVERLOADED` では、バッチ挿入が保持する接続数に合わせてプールのサイズを決めます（[バッチ挿入](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#batched-inserts)）。次のいずれかを行います。`LOOM_CH_POOL_MAXSIZE` を引き上げる（再起動が必要です）。切り替え対象のグループで `loom settings set ch_batched_inserts false` を使ってバッチ処理を無効にする（起動時は `LOOM_CH_BATCHED_INSERTS=false`。待機する非同期挿入は引き続き待機します）。`loom settings set ch_batched_insert_wait_max_ms <ms>`（および `ch_batched_insert_background_wait_max_ms`）で挿入の待機時間を実行中に短くする。
  ライブでの上書きについては[ランタイム設定](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)を参照してください。すべてのコードは[エラーリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)に一覧があります。

<a id="providers-and-search-quality"></a>

## プロバイダーと検索品質

<a id="searches-fail-with-503-embed_timeout"></a>

### 検索が 503 EMBED_TIMEOUT で失敗する

- **原因：** 埋め込みプロバイダーが、デフォルトでは 2 回の試行（再試行は 1 回、`LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`）のどちらでも、`LOOM_MEMORY_SEARCH_EMBED_BOUND_S`（2.5 s）以内にクエリの埋め込みを返しませんでした。ベクトルが埋め込みキャッシュにあるクエリは、引き続き応答します。
- **確認：** サーバーログの `query embed exceeded …s (attempt n/m)` という警告と、`error_code = 'EMBED_TIMEOUT'` の `loom.agent_events` の行を確認します。応答しないプロバイダーはヘルスフラグを立てません。`/v1/health` に `provider_failing:<name>` が表示されるのは、プロバイダーがエラーも返している場合だけです。プロバイダーのステータスページも確認してください。
- **対処：** 生の HTTP や MCP の呼び出し元は 1 回再試行します。SDK の呼び出し元はすでに再試行済みなので、バックオフします。繰り返し発生する場合は、プロバイダーが遅いか停止しています。復旧を待つか、遅いながらも応答しているプロバイダーに対しては上限値を引き上げてください。失敗した検索は、1 回の試行につき最大で上限値の時間、サーバーを占有します。

<a id="search-returns-arbitrary-results-or-extraction-writes-nothing"></a>

### 検索が無関係な結果を返す、または抽出が何も書き込まない

- **症状：** `set_from_messages` がファクトを書き込まない、`memory.ask` が空の回答を返す、または検索が文字の重なりで順位付けされる。
- **原因：** プロセスが fake プロバイダーで動作しています。fake に設定されている場合（`LOOM_LLM_PROVIDER=fake`。標準の `.env` はすべてこの設定です。あるいはチェックアウトした `.env` やアップグレードしたインストールの `fake-ngram-*` 埋め込み）、または実プロバイダーに設定されているのにキーがない場合です。
- **確認：** `curl -s localhost:7777/v1/health | jq '.problems[].code'` が `llm_key_missing`、`embedder_key_missing`、`fake_llm`、`fake_embedder` のいずれかを示します（本文では `fake_llm: true`、`fake_embedding: true`）。
- **対処：** problem の `fix` に示されたキー（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY`）と、`.env` のプロバイダーブロックを設定し、コンテナーを再作成します（`docker compose … up -d --force-recreate`）。キーを設定するだけでは、`.env` で `fake` に固定されたプロバイダーは変わりません。ローカルスタックではベクトルの幅がテーブル作成時に固定されるため、fake の埋め込みから切り替えるには `make down-all && make dev` が必要です。fake の埋め込みで埋め込まれた行には `loom reembed` が必要です。次回からこれが `status: degraded` と表示されるように、`LOOM_REQUIRE_REAL_PROVIDERS=true` を設定してください。

<a id="search-quality-dropped-after-an-embedder-change"></a>

### 埋め込みモデルの変更後に検索品質が低下した

- **原因：** 新しい埋め込みモデルにより、すべての類似度スコアが変化します。古いモデルで埋め込まれたままの行は、別のベクトル空間にあります。
- **確認：** `loom reembed --dry-run` が、モデルが異なる行を数えます。`loom.search.top_similarity` で変化を確認できます。
- **対処：** `loom reembed` を実行し（[メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#re-embed-and-re-extract)）、`loom.search.top_similarity` に対するアラートのベースラインを取り直します。`LOOM_ADAPTIVE_THRESHOLDS` が有効（デフォルト）の場合、起動時のプローブの後、類似度のゲートは新しい埋め込みモデルに追従します。`loom recalibrate` を実行すると、すぐに再実行されます。

<a id="a-write-is-missing-from-search-or-was-not-stored"></a>

### 書き込みが検索に出てこない、または保存されなかった

- **確認：** インジェストパイプラインが破棄または切り詰めた内容は `loom drops --namespace NS` で確認し、その書き込み自体のレスポンス（`status`、競合の判定、`pipeline_errors`）も確認します。`loom.embed.pending` は、ベクトル待ちの行を数えます。
- **対処：** ドロップの理由に従ってください（[メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#memory-ingest-drops)）。競合した書き込みは、ダッシュボードの Review キューで解決します（[競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)）。

<a id="auth-and-tokens"></a>

## 認証とトークン

<a id="requests-fail-with-401-auth_invalid-or-503-auth_unavailable"></a>

### リクエストが 401 AUTH_INVALID または 503 AUTH_UNAVAILABLE で失敗する

- **症状：** 401 `AUTH_INVALID`、または `Retry-After: 5` を伴う 503 `AUTH_UNAVAILABLE`。
- **確認：** エラー詳細の `message`（期限切れ、署名不正、失効済み、bearer の欠落）を確認します。クライアントマシンでは、`loom doctor --client` が、フックと SDK が送信するトークンを検査します。
- **対処：**
  - 署名不正は、トークンが別の `LOOM_JWT_SECRET` で発行されたことを意味します（シークレットをローテーションした、または別のデプロイ向けに発行した場合）。`docker exec loom-server loom mint-token …` で再発行してください。
  - 期限切れまたは失効済みのトークンは、新しいトークンが必要です。`loom revoke-token --list` で失効の一覧を確認できます。
  - 起動直後の `AUTH_UNAVAILABLE` は、失効リストがまだ読み込まれていないこと（`revocation_not_loaded`）を意味します。読み込みが完了するまでサーバーはトークンを拒否するため、数秒後に再試行し、続く場合は ClickHouse を確認してください。
  - 環境内に古い `LOOM_API_KEY` が残っていると、SDK とプラグインでは `~/.loom/token` よりそちらが優先されます。
  - 認証を初めて有効にした後は、すべての呼び出しにトークンが必要です。上記の方法で発行してください。

<a id="an-id-you-know-exists-reads-as-missing"></a>

### 存在するはずの id が存在しないものとして読み取られる

- **原因：** `memory.get_by_id` のように組織全体の id だけで対象を指定する op は、id の存在を確認させてしまう 403 ではなく、行が存在しない場合と同じ応答を返します。通常の原因は、その行の名前空間に対する op のロールをトークンが持っていないことです。読み取りの前に id を解決する op も、呼び出し元が同じ id を持つ複数の名前空間に到達できる場合（`namespace.restore` の後など）は、同様に応答します。ルールと例外は[行スコープの操作](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#row-scoped-operations)を参照してください。
- **対処：** その行の名前空間に対する op のロールを持つトークンを使用してください（[トークンと権限付与](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)）。複数の名前空間にまたがる id の場合は、その名前空間内のキーで行を指定してください（`memory.get`、書き込みなら `memory.update`）。

<a id="schema-and-upgrades"></a>

## スキーマとアップグレード

<a id="migrations-pending-or-checksum-drift"></a>

### マイグレーションの保留またはチェックサムのずれ

- **症状:** `/v1/health` が `pending_migrations` を報告する、`loom migrate --dry-run --strict` が終了コード 1 で終了する（保留中のファイルだけでは終了コード 1 にならず、台帳にのみ存在する行やチェックサムのずれがある場合に終了コード 1 になります）、または `redeploy.sh` の事前チェックが停止する。
- **確認:** `loom migrate --dry-run --strict` は、保留中のファイルを表示し、続けて `ledger-only` の行（リポジトリにはもう存在しない台帳上のファイル名）と `checksum drift`（適用済みのファイルのうち、内容が記録された SHA-256 と一致しなくなったもの）を表示します。
- **対処:**
  - 保留中のファイル: `loom migrate` を実行するか、`LOOM_BOOT_MIGRATE=true` を指定して再起動します。
  - チェックサムのずれや ledger-only の行は、いずれかのサーバーが適用した後にマイグレーションファイルが変更された（または名前が変更されたうえで変更された）ことを意味します。多くの場合、マージされていないブランチから実行したサーバーが原因です。適用済みの DDL とリポジトリ内のファイルを比較してください。結果が同一であれば、現在のファイルを記録します（最も新しい行が優先されます）。

    ```sql
    INSERT INTO loom._migrations (filename, checksum, applied_by)
    VALUES ('<file>', '<sha256 of the file>', 'operator');
    ```

    DDL が新しい名前で存在する ledger-only の行は、`DELETE FROM loom._migrations WHERE filename = '<old name>'` で削除できます。

  - 結果が異なる場合は、スキーマを整合させる新しいマイグレーションを作成してください。適用済みのファイルは決して編集しないでください（[マイグレーション](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)）。
  - 「旧レイアウトの行を持つ」テーブルが原因で起動に失敗する場合は、先にそのテーブルに対して `scripts/ops/table_rebuild.py` を実行する必要があります（[アップグレード](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run)）。

<a id="requests-fail-with-503-schema_pending"></a>

### リクエストが 503 SCHEMA_PENDING で失敗する

- **原因:** ステートメントが、このビルドに含まれるマイグレーションによって作成されるテーブルを参照しているにもかかわらず、サービスがそのマイグレーションを適用していません（`LOOM_BOOT_MIGRATE=false` で起動したプロセス、またはマイグレーションの失敗）。
- **対処:** 上記の保留中のマイグレーションの場合と同じです。

<a id="cells"></a>

## セル

<a id="a-process-with-loom_cell-unset-is-gated"></a>

### LOOM_CELL が未設定のプロセスはゲートされる

- **症状：** ヘルスプローブ以外のすべてのリクエストに 503 が返り、`/v1/health/ready` の `cell` チェックが失敗し、ワーカーは1つも動作しません。理由には、デプロイメントが「is a cell in enforced mode and LOOM_CELL is unset」、「holds a partial cell state (... present)」、または「cell state is not read yet」であると示されます。org を宣言する CLI コマンドは、「this deployment is a cell in enforced mode (cell ...) and LOOM_CELL is unset」、「holds a partial cell state and LOOM_CELL is unset」、または「cell state could not be read (...)」というメッセージで拒否されます。最初のメッセージの場合は CLI に `LOOM_CELL` を設定してください。最後のメッセージの場合は、示されている理由に対処してください。つまり、不足している権限（[アップグレードのステップ 15](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure)）か、応答しない Postgres です。
- **原因：** Postgres をオーソリティとする構成では、すべてのサーバープロセスがデプロイメントのセルの状態（`loom.cell_first`、`loom.tenancy_mode`、`loom.cell_self`、`loom.tenancy`）を読み取ります。`LOOM_CELL` が未設定の場合、プロセスがリクエストを処理できるのは、これらのいずれにも行がない間、または最初のセルの完全な状態を `recording` で保持している間だけです（[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-tenancy-check-and-placement)）。
- **確認：** ルーターのディレクトリにこのデプロイメントの行があるかどうかを確認します。ルーターのないデプロイメントでは省略してください。`LOOM_PG_DSN` に対する psql セッションで開始します。

  ```sql
  SELECT deployment_id AS id FROM loom.authority \gset
  \connect '<router DSN>'
  SELECT cell, is_first, state, placed FROM loom_router.cells WHERE deployment_id = :'id';
  ```

- **対処**（「not read yet」の場合）：プロセスが Postgres マイグレーション未適用のまま（`LOOM_BOOT_MIGRATE` が off）起動したか、それ以降 Postgres に到達していません。マイグレーションを適用する（`/v1/health` に一覧が表示されます）か、接続を復旧してください。1秒以内に行われる次の読み取りで、以下のとおりプロセスが開放またはゲートされます。
- **対処**（それ以外の場合）：確認結果に応じて次のとおりです。
  - **デプロイメントがセルである場合**（ディレクトリに行があり、後述の未接続の後続セルでも、切り戻し中の最初のセルでもない）：`LOOM_CELL` にセルの名前を設定します。部分的な状態の場合は `loom cell prepare` をもう一度実行すると完了します。
  - **デプロイメントがセルとして意図されていない場合**（ディレクトリに行がない、またはルーターがない）、または**未接続の後続セル**（行が `is_first = false`、`state = 'attaching'`、`placed = 0`）を単一のデプロイメントに戻す場合：`LOOM_PG_DSN` に対する psql セッション（確認の後は `\connect` で戻ります）で、スキーマ `loom` のテーブルを所有するロール（マイグレーションを行うロール。トリガーの変更にはテーブルの所有者が必要です）として、1つのトランザクションで状態をクリアします。その後、デプロイメントは `open` になります。未接続の後続セルのディレクトリ行は、`loom cell remove --cell <cell>` で削除するまで有効なままです。デプロイメントを新たに prepare するにはこの削除が必要です。ロックの取得に失敗した場合は、psql がまだ開いていれば `ROLLBACK` し、ステップ全体をやり直してください。

    ```sql
    \set ON_ERROR_STOP on
    BEGIN;
    SET LOCAL lock_timeout = '100ms';
    -- the attach lock: held by a running attach, prepare or update
    SELECT pg_advisory_xact_lock(7813586394088235880);
    SELECT pg_advisory_xact_lock(7813586394288842610);  -- the migration lock
    ALTER TABLE loom.cell_first DISABLE TRIGGER cell_first_immutable;
    DELETE FROM loom.cell_first;
    DELETE FROM loom.tenancy_mode;
    DELETE FROM loom.cell_self;
    DELETE FROM loom.tenancy;
    ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_immutable;
    ALTER TABLE loom.cell_first ENABLE ALWAYS TRIGGER cell_first_no_truncate;
    COMMIT;
    ```

    次に、状態のトランザクションの後で（マイグレーションを行う起動の `ensure` がトリガーを再作成しないように）、同じセッション・同じロールで org トリガーと `loom.tenancy` ガードを削除します。ロックの取得に失敗した場合は、psql がまだ開いていれば `ROLLBACK` し、ステップをやり直してください。

    ```sql
    \set ON_ERROR_STOP on
    BEGIN;
    SET LOCAL lock_timeout = '100ms';
    SELECT pg_advisory_xact_lock(7813586394288842610);  -- the migration lock
    SELECT format('DROP TRIGGER %I ON loom.%I', t.tgname, c.relname)
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'loom'
      AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record') \gexec
    COMMIT;
    ```

    状態トランザクションの attach ロックでの失敗は、この Postgres に対して attach、prepare、update のいずれかが実行中であることを意味します。数時間かかることもあり、その間にステップをやり直しても同じように失敗します。ロックを保持しているセッションは次のクエリで特定できます。

    ```sql
    SELECT a.pid, a.usename, a.application_name, a.client_addr, a.backend_start
    FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.granted
      AND l.classid = (7813586394088235880 >> 32)::oid
      AND l.objid = (7813586394088235880 & 4294967295)::oid AND l.objsubid = 1
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database());
    ```

  - **切り戻し中の最初のセル**（行が `is_first = true`、`state = 'attaching'`、`placed = 0` で、`mode` は問いません。ステップ 3 のロックで拒否された attach は `enforced` のまま残るためです。また、その Postgres での `SELECT mode FROM loom.tenancy_mode` は `recording` を返します）：次の順序で切り戻します。まずすべてのプロセスで `LOOM_CELL` を解除します。各プロセスは `recording` と同様に動作します。次に上記の状態トランザクションと削除ステップを実行します。その後、ルーターのオペレーター認証情報とセルの `LOOM_PG_DSN` を使って `loom cell remove --cell <cell>` を実行します。このコマンドは、その Postgres が `open` を示し、行のデプロイメント ID が記録されるまで拒否されます。行を先に削除すると、すべての新しい org に 503 が返ります。状態を先にクリアすると、`LOOM_CELL` が設定されたすべてのプロセスがゲートされます。ルーターはセルの Postgres を読み取れないため、ルーターで行を直接 `DELETE` するとこの確認が省略されます。その場合、prepare 済みのままのセルは新しい org に 503 を返し続け、上記のステップを実行するまで再登録もできません。クリアしたストアにはセルの状態が残らず、デプロイメントは `loom cell prepare` からあらためて prepare します。行が `placed > 0` の最初のセルは先へ進めるだけで、戻すことはできません。attach をもう一度実行してください（削除は拒否されます）。

  - **別のデプロイメントに復元されたセルの Postgres のコピー**は、元のセルのデプロイメント ID を引き継ぐため、確認クエリは稼働中の元のセルの行を返します。クリアしないでください。また、元のセルと並べて稼働させないでください。

<a id="a-cli-with-loom_cell-set-refuses-a-command-that-writes-for-no-org"></a>

### LOOM_CELL を設定した CLI が、org を指定せずに書き込むコマンドを拒否する

- **症状：** org を指定せずに書き込む、または取り消しを行うコマンド（`loom secrets reseal`、`loom settings set`、`loom ch-setup`、`loom revoke-token` など）が、「refusing: this Postgres's first row names cell ..., not LOOM_CELL=...」、「refusing: this cell's state could not be read (...)」、または「refusing: this ClickHouse could not be read for the cell check (...)」で終了します。
- **原因：** `LOOM_CELL` が設定されていると、このようなコマンドは何かを変更する前に、`LOOM_PG_DSN` からセルの `first` 行を読み取り、その Postgres に照らして ClickHouse を確認します（[セル](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md#the-tenancy-check-and-placement)）。first 行が別のセルを指している場合、CLI がそのセルのストアを向いていることを意味します。アクセスを元に戻せない取り消しは、Postgres の障害時でも実行され、セルを確認しなかったことが表示されます。
- **対処：** 拒否メッセージが示す状態は別のセルのものなので、決してクリアしないでください。`LOOM_PG_DSN` と ClickHouse の設定をこのセル自身のストアに向けるか、意図していたセルが first 行の示すセルであれば `LOOM_CELL` をそのセルに設定してください。状態を読み取れなかった場合は、示されている理由に対処してください。つまり、不足している権限（[アップグレードのステップ 15](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#upgrade-procedure)）か、応答しない Postgres または ClickHouse です。

<a id="a-boot-logs-cell-triggers-not-applied"></a>

### 起動時に「cell triggers: not applied」がログに出力される

- **症状：** `open` 以外の状態のセルの起動時に `cell triggers: not applied on loom.<table> (...)` がログに出力され、`loom doctor` がそのテーブルのトリガーの欠落または無効化を示します（`loom.cell_first` についてはどの状態でも、org テーブルのトリガーまたは `loom.tenancy` ガードについては `enforced` の場合）。
- **原因：** 起動時にセルの org トリガーとガードが作成され、`loom.cell_first` のトリガーが always に戻されます。このときロック待ちは1回あたり 100 ms で、1秒間隔で3回試行します。次の2つの場合には、そのテーブルを残したまま処理を続けます。1つは、テーブルがまだビジー状態の場合で、ログには競合するロックを保持または待機していたセッションが示されます。そのテーブルに対する長いトランザクションや一括書き込み、または autovacuum がその例です。短い試行では autovacuum を譲らせることはできず、完了するまで待つことになります（キャンセルできるほど長く待つと、そのテーブルへのすべての書き込みが起動の後ろで待たされます）。もう1つは、マイグレーションを行うロールがテーブルを所有していない場合で、ログに `this role cannot alter it` と出力され、`loom doctor` は `owner` の不具合として示します。後続の起動でテーブルが完了するまで、テナンシーチェックを行わない書き込み元はそのテーブルで拒否されません。Loom 自身の書き込みには影響しません。
- **対処：** ビジー状態のテーブルの場合は、ログに示されたトランザクションが古ければ終了させるか、autovacuum の完了（`pg_stat_progress_vacuum`）を待ちます。ロールが変更できないテーブルの場合は、スキーマ `loom` のテーブルを所有するロールとして起動するか、`ALTER TABLE loom.<table> OWNER TO <role>` でそのロールにテーブルを譲渡します。その後、`LOOM_BOOT_MIGRATE` を on にして1つの Pod を再起動し、`loom doctor` が欠落または無効のトリガーを示さなくなるまで繰り返します。

<a id="the-cell_marker-readiness-check-names-the-tenancy-marker"></a>

### cell_marker のレディネスチェックがテナンシーマーカーを示す

- **症状：** `LOOM_CELL` を設定したプロセスの `/v1/health/ready` が、`cell_marker` チェックで「pooled Postgres sessions do not keep the tenancy marker」を報告します。`enforced` または `partial` のセルでは失敗し、まだ `recording` の最初のセルでは同じ理由で成功し、プロセスのセルのハートビートは `marked` を `false` と報告します。
- **原因：** プロセスと Postgres の間にコネクションプーラーがあります。プーラーは独自の起動パラメータでサーバー接続を開くため、それらの接続には `loom.tenancy_checked` がありません。クライアントのパラメータを `SET` で転送するプーラーも、リフレッシュがマーカーを読む前に `RESET ALL` を実行するため、マークなしと判定されます。セルに org トリガーが設定されると、そのようなセッションによる、セルが保持していない org への書き込みは、Loom 自身のものを含めてすべて拒否されます。
- **対処：** セルの要件（[Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md)）に従い、どのモードのプーラーも介さず `LOOM_PG_DSN` を Postgres に直接向け、プロセスを再起動してください。`LOOM_PG_DSN` の変更には再起動が必要です。このチェックはセルのリフレッシュ（`LOOM_TENANCY_MODE_CACHE_S`）のたびに再度読み取られ、再起動後の最初のリフレッシュで解消されます。

<a id="a-boot-fails-with-function-loomtenancy_refuse_-does-not-exist"></a>

### 起動が「function loom.tenancy_refuse_… does not exist」で失敗する

- **症状：** `enforced` のセルでマイグレーションを行う起動が、セルのトリガーの作成中に `function loom.tenancy_refuse_unheld() does not exist`（または `…_unmarked()`）で失敗し、`loom doctor` がどの状態でもその関数が存在しないことを示します。
- **原因：** マイグレーションの実行後に、関数が手動で削除されました（org トリガーは 011、`loom.tenancy` ガードは 012 です。`DROP FUNCTION … CASCADE` は、そのトリガーも一緒に削除します）。マイグレーション台帳にはすでにそのファイルが記録されているため、どの起動でも関数は再作成されず、関数が戻るまでチェックされない書き込み元は拒否されません。新しいリリースの `loom doctor` が、そのリリースで最初にマイグレーションを行う起動の前に関数の不在を示した場合は、対処は不要です。その起動がファイルを実行します。
- **対処：** スキーマ `loom` のテーブルを所有するロールとして、`psql` でマイグレーションファイル（`schemas/pg_migrations/011_tenancy_triggers.sql` または `012_tenancy_guard.sql`）を実行します。その後、`enforced` のセルでは `LOOM_BOOT_MIGRATE` を on にして1つの Pod を再起動してください。その起動でトリガーが作成されます（ほかのセルでは、attach が後で作成します）。

<a id="writes-are-slow-or-a-script-is-refused-on-a-deployment-that-is-not-a-cell"></a>

### セルではないデプロイメントで、書き込みが遅い、またはスクリプトが拒否される

- **症状：** セルの状態が `open`（すべてのプロセスが稼働）のデプロイメントで、Loom 自身の書き込みや、スクリプトまたは一括ローダーの書き込みが1行あたりに時間がかかる、`loom.cell_first` に対する `SELECT` 権限を持たないロールが「permission denied for table cell_first」を受け取る、あるいはスクリプトの `loom.tenancy` への insert が SQLSTATE `LT001` で失敗します。
- **原因：** org トリガーがテーブルに残っています。prepare 済みのセルのスキーマのみの復元や、トリガーを削除するステップを省略したクリーンアップが原因です。`open` ではトリガーはすべての行を通しますが、マークのない書き込みのたびに状態が読み取られます。`loom.tenancy` ガードの `tenancy_record` は、スクリプトによるテナンシー行を SQLSTATE `LT001` で拒否します。その行自体が状態を `partial` にするためです。
- **確認：**

  ```sql
  SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'loom'
    AND t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record');
  ```

- **対処：** スキーマ `loom` のテーブルを所有するロールとして、[LOOM_CELL が未設定のプロセスはゲートされる](#a-process-with-loom_cell-unset-is-gated)の削除ステップを実行してください。

<a id="workers"></a>

## ワーカー

<a id="a-worker-fails-every-pass"></a>

### ワーカーが毎回のパスで失敗する

- **症状：** `loom.worker.consecutive_failures` が 3 以上のままになる。ワーカーのログに `<name>: pass failed` または `pass exceeded its deadline` が繰り返し出力され、その後 `DEGRADED` と出力される。
- **確認：** ワーカーの直近のエラーは `GET /v1/admin/workers.status` で、トレースバックは `docker logs loom-workers 2>&1 | grep -E '<name>: (pass|DEGRADED)'` で確認する。
- **対処：** 例外の内容に従って対処する。デッドラインによるキャンセルが繰り返される場合は、パスが実行間隔に対して大きすぎる（バックログが大きい、プロバイダが遅いなど）ことを意味する。プロバイダのエラーは、多くの場合キーの不足かクォータが原因である。フェイクプロバイダ上の LLM ワーカーについては、そのワーカーを無効にするか、実際のプロバイダを設定する。調査中に特定のワーカーだけを止めるには、そのフラグを false に設定してワーカーのコンテナを再作成する。他のワーカーは動き続ける。

<a id="no-worker-runs-at-all"></a>

### ワーカーがまったく動かない

- **確認：** `/v1/health?include=workers` の各プロセスの `workers_leader` を見る。いずれか 1 つがリースを保持している必要がある。`workers_lease_absent` が warn の場合、有効なリースを保持しているプロセスがない。`workers_lease_unreachable` の問題は、プロセスが ClickHouse に問い合わせられなかった理由を示す。`workers_lease_given_up` は、そのワーカーの子プロセスが起動に失敗し続けたことを示す。
- **対処：** すべてのレプリカを `LOOM_BACKGROUND_WORKERS=auto` と `LOOM_DEPLOYMENT=1` で実行する。`off` のプロセスはワーカーを一切実行せず、デプロイメント外のプロセスも `auto` ではワーカーを実行しない。`on` は、どのデプロイメントも使っていない ClickHouse に対してのみ設定する。この場合、プロセスは独自のコピーを実行し、リースを取得しない。アップグレード中は、古いビルドは有効な新しいビルドにリースを譲る。

<a id="host"></a>

## ホスト

<a id="the-host-or-clickhouse-runs-out-of-disk"></a>

### ホストまたは ClickHouse のディスクが不足する

- **症状:** Loom ホストのディスクが満杯になる（コンテナが書き込みに失敗する、イメージのビルドが失敗する）、または ClickHouse が空き容量の不足を報告する。
- **確認:** ホストでは `docker system df` と `var/code_repos` のサイズを確認します。ClickHouse では、[メンテナンス](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#disk-pressure-hygiene)にあるテーブルごとのサイズと `system.disks` のクエリを確認します。ディスクが満杯の場合は、診断出力を `/dev/shm` に書き込んでください。
- **対処:** Docker の JSON ログに上限を設定し、ビルドキャッシュと不要なイメージ（dangling image）を削除します（`docker builder prune`、`docker image prune`）。停止中のコンテナをロールバック用に残している場合は、停止中のコンテナを prune しないでください。ClickHouse では、大量削除の後にマージでバージョンを集約させるか、`loom code compact --apply` でコードのベクトルテーブルをコンパクト化するか、テーブルごとの保持期間設定を有効にします。

<a id="local-stack"></a>

## ローカルスタック

- **ダッシュボードが 503 を返す。** サーバーをソースから (`make server`) 、ビルド済みのフロントエンドなしで実行しています。`make frontend-build` を実行してください。`/v1/admin/*` はどちらの場合でも応答します。
- **スタックが `.env` を無視する。** `--env-file .env` を付けずに `docker compose` コマンドを実行すると、compose ファイルのフォールバック値（認証が有効、キーのない OpenAI プロバイダー）で起動します。`make dev` を使うか、このフラグを渡してください。
- **ソースから起動したサーバーが別のコードを実行している。** [ローカル開発](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md#running-the-server-from-source)を参照してください。

<a id="clients-plugins-and-mcp"></a>

## クライアント、プラグイン、MCP

- **コーディングエージェントのプラグイン**には、それぞれ症状別の表があります（Node が見つからないフック、primer がない、キャプチャが届かない、ツールが二重に表示される）。[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) と [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) を参照してください。プラグインの `loom-status` スキルは、Loom のチェックアウトなしでクライアントのチェックを実行します。
- **リクエストに対応するレジャーの行がない。** リクエストが届いていません。`loom doctor --client` は、SDK とフックが解決するサーバー URL とトークン、およびそれぞれの取得元を示します。環境変数に古い `LOOM_API_KEY` や `LOOM_BASE_URL` が残っていると、`~/.loom/` よりも優先されます。トークンを付けた `GET /v1/whoami` は、サーバーが検証したサブジェクト、組織、名前空間、ロールを返します。トークンに含まれない名前空間に対する 403 や、スコープ付きトークンでボディに名前空間がない場合の `IDENTITY_INCOMPLETE` は、バグではなく認可の結果です（[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#namespace-access)）。一時ディレクトリに残った `loom-flush-*.json` ファイルは、サーバーに届かなかったプラグインのキャプチャです（[ヘッドレスボット](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md#5-when-the-bot-is-quiet)）。
- **MCP ツールが一覧に表示されない。** 一覧は、呼び出し元ではなく MCP URL の `?toolset=` によって変わります。`all` では、コアにもアトミックなロスターにも含まれない実験的な op は一覧から除外されますが、名前を指定するか `loom_op` を通じて呼び出せます。`code.*` ツールがデフォルトのツールセットに表示されるのは、`LOOM_MCP_CODE_TOOLS=true` の場合のみです（[MCP ツールセット](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)）。
- **MCP 呼び出しが `isError: true` を返す。** 401 以外の拒否（`LOCKED`、`PINNED_CONFLICT`、`BLACKLISTED`、`RATE_LIMITED`、`UNKNOWN_ARGUMENT` など）は、`isError` を持つツール結果として返され、HTTP エラーの詳細は `structuredContent` に入ります。401 や 5xx は JSON-RPC エラーです。判別には `code` を使ってください。
- **送信したフィールドが無視された。** レスポンスの `ignored_args` にそのフィールドが示され、近い既知のフィールドがある場合は `did_you_mean` も付きます。書き込み操作では、近いスペルミスは 422 `UNKNOWN_ARGUMENT` で拒否されます（[不明なフィールドと削除されたフィールド](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields)）。
- **429 `RATE_LIMITED`。** バケットについては[モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md#rate-limits-and-traffic-classes)を参照してください。再試行できる時刻は `Retry-After` が示します。

<a id="filing-a-bug"></a>

## バグの報告

`loom report` は、バグ報告フォームの Environment フィールドに入力する内容を `loom doctor` の診断結果として出力します（出力されるのは有無を示す真偽値のみで、シークレットの値は一切含まれません）。`--open` を指定すると、バージョン情報が入力済みのバグ報告フォームが開きます（[issue の報告](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md#filing-an-issue)）。
