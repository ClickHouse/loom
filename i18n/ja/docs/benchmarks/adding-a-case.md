<!-- i18n: source=docs/benchmarks/adding-a-case.md blob=63bcbeab205c463f07cd6aa007c0f8d4eeabefab model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), which is authoritative.

<a id="adding-a-benchmark-case-dataset-or-suite"></a>

# ベンチマークのケース、データセット、スイートを追加する

コミット済みのセットは小さいため（[セットのサイズ](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#set-sizes)）、新しいケースを追加するのがスイートを改善する最も手軽な方法です。まずファイルのヘッダーまたは `_comment` を読んでください。ほとんどのファイルに、正解データ（gold）をどのように作成したか、新しい行が満たすべき条件が記載されています。

<a id="add-a-case-to-an-existing-set"></a>

## 既存のセットにケースを追加する

<a id="memory-qa-benchdatasetssynthetic_memory_qajson"></a>

### メモリ QA（`bench/datasets/synthetic_memory_qa.json`）

会話に質問を追加するか、会話そのものを追加します。

```json
{
  "conversation_id": "06-billing",
  "conversation": [{ "role": "user", "content": "Our invoices go out on the 3rd of each month." }],
  "questions": [{ "question": "When do invoices go out?", "gold_keys": ["invoice", "3rd"] }]
}
```

返された `memory_key` または抜粋のいずれかが正解の文字列を含んでいれば、ヒットとみなします（大文字小文字は区別せず、アンダースコアはスペースとして扱います）。正しいメモリには必ず含まれ、誤ったメモリには含まれにくい語を正解の語として選んでください。内容は合成のものにとどめ、第三者のテキストや実在の人物は使わないでください。

**ベースラインが変わります。** 0.80 というゲートのしきい値は、このファイルで記録されたものです。実際のデプロイ環境で、変更の前後にゲートを実行し、値が変わる場合は `Makefile` の `MIN_RECALL` を更新してください。テスト：`tests/unit/test_bench_quality_gate.py`、`bench/test_quality.py`。

<a id="extraction-and-graph-regressions"></a>

### 抽出とグラフのリグレッション

- `bench/fixtures/extraction_regressions.json` の各行は `{id, messages, expected}` の形式で、いずれかのメモリのキーまたは値に現れなければならないフレーズを持ちます。
- `graph_regressions.json` の各行は `{id, messages, expected_triples: [{subject, relation, object}], as_of}` の形式です。
- `_note` フィールドには失敗の分類を記します。ハーネスはこのフィールドを無視します。

`tests/unit/test_algorithm_gate_assets.py` は、すべての行にメッセージと空でない期待値があることを検査します。ケースが通ることを確認するには、実際の抽出用 LLM に対して `make bench-algorithm-gate` を実行してください。

<a id="code-plane-qa-benchfixturescode_intel_qajsonl"></a>

### コードプレーン QA（`bench/fixtures/code_intel_qa*.jsonl`）

1 行に 1 つの JSON オブジェクトを書きます。`//` で始まる行はコメントです。

```json
{"id": "co037", "type": "conceptual", "query": "where are part merges scheduled", "gold": {"paths": ["src/Storages/MergeTree/MergeTreeBackgroundExecutor"]}}
{"id": "sd011", "type": "symbol_def", "query": "IColumn::insert", "gold": {"qualified_name": "DB::IColumn::insert", "path_prefix": "src/Columns/"}}
{"id": "ab011", "type": "abstain_probe", "query": "kubernetes ingress annotation for sticky sessions", "gold": {}}
```

タイプごとの正解の形式は `bench/code_intel_qa.py` の docstring に記載されています（`callers` は `expected_callers`、`hub` は `path_prefix` と `symbols` を取ります）。パスは拡張子を除いたプレフィックスです。任意の `lane`、`facets`、`namespace` で、その行の振り分けと切り出しを指定します。

- **ゲートに使う前に、稼働中のインデックスで正解を検証してください。** 一度フロアなしで実行し、不一致を確認して、正解を修正します。
- **abstain probe は、リポジトリに存在しないコードについてのものにしてください。**
- **メインファイルのタイプ構成を保ってください。** `bench/test_code_intel_qa.py`（`test_fixture_loads_and_validates`）は、`code_intel_qa.jsonl` が 30 行以上であり、タイプが `symbol_def`、`callers`、`conceptual`、`abstain_probe` だけであることを検証します。hub、履歴、テストセットの行は、それぞれ専用のレーンファイルに入れます。
- `make bench-code-intel-dry` は引き続き成功しなければなりません。完全な合成検索のもとで、すべてのレーンが上限のスコアを出す必要があります。
- マイニングで作られたレーンは手作業で編集せず、再生成します。`code_intel_mine_prs.py` と `code_intel_ka_slice.py` が、ファイルとそのヘッダーの件数を書き出します。

<a id="coding-agent-lessons-benchfixturescoding_agent_qajson"></a>

### コーディングエージェントのレッスン（`bench/fixtures/coding_agent_qa.json`）

`lessons`（`memory_key` をキーとするメモリの行）と `questions`（`question`、分類体系から選ぶ `question_type`、`gold_keys`、任意の `fix_reuse`）に追記します。すべての正解キーは実在する lesson のキーでなければなりません。そうでない場合は `bench/test_coding_agent_qa.py`（`test_load_corpus_rejects_dangling_gold_key`）が失敗します。`--dry` は引き続き成功しなければなりません。

<a id="op-planning-tasks-benchfixturesop_planning_tasksjsonl"></a>

### op プランニングタスク（`bench/fixtures/op_planning_tasks.jsonl`）

タスクには、`id`、`class`（`instruction_type`、`structure`）、`prompt`、正解のプラン `gold`（ツール、必須引数、リテラル引数）、ストアの `assertions` を含めます。正解のツールや引数が MCP のサーフェスに存在しなくなった場合は、`tests/unit/test_op_planning_bench.py`（`test_every_gold_tool_and_required_arg_exists_on_the_surface`）が失敗します。op が廃止されたタスクは、削除せずに `op_planning_tasks_retired.jsonl` に移します。

<a id="skill-eval-tasks"></a>

### スキル評価タスク

`bench/skill_eval/tasks/` 配下のブリーフには、フロントマター（`task`、`namespace`、`agent`、`traps`）があり、本文で `skill-eval-…` 名前空間を指定し、その名前空間が監査されることを述べて終わります。同じ本文は `integrations/claude-code-plugin/evals/loom-memory/<task>/prompt.md` にある `claude plugin eval` のケースでもあり、フロントマターを除いてバイト単位で同一です。`tests/unit/test_skill_eval.py` はこの 3 つの規則をすべて検査し、タスク一覧を固定しているため、新しいタスクを追加するときはこのテストも更新します。[スキル評価](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)を参照してください。

<a id="wire-in-a-new-dataset"></a>

## 新しいデータセットを組み込む

1. **コミットしないこと。** `bench/datasets/` の下に置くか、パスのフラグで受け取ります。取得が必要なデータセットの場合は、`longmemeval.py` と同様に `--fetch` のパスを追加するか、ファイルが見つからないときにダウンロードコマンドを表示します。
2. **ハーネスの docstring に出典を記録します。** 出典、ファイル名、項目数、データセットが明示しているライセンスを書きます。[データセット](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)に行を追加します。
3. **データセット自身のカテゴリ**を列として残し、公開されている表が比較用のサブセットを使っている場合は、そのサブセットを明記します。
4. **公開ベンチマークの名前は `bench/` 内では使ってかまいません**が、`server/` 配下の文章で選択の理由として使ってはいけません（`tests/unit/test_public_copy.py` の `BENCHMARK_NAMES`）。
5. 使い捨ての名前空間は、`LOOM_NAMESPACE_BENCH_PREFIXES`（`server/settings/catalog.py`。新しいハーネスでは `bench-` を使います）に含まれるプレフィックスの下で作成してください。そうすれば、`loom namespaces-gc` と名前空間 GC が、中断した実行の残りを見つけられます。

<a id="write-a-new-harness"></a>

## 新しいハーネスを書く

すべてのハーネスは次の形に従います。

| ルール | 方法 |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| スタンドアロンスクリプト | argparse、`--base-url`、`--token`（デフォルトは `LOOM_API_KEY`）、`--out`。`PYTHONPATH=$PWD python bench/<name>.py` としても、モジュールとしても実行できる |
| 共通の HTTP ポリシー | `bench/_http.py`（`post_json` / `apost_json`）経由で送信する：Bearer トークン、429 時の待機、上限付きの 5xx リトライ、レスポンスボディを含むエラー |
| docstring がマニュアル | 何を測定し、何を測定しないか、データセット、コスト、実行コマンドを記載する |
| 純粋なスコアリングコア | スコアリング関数をライブ処理より上位に置き、サーバーなしでインポートできるようにして、ユニットテスト（`bench/test_<name>.py` または `tests/unit/test_bench_<name>.py`）を用意する |
| `--dry` またはセルフテストモード | サーバーも費用も使わず、合成入力でスコアラーを動かす |
| 評価用の読み取り | まず `bench/_gate.py` の `measurement_fields("memory.search")` / `("memory.ask")` をボディにマージする。`--credit` を提供する |
| 使い捨てで登録済みの名前空間 | 最初の書き込みの前に `register_bench_namespace` と `remember_bench_namespace` を呼び、処理を `erase_on_exit` の中で行い、`--keep-namespaces` フラグを用意する（`bench/bench_namespaces.py`） |
| 固定されたスコープ | 読み取りでは `namespace_scope: "this"` を送る |
| 判定あり？ 共通ゲートを使う | `--repeats`、`--gate*`、`--credit`、`--judge-calibration` には `add_gate_arguments(parser)` を使い、判定結果には `gate_from_args` を使う。リーダーモデルとジャッジモデルを固定し、上書きされた場合は警告する。ヘルプテキストに生の `%` があると `--help` が壊れる理由は `tests/unit/test_bench_gate_help_renders.py` を参照 |
| 計測器を記録する | 埋め込みモデル、LLM、プロバイダー、`/v1/health` の `build_commit` を数値と並べて記録する（`quality.py` の `run_metadata` を参照） |
| 失敗をミスと分けて報告する | リクエストエラーを数えて表示する。ミスとして採点しない |
| 対比較 | 項目ごとの差分をクラスタキー付きの `paired_bootstrap` に通し、下限値に基づいてリリースする |

そのうえで次を行います。

- [`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) の適切なセクションに項目を追加する。よくある疑問に答えるものであれば、「When to run what」の表にも行を追加する。
- Makefile ターゲットを追加するのは、誰かが日常的に実行するゲートに限り、その下限値は上書き可能な変数にする。
- `make format`（ruff が `bench/` を整形します）、`pytest bench -q`、およびコピーリントを実行する。

```bash
.venv/bin/python -m pytest tests/unit/test_public_copy.py tests/unit/test_bench_namespaces.py -q
```

コピーリントは `bench/` をスキャンします。コード、コメント、フィクスチャに、製品名、人名、社内ホスト、社内の名前空間、論文の引用、決定記録への参照を含めてはいけません。

<a id="related"></a>

## 関連ドキュメント

- [方法論](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)：ゲートとブートストラップが何を計算するか。
- [実行方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)：コマンドと出力。
- [テスト](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)：ユニットテストと統合テストのスイート。
