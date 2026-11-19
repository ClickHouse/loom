<!-- i18n: source=docs/features/code-index/history-and-prs.md blob=6c133daf27b7ef459055a1cc94a7dd9563aa4255 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md), which is authoritative.

<a id="history-prs-and-releases"></a>

# 履歴、PR、リリース

これらの op にはマージ済み PR の履歴が必要です。`code_pr_history` ワーカーはデフォルトでオフになっています（`LOOM_CODE_PR_HISTORY_ENABLED`）。

PR プレーンは「コードがどのようにして現在の状態になったのか」に答えます。インデックスと同じコード名前空間に存在するため、履歴に関する回答では、マージ済み PR のファイルを現在のシンボル、呼び出し元、テストと 1 回の読み取りで結合でき、質問時に GitHub を呼び出す必要はありません。チームの課題やインシデントの記録は、リンクされた[トラッカー](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)を通じて結合されます。

<a id="the-pr-plane"></a>

## PR プレーン

| テーブル                  | 保持する内容                                                                                                       | 書き込み元                                                                      |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `loom.code_pr_history`    | 名前空間のブランチにマージされた PR ごとに 1 行：番号、タイトル、作成者、ラベル、ファイル、リンクされた issue、マージコミット | `code_pr_history` ワーカー。最初の数か月分は `loom code backfill-prs`           |
| `loom.code_pr_reviews`    | マージされた PR のインラインレビューコメント                                                                       | 同じワーカー（`LOOM_CODE_PR_REVIEWS_ENABLED`、デフォルトで有効。トークンが必要） |
| `loom.code_gh_items`      | 状態を問わない issue と PR。オープンな PR については head が変更するファイルも保持する                             | 同じワーカー（`LOOM_CODE_GH_ITEMS_ENABLED`、デフォルトで有効。匿名の場合はスキップ） |
| `loom.code_backport_gaps` | 毎晩作成される、セット全体のバックポートレポート                                                                   | `code_backport_gaps` ワーカー                                                   |

ワーカーは `LOOM_CODE_GITHUB_TOKEN` またはエントリの `github_token` を使用し、どちらもない場合は GitHub の匿名レート制限が適用されます。リリースブランチの名前空間には、そのブランチにマージされた PR が保持されるため、「2.4 で何が変わったか」は 2.4 の名前空間を読めば分かります。`loom code backfill-prs` は、新しいリポジトリに対して `LOOM_CODE_PR_BACKFILL_MONTHS`（12）か月分の履歴を読み込みます（[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)）。

<a id="which-op-answers-which-question"></a>

## どの op がどの質問に答えるか

| 質問                                                                      | Op                                                                                       |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| この PR、またはこれから行う変更について説明してほしい                     | `code.context_pack` (`pr_number` または `task`。`sections` は対処するブロックだけを残す) |
| このシンボルやパスに触れたマージ済み PR はどれか。レビュアーは何と言ったか | `code.history`, `code.symbol_history`                                                    |
| リリース A と B の間で、これらの行に最後に触れたマージ済み PR はどれか    | `code.blame_range`                                                                       |
| このマージ済み PR でレビュアーは何を確認したか。通常は何を確認するか      | `code.review_brief`, `code.conventions`                                                  |
| PR を開く前に：同様の PR がすでに存在するか                               | `code.pr_check`                                                                          |
| このバグを持ち込んだ可能性があるマージ済み PR はどれか                    | `code.bisect_candidates`                                                                 |
| 修正で取り除かれたバグを、まだ抱えているリリースブランチはどれか          | `code.backport_gap` (1 件の修正)、`code.backport_gaps` (ラベル付きのすべての修正)        |
| このコミットを最初に含んだ CI ビルドはどれか                              | `code.first_build`                                                                       |

引数は[操作リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)に記載しています。

<a id="history-of-a-symbol-a-path-or-a-line-range"></a>

### シンボル、パス、行範囲の履歴

`code.history` は、シンボル (`name` または `symbol_id`)、`path`、または指定した PR の `numbers` に最近触れたマージ済み PR を一覧にします。各 PR について、番号、タイトル、作成者、ラベル、そのファイルに対する人間のレビューコメント（最大 `reviews_per_pr` 件、デフォルトは 3、`0` はなし）を返します。`code.symbol_history` はこれを 1 つの関数の回帰履歴に絞ります。その範囲に触れた PR、リンクされた issue と revert、範囲内のレビューコメントを返します。

