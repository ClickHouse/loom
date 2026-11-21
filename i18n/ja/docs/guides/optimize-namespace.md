<!-- i18n: source=docs/guides/optimize-namespace.md blob=bcf139a5e94cdfb8e192ed26d7bf748075392110 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md), which is authoritative.

<a id="optimize-a-namespace-from-its-audit"></a>

# 監査結果に基づく名前空間の最適化

`memory.set` をオブジェクトストアとして使うライター（ハッシュキー配下の JSON blob、メモリとして保存されたリース、claim として保存されたドキュメント）は、すべてのテーブルを整合した状態に保つ一方で、recall を役に立たないものにします。この手順では、そのような名前空間を[調査エージェントガイド](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)のレイアウトに作り直します。移動のたびにリダイレクトが残るため、旧キーを読み続けている実行中のライターも、引き続き応答を得られます。

[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) と [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) のプラグインに含まれる `loom-optimize` スキルは、同じ手順を実行します。

<a id="the-two-operations"></a>

## 2つの操作

**`namespace.audit`**（`POST /v1/namespace.audit`、MCP `loom_namespace_audit`、CLI `loom audit-namespace`）。読み取り専用です。

| 入力               | デフォルト      |
| ------------------ | --------------- |
| `org`, `namespace` | 必須            |
| `window_seconds`   | 604800（7日）   |
| `sample_limit`     | 5               |

