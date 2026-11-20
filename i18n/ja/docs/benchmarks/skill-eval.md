<!-- i18n: source=docs/benchmarks/skill-eval.md blob=c855282f25bb802cf3b249aa01819ec8e21227b7 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md), which is authoritative.

<a id="skill-eval"></a>

# スキル評価

スキル評価では、エージェントが残した名前空間を基準に `loom-memory` スキルを採点します。スキルは何をどこに置くかを定めています。主張（claim）は読みやすいキーを付けた平文の `memory.set` 1 件、ドキュメントは短いポインタ行を添えて knowledge へ、識別子を持つものの表は records へ、リースは作業用メモリへ置き、改訂は同じキーを書き換えます。評価ではタスクを実行し、`namespace.audit` で名前空間を採点して、critical の指摘が 1 件でもあれば失敗とみなします。

<a id="the-pieces"></a>

## 構成要素

| 構成要素 | 場所 | 内容 |
| ----------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| タスクブリーフ | `bench/skill_eval/tasks/*.md` | アンチパターンを誘発する 3 つのブリーフです。長いログ、スニペット、テーブル、リース、保持すべき大きなモデルを含むアラート調査（`sre_alert_investigation`）、構造化フィールドを持つ PR のバッチ（`pr_review_batch`）、2 回改訂されるノート（`research_note_revisions`）です。各ブリーフは自身の名前空間を指定し、監査されることを明記しています。 |
| ランナー | `bench/skill_eval.py` | タスクを実行し（`--runner claude-code`、`codex`、`manual`、`replay`）、続いて `namespace.audit`（1 時間のウィンドウ、5 サンプル）、さらにシェイプカウンターを実行して、結果の JSON を書き出します。 |
| 記録済みエージェント | `bench/skill_eval/replays/{bad,good}_agent.jsonl` | `{op, body}` のストリームです。bad エージェントは値を二重にエンコードし、ハッシュキーを使い、メモリとして保存されたリースをポーリングします。good エージェントは[調査エージェントのメモリ](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)のガイドに従います。 |
| プラグイン評価スイート | `integrations/claude-code-plugin/evals/loom-memory/` | 同じブリーフを `claude plugin eval` のケースとしたものです。モックの Loom サーバー（`evals/mocks/loom/`）に対するツールトレースから採点します。 |
| ユニットテスト | `tests/unit/test_skill_eval.py` | 監査にハンドラー自身の分類器を使うフェイクの Loom に対して、両方のリプレイをランナー経由で実行します。bad は想定されるコードで失敗し、good は合格します。タスク一覧と評価プロンプトの本文も固定します。 |

各ブリーフの front matter には、`task`、`namespace`、`agent`、`traps`（誘発するアンチパターン）が含まれます。ランナーはリプレイストリーム内の `$ORG`、`$NAMESPACE`、`$AGENT` を置換するため、1 つの記録をどの org でも実行できます。

<a id="the-score"></a>

## スコア

`pass` は、監査の結果が **critical 0 件**、かつ **warning が 1 件以下**（`MAX_WARNINGS_TO_PASS`）であることを意味します。レポートには、各検出事項について、件数、チェックした行に占める割合、バイト数、書き込み側の対処方法が一覧で示されます。その後に行そのものから読み取った形状カウンタが続くため、グレードは監査だけで決まるわけではありません。

| カウンタ                 | 意味                                                                                                        |
| ------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `rows`                   | 有効なメモリ行                                                                                              |
| `readable_key_share`     | 最後のセグメントが長い16進数の連続ではなく、SHA 長のセグメントも含まないキー（監査の `hash_keys` ルール）   |
| `plain_text_value_share` | 文字列であり、シリアライズされた JSON ではない値                                                            |
| `documents`              | ナレッジドキュメント（`knowledge.list`）                                                                    |
| `records`                | 型付きレコード（登録済みスキーマごとの `memory.record.query`）                                              |
| `working_memory_blocks`  | ワーキングメモリのブロック（`working_memory.render`）                                                       |

