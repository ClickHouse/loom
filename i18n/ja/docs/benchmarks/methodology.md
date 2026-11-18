<!-- i18n: source=docs/benchmarks/methodology.md blob=0e0708fc624ae94fe8f285a72bac00e00924c567 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), which is authoritative.

<a id="benchmark-methodology"></a>

# ベンチマーク手法

このページでは、ハーネスが何を計算するか、差分をノイズに対してどのように判定するか、2 つの数値がいつ比較可能になるか、そして数値にどこで偏りが生じるかを説明します。ルールはコード内にあります：`bench/_gate.py`（共通ゲート）、`bench/bootstrap.py`、`bench/judge_calibration.py`、`bench/bench_namespaces.py`、`bench/failure_taxonomy.py`（[ソースのエントリポイント](#source-entry-points)）。

<a id="recall-and-answer-accuracy-are-separate-axes"></a>

## 再現率と回答精度は別の軸

**Recall@K** は、関連するメモリが上位 K 件に入ったかどうかを測る指標です。**回答精度**は、合成された回答が正しいかどうかを測る指標です。一方が飽和しても、もう一方は低いままのことがあります。`longmemeval.py` は recall@K と判定済みの精度を別々の指標として出力します。`coding_agent_qa.py` は R@5 / R@10 / MRR を、判定済みの QA 精度とは分けて出力します。リーダーも判定器も使わず、エンベッダーだけで得た見出しの数値は再現率の値です。精度の数値と並べて比較しないでください。

<a id="task-categories"></a>

## タスクカテゴリ

各ハーネスは元のデータセットのカテゴリラベルをそのまま保持するため、結果はカテゴリごとに読み取れます。

| ハーネス              | カテゴリ                                                                                                                                                        |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `locomo_qa.py`        | 1 マルチホップ、2 時間、3 オープンドメイン、4 シングルホップ、5 敵対的。`overall_comparable` は 1〜4 を対象とし（公開されている列）、`overall_all` は 5 を棄権として採点します。 |
| `longmemeval.py`      | データセットの `question_type`（`--question-type`）。`--stale-check` は、置き換え済みの値を返した誤った知識更新の回答にラベルを付けます。                          |
| `memoryagentbench.py` | 正確な検索、テスト時学習、長距離理解、矛盾の解決。                                                                                                              |
| `beam.py`             | 10 種類のメモリ能力。データセットのルーブリックジャッジで採点します。イベント順序には Kendall τ_b × F1 が加わります。                                           |
| `membench.py`         | 事実レベルと内省レベルの、参加シナリオと観察シナリオ。                                                                                                          |
| `code_intel_qa.py`    | 行の `type`：`symbol_def`、`callers`、`conceptual`、`hub`、`history`、`test_set`、`abstain_probe`。さらに `lane` と `facets`。                                  |
| `coding_agent_qa.py`  | バグ修正の反映先、インフラ修正、リファクタリング箇所、不安定なテスト、パフォーマンスリーク、スキーマ移行、複数セッションにわたる因果関係、時間、チームの規約。      |
| `op_planning.py`      | 各タスクの `class`：`instruction_type` × `structure`。                                                                                                          |

<a id="ground-truth"></a>

## 正解データ

| 種類                            | 場所                                                                                                 | 作成方法                                                                                                                           | 弱点                                                                           |
| ------------------------------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| 部分文字列の正解キー            | `bench/datasets/synthetic_memory_qa.json`, `extraction_regressions.json`, `graph_regressions.json`   | 手書き。返された `memory_key` または抜粋に正解文字列のいずれかが含まれていればヒットとする（大文字小文字を区別せず、`_` は空白として扱う） | 意図的に緩い。冗長な抜粋が偶然一致することがある                               |
| 公開されている正解回答          | サードパーティのデータセット（[データセット](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)）                                             | データセットの回答と根拠ラベル                                                                                                     | データセットのラベル誤りや飽和をそのまま引き継ぐ                               |
| 手作業でラベル付けしたクエリ → キーの組 | `retrieval_recall.py`, `plane_ablation.py`, `hard_distractors.py` の `--dataset`                        | 利用者が既存のコーパスに対してクエリにラベルを付ける                                                                               | 小規模なセットであり、ラベルはリポジトリには置かない                           |
| 利用に基づくラベル              | `relied_on_set.py` → `bench/datasets/relied_on/`                                                     | ある検索で返されたキーのうち、短い時間枠内にエージェントが後で利用した（`used` / `used_cited` イベント）ものを正解とする                         | id ではなく時刻で結合する。現行のランキングがすでに上位に出したものが有利になる |
| マージ済み PR から抽出          | `bench/fixtures/code_intel_mined_*.jsonl`, `code_intel_history.jsonl`, `code_plane_ablation.py mine` | クエリ = issue または PR のタイトル。正解 = 変更された `src/` のファイル。テキストが正解パスの語幹を含む行は除外し、その割合を報告する | インデックスにはすべての修正が含まれる。絶対値は上限なので、対応のある差分で読むこと |
| モデルが作成しモデルが検証      | `bench/fixtures/code_intel_oss.jsonl`                                                                | クエリとパスをモデルが書き、モデルの判定器が確認したサードパーティのコーパス                                                       | ミスの原因がラベル側にある場合がある。`secondary` のパスは評価されない         |

構造的なコードの正解（`symbol_def`, `callers`）は厳密だが、上流リポジトリの知識に基づいて書かれている。新たにバックフィルするたびに、ゲートなしで一度実行し、不一致を確認して正解を修正すること。誤った正解はコーパスのバグである。

判定器のキャリブレーションでは、独自のプローブを作成する（`build_probes`）。ある項目の正解を読み手が答えるような表現にしたものが正答プローブ、同じカテゴリの別の項目の正解が誤答プローブとなる。敵対的な項目は除外する。

<a id="judges"></a>

## ジャッジ

リーダーとジャッジは測定器の一部であり、評価対象のシステムには含まれません。

- **ハーネスごとに固定。** `longmemeval.py` と `coding_agent_qa.py` は `gpt-4o-mini` で読み取り、`gpt-4o-2024-08-06`（LongMemEval のジャッジ用スナップショット）で判定します。`locomo_qa.py` は `gpt-4.1-mini` で読み取り、`gpt-4.1` で判定します。`BENCH_READER_MODEL` / `BENCH_JUDGE_MODEL` でこれらを上書きでき、その場合は比較可能性に関する警告が出力されます。`OPENAI_BASE_URL` には互換性のあるゲートウェイを指定します。
- **Loom の回答の採点。** `longmemeval.py --answerer loom-ask` と `locomo_qa.py --reader ask` は `memory.ask` を採点します。`--ask-arms` は、1 回の取り込みに対して `memory.ask` の複数の構成を比較します。
- **ルーブリックはファイル。** `bench/rubrics/entailment.md` は、`dialogue_consistency.py` が読み込む NLI プロンプトです。`beam.py` はデータセットのルーブリックジャッジを使用します。
- **一致度。** `locomo_qa.py --second-judge-model` はサンプルを再採点し、一致率と Cohen のκ係数を報告します。

字句ベースのスコアは、公開されている字句ベースの表と並べて比較するためだけに存在します。`longmemeval.py --lexical-scores` は、**簡略化した**回答に対してトークン F1、BLEU-1、ROUGE-L を追加します（`--simplify-model`、デフォルトはジャッジ。指定がなければ、その旨を明示したヒューリスティックを使用）。簡略化しない場合、これらは冗長さを採点することになり、誤った日付のほうが正しい文よりも高いスコアになることがあります（[参考値](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#lexical-scoring-reference-values)）。字句ベースのスコアがジャッジに取って代わることはありません。

<a id="metrics"></a>

## メトリクス

| メトリクス                 | 定義                                                                                                                                                        | 計算箇所                                         |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 再現率@K                   | 上位 K 件に正解が含まれる質問の割合                                                                                                                         | `quality.py` `summarise`, `retrieval_recall.py`  |
| MRR                        | 最初の正解の順位の逆数 1 / rank の平均（正解がない場合は 0）                                                                                                | 同上                                             |
| rank@1                     | 正解が1位になった質問の割合                                                                                                                                 | 同上                                             |
| 適合率@K                   | 返されたヒットのうち正解の割合の平均                                                                                                                        | `quality.py`                                     |
| `best_cos_rank`            | コサイン類似度が最も高いヒットの順位。1位との差は、融合によってより弱い一致が先頭に置かれたことを示す                                                       | `retrieval_recall.py`                            |
| 厳密一致@1、呼び出し元集合の F1 | コードの構造に関する厳密一致の指標                                                                                                                     | `code_intel_qa.py`                               |
| 回答控えの適合率 / 再現率  | 再現率 = 回答を控えたプローブ数 / プローブ数。適合率 = 回答を控えたプローブ数 / 回答を控えた総数                                                           | `code_intel_qa.py`                               |
| `placebo@k`                | 質問ごとに、全ての概念的な質問に対してシステムが返したパスのプールから、同数のパスを（シード固定で）抽出したもの。これを下回る再現率は、順位付けではなくプールのカバレッジを示す | `code_intel_qa.py`                               |
| 回答あたりのトークン数     | 回答した質問ごとの `tokens_returned` の平均 / p90                                                                                                           | `code_intel_qa.py`、受け入れベンチ               |
| 判定正答率                 | 繰り返し試行のうち、多数決のラベルが CORRECT となる割合                                                                                                     | `_gate.py`                                       |
| フリップ率、不一致率       | 同一アーム内で、繰り返した項目のうちラベルが一致しなかった割合。アーム間で、多数決ラベルが異なるペアの割合                                                  | `_gate.py` `flip_rate`, `GateResult.discordance` |
| 失敗ステージ               | `extraction`, `graph_resolution`, `retrieval_ranking`, `compression_loss`, `reader`, `not_verbatim`                                                         | `failure_taxonomy.py`                            |

失敗ステージは、取りこぼした項目ごとにパイプラインの順序で計算します。再現されなかった場合は `retrieval_ranking`、取得した抜粋に正解が含まれるのに回答が誤っていた場合は `reader`、取得したメモリの背後にある生の証拠にのみ正解がある場合は `compression_loss`、正解がどこにも原文のまま存在しない場合は `not_verbatim` です。`lme_diagnose.py` は、LongMemEval の取りこぼしをこれらのラベル付きで出力します。検索結果が変わらないまま `reader` が悪化した場合は、reader の問題です。

リクエストの失敗は比率の計算から除外し、取りこぼしとしては数えません（`code_intel_qa.py` は `errors(excluded)` を出力します）。`quality.py` は HTTP の失敗が1件でもあると中断します。

<a id="baselines"></a>

## ベースライン

Loom は、同じ ingest に対して同じ計測器を使い、他の手法および自身の各 arm と比較します。

| ベースライン                                                      | 場所                                                                               |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 字句検索のみ、字句検索とクエリ拡張、ベクトル検索のみ              | `make bench-code-legs`：それぞれがハイブリッドのデフォルトに対するペアの B arm      |
| 要約のみのメモリ                                                  | `msc_dmr.py` の `digest` arm（`ask`、`search`、`search_recall`、`search_reader` と並置） |
| 原文のまま保存と事実の抽出                                        | `memoryarena/` と `memorydata/` の `verbatim` / `extract` モード                    |
| ロングコンテキスト、BM25、素の密検索                              | `memoryarena/memory_server.py` の arm                                              |
| プラグインを無効にしたコーディングエージェント                    | `code_plane_ablation.py` の arm A                                                  |
| ファイルの一括読み込み                                            | `code_shunt_tokens.py` の "cold" arm                                               |
| 総当たりの最近傍探索                                              | `vector_tenant_recall.py`                                                          |
| 強力な合成モデル 1 つ、または安価な合成モデル 1 つ                | 固定した検索結果に対する `route_replay.py` の strong、cheap、cascade arm            |
| 移行元システム自身のネイティブなランキング                        | `clickgap_migration_validate.py`：エクスポートの `recall_count` 順                  |
| プレーンごとのアブレーション                                      | `plane_ablation.py` の `execution_planes` マスク                                    |

<a id="statistics"></a>

## 統計

<a id="paired-clustered-bootstrap"></a>

### 対応ありのクラスタブートストラップ

ランキングの変更は、平均ではなく、対応ありの差分の**下限**に基づいて出荷します。`paired_bootstrap` はアイテムごとの差分（B から A を引いた値）を受け取り、クラスタ単位で復元抽出によるリサンプリングを行い（10,000 回、`seed=7`）、2.5 パーセンタイルと 97.5 パーセンタイルを読み取ります。改善と見なすのは、下限がゼロを上回る場合のみです。同じメカニズムに関するアイテムは一緒に動くため、ハーネスはクラスタキーを渡します。キーは、会話、質問タイプ、コードの場合はパスの先頭 2 セグメント（`cluster_of_path`）です。クラスタ数が少なすぎると区間が狭くなりすぎるため、下限と併せて `clusters` を確認してください。`code_plane_ablation.py` は、シード間のタスクごとの平均に対して、対応ありの Wilcoxon 符号順位検定を追加します。

<a id="repeats-and-the-majority-label"></a>

### 反復と多数決ラベル

リーダーは温度 0 でも結果が変わり、合成の中の検索はクエリを再分解します。`--repeats N` は、同じ検索結果に対して各アイテムの質問と判定を N 回行い、**多数決**のラベルを採用します。同数の場合は WRONG とし、すべての実行がエラーになった場合に限り、そのアイテムを ERROR とします。ゲートの実行では 3 回の反復を使います（`GATE_REPEATS`）。反復間の `top_keys` に対する `identical_share` を見ると、判定の反転がリーダーによるものか検索によるものかが分かります。反復はラベルのノイズを減らします（[シミュレーション](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#majority-voting-on-simulated-reader-noise)）。サンプリングのノイズを減らせるのは、アイテム数を増やすことだけです。

<a id="the-verdict"></a>

### 判定

`paired_gate` は、各アームの多数決ラベルに対してブートストラップを実行します。

| 判定         | 条件                                                                                                                                                                       |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pass`       | 95% 下限が `--gate-min-delta` を上回る（デフォルトは 0。負の値は非劣性ゲートになります）                                                                                   |
| `fail`       | 95% 上限が閾値を下回る                                                                                                                                                     |
| `unresolved` | 区間が閾値をまたぐ。帰無帯域（`--gate-band`、デフォルトは `DEFAULT_BAND_PP` = 4.0 pp）の内側ならノイズフロアであり、外側なら検出力が不足している                           |

どちらかのアームの反復内で不安定なアイテムは、安定アイテムのみのペアとしても報告されます。

**自分の帯域を測定してください。** デフォルト値は、1 つのハーネスと 1 つのリーダーから得たものです。同じ構成をアーム A と B として `--repeats 3 --gate` で実行し、その結果のうち広い側（`band_from_null`）を、実際の比較で `--gate-band` として渡してください。

<a id="how-wide-the-interval-is"></a>

### 区間の幅

幅は n だけでなく、アーム間で結果が食い違う対応アイテムの数によって決まります。以下の行は、固定の差分に対する `paired_bootstrap` の結果です（+1 = B が正解で A が不正解、−1 はその逆、0 = 一致）。

| n                    | 勝ち / 引き分け / 負け           | 平均差分   | 95% 区間          | 解釈                                           |
| -------------------- | -------------------------------- | ---------- | ----------------- | ---------------------------------------------- |
| 60                   | 1 / 59 / 0                       | +1.7 pp    | +0.0 .. +5.0 pp   | 1 問のみ。下限がゼロを超えない                 |
| 30                   | 2 / 26 / 2                       | 0          | −13.3 .. +13.3 pp | 同一構成のペア：13 ポイントの振れは範囲内      |
| 60                   | 5 / 50 / 5                       | 0          | −10.0 .. +10.0 pp |                                                |
| 164                  | 13 / 138 / 13                    | 0          | −6.1 .. +6.1 pp   |                                                |
| 500                  | 40 / 420 / 40                    | 0          | −3.6 .. +3.6 pp   |                                                |
| 60                   | 6 / 52 / 2                       | +6.7 pp    | −1.7 .. +16.7 pp  | 「7 ポイントの改善」だが unresolved            |
| 60                   | 8 / 50 / 2                       | +10.0 pp   | +0.0 .. +20.0 pp  | 下限がゼロに届くだけで、pass ではない          |
| 500                  | 40 / 440 / 20                    | +4.0 pp    | +1.0 .. +7.0 pp   | n が大きいため、より小さな改善でも pass になる |
| 60、10 件ずつ 6 クラスタ | 8 / 50 / 2、勝ちは 2 クラスタに集中 | +10.0 pp   | +0.0 .. +30.0 pp  | 相関のあるアイテムが区間を広げる               |

```bash
.venv/bin/python - <<'EOF'
from bench.bootstrap import paired_bootstrap
def row(w, t, l, clusters=None):
    d = [1.0] * w + [-1.0] * l + [0.0] * t
    print(paired_bootstrap(d, clusters).line(unit="pp", scale=100))
row(1, 59, 0); row(2, 26, 2); row(5, 50, 5); row(13, 138, 13); row(40, 420, 40)
row(6, 52, 2); row(8, 50, 2); row(40, 440, 20)
row(8, 50, 2, [f"c{i // 10}" for i in range(60)])
EOF
```

<a id="set-sizes"></a>

### セットのサイズ

小さなセットでは、近いアーム同士を順位付けできません。

| セット                                 | サイズ                       | 1 アイテムあたり                  |
| -------------------------------------- | ---------------------------- | --------------------------------- |
| `synthetic_memory_qa.json`             | 15 問                        | 6.7 ポイント：崩壊を検出できるのみ |
| `code_intel_qa.jsonl` の概念スライス   | 60 問中 36 問                | 2.8 ポイント                      |
| アルゴリズムゲートのフィクスチャ       | 抽出 19 件、グラフ 2 件      | スモークテスト                    |
| `code.ask` のキャッシュペア            | 言い換え 42 件、別内容 43 件 | 2 ポイント超                      |

<a id="judge-calibration"></a>

### 判定器のキャリブレーション

`bench/judge_calibration.py` は、プローブに対する判定器の真陽性率と偽陽性率を測定します。観測される正解率は `TPR·p + FPR·(1−p)` であるため、対応ありの差分は Youden の `J = TPR − FPR` の分だけ縮小します。`--judge-calibration <json>` を指定すると、ゲートは `Δ/J` を報告し、キャリブレーションセット自体の分散（デルタ法）の分だけ区間を広げます。`J ≤ 0` の判定器は情報を持たないため、入力はそのまま通過します。

<a id="when-two-numbers-compare"></a>

## 2つの数値を比較できる条件

以下はすべて一致している必要があります。1つでも異なる場合は、その比較を比較不可（non-comparable）と表記してください。

| 一致させる項目 | 理由 |
| ------------------------- | ------------------------------------------------------------------------------------------- |
| 評価軸 | Recall@K と judged accuracy は異なる指標です |
| リーダーとジャッジ | より強力なリーダーは推論負荷の高いカテゴリのスコアを動かし、ジャッジが異なるとラベルが変わります |
| データセットの分割とスライス | LongMemEval の oracle と S、LoCoMo のカテゴリ 1–4 と 1–5、1つのレーンとファイル全体 |
| 検索モード | 固定しない場合の既定値は `LOOM_MEMORY_SEARCH_DEFAULT_MODE` です。別のモードはアブレーションです |
| エンベッダーとサーバービルド | エンベッダーを入れ替えると類似度のスケール全体が変わります。`quality.py` の `.meta.json` は両方を記録します |
| インデックスのリビジョン（コード） | `code_intel_qa.py` は、回答を返した `as_of.commit` と古いもの（stale）の件数を記載します |
| クレジット付与 | `--credit` を付けた実行は、読み取った内容を強化します |
| 精度の分母 | 棄権（abstention）を除外すると、より多く棄権するアームが有利に見えます |

**棄権。** judged ゲートは棄権を不正解として数えます。`route_replay.py` は両方を報告します。`accuracy` = correct / (correct + wrong)、`accuracy_abstain_wrong` = correct / (correct + wrong + abstained) で、後者がゲートの分母です。エラーとなった ask はどちらからも除外されます。判定が参照する方を引用し、どちらであるかを明記してください。

**上限値とプラセボ。** マイニングされたコードのレーンは、すべての修正を含むインデックスに対して実行されます（各アブレーションタスクの `fix_in_index`）。絶対値ではなく、対応のある差分を引用してください。`placebo@5` / `placebo@10` を上回らない recall は、プールのカバレッジによるものです。セットが小さいと、プラセボは自明に強くなります。ほとんどのアームが上限付近のスコアになるセットでは、アーム同士を区別できません。カテゴリ別の結果と stable-only のペアを確認してください。

<a id="fake-providers-make-scores-meaningless"></a>

## フェイクプロバイダではスコアが意味をなさない

チェックアウトの `.env` では `LOOM_EMBEDDING_MODEL=fake-ngram-3072` を、CI では `fake-ngram-1024` を使い、どちらも `LOOM_LLM_PROVIDER=fake` で動作します（リリース済みのスタックは `local` で埋め込みを行い、フェイク LLM を使い続けます）。フェイク埋め込み（`server/embedding/fake.py`）は文字トライグラムをバケットにハッシュするため、類似度は意味ではなく綴りに従います。フェイク LLM（`server/llm/fake.py`）はアンカーフレーズをキーに、整形済みの JSON を返します。そのため、パイプラインは実際には何も抽出せずに動作します。これらを使うと次のようになります。

- リコールは、質問と保存済みテキストの文字列の重なりを測定します。
- 抽出ベンチは、プロンプトではなくフェイクの出力を測定します。
- 適応型キャリブレーションはフェイク埋め込みをスキップするため、類似度ゲートはスカラーのデフォルト値を使います。

フェイク埋め込みでゲートが通っても、ゲートがないより悪い結果になります。測定されていない安全性を報告してしまうからです。そのため `make bench-quality-gate` と `make bench-code-intel` は CI に含まれていません（これらを強制するには有料プロバイダのシークレットが必要です）。CI が実行するのは、純粋なスコアリングコード（`pytest bench`）と、ゲート関数のユニットテスト（`quality.gate`：境界値は合格、0/0 は不合格）だけです。ベンチ用サーバーでは `LOOM_REQUIRE_REAL_PROVIDERS=true` を設定してください。フェイクのプロバイダやキーのないプロバイダがある場合、実行に時間をかける前に `/v1/health` が `degraded` を報告します。

<a id="reproducibility-controls"></a>

## 再現性の制御

| 制御                       | 仕組み                                                                                                                                                                             |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 決定的なリサンプリング     | `paired_bootstrap` と `paired_gate` は `seed` のもとで決定的です。ゲートは `resamples` と `seed` を記録します                                                                      |
| 決定的なサンプル           | サンプル用のフラグはシードを取ります（`musique.py --seed`、`judge_calibration.py`、マイニングしたレーンのサンプル）                                                                |
| 記録されるサーバー計測情報 | `quality.py` は `/v1/health` から取得した `embedding_model`、`embedding_dim`、`llm_model`、`llm_provider`、`build_commit` を `.meta.json` に書き出します。`code_intel_qa.py` は `as_of.commit` を記録します |
| 固定されたスコープ         | 検索ハーネスは `namespace_scope: "this"` を送るため、読み取り可能な他の名前空間の行がウィンドウに入ることはありません。アドホックなプローブでも同様にしてください                  |
| 評価時の読み取り           | `measurement_fields()` は `auto_touch: false` / `reinforce: false` を送ります。`--credit` で有効化できます。過去に読み取った名前空間では `reinforcement_weight: 0` を渡すか、新しい行を使ってください |
| 新規の名前空間             | 項目または会話ごとに使い捨ての名前空間を 1 つ作り、最後に消去します                                                                                                                |
| ウォームアップ             | `search_latency.py --warm 1` はクエリごとの最初の呼び出しを破棄するため、埋め込みキャッシュのミスは検索の計測に含まれません                                                        |
| トークンの有効期間         | 実行より長く有効なトークンを発行します（[実行方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#2-mint-a-token-that-outlives-the-run)）                  |

<a id="bench-namespaces"></a>

### ベンチマーク用の名前空間

データを投入するハーネスは、使い捨ての名前空間（`lme-…`、`locomo-…`、`mab-…`、`qabench-…`。新しいハーネスは `bench-` を使います）を作成します。`bench/bench_namespaces.py` は次のように動作します。

- 最初の書き込みの前に、それぞれを `traffic_class = "bench"`、`kind = "bench"`、`capture_generation_io = True`（デフォルト）で登録します。
- 処理を `erase_on_exit` の内側で実行します。実行が終了、失敗、または中断されたときに、発行したすべての名前を 1 回のパージ消去で消去します。本体は `external: false` を設定するため、外部バックエンドには触れません。
- `--keep-namespaces` を指定すると、後の再スコアリング（`--namespaces-json`）のために名前空間を残し、アイドル状態の回収対象にならないよう `kind = "memory"` で登録します。

消去には org 管理者のトークンが必要です。拒否された場合、ハーネスは `loom namespaces-gc` コマンドをログに出力します。`namespace_gc` ワーカーは、`LOOM_NAMESPACE_GC_IDLE_HOURS` を過ぎてアイドル状態にある `kind = bench` の名前空間をパージします。発行された名前は `LOOM_NAMESPACE_BENCH_PREFIXES` のいずれかのプレフィックスで始まるため、中断した実行の名前空間も見つかります。

<a id="http-policy"></a>

### HTTP ポリシー

ハーネスは `bench/_http.py` を通じて POST します（`post` / `apost` はレスポンスを返し、`post_json` / `apost_json` は本体を返します）。

| レスポンス                                 | ポリシー                                                                                                         |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| 429                                        | `Retry-After` の分だけ待ってリトライします（指定がなければ 1 秒、0.2〜60 秒に制限）。待機は 5xx の回数に数えません |
| 5xx                                        | `0.5 × 2^n` 秒のバックオフで、`attempts` 回リクエストを送るまでリトライします                                    |
| 400 以上のその他のステータス、または最後の 429 / 5xx | 本体の先頭部分を付けて `httpx.HTTPStatusError` を送出します                                                      |
| トランスポートエラー                       | そのまま伝播します。再起動をまたいで動作するハーネスは独自のループを持ちます                                     |

<a id="sources-of-bias"></a>

## バイアスの要因

| 要因                     | 影響                                                                               | コード上の緩和策                                                     |
| ------------------------ | ---------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 作者が作成したセット     | システムを調整する人が合成セットを作り、そのしきい値も選んだ                       | スモークゲートにとどめる。順位の判断には公開セットか利用に基づくラベル付きセットを使う |
| 寛容な部分文字列の正解   | 冗長な抜粋に、偶然正解の語が含まれることがある                                     | recall に加えて MRR、rank@1、precision@K を見る                      |
| 上限値のコードレーン     | インデックスにすべての修正がすでに含まれている                                     | ヘッダーの警告、`fix_in_index`。対応のある差分のみを引用する         |
| モデルが作成した正解     | サードパーティのコード検索ラベルは、モデルが書いて確認した                         | `secondary` パスは加点しない。対応のある読み方のみ                   |
| 利用に基づくラベル       | 頼りにしているセットには、現在のランキングが提示したものしか含まれない             | ブートストラップ境界での対応のあるスイープ。複数ある情報源の一つとして扱う |
| ジャッジの寛容さ         | LLM ジャッジが、話題は合っているが誤った回答を一定の割合で通してしまう             | `judge_calibration.py` の TPR/FPR、`--second-judge-model`            |
| ジャッジとリーダーのドリフト | 固定した名前の背後でプロバイダーが改訂され、ラベルが変わる                     | データセットが指定する場合はスナップショットを固定する。両方のアームを同時に実行する |
| 自己強化                 | 加点付きの読み取りが、次のパスのランキングを強化する                               | 既定では評価用の読み取りを使う                                       |
| 除外されたエラー         | 失敗が難しい項目に集中すると、比率が上がる                                         | エラー数を出力する。`quality.py` は失敗があれば中止する              |
| エージェントベンチでのリーク | エージェントがネットワークや git 履歴から修正を見つける                         | アブレーションの除外台帳がそのようなセルを除外する（フェイルクローズ） |
| 飽和                     | ほとんどのシステムが満点を取るカテゴリでは、アームの順位を付けられない             | カテゴリ別の結果。安定したもののみのペア                             |
| ハーネスの同等性         | 外部のテストベッドは、すべての手法に対して各クエリを独自のテンプレートで包む       | アダプターがその同等性を保つ                                         |

<a id="source-entry-points"></a>

## ソースのエントリポイント

- `bench/_gate.py`：`paired_gate`、`verdict_for`、`majority_label`、`flip_rate`、`JudgeCalibration`、`measurement_fields`、`add_gate_arguments`。
- `bench/bootstrap.py`：`paired_bootstrap`、`cluster_of_path`。
- `bench/judge_calibration.py`：`build_probes`。
- `bench/bench_namespaces.py`：`register_body`、`erase_on_exit`、`erase_on_exit_sync`。
- `bench/failure_taxonomy.py`：`classify_answer_failure`。
- `bench/answer_scoring.py`：`token_f1`、`bleu1`、`rouge_l`、`cohen_kappa`。
- テスト：`tests/unit/test_bench_gate.py`、`test_bench_bootstrap.py`、`test_bench_judge_calibration.py`、`test_bench_namespaces.py`、`test_bench_failure_taxonomy.py`、`test_answer_scoring.py`、`test_bench_gate_help_renders.py`。

<a id="related"></a>

## 関連ページ

- [実行方法](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)と[結果](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)。
- [検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)：ランキングを左右する要因。