戻り値は次のとおりです。`summary`（`live_rows`、`live_bytes`、`excerpt_bytes`、`version_rows`、`version_bytes`、`unembedded_rows`、`gets`、`searches`、`sets`、`get_search_ratio`、および `inspector_calls`。後者はダッシュボードとアシスタントのツールセットによる get と search で、他のカウントには含まれません。ダッシュボードからの書き込みは `sets` に数えられます）、`findings[]`（`code`、`severity`、`count`、`share`、`bytes`、`samples[]`、`detail`、`remedy`）、`skipped`（実行できなかったルール：チェック、summary の読み取り、evidence の読み取り。それ以外は引き続き返されます）、`coverage`、`healthy`（「critical な finding がない」ことを示します）、そして `criticals` / `warnings` / `infos` の件数です。サンプルにはキー、行 ID、識別列、件数を含み、値は含みません。各 finding には[evidence フィールド](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md#reading-a-namespaces-health)も含まれます。プランは `kind`（使用状況に基づくヒューリスティックは誤用の証明ではありません）、`samples_complete`、`evidence[]`（サンプルした各行の現在の `version` と `content_sha256`）を参照します。

**`memory.move`**（`POST /v1/memory.move`、MCP `loom_memory_move`）。

| 入力                                                                         | デフォルト                  |
| ---------------------------------------------------------------------------- | --------------------------- |
| `org`, `namespace`, `agent`, `from_key`, `to_key`                            | 必須                        |
| `value`, `memory_type`, `tags`, `entities`, `valid_at`, `source_resource_id` | 移動元の行から引き継ぎ      |
| `keep_redirect`                                                              | `true`                      |
| `if_match_sha256`                                                            | なし                        |
| `dry_run`                                                                    | `false`                     |

戻り値は `{from_key, to_key, redirected, memory_id, version, content_sha256, advisories,
dry_run}` です。dry run では `memory_type` と `value_preview` が加わります。移動後の動作は次のとおりです。

- `memory.get(from_key)` は、引き続き古い値と `canonical_key` を返します。
- `memory.get(from_key, follow=true)` は新しい行を返します。
- `memory.search` と `memory.list` には新しい行だけが表示されます。

<a id="1-audit"></a>

## 1. 監査

```bash
loom audit-namespace --org <org> --namespace <ns> --window-days 7 --samples 5
```

CLI はサーバー上で実行されます（ClickHouse を直接読み取ります）。コード、重大度、種別、件数、割合、最初のサンプルを表にして出力し、`--json` を指定すると出力モデルを返します。名前空間が健全でない場合は終了コード 1 で終了します。それ以外の場所からは、HTTP または MCP で `namespace.audit` を呼び出してください。

まず `summary` を確認します。

- `get_search_ratio` が数百になっている場合、その名前空間はキーバリューストアとして使われています（プロシージャの読み取りは意図的にキーで読み戻されるため、この集計には含まれません）。
- `unembedded_rows` が 0 より大きい場合、検索では決して見つからない行があります。

次に検出結果を確認します。`healthy` が true で、`warning` や `critical` がなければ、そこで終了します。`info` の検出結果は書き込み側へのメモであり、修正作業ではありません。`system_state` の検出結果（インデックス作成の滞留、テレメトリの欠落）はデプロイに関するもので、書き込み側の問題ではありません。`telemetry_unavailable` が警告の間は、`exact_key_store` と `intentional_exact_key_reads` は問題なしではなく `skipped` に入り、`coordination_as_memory` はキーセグメントのチェックだけを実行します（`samples_complete: false`）。

ダッシュボードの名前空間監査パネルにも同じ監査結果が表示されます。名前空間が健全でない場合（critical の検出結果が 1 件、または `exact_key_store`、`double_encoded_values`、`unembedded_rows`、`coordination_as_memory` のうち 2 件が warning の場合）、`context.pack` には `namespace_health` ブロックが含まれるため、エージェントはセッション開始時にそれを確認できます。

<a id="2-plan"></a>

## 2. 計画

検出結果ごとに1つのセクションを書きます。監査が返したコードに対してのみセクションを作成します。

| 検出結果                                                                | 変更内容                                                                 | 方法                                                                                                                                                   |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `double_encoded_values`, `json_prefix_excerpts`, `hash_keys`            | 行を、読みやすいキーと主張を先頭に置いた値へ移す                         | 一度だけデコードする。主張の文、出典の行、残りを短い行にまとめる。`memory.move` に `value`、`tags`、`entities`、`valid_at`、`if_match_sha256` を渡す    |
| `oversized_values`, `episodic_blobs`                                    | 本文をドキュメントまたは blob にする。1段落の行がキーを引き継ぐ          | `knowledge.upsert`（Markdown、`source_uri=loom://<org>/<ns>/<key>`）または `context.offload` を実行し、その後、元のキーを段落を `value` にして `memory.move` する |
| `unembedded_rows`                                                       | 埋め込めるように値を分割または短縮する                                   | `oversized_values` と同じ。`namespace.validate` は、これらを `unembedded` の行に含めて一覧する                                                         |
| `coordination_as_memory`                                                | ブロックをワーキングメモリへ移す。元のキーは引き続き応答する             | `working_memory.set` を実行し、その後 `memory.move` を `keep_redirect=true` で実行する                                                                 |
| `hand_rolled_index_rows`                                                | 行は読みやすいまま `tags` を付ける。書き込み側はこれらの行の生成をやめる | タグのみ（`memory.update_by_id`）。移動はしない                                                                                                        |
| `exact_key_store`, `version_churn`                                      | 書き込み側を変更する                                                     | オーナー向けにキーを一覧する                                                                                                                           |
| `telemetry_unavailable`, `index_backlog`, `intentional_exact_key_reads` | 何もしない。デプロイメントの状態、またはワークフローである               | 作り直しは不要。`telemetry_unavailable` は台帳が期間をカバーした後に再実行し、停滞した `index_backlog` は埋め込みワーカーを確認する                     |

メンター（`mentor.brief`）は、データ変更にあたる形をさらに3つ挙げます。これらは同じ規則で作り直し、ヒントの `evidence` を件数として使います。

| メンターのヒント         | 変更内容                                                                                        | 方法                                                                                                |
| ------------------------ | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `documents_as_memories`  | `oversized_values` と同じ                                                                       | `knowledge.upsert` を実行し、その後、段落を `value` にして `memory.move` する                       |
| `checkpoints_as_memory`  | 実行ダンプを `context.offload` の blob にする。実行 ID をキーにした短いエピソード行で置き換える | `context.offload` を実行し、その後、1行の台帳 `value` と `memory_type=episodic` で `memory.move` する |
| `records_for_identities` | `<prefix>-<id>` をキーとし、スカラーフィールドを持つ行を、1つのスキーマと行ごとの1レコードにする | `memory_schema.register` を1回、行ごとに `memory.record.write` を実行し、その後ポインタ行へ `memory.move` する |

各セクションには、次の内容を記載します。件数とバイト数、検出結果の `samples` から取った変更前 → 変更後のキー例3件、引数を含む正確な呼び出し、そして実行**しない**こと。

- `memory.delete` は使わない。
- 値をその場で変更しない。書き換えた値は新しいキーの下にのみ置く。
- 直近1時間以内に書き込まれた行は対象にしない。書き込み側が処理の途中である可能性があるため。

計画は、`loom://<org>/<ns>/_optimize/plan-<YYYYMMDD>` のナレッジドキュメントとして保存します（その `source_uri` を指定した `knowledge.upsert`）。これにより、オーナーの承認と前後の数値を1か所にまとめられます。

<a id="3-get-approval"></a>

## 3. 承認を得る

名前空間の所有者が書面で承認します。計画を提示しただけでは、計画が承認されたことにはなりません。部分的な承認は、指定されたセクションにのみ適用されます。所有者のライターが実行中の場合（ウィンドウ内の監査の `sets` で確認できます）は、所有者に伝えてください。いずれの場合も、直近 1 時間の行は対象外です。

<a id="4-apply"></a>

## 4. 適用

名前空間が重要な場合は、先にバックアップを取ってください。`memory.list … fields=["value"]` の結果を JSONL ファイルに保存する方法、`namespace.snapshot`（HTTP または SDK。MCP 経由では提供されません）を使う方法、またはサーバー上で `CREATE TABLE … AS loom.memory` と `INSERT SELECT` を実行する方法があります（[バックアップとリカバリ](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)）。

1. 各セクションの最初の3件の移動を `dry_run=true` で実行し、`to_key`、`redirected`、`advisories` を読み取って確認します。
2. その後、`created_at` 順に最大50件ずつのバッチで移動します。これにより、リビジョンが新しいキーのバージョンとして配置されます。
3. 監査サンプルまたは新たに実行した `memory.get` の結果から `if_match_sha256` を渡します。409 は、行が途中で変更されたことを意味します。そのセクションを一時停止し、再読み込みしたうえで確認してください。
4. `memory_id`、`version`、`content_sha256` を含まない応答は、移動が行われなかったことを示します。件数に含めないでください。

```bash
curl -s "$LOOM_BASE_URL/v1/memory.move" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "org": "acme", "namespace": "agent-sre", "agent": "sre-bot",
    "from_key": "3f9a0c1e5b7d2a4c6e8f0a1b3c5d7e9f",
    "to_key": "sre/case/c-42/evidence/e-7",
    "value": "Disk on ingest-3 filled at 02:10 UTC.\nSource: alert A-981.",
    "tags": ["case:c-42", "kind:evidence"],
    "dry_run": true}'
```

<a id="5-measure"></a>

## 5. 計測

同じ期間に対して監査をもう一度実行し、計画ドキュメントに before/after の表を追記します（同じ `source_uri` に対して `knowledge.upsert` を実行します）。オーナーには[ライタールール](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md#writer-rules)を渡します。

<a id="6-retire-nothing"></a>

## 6. 何も廃止しない

古いキーは、オーナーが望む限り、リダイレクト経由で引き続き読み取れます。ライターが新しいキーを使い始めた後は、オーナーが `memory.delete` で古いキーを論理削除（ソフト削除）できます（`memory.history` には残ります）。この手順では削除を一切行いません。

> **Warning:** 稼働中のライターがまだ読み取っているキーを廃止すると、そのライターから見える内容が分断されます。`memory.get` は `found: false` を返し、クライアントがローカルの状態にフォールバックする場合があり、その結果、2 つのストアの内容が食い違います。リダイレクトは、これを防ぐために存在します。

<a id="related"></a>

## 関連ドキュメント

- [調査エージェントのメモリ](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)：目標とするレイアウト
- [エージェント連携](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#mistakes-to-avoid)
- [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
