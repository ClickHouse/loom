<!-- i18n: source=docs/guides/importing-clickgap-memory.md blob=5fff9080da2e5f36035250de4a491ae273d3484c model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md), which is authoritative.

<a id="import-clickgap-memory"></a>

# ClickGap メモリのインポート

`loom import-clickgap` は、ClickGap の `lessons` ストアという 1 種類の SQLite エクスポート形式を Loom の名前空間に読み込みます。このようなエクスポートがない場合は、このページを読み飛ばしてください。lesson は、型付きのエンティティ、カテゴリ、kind、行ごとの TTL、`recall_count` を持つ、重複排除された短いレビュー用メモリです。

次の 2 つのコマンドで作業を行います。

- `loom import-clickgap`：インポーター（`server/cli/import_clickgap.py`）です。`loom` CLI が ClickHouse に到達できる場所（サーバーコンテナ、またはサーバーの `.env` を持つクローン）で実行します。
- `python -m bench.clickgap_migration_validate`：バリデーター（`bench/clickgap_migration_validate.py`）です。

<a id="what-the-importer-preserves"></a>

## インポーターが保持するもの

| ClickGap                    | Loom                                                                    | 備考                                                                                                                                                                                                                                                                            |
| --------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lessons.text`              | `loom.memory.value` + 埋め込み済みの `content_excerpt`                  | アサーションは埋め込まれ、推論は埋め込まれません                                                                                                                                                                                                                                |
| `lessons.reasoning`         | `value_json.reasoning`（`.text` = アサーション付き）                    | `get` は `value_json` を返すため、アサーションは推論と一緒に渡され、推論に連結されることはありません                                                                                                                                                                            |
| `lessons.kind`              | `memory_type`（ほぼ 1:1）**+** タグ `kind:<kind>`                       | `summary → episodic`（検索対象外）：サマリーは `memory.get` / `memory.list` と `kind:summary` タグで引き続き参照できますが、`memory.search` ではランク付けされません。`kind:<kind>` タグは元の分類を保持します。`_clickgap_memory_type` / `_CLICKGAP_TYPE_REMAP` を参照してください。 |
| `lessons.source`            | `source_trace='clickgap://<class>/<source>'` + タグ `source:<class>`    | クラス：pipeline / session / human / reviewer / eval                                                                                                                                                                                                                            |
| `lessons.created_at`        | `created_at`, `valid_at`, `temporal_anchor`                             | インポート時刻ではなく、元の記録時刻                                                                                                                                                                                                                                            |
| `lessons.expires_at`        | `expires_at` **行ごとにそのまま**                                       | グローバルな TTL から再計算されることはありません                                                                                                                                                                                                                               |
| `lessons.recall_count`      | `recall_count` **および** `memory_embeddings.reinforcement_count`       | [リコールシグナル](#recall-signal)を参照                                                                                                                                                                                                                                        |
| `lessons.last_recalled_at`  | `last_recalled_at` **および** `last_reinforced_at`                      | リコールの新しさの軸                                                                                                                                                                                                                                                            |
| `lessons.evidence_count`    | `evidence_count`                                                        |                                                                                                                                                                                                                                                                                 |
| `lesson_entities`           | `entities`（素）+ `entity:<kind>:<text>` タグ + `loom.entity_vectors`   | 素の `entities` 配列では種別が失われますが、タグが種別を保持します                                                                                                                                                                                                              |
| `lesson_categories` + items | `loom.categories` + `loom.memory_categories`                            | 決定論的な `category_id` を生成する前に、名前は大文字小文字が正規化されます                                                                                                                                                                                                     |
| `false_positive` の lesson  | タグ `polarity:negative`                                                | 抑制（「再度指摘しない」）メモリ                                                                                                                                                                                                                                                |
| 人に帰属するソース          | タグ `pii:human-attributed`                                             | タグによる消去の候補                                                                                                                                                                                                                                                            |

**インポートは冪等です。** `memory_key='lesson_<id>'` と UUIDv5 の `memory_id`（`clickgap://<org>/<namespace>/lesson/<id>` から生成）は決定論的で、実行のたびに新しい `version` が 1 つ付与されます。再インポートすると `ReplacingMergeTree(version)` のもとで置き換えられ、行数は変わりません。

