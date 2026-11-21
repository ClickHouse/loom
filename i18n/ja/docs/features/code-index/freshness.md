<!-- i18n: source=docs/features/code-index/freshness.md blob=9dcf44236c7be8f6d338e452c10b5a0be2e98f93 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md), which is authoritative.

<a id="code-index-freshness"></a>

# コードインデックスの鮮度

インデックスはリポジトリに遅れて追随します。設定されたブランチにマージされたコミットを、パスごとにインデックス化し、各ティアは独自のウォーターマークを進めます。インデックスされたコードに依拠する前に、回答に鮮度メタデータが含まれていればそれを確認し、該当するティアのステータスも確認してください。

<a id="the-as_of-block"></a>

## `as_of` ブロック

`as_of` を返す操作は、次の鮮度フィールドを公開します。通常の `code.symbol` の定義には、このブロックもコミットも含まれません。代わりに `code.index_status` を確認し、返された定義を該当するソースのリビジョンと照合してください。

| フィールド           | 意味                                                                                                                                                   |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `commit`        | この名前空間における高速ティアのコミット（`git_head`）。すべてのコンパイラティアのリビジョンではありません                                                                                           |
| `branch`        | 名前空間が追跡するブランチ                                                                                                                                        |
| `indexed_at`    | そのコミットが反映された時刻                                                                                                                                       |
| `lag_commits`   | `commit` からミラーが取得した先頭までの first-parent コミット数。`-1` は不明                                                                                                  |
| `stale`         | `lag_commits` が `LOOM_CODE_STALE_AFTER_COMMITS`（デフォルトは 5）を超えている、またはミラーの取得が遅れている                                                                          |
| `status`        | `fresh`（先頭に追従）、`behind`（stale のしきい値未満）、`stale`、`unknown`（lag が `-1`）                                                                                  |
| `stale_reason`  | 取得が止まったミラーを基準に lag を数えている場合は `mirror_not_fetched`                                                                                                     |
| `fetched_at`    | `stale_reason` とともに返され、ミラーが最後に取得した時刻                                                                                                                  |
| `failing_tiers` | エントリでティアが失敗している間に存在します。`tier`、`kind`、`failing_since`、`failed_passes`、`fix` を含み、ウォーターマークを持つティアでは `lag_commits` も含みます |

このブロックは名前空間ごとに 30 秒間キャッシュされ、`git_head` を進めるパスが実行されると無効になります。フェイルオープンで動作し、鮮度を読み取れない場合はブロックが空になりますが、回答は返されます。`as_of` が空、または存在しない場合は、不明として扱ってください。

失敗しているティアの情報は、`stale` が false であっても先頭より遅れていることがあります。`failing_tiers` がコンパイラティアを示している場合は、コンパイラ品質の回答（正確なコールグラフや参照）が遅れている可能性があるものとして扱い、依存するコードを直接読んでください。

**ビルド状態だけでなく、鮮度も確認してください。** 最初のビルドが完了した名前空間でも、数コミット遅れていることがあります。`as_of` が返された場合は `stale` を確認し、返されない場合は `code.index_status` で該当するウォーターマークを確認してください。鮮度が stale または不明のときは、編集しようとしているファイルを自分のチェックアウトから読んでください。

<a id="the-mirrors-own-age"></a>

## ミラー自体の経過時間

ラグは Loom のローカルミラーを基準に数えるため、ミラーが取得を停止していても 0 と表示されます。そのため、取得が遅れている場合にも `stale` が true になり、ステータス系の op はミラーの経過時間を報告します。

- `code.index_status`（1 つの名前空間）と `code.repos`（読み取り可能な全エントリ）は、`clone_head`、`clone_fetched_at`、`fetch_overdue`（`LOOM_CODE_FETCH_STALE_AFTER_SECONDS` より古い場合。デフォルトは 3600）を返します。これらに加えて、`git_head`、`fast_tier_lag`、`scip_lag` / `scip_py_lag`、`compiler_tiers` リスト、`last_refresh_at`、`chunks_total` / `chunks_embedded` / `embedding_coverage` も返します。
- どちらも `tier_health` を返します。これは各ティアの最後に記録された結果（取得、インジェスト、コンパイラティア、週次の C++ フル実行）で、失敗中はエラーの種類、`failing_since`、`failed_passes`、対処方法を含みます。このホストでは依存関係を取得できないためにモジュールを除外した Go ティアは、`state: "unavailable"`（種類は `dependency_unavailable`、`failing_since` には最初に検知した時刻、パス数なし）と対処方法を示します。これは失敗ではなく、`as_of.failing_tiers` にも含まれません。
- `code.repos` の各行には `searchable` が含まれます。これは `embedding_coverage` が `LOOM_CODE_READY_EMBED_COVERAGE` に達したことを示します。

`compiler_tiers` には、エントリが実行するティアごとに 1 枚のカード（`producer`、`tool`、`label`、`commit`、`lag`、`workers`）があります。`code.index_status` は MCP 経由の場合と `commit` が設定されている場合に簡潔な形式で応答し、`brief: false` を指定すると完全な応答を返します。

<a id="is-one-commit-in-every-tier"></a>

## 1 つのコミットはすべてのティアに反映されているか

マージ済み PR のレビュー担当者は、そのマージがすべてのティアに反映されていることを確認する必要があります。`code.index_status` に `commit=<sha>` を指定すると、`commit_coverage` で結果が返ります。