`code.blame_range` は `path`、行範囲（最大 400 行）、およびリリースバージョン (`from_version`, `to_version`) またはコミット sha で表したウィンドウを受け取ります。ウィンドウ内の PR 台帳のファイル一覧（下限）と、ミラー上の `git blame -w -M -L` を組み合わせ、blame された各コミットを PR に対応付けます。リリースブランチの名前空間では、呼び出し元がトランクを読める場合に限り、トランク名前空間の台帳も読みます。読めない場合はトランクの行を除外し、`legs.ledger.skipped` にその名前が入ります。リリースバージョンは、呼び出し元がそのブランチ名前空間を読めるならそのインデックス済み head に解決され、読めなければミラーのブランチまたは `-new` タグを通じて解決されます。

- `stage: "introduced"`（デフォルト）は、バックポート、revert、空白のみまたはリネームのみの変更の順位を下げます。また、囲んでいる関数を名指ししている PR、その中でレビューコメントを受けた PR、リンクされた issue が `mentions` の語を挙げている PR の順位を上げます。
- `stage: "last_touched"` は blame の順序を保ちます。

「最後に触れた」については正確な答えですが、「バグを持ち込んだ」については正確ではありません。`note` にもそう記されます。

<a id="review-briefs-and-conventions"></a>

### レビューブリーフと規約

`code.review_brief` は、PR のマージ時にインジェストが作成したブリーフを読みます。内容は、変更された各ファイルの分類、触れられた関数とその呼び出し元およびファンイン、それらをカバーするテスト、レビュアーの発言です。

`standard` と `full` のティアでは、ブリーフに `rationale` セクションも含まれます。これは、リポジトリのトランクのコード名前空間にある設計上の決定と制約（型が `decision` の行、または `decision` か `constraint` のタグが付いた行で、pre-edit ブロックに示されるとおり active または conflicted のもの）のうち、`files` またはコードリンクが PR のファイルに触れるものです。呼び出しのたびに読み込まれ、呼び出し元が読める場合に限りトランク名前空間から取得し、ブリーフと一緒には保存されません。そのため、マージ後に記録または変更された決定は次の読み取りで反映されます。ブリーフが保持する変更ファイルのうち、ソースファイルを先頭にして最初の `LOOM_CODE_REVIEW_BRIEF_RATIONALE_PATHS` (10) 件を読み、確認すべき内容がある行を優先して最大 `LOOM_CODE_REVIEW_BRIEF_RATIONALE_ROWS` (8) 行を保持します。行は丸ごと返し、`check` の行を先に、次に制約を並べ、`LOOM_CODE_REVIEW_BRIEF_RATIONALE_TOKENS` (250) トークンまで含めます。先頭の行は常に含めます。ティアの予算によって、他のセクションと同様にこのセクションが削られることもあります。各行には、pre-edit ブロックのその行の内容（値は行の長さで切り詰め、ソース、`(pinned)`、コード変更の注記は保持）、`pinned`、`code_state`、`verdict` が付きます。読み取る変更ファイルごとに `memory.for_file` の読み取りが 1 回行われ、ClickHouse への読み取りは 4 回です（files の一致、コードリンク、そのメモリ、リンクされた行。リンクが多いファイルでは最後の読み取りを分割します）。各読み取りは最大 50 行を保持し、値は最大 `LOOM_MEMORY_FOR_FILE_WHOLE_TOKENS` × 48 バイトです。

チェックは 2 種類の名前を探します。1 つは行の `Rejected:` 句（`Realized:` 句がある場合はそこまで）にあるコード形式の名前（バッククォートで囲んだもの、`A::b` のように修飾されたもの、`f()` のような呼び出し）です。もう 1 つは制約の値の中にある snake_case の名前で、バッククォートの有無、単独か修飾（`Setting::distinct_spill_ratio`）かを問いません。差分で追加された行に対する完全一致で、投稿された差分は最大 200,000 文字、ミラーの差分は最大 200,000 バイトまで読みます。したがって `check` は「この行を差分と照らして読むこと」を意味し、違反が見つかったという意味ではありません。