形状の読み取りは監査の**後**に実行されるため、エージェントによる読み取りとしてカウントされることはありません。トークンで読み取れないカウンタは、0 として報告されるのではなく、`shape.skipped` に記載されます。

<a id="run-it"></a>

## 実行する

<a id="replay-no-model-is-called"></a>

### リプレイ（モデルは呼び出されません）

どの Loom でも動作します。ローカルスタックで十分です。

```bash
export LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY=<writer token> LOOM_ORG=<org>
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/bad_agent.jsonl            # exits 1: FAIL
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/good_agent.jsonl --baseline   # exits 0: PASS
```

実行ごとに新しい名前空間を使用してください（`--namespace skill-eval-sre-$(date +%s)`）。監査ウィンドウは 1 時間であるため、名前空間を再利用すると、前回の実行での読み取りが採点に含まれます。

オフラインでは、同じチェックをサーバーなしでユニットテストとして実行できます。

```bash
.venv/bin/python -m pytest tests/unit/test_skill_eval.py -q
```

<a id="a-live-agent-with-the-skill-loaded"></a>

### スキルを読み込んだ実エージェント

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code --baseline
```

この実行ではモデルのトークンを消費します。リポジトリのルートから次を実行します。

```bash
claude -p --plugin-dir integrations/claude-code-plugin \
  --mcp-config <generated file: loom at $LOOM_BASE_URL/mcp?toolset=all, Bearer $LOOM_API_KEY> \
  --strict-mcp-config --allowedTools "mcp__loom__*" "Skill" "Read" \
  --permission-mode bypassPermissions --output-format json --max-turns 60 \
  "<the task body>"
```

- `--plugin-dir` はチェックアウトからスキルを読み込むため、この実行では作業ツリー内のスキルのテキストを測定します。
- `toolset=all` では、デフォルトの `core` 一覧に含まれない `memory_schema.register` と `memory.record.write` が一覧に表示されます。`knowledge.upsert` は `all` でも一覧に表示されません（core にも atomic ロスターにも含まれていません）。エージェントは `loom_op` 経由でこれを呼び出します。
- 生成される MCP 設定にはトークンが含まれます。このファイルは `0600` で作成され、実行後に削除されます。
- `--max-turns`（デフォルトは 60）は実行の上限を設定します。

`--runner codex` は、同じサーバー接続で `codex exec` を実行します。Codex プラグインがインストールされている場合は、そのプラグインがスキルを提供します。

<a id="any-other-agent"></a>

### その他のエージェント

`--runner manual` はブリーフを出力します。任意のエージェントを、そこに示された名前空間に対して実行し、残された内容を採点します。

```bash
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual --audit-only
```

同じ `--audit-only` で、実サーバーを使った `claude plugin eval` の実行が書き込んだ名前空間も採点できます。

<a id="compare-with-a-baseline"></a>

### ベースラインと比較する

スキルを編集し、再度実行して比較します。

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code \
  --compare bench/out/skill_eval_sre_alert_investigation_baseline.json
```

差分テーブルには、検出コードごとに 1 行（重大度と件数、前後）と、形状カウンターごとに 1 行が含まれます。実エージェントは非決定的であるため、スキルの編集は 1 回ではなく複数回の実行で比較してください。

<a id="output"></a>

### 出力

すべての実行は `bench/out/skill_eval_<task>_<runner>_<ts>.json` を書き出します（`--out-dir` で出力先を変更できます）。`--baseline` を指定すると、`--compare` が読み込む基準ファイル `bench/out/skill_eval_<task>_baseline.json` も書き出されます。`bench/out/` は gitignore の対象です。

<a id="the-plugin-eval-suite"></a>

## プラグイン eval スイート

