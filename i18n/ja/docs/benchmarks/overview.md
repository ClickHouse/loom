<!-- i18n: source=docs/benchmarks/overview.md blob=f3e3a30fcb2c5a330a968c35eb380c8c210ad12f model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md), which is authoritative.

<a id="benchmarks-overview"></a>

# ベンチマークの概要

ランキングのレバー、類似度ゲート、キャッシュのしきい値、抽出プロンプトは、ユニットテストでは判断できない挙動を変えます。これらはそれぞれ、`bench/` 配下のハーネスによって決定されます。ハーネスは、実際のプロバイダーを使用するライブサーバーに対して HTTP 経由で実行されます。このセクションでは、[方法論](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)、スイートの[実行方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)とデータの入手元、[カバレッジマトリクス](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)、リポジトリが再現する[結果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)、[ケースの追加方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md)、および[スキル評価](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)を扱います。

[`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) にすべてのハーネスの一覧があります。各スクリプトのモジュール docstring がそのマニュアルであり、プロトコル、フラグ、実行コマンドが記載されています。

<a id="what-the-suite-measures"></a>

## スイートが測定するもの

ハーネスは単体のスクリプトです。argparse と HTTP クライアントで構成され、共有フィクスチャやテストランナーはありません。

| 領域                     | 問い                                                                                           | 主なハーネス                                                                                                                     |
| ------------------------ | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| 検索の再現率             | 正しいメモリが上位 K 件に入ったか。                                                            | `quality.py`, `retrieval_recall.py`, `k_curve.py`, `plane_ablation.py`, `hard_distractors.py`                                    |
| 回答の正確さ             | 固定されたリーダーとジャッジのもとで、検索されたメモリから組み立てた回答は正しいか。           | `longmemeval.py`, `locomo_qa.py`, `memoryagentbench.py`, `membench.py`, `beam.py`, `musique.py`, `msc_dmr.py`, …                 |
| 書き込み側の品質         | 抽出は、ターンが述べている内容だけを書き込んだか。                                             | `extraction_eval.py`, `graph_eval.py`, `memory_integrity.py`, `supersession_audit.py`                                            |
| 時間とリビジョン         | 置き換えられた事実は、その区間が閉じた後は隠れ、区間内では見えるか。                           | `fama_forgetting.py`, `mutation_sequence.py`, `fact_consolidation.py`, `pi_llm.py`                                               |
| エージェントと操作       | エージェントは正しい op を選び、ストアを正しい形で残したか。                                   | `op_planning.py`, `skill_eval.py`, `op_trajectories.py`, `procedure_replay.py`                                                   |
| コスト                   | 1 回の回答や 1 セッションにいくらかかるか。                                                    | `ask_cost_rollup.py`, `session_ab.py`, `route_replay.py`, `ch_cost_by_op.py`                                                     |
| 安全性とプライバシー     | 消去はすべてのティアに及ぶか。プロンプトだけを使う攻撃者はメモリを抽出または汚染できるか。     | `erasure_compliance.py`, `memory_extraction_redteam.py`, `memory_poison.py`, `sensitivity_leakage.py`                            |
| コードインデックス       | コードインデックスは定義、呼び出し元、概念的な回答を見つけ、それ以外では回答を控えるか。       | `code_intel_qa.py`, `code_plane_ablation.py`, `code_shunt_tokens.py`, `code_ask_cache_threshold/`                                |
| ドキュメント解析         | 抽出器はドキュメントのテキスト、見出し、表、ページを読み取り、何も失わないか。                 | `docparse/`（オフライン）                                                                                                        |
| トラッカー               | `tracker.similar` は、issue がクローズされた際の元になった古い重複を見つけるか。               | `tracker_similar_bench.py`                                                                                                       |
| レイテンシとインデックスの健全性 | 検索時間はどこに費やされるか。ベクトルインデックスは総当たりと同じ結果を返すか。       | `search_latency.py`, `vector_tenant_recall.py`, `ann_final_staleness.py`, `vector_index_vs_exact.py`, `scripts/ops/benchmark.py` |
| 書き込み負荷             | 持続的な書き込みレートは、どれだけのレイテンシと ClickHouse パーツ数を要するか。               | `write_rate.py`                                                                                                                  |
| スケール                 | 名前空間、組織、キーが増えるとき、メモリが最初に突き当たる上限はどれか。                       | `scale_load.py`                                                                                                                  |
| コンシューマー受け入れ   | ある種類のデプロイは、許容できるコストで正確な回答を得られるか。                               | `acceptance/{code_agent,memory_app,plugin,harness}.py`                                                                           |

[`bench/memoryarena/`](https://github.com/ClickHouse/loom/blob/main/bench/memoryarena/README.md)（依存関係のあるタスクの後続セッションを採点するエージェントジム）と [`bench/memorydata/`](https://github.com/ClickHouse/loom/blob/main/bench/memorydata/README.md)（Loom を他の手法と並べて実行するテストベッド）は、Loom を組み込んだ外部ハーネスです。実行方法は、それぞれの README を参照してください。

ランキングのコードを変更するのではなく Loom を評価する場合は、まず [Loom を評価する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md) から始めてください。ハーネスの一覧を、プロダクトへの組み込み、独自のメモリワークロード、コードインテリジェンス、公開済み結果の再現という短い判断の流れにまとめています。

<a id="entry-points"></a>

## エントリポイント

| エントリポイント                | 実行する内容                                                                  | 必要なもの                                                        |
| ---------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `make bench-quality-gate`    | コミット済みの合成セットに対する `quality.py`。recall が 0.80 を下回ると exit 1 | 実際のエンベッダーと抽出 LLM を備えたサーバー                  |
| `make bench-algorithm-gate`  | リグレッション用フィクスチャに対する `extraction_eval.py` と `graph_eval.py`        | 実際の抽出 LLM を備えたサーバー                               |
| `make bench-quick`           | 固定された reader と judge を使い、シャッフルした 30 件に対する `longmemeval.py`     | ダウンロード済みの LongMemEval ファイル、`LOOM_API_KEY`、`OPENAI_API_KEY` |
| `make bench-matrix`          | `matrix.py`：LongMemEval における検索モード × リランク × プール幅                     | `bench-quick` と同じ                                                |
| `make bench-code-intel-dry`  | `code_intel_qa.py --dry`：完全な合成検索結果に対するスコアラー                     | なし（オフライン）                                                  |
| `make bench-code-intel`      | ロールアウトの下限値を指定した `code_intel_qa.py`                                | インデックス済みの ClickHouse リポジトリと実際のエンベッダーを備えたサーバー     |
| `make bench-code-legs`       | 3 つのレッグのアブレーションに対する `code_intel_qa.py --compare-extra`。それぞれペアで比較 | `bench-code-intel` と同じ                                          |
| `make bench-clickgap-replay` | `clickgap_code_replay.py`：PR レビューボットの呼び出しシーケンスのリプレイ        | そのコンシューマーの PR の replay JSONL                             |
| `make bench-clickgap-golden` | そのコンシューマーのゴールデンフィクスチャに対する `clickgap_golden_replay.py`      | `GOLDEN_DIR`、このリポジトリに含まれない非公開フィクスチャ                 |
| `make bench`                 | `scripts/ops/benchmark.py`：1 つの op に対する並行負荷、p50/p95/p99 と RPS | サーバー                                                            |
| `make test-unit`             | `pytest bench -q` も実行：スコアリングのコアとデータセットリーダー                  | なし（オフライン）                                                  |

その他のハーネスはすべて `python bench/<name>.py` として実行します（[実行方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。

<a id="how-a-bench-differs-from-a-production-caller"></a>

## ベンチと本番の呼び出し元の違い

ベンチは同じ操作を HTTP 経由で呼び出しますが、3 つの違いがあります。

1. **評価用の読み取り。** クレジットを付与する読み取りは、次のパスがランキングに使う行を強化するため、ベンチ自身の結果が変動してしまいます。実行時に `--credit` を指定しない限り、ハーネスは `memory.search` に `auto_touch: false` を、`memory.ask` に `reinforce: false` を送ります。
2. **使い捨てのベンチ用名前空間。** 各名前空間は `traffic_class` と `kind` を `bench` として登録されます。これにより、ベンチ用のレートバケット（デフォルトでは基本上限の半分）、ベンチ用の ClickHouse 優先度、コスト台帳の別カラムが適用され、実行の終了時に消去されます（[ベンチ用名前空間](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#bench-namespaces)）。
3. **固定された計測器。** リーダーモデルとジャッジモデルはハーネスごとに固定され、`LOOM_LLM_MODEL` には従いません。変数となるのは、サーバーの抽出モデル、エンベッダー、設定です。`--answerer loom-ask`（LongMemEval）または `--reader ask`（LoCoMo）を指定すると、`memory.ask` そのものを採点します。

サーバーのデフォルト値は、これらのベンチが選んだものです。`LOOM_LLM_MODEL` や `LOOM_EMBEDDING_MODEL` のデフォルトを変更する場合は、まず `make bench-quick` を実行してください。新しいエンベッダーには[ベースラインの再取得](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#re-baselining-after-an-embedder-change)が必要です。

<a id="coverage-and-what-it-leaves-out"></a>

## カバレッジと対象外の範囲

- **CI に品質シグナルがない。** CI は偽のプロバイダーで実行されるため、検索そのものではなくスコアラーをテストする（[理由](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#fake-providers-make-scores-meaningless)）。実際のデプロイメントでゲートを実行する人がいなければ、ランキングの退行に気づかないままマージされる可能性がある。
- **コミットされている測定値は少ない。** [結果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)には、コミット済みの各測定について、`bench/results/` 配下の成果物、実行に使った計測器、再現コマンドを記載している。自分のデータとプロバイダーでの数値は、自分のデプロイメントで算出する必要がある。
- **ほとんどのハーネスは、リポジトリに含まれないデータを必要とする。** サードパーティのコーパスはダウンロードして使う（[データセット](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)）。リプレイ、relied-on セット、トラッカーのゴールド、トレースからのレイテンシ計測は、1 つのデプロイメントの非公開データを読み取る。
- **コードのフィクスチャはリポジトリ固有である。** ClickHouse のレーンには ClickHouse/ClickHouse のインデックスが、Langfuse のレーンにはそのリポジトリのインデックスが必要になる。これらの数値は、他のコードベースについてはほとんど何も示さない。
- **名指しのシステム間スコアボードはない。** 公開データセットを用いるハーネスは、Loom の数値と、公開されている表と並べるために必要な列を報告する。リポジトリ内で比較するのは、Loom 自身のビルドとアームだけである。
- **ハードウェアの記録もキャパシティモデルもない。** ホストを記録するハーネスはない。`scripts/ops/benchmark.py` は 1 つの op を 1 つの同時実行数で測定し、`search_latency.py` は `memory.search` を名前空間のサイズ別にステージへ分解する。どちらも、実際のエージェントの利用パターンやマルチテナントでの競合はモデル化しない。
- **スキル評価が採点するのは形式であり、再現率ではない**（[スキル評価](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md#what-this-does-not-measure)）。

<a id="what-a-pass-does-not-prove"></a>

### 合格が証明しないこと

- **ゲートの合格は下限にすぎない。** ビルドが、1 つのエンベッダーの下で小さなセットに対し、記録済みのしきい値を超えたことを示すだけである。自分のデータで再現率が高いことを意味しない。
- **再現率は回答品質ではない。** メモリが 1 位にランクされても、誤読されたり古かったりすることがある。判定軸と失敗したステージを確認すること。
- **回答の正確さはエージェントの成功ではない。** ほとんどのハーネスは 1 問ずつ採点する。エージェントがメモリを使って何を行ったかを測定するのは、`code_plane_ablation.py` とエージェントのジムだけである。
- **安全性ベンチが測定するのは、名指しされた攻撃である。** `memory_poison.py` と `memory_extraction_redteam.py` は、それぞれ独自の攻撃パターンについて報告する。`erasure_compliance.py` は、すべての `loom.*` テーブルのすべての String 型の列をカナリアについてスキャンする。外部バックエンドの削除の完全性は、そのバックエンド自身の削除機能の範囲にとどまる。

<a id="related"></a>

## 関連ページ

- [検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)：検索ベンチマークが対象とする内容です。
- [コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)：コードベンチマークが対象とする内容です。