| 行の verdict | 意味                                                                                                                                                                                                                         |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check`      | 差分が、その行のチェック対象の名前 (`names`) のいずれかを追加している                                                                                                                                                         |
| `clear`      | その行にチェック対象の名前があり、差分がそのいずれも追加していない                                                                                                                                                            |
| `unknown`    | 名前は見つからなかったが、ここで差分を読めなかった（git が失敗した。途中まででも同様）、差分が切り詰められた、行数が合わないハンクがある、または行の値が切り詰められた (`diff_unread`, `diff_cut`, `diff_malformed`, `value_cut`) |
| `""`         | その行にチェック対象となる名前がない                                                                                                                                                                                          |

セクションの verdict は、いずれかの行がそう示せば `check`、次に `unknown`、次に `clear` の順で決まり、確認すべき行がなければ `none` です。`clear` または `none` になるのは、すべての変更パスとすべての一致した行を確認できた場合だけです。そうでなければ `unknown` となり、causes に残った理由が示されます。理由は、名前空間にリポジトリが設定されていない (`no_repo`)、ブリーフが変更パスを保存していない (`no_paths`)、呼び出し元がトランク名前空間を読めない (`trunk_not_readable`)、保存された変更パス一覧が切り詰められている可能性がある (`paths_maybe_cut`：ブリーフは最初の 50 件の変更ファイルと差分のハンクのファイルを最大 200 件まで保存するため、50 件以上の一覧は PR の変更より少ない場合がある)、その一覧の変更パスを読めていない (`paths_not_read`)、あるパスの読み取りが 50 行に達した (`paths_at_row_ceiling`)、あるパスのコードリンクの読み取りに失敗した (`links_not_read`)、行数の上限により確認すべき行が除外された (`rows_not_read`) です。

`code.conventions` は、リポジトリでメンテナーが繰り返し行うチェックを、1 つの PR の差分 (`pr_number`、`head_sha`、`files` または `diff`) に対する事実として述べ、意見としては述べません。`rules` で実行するチェックを絞れます。読み込む差分は `LOOM_CODE_CONVENTIONS_DIFF_MAX_BYTES` で上限が設けられます。

<a id="pre-pr-duplicate-check-codepr_check"></a>

## PR 作成前の重複チェック：`code.pr_check`

プルリクエストを作成しようとしているエージェントがドラフトを渡すと、次の 3 つの判定のいずれかが返ります。`duplicate_of`（同じ内容のオープンな PR がすでにある、またはマージ済みの PR がすでに行っている）、`related`、`none` です。

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="pr-bot") as bot:
    check = await bot.call(
        "code.pr_check",
        title="Retry the upload when the token expires",
        paths=["src/upload/client.py"],
        symbols=["UploadClient.send"],
        issues=[1432],
    )
    if check["verdict"] == "duplicate_of":
        for item in check["items"]:
            print(item["pr_number"], item["state"], item["evidence"])
```

同じ呼び出しを HTTP で行う場合は `POST /v1/code.pr_check` で、MCP ではツール名が `loom_code_pr_check` です（`coding` と `tracker` のツールセットに含まれます）。タイトル、変更したパス、issue 番号のいずれかが必須です。

候補は GitHub を呼び出さず、Loom 自身の台帳から 4 つの経路で集められます。

| 経路            | 読み取る内容                                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| オープンな PR   | `code_gh_items` にあるすべてのオープンな PR：そのタイトルと、`refs/pull/N/head` が変更するファイル（本文は含みません）                 |
| マージされずにクローズ | 直近 `LOOM_CODE_PR_CHECK_CLOSED_DAYS` 日（180）以内にマージされずクローズされた PR：「誰かが試みた」もの。判定は `related` が上限です |
| マージ済み（パス基準） | ドラフトの `paths` を変更したマージ済みの PR                                                                                    |
| マージ済み（テキスト基準） | テキストがドラフトのタイトルとシンボルに対してスコアされるマージ済みの PR、およびドラフトの `issues` と同じ issue をクローズするマージ済みの PR |

各候補は 1 つの固定ルールでスコアが付けられます。ルールの要素は、候補が持つドラフトのタイトルとシンボルのトークンの割合（希少性で重み付け）、マージ済み PR に対する小さな本文の項、双方向のパスの重なり、ドラフトが挙げた issue をクローズするかどうかです。重複した PR はマージされずクローズされるため、重みを当てはめるためのラベル付きデータセットはありません。レスポンスには `epistemic: "heuristic"` が付き、タイトルだけでは `duplicate_of` には達しません。