```bash
claude plugin eval integrations/claude-code-plugin --trust-plugin --no-publish \
  --case 'loom-memory/*' --runs 3 --threshold 1.0 --json evals-result.json
```

このコマンドは、各実行をツールトレースからオフラインで採点します。Loom MCP サーバーは固定の応答を返すモック（`evals/mocks/loom/`）なので、何も書き込まれません。グレーダーには、ドキュメント化されているグレーダータイプのみを使用します。

- `tool_used` に `max: 0` を指定し、次のいずれかに該当するメモリ書き込みを対象にします。入力にハッシュキーがある、JSON 文字列の JSON 値である、値が約 2 KB を超える、リースキーまたはロックキーである、バージョン付きのリビジョンキーである。これらは `namespace.audit` が critical または warning と判定する形です。
- `tool_used: Skill` で `loom-memory` に一致するもの。プラグインが起動したことを示す指標です。
- ワーキングメモリとアイテム単位のクレームに対する、正の最小値。

`llm` グレーダーはありません。文章をルーブリックで読み取るだけでは、ゲートになりません。ケースは名前の glob で絞り込めます（`--case sre_alert_investigation`）。プラグインが解決される場合、デフォルトである `--ablation with-without` は、プラグインなしの同じプロンプトが同じグレーダーにどのくらいの頻度で引っかかるかを報告します。

このフレームワークにはスクリプトグレーダーがないため、名前空間レベルの採点は別のコマンドで行います。`--mocks off --allow-real-servers --allow-tools "mcp__plugin_loom_loom__*"` を指定し、`LOOM_API_KEY` に書き込み用トークンを設定すると、エージェントはそのケースの名前空間に書き込みます。それを `bench/skill_eval.py --task <case> --runner manual --audit-only` が採点します。

<a id="reading-a-failure"></a>

## 失敗の読み方

各 finding の対処は、書き込み側の修正です。`loom-optimize` スキルは同じコードを、すでに存在する行の作り直しに対応付けます（[名前空間の最適化](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)）。

| 検出項目                                   | 守られなかったルール                                                |
| ------------------------------------------ | ------------------------------------------------------------------- |
| `double_encoded_values`                    | 値は一度だけ送ります。文字列に対して `json.dumps` を二度呼ばないでください。 |
| `hash_keys`                                | キーはハッシュではなく、読みやすい `<scope>/<kind>/<id>` にします。 |
| `coordination_as_memory`                   | リースとロックはワーキングメモリに置きます。                        |
| `exact_key_store`                          | キーでポーリングする代わりに、ポインタをワーキングメモリに保持します。 |
| `oversized_values`, `json_prefix_excerpts` | ドキュメントはナレッジに置き、短いポインタ行を残します。            |

SRE タスクで `documents: 0` のまま監査に通った実行は、検索が届かない場所に大きなモデルを残していました。判定結果だけでなく、shape 行も確認してください。

<a id="adding-a-task"></a>

## タスクの追加

新しいブリーフには、front matter、本文中で指定する `skill-eval-…` 名前空間、およびその名前空間が監査されることを述べる末尾の一文が必要です。あわせて、本文がバイト単位で一致する対応するプラグイン eval ケースと、`tests/unit/test_skill_eval.py` が固定しているタスク一覧の更新も必要です。詳しくは[ケースの追加](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md#skill-eval-tasks)を参照してください。

<a id="what-this-does-not-measure"></a>

## このベンチマークが測定しないもの

リコールの品質です。エージェントが書いたクレームが来週の質問に答えられるかどうかは、リコールベンチマークの役割です（[概要](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)）。このベンチマークが評価するのは、リコールを不可能にする形状です。

<a id="related"></a>

## 関連情報

- [調査エージェントのメモリ](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)：良いリプレイが従う手順。
- [名前空間の最適化](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)：監査で指摘された行の見直し。
- [Claude Code プラグイン](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)と [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)。
- [`integrations/claude-code-plugin/evals/README.md`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/evals/README.md)。