| フィールド                  | 意味                                                                         |
| ----------------------- | -------------------------------------------------------------------------- |
| `ready`                 | 高速ティアと、ウォーターマークを持つすべてのコンパイラティアがそのコミットに到達した時点で true になる                    |
| `waiting_on`            | 到達していないティア                                                                 |
| `in_mirror`             | ミラーがそのコミットを保持しているかどうか                                                      |
| `fast`, `tiers[]`       | ティアごとの `covered`（ミラーにそのコミットがない場合は null）と `commits_since`               |
| `commits_since`         | 高速ティアがそのコミットからどれだけ先に進んでいるか                                                 |
| `paths_changed_overlap` | `paths=[…]` を指定した場合、それらのパスのうち、後続のマージで変更されたもの                               |

`paths_changed_overlap` のパスは、インデックス内ではそのコミットのパスと異なります。チェックアウトしたものから読み取ってください。

```python
st = await agent.code.index_status(namespace="code-myrepo", commit=merge_sha, paths=pr_files)
cov = st["commit_coverage"]
if not cov["ready"]:
    print("waiting on", cov["waiting_on"])
```

<a id="waiting-for-coverage-without-polling"></a>

## ポーリングせずにカバレッジを待つ

`code.coverage.watch` は、コミットがカバーされた時点でタスクインボックスにイベントを投稿します。

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.coverage.watch" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","commit":"'"$MERGE_SHA"'","inbox":"<your-inbox>"}'
```

- `commit` は完全な SHA、またはミラーが解決できる省略形です。`inbox` はタスクインボックス（`inbox.register`）です。呼び出し元には、コード名前空間に対する viewer 権限と、インボックスの所有者名前空間に対する writer 権限が必要です。
- `code_coverage_notify` ワーカー（デフォルトで有効、`LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` ごとに実行、デフォルトは 60）は、`patch` が `{namespace, commit, tiers: [{producer, label, watermark}]}` である `code.coverage.ready` イベントを 1 件投稿します。このイベントは `/v1/task/watch?inbox=…`、`task.updates`、およびインボックスの署名付き webhook を通じて届きます。
- すでにカバー済みのコミットに対しては、`ready=true`、`emitted=true` を返し、同じ呼び出しの中でイベントを投稿します。
- イベントは (名前空間, コミット, インボックス) ごとに 1 件です。その `task_id` が応答の `event_id` になるため、リトライしても二重に投稿されません。1 つのイベントの配信は、その id に対するリースのもとで 1 つずつ実行されるため、競合する 2 つの配信のうち `emitted=true` を返すのは、イベントを保存した側だけです。同じ呼び出しで配信を行う `code.coverage.watch` が、別の配信を 10 秒より長く待った場合は 409 `PRECONDITION_FAILED` を返します。リースストア（Keeper）が応答しない場合は 503 を返します。どちらの場合もリトライしてください。
- `code.coverage.unwatch`（フィールドは同じ）は、保留中の watch を削除します。`removed: false` は、有効な watch がなかったことを意味します。

Python SDK には `agent.code.coverage_watch(commit, inbox, namespace=…)` と `agent.code.coverage_unwatch(...)` があります。インボックスについては、[タスクと決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox)で説明しています。

<a id="pushing-freshness-from-ci"></a>

## CI から鮮度をプッシュする

ポーラー（`LOOM_CODE_INGEST_INTERVAL_SECONDS`、120）はすべてのマージを検出します。マージを即座にインデックスするには、マージ後のジョブから、コード名前空間にスコープされたライタートークンを使って `code.refresh` を呼び出します。

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.refresh" \
  -H "Authorization: Bearer $LOOM_CI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","expected_head":"'"$GITHUB_SHA"'"}'
```

- このパスの対象は指定された名前空間のみです。その名前空間は設定済みのエントリである必要があり（そうでない場合は 404）、設定されたブランチを取得した先頭までインデックスします。
- `expected_head` はログに記録されるだけです。`wait=true` を指定すると、パスが終了するまでブロックします（最大 `wait_timeout_seconds`、デフォルトは 300）。応答には `triggered`、`already_running`、`head_before`、`head_after` が含まれます。
- この呼び出しはベストエフォートです。トリガーを取りこぼした場合は、ポーラーが回復します。

オペレーターは `loom.code.tier_lag_seconds{tier}` を監視します。遅延したティアに関するランブックは [コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md) にあります。

<a id="settings"></a>

## 設定

| 設定                                         | デフォルト | 効果                                                                 |
| -------------------------------------------- | ---------- | -------------------------------------------------------------------- |
| `LOOM_CODE_STALE_AFTER_COMMITS`              | 5          | `as_of.stale` が true になるまでに遅れるコミット数                   |
| `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`        | 3600       | `fetch_overdue` と `stale` になるまでのミラーの経過時間（0 でチェックを無効化） |
| `LOOM_CODE_INGEST_INTERVAL_SECONDS`          | 120        | `code_ingest` がミラーをポーリングする間隔                           |
| `LOOM_CODE_READY_EMBED_COVERAGE`             | 0.95       | `code.search` がベクトル検索を使い始めるまでの埋め込み済みの割合     |
| `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` | 60         | カバレッジ監視を確認する間隔                                         |

<a id="limits"></a>

## 制限事項

- 鮮度はブランチの名前空間ごとに管理されます。`main` にマージされた修正は、リリースブランチの名前空間にマージされるまでそこには反映されません。複数の名前空間を確認するには `code.grep mode=presence namespaces=[…]` を使用します。
- オープン中のプルリクエストと作業ツリーがインデックスに含まれることはありません。
- 「インデックスが更新された」ことを知らせるプッシュ通知はありません。`code.coverage.watch` はコミットごとに通知します。

<a id="related"></a>

## 関連ドキュメント

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)：回答の読み方、トラブルシューティング
- [operations.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)：status op
- [コードインデックスのアーキテクチャ](../../architecture/code-index-architecture.md)：ウォーターマーク、リコンシリエーション、extractor stamp