| 設定                                     | デフォルト | 効果                                                                          |
| ---------------------------------------- | ---------- | ----------------------------------------------------------------------------- |
| `LOOM_CODE_PR_CHECK_DUPLICATE_THRESHOLD` | 0.55       | 候補が `duplicate_of` になるスコア                                            |
| `LOOM_CODE_PR_CHECK_RELATED_THRESHOLD`   | 0.2        | 候補が `related` になるスコア                                                 |
| `LOOM_CODE_PR_CHECK_MERGED_DAYS`         | 365        | これより古いマージ済み PR は、重複としての投票に参加しません                  |
| `LOOM_CODE_PR_CHECK_MASS_FILES`          | 200        | これより多くのファイルを変更する候補は大量変更とみなされ、そのファイルは投票に参加しません |

`tracker.duplicate_check` は issue 側の対になる機能です。判定が `none` または `related` の場合、ドラフトにすでに対応している可能性のある `code.pr_check` の PR を `related_prs` として返します（[tracker の概要](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#duplicate-check)）。

<a id="bisect-candidates-codebisect_candidates"></a>

## bisect 候補: `code.bisect_candidates`

報告されたバグに対して、エンジニアが最初に調査すべきマージ済み PR はどれか。
`code.bisect_candidates` は候補の優先順位付けを行います。自動化された `git bisect` ではありません。
リポジトリのビルド、テストの実行、報告の再現、正常バージョンと不具合バージョンの特定、候補がバグの原因であることの証明は行いません。候補のスコアは調査の優先順位を表すものであり、原因である確率ではありません。

リグレッションの境界は、呼び出し前に記録された証拠から決まります。

| ウィンドウの入力 | 入力の出どころ | Loom の処理 |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bad_version`, `good_version` | ユーザー、CI ジョブ、サポートワークフロー、またはテスト用エージェントが、不具合バージョンでバグを確認し、正常バージョンでは問題がないことを確認している。 | これらの観測の間にあるリリースをスキャンします。どちらのリリースも Loom は再テストしません。 |
| `issue_number` | リンクされたトラッカーの issue に、トラッカーのワークフローが設定した `reproduces_on` と `clean_on` のトリアージ観測が既にある。 | 明示的なバージョンが指定されていない場合に、これらの観測を読み取ります。issue やトリアージ値が存在しても、報告が正しいことの証明にはなりません。 |
| `after`, `before` | 呼び出し側がおおよそのマージ時刻の区間（最後に正常だった日付と最初に確認された日付など）しか把握していない。 | PR 履歴のスキャンをその区間に絞り込みます。日付は境界であり、再現結果ではありません。 |
| `bad_sha` | 呼び出し側が、最初に観測された失敗と同時点かそれより前のコミットを把握している。 | リポジトリミラー上でコミット時刻を解決し、`before` がない場合はそれを `before` として使います。コミットは実行しません。 |

その後、呼び出し側は手元にある報告の証拠を渡します。クラッシュしたフレーム（`symbols`）、ソースの `paths`、`patterns`、自由記述の `text`、`issue_number`、そして判明していれば `fix_pr` です。
正確なリリース範囲がない場合、リポジトリ履歴へのフォールバックでは、このスキャンのために保持されている直近の履歴（`LOOM_CODE_BISECT_HISTORY_DAYS`、デフォルトは 90）を使います。これはより広く、精度の低い調査ウィンドウです。

1. フレームはコードインデックスを通じてファイルに解決されます。
2. 候補スキャンでは、最初のリリースがウィンドウ内にあり、それらのファイルに変更を加えた、マージ済みで派生ではない PR を読み取り、diff が名指しするフレームで順位付けします。トラッカーがリンクされている場合は、トラッカーの PR diff をスキャンします。`issue_number` に以前記録されたトリアージ列（`reproduces_on`、`clean_on`）がウィンドウを与えることもあります。トラッカーがない場合、またはトラッカーに変更ファイルがない場合は、リポジトリ自体の PR 履歴（`LOOM_CODE_BISECT_HISTORY_DAYS`、デフォルトは 90）を読み取り、応答には `candidates_from: "code_history"` が含まれます。
3. フレームがなく（誤った結果を返す報告）、`issue_number` がある場合、スキャンは、ウィンドウ内の diff における issue の最も希少な識別子、コンポーネントのディレクトリ、修正のファイルによって範囲が絞られます。どれを使うかは `wideners` で選択し、どれが適用されたかは `widened_by` に示されます。
4. 報告時の融合では、diff スキャンの結果を、ベクトル、字句、パス、サブツリー、新しさの各順位と融合します。報告がバグの混入元として名指ししている PR が先頭になります。融合結果の先頭が弱い場合（2 位との差が 0.1 未満で、報告が名指しした容疑 PR でもない場合）、より広いウィンドウ（12 リリース、または `LOOM_CODE_BISECT_WIDE_DAYS`）が加わり、1 回の LLM 呼び出しで上位 20 件を再ランク付けします（`LOOM_CODE_BISECT_LLM_RERANK_MODEL`。タイムアウトした場合は融合後の順序を維持します）。`rerank: true` を指定して再ランク付けを要求することもできます。
5. 修正も issue も名指ししていない報告は、`LOOM_CODE_BISECT_DUPCHECK_TIMEOUT_S` の範囲内で、重複チェックが一致させた issue の修正を借用します。

各候補にはマーク（バックポート、リバート、リバートされた、再導入、空白のみまたはリネームのみ、diff のハンクヘッダーに基づく `touches_symbol`）が付きます。`marks: false` を指定すると、マークなしの順位付きリストが返ります。どのパスをソースコードとして扱うかは、トラッカーの `bisect.code_prefixes` で決まります（[レコード規約](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#source-entry-options)）。
再ランク付けには実際の LLM プロバイダーが必要です。それ以外の段階は、PR 履歴を読み込んだ後は標準のスタックで動作します。bisect の設定は[設定リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#code)に一覧があります。

以下の例では、正常なリリースと不具合のあるリリースの間にマージされ、クラッシュした関数のファイルに変更を加えた PR を順位付けします。`bad_version` はバグが最初に確認されたリリース、`good_version` は問題がないことが確認された最新のリリースです。Loom はどちらもテストしません。

バージョンウィンドウを使うには、PR レコードに `minor_version` を持つ、リポジトリにリンクされたトラッカーの名前空間が必要です（[レコード規約](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)）。マージ済み PR の履歴だけがある場合（`LOOM_CODE_PR_HISTORY_ENABLED` または `loom code backfill-prs`。[初回のインデックス作成](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)を参照）、バージョンはスキャンの範囲を制限しません。その代わり、この op は直近の `LOOM_CODE_BISECT_HISTORY_DAYS`（デフォルトは 90）を読み取ります。

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="triage-bot") as bot:
    result = await bot.call(
        "code.bisect_candidates",
        bad_version="26.3",
        good_version="26.2",
        symbols=["UploadClient.send"],
    )
    for candidate in result["candidates"]:
        print(candidate["pr_number"], candidate["score"])
```

候補は順位付けされて返ります。`score` は、どの PR を最初に調査すべきかを示すものであり、PR がバグの原因であることを示すものではありません。バグを再現し、候補をテストする作業は、エンジニアまたは CI ジョブが行う必要があります。

最終的な検証はこの操作の範囲外です。エンジニア、CI ワークフロー、またはテスト用エージェントが、issue を再現し、順位付けされた候補をテストする必要があります。

<a id="backport-gaps"></a>

## バックポートの欠落

`code.backport_gap` は、マージ済みの修正（`pr_number`）、またはその修正が閉じる issue（`issue_number`。トラッカーの `fixes` エッジまたは復元されたリンクで解決します）を指定します。結果は、同じリポジトリのリリースブランチの名前空間ごとに新しい順で返されます。`branches` を指定した場合は、指定したブランチ（名前空間、または `26.4` のようなバージョン）ごとに返されます。3 つの要素を突き合わせます。1 つ目はバックポートの記録（トラッカーの `is_backport_pr`、`upstream_pr_number`、`base_branch`、およびブランチ台帳のマージ済みバックポート）、2 つ目は修正が最初に入ったリリース（それ以降に切られたブランチには修正が含まれます）、3 つ目は修正のハンクが触れる関数が各ブランチに存在するかどうかです。

| 判定           | 意味                                                                                                                                 |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `gap`          | バックポートがなく、ブランチが修正より前のもので、触れられる関数（またはファイル）が存在する。`cherry_pick` に PR とマージ sha が示されます |
| `pending`      | バックポート PR が存在し、オープンである                                                                                             |
| `covered`      | バックポートがマージ済み、ブランチが修正の最初のリリース以降に切られている、または issue の `fix_versions` にそのブランチが含まれる |
| `not_affected` | 触れられるすべての関数（またはファイル）がそのブランチに存在しない                                                                   |
| `unknown`      | コードが存在するのに記録が利用できなかった、存在確認がタイムアウトした、またはブランチに照合できるバージョンがない                   |

シンボルレベルの `gap` は下限値です。コードがブランチに存在しても、バグが存在することの証明にはなりません。

- `exact: true` はパッチをハンクごとに確認します。削除された行が残っていて、追加された行が存在しない場合は `gap` が確定します。追加された行が存在し、削除された行がなく、バックポートの記録もない場合は `not_affected` が確定します。
- `history: true`（`exact` を含意）は、行の比較で判定できなかった行について、ミラー内のブランチのツリーと履歴も読み取ります。
- `presence.patch` は、モードと重なりを根拠として示します。

`code.backport_gaps` はセット全体のレポートで、直近 90 日間の修正のうちメンテナーがバックポート対象としてラベル付けしたものすべてを、リリースブランチごとに、欠落を先頭にして返します。`code_backport_gaps` ワーカーは、修正ごとに `code.backport_gap(exact, history)` を実行して、毎晩 `loom.code_backport_gaps` に書き込みます（`LOOM_CODE_BACKPORT_GAPS_ENABLED`、デフォルトは有効。`LOOM_CODE_PR_HISTORY_ENABLED` が有効な場合のみ機能します）。この op はそのテーブルを読み取ります。`wanted_only: false` を指定すると、ラベルのない修正も含まれます。すべてのブランチの名前空間は、読み取る前に認可され、失敗時は拒否されます。

<a id="first-build"></a>

## 最初のビルド

`code.first_build` は、コミットの `sha` またはマージ済み PR の `pr_number` を受け取り、どの CI ビルドが最初にそのコミットを含んでいたかを返します。リンクされたトラッカーの `version_history` テーブルが必要です。sha を指定した実行があれば、結果は正確です。ない場合は、マージ後の実行のベースをミラー上で確認し（`git merge-base --is-ancestor`、最大 10 件）、最も早い子孫を探します。この場合、返される答えは下限値です。

<a id="pr-and-tracker-planes"></a>

## PR とトラッカーのプレーン

メンテナーは `code.repo.set_source` でトラッカーをリポジトリに紐付けます。最初に指定したものがプライマリになります（[トラッカーの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)）。その後の動作は次のとおりです。

- `code.bisect_candidates` は、トラッカーの PR の diff をスキャンし、疑わしい変更を既知の修正と照合します。
- `code.backport_gap` はバックポートの記録を読み取り、`code.first_build` はトラッカーの CI 実行を読み取ります。
- `code.history`、および `code.search` と `code.ask` の履歴部分は、バインディングが解決でき、呼び出し元に読み取り権限がある場合、PR と並べてトラッカーの記録も返します。
- `code.context_pack` にはトラッカーのアイテムを含められます（`sections=["qa"]`）。

トラッカーがない場合、各 op は可能な範囲で PR プレーンから回答し、トラッカーがないと得られない部分は欠落として示します。

<a id="limits-and-failure-modes"></a>

## 制限と障害モード

- **履歴がなければ回答もありません。** `LOOM_CODE_PR_HISTORY_ENABLED` が off で `backfill-prs` も実行していない場合、履歴テーブルは空であり、op はランク付けする対象がないまま応答します。
- **匿名の GitHub アクセス**では、公開リポジトリのみを公開レート制限の範囲で扱えます。レビューコメントと issue ミラーにはトークンが必要です。
- **オープンな PR はインデックス済みのコードではありません。** `code.pr_check` が参照できるのはオープンな PR のタイトルとファイルのみで、本文やシンボルは参照できません。
- **ヒューリスティックな判定。** `code.pr_check` は設計上キャリブレーションされていません。`code.blame_range` が正確なのは「最後に変更された箇所」だけです。`code.bisect_candidates` は容疑箇所をランク付けするだけで、原因を事実として断定することはありません。
- **トラッカーの障害時は機能が縮退し、失敗にはなりません。** トラッカーの読み取りがタイムアウトした場合やブレーカーが作動した場合は `tracker_unavailable` が設定され、PR プレーンの各処理は引き続き応答します。

<a id="related"></a>

## 関連ドキュメント

- [トラッカーの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)と[レコード契約](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)
- [リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)
- [質問別のコードインデックス操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [コードインデックスのアーキテクチャ](../../architecture/code-index-architecture.md)
- [操作リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)