<a id="1-dry-run"></a>

## 1. ドライラン

書き込みは行いません。ドライランでは、すべてのタイムスタンプが解析できることを確認し、kind → type と TTL のクロス集計およびカテゴリ数を出力し、行が埋め込みなしで保存されないようにエンベッダーへの疎通を確認します。

```bash
loom import-clickgap \
  --sqlite ~/Downloads/clickgap-memory-YYYYMMDD/memory.db \
  --org <org> --namespace clickgap --dry-run
```

クロス集計が元データと一致していること、およびエンベッダーに到達できることを確認してください。

<a id="2-import"></a>

## 2. インポート

**専用の名前空間**（`clickgap`）を使うと、ロールバックは `namespace.delete` の1回で済みます。エクスポートにはベクトルが含まれないため、すべての行はインポート時に現在の `LOOM_EMBEDDING_MODEL` で埋め込まれます。

```bash
loom import-clickgap \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap
```

`--agent` のデフォルトは `clickgap` です。`--batch-size` は ClickHouse への1回の insert あたりの行数を設定します（デフォルトは 1000）。最後の検証ブロックは結果を出力し、次の項目で不一致があると実行を失敗させます（終了コード 1）。

- 有効なメモリ行数と、ソースの lesson 数の比較
- 誤ったモデルによる埋め込み行（0 でなければなりません）
- 永続（TTL なし）の行数
- 読み戻した `recall_count` の最大値

`lesson_recalls`（recall ごとのイベントログ）は、デフォルトではインポートされません。非正規化された `recall_count` 列に、ランキング用のシグナルがすでに含まれているためです。分析のために `loom.memory_usage_events` に recall ごとのイベントログが必要な場合にのみ、`--seed-recall-events` を指定してください。

> **警告:** `--seed-recall-events` は追記方式であり、冪等ではありません。新しい名前空間に対して1回だけ実行してください。Loom の使用イベントには PR 列がないため、`pr_number` は **破棄されます**。

<a id="recall-signal"></a>

## リコールシグナル

インポータは `recall_count` を `memory_embeddings.reinforcement_count` に、`last_recalled_at` を `last_reinforced_at` にミラーします。`memory.search` では、このカウントが減衰付き強化ランク項に反映されます。`LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` でその大きさを指定し（0 で無効）、`LOOM_MEMORY_SEARCH_REINFORCEMENT_HALF_LIFE_DAYS` で減衰を設定します。リクエストでは `reinforcement_weight` / `reinforcement_half_life_days` により両方を上書きできます（[設定](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)）。

この項は関連度を超えないように上限が設けられているため、Loom は ClickGap の `recall_count DESC` による順序を**再現しません**。Loom は主に類似度でランク付けし、エンティティ、新しさ、強化の各項を加味します（[検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)）。ワークロードがリコール頻度による順序に依存している場合は、切り替え前に、得られる順序が許容できるかどうかを検証してください（バリデータはリコール順序の τ を報告します）。

<a id="3-validate-before-cutover"></a>

## 3. 切り替え前の検証

バリデータは `server.auth.jwt.mint_token` で独自の管理者トークンを発行するため、`LOOM_JWT_SECRET` と明示的な `LOOM_BASE_URL` が必要です。デフォルトの接続先はありません。プローブは評価用の読み取りです（`auto_touch: false`、`bench/_gate.py` の計測フィールド）。そのため、実行してもインポートしたレッスンに recall の利用が記録されることはなく、繰り返し実行しても τ ゲートが測定する強化シグナルには影響しません。

> **警告:** 信頼できるエンドポイントを指定してください。プローブのクエリはレッスンのアサーションそのものです。インポートデータをサードパーティの埋め込みモデル経由で自社インフラの外に出してはいけません。同じ理由から、`--show-queries` は切り詰めたプローブテキストを出力するもので、デフォルトではオフです。

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_JWT_SECRET=... \
python -m bench.clickgap_migration_validate \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap \
  --probes 200 --top-k 10 \
  --min-recall 0.90 --min-recallw 0.90 --min-tau 0.2
```

各ゲートは、0 より大きい値を設定すると、基準を満たさない場合に実行が非ゼロの終了コードで失敗します（0 はレポートのみ）。

| ゲート          | 測定内容                                                                                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--min-recall`  | 既知アイテムの recall@k：プローブ対象のレッスン自体が Loom の上位 k 件に入っているか。種別ごと、エンティティタイプごとの内訳により、特定の区分だけが崩れる事態（たとえばタイプの再マッピングの誤り）を検出できます。 |
| `--min-recallw` | 重み付き recall@k：各プローブを `recall_count` で重み付けするため、頻繁に使われるレッスンを取りこぼすほどコストが大きくなります。                                                              |
| `--min-tau`     | recall 順序の τ：Loom の順序と `recall_count` の Kendall 一致度。診断用の指標です。Loom は `recall_count` で並べ替えないため、高い τ は期待しないでください。            |

失敗した検索は報告され、比率の計算から除外されます。ミスとして採点されることはありません。ただし、検索が 1 件でも失敗すると、ゲートの有無にかかわらず実行は非ゼロで終了します。τ を計算できない実行では `n/a` と表示され、`--min-tau` は失敗扱いになります。

サマリー（`summary → episodic`）は設計上検索対象外のため、プローブから除外されます。`--seed`（デフォルトは 42）により、プローブのサンプルを再現可能にできます。`--concurrency`（デフォルトは 8）は同時に実行する検索の数を制限します。

<a id="use-suppression-memories"></a>

## 抑制メモリを使う

`false_positive` の教訓（「バグに見えるが、再度指摘してはならない」）は抑制レイヤーです。これらは検索可能で、`polarity:negative` タグを持ちます。検出器が候補の指摘事項を報告するかどうかを判断するときは、指摘事項のテキストに対してタグでフィルタした検索を実行します。

```python
hits = await agent.memory.search(finding_text, tags=["polarity:negative"], top_k=5)
if hits and hits[0].similarity >= SUPPRESS_THRESHOLD:  # calibrate per embedder
    skip_or_downweight(finding)  # a known false positive
```

`tags` フィルタはすべての取得経路（ベクトル検索、字句検索、および新しさ・確信度によるバックフィル）に適用されるため、結果には抑制メモリだけが含まれます（`tests/unit/test_memory_search_service.py` の `test_lexical_fallback_applies_caller_filters`）。肯定的なメモリが混入すると、本物の指摘事項を隠してしまうおそれがあります。

ほかの種類も、`kind:<kind>` タグを使って同じ方法でフィルタできます。`tags=["kind:principle"]` のように指定します。複数の種類をまとめた除外リストには `tags=["kind:false_positive", "kind:maintainer_preference"]` を使います（`tags` は OR フィルタです）。`memory.search` の `memory_type` パラメータが受け付けるのは大まかな種類（`semantic`、`procedural`、`preference`、`reflection`、`decision`）のみなので、種類の完全な分類はタグで表現します。

<a id="roll-back"></a>

## ロールバック

- **完全な取り消し：** `clickgap` に対して `namespace.delete` を実行します（admin 権限が必要です。MCP 経由の場合は `loom_op` を通じて呼び出します）。インポート先は専用の名前空間であるため、他のデータは削除されません。
- **消去またはコホートの削除：** 名前空間スコープを指定した `erasure.request` を実行します（チェックポイントが保存され、リカバリワーカーが再開します）。または、メンテナーからの削除依頼に対応するため、`pii:human-attributed` の行だけを消去します（[保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)）。
- **マッピングの修正：** インポーターを再実行します。決定的なキーとより高い `version` により、誤った行はその場で置き換えられます。

<a id="consolidate-optional-after-validation"></a>

## 統合（任意、検証後）

エンティティセットのほぼ重複したメモリを圧縮する場合は、検証の**後**に `memory.consolidate` を実行します。`server/memory/app/consolidate.py` のガードは、**ピン留め**された行を対象から除外し、`memory_type` をまたぐ統合や**極性**をまたぐ統合を行いません。そのため、`false_positive` の抑制が肯定的な観測に吸収されることはなく、原則が観測を吸収することもありません。

名前空間（`namespace.register`）では `consolidation_enabled=false` のままにしてください。これにより、バックグラウンドの統合ワーカーが、ClickGap がすでに集約した `summary` の教訓を再度統合することを防げます。

<a id="related"></a>

## 関連項目

- [メモリの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- [名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
