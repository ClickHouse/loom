<!-- i18n: source=docs/features/runs.md blob=7aa894eee29c65b040ebfbad315df18b437034ae model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md), which is authoritative.

<a id="runs"></a>

# 実行（Runs）

run とは、「issue 1234 をトリアージする」のような、タスクを 1 回実行することです。`run.begin` が run を開いて `run_id` を返します。エージェントはその id を書き込み（`memory.set`、`memory.update`、`observe.record`）に渡します。`run.complete` は、ステータス、エージェントが依拠したメモリ、および任意でリフレクションを指定して run を閉じます。読み取り（`memory.search`、`memory.ask`）でボディに `run_id` を指定すると、その run 自身の行に絞り込まれます。そのため、読み取りには別の方法でラベルを付けます。HTTP では `X-Loom-Run-Id` ヘッダー（Python SDK では `run_scope`）、MCP ではセッション自身の run を使います。成功した run が依拠したメモリは次回以降のランキングで上位になり、失敗した run が依拠したメモリは、それまでに得たブーストを失います。

<a id="what-a-run-records"></a>

## run が記録する内容

run の行は `loom.agent_runs` に保存されます。`run.get` が表示する内容の大半は、読み取り時に他のプレーンから導出されます。

| フィールド                         | 保存または導出 | ソース                                                                                                                                                  |
| ---------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `goal`, `user_request`, `model`, … | 保存           | `run.begin`                                                                                                                                             |
| `status`, `final_response`         | 保存           | `run.complete`（それまでは `active`）                                                                                                                   |
| `memories_used`                    | 保存           | `run.complete`：エージェント自身が、何に依拠したかを申告した内容                                                                                        |
| `grade`, `grade_source`, …         | 保存           | `run.grade`（run の終了後）                                                                                                                             |
| `memories_written`                 | 導出           | run の `run_id` を持つ `loom.memory` の行                                                                                                               |
| `recalled`, `recalled_keys`        | 導出           | `run_id` に紐づく検索トレースログと、`run.complete(recalled_keys)` の和集合                                                                             |
| `observations`                     | 導出           | `run_id` が付与された、`observe.record` によるエピソード行                                                                                              |
| `turns`, `cost_usd`                | 導出           | run が開いている間に行われたセッションのターンで、`loom.agent_turn_usage` にあるもの（Claude Code プラグイン、または `cost.record_turns` を使うエージェントが書き込みます） |
| `budget_usd`, `budget_seconds`     | 保存           | `run.begin`（0 = 予算なし）                                                                                                                             |
| `elapsed_seconds`, `over_budget`   | 導出           | run の開始から終了（または現在）までを、サーバーの時計で計測した値と `cost_usd` を、予算と比較して求めます                                              |

`LOOM_SEARCH_TRACE_LOG_ENABLED` が無効で、`recalled_keys` も送信されていない場合、`recalled` は `-1` になります。

<a id="budgets"></a>

## 予算

`run.begin` は、任意の `budget_usd`（モデルの支出）と `budget_seconds`（run にかけてよい時間）を受け取ります。`run.get` はこれらを `cost_usd` および `elapsed_seconds` と比較し、超過したものを `over_budget`（`cost`、`time`、またはその両方）に列挙します。Loom はエージェントを実行しないため、予算は実行する側へのシグナルであり、停止させるものではありません。

run の支出は、その run が開いていた間にセッションで行われたターンを、各ターン自身の時刻（トランスクリプト由来の `turn_at`）で集計したものです。そのため `budget_usd` には `session_id` が必要です（ない場合は 422）。コストと同様に小数点以下 6 桁で保持されるので、最小値は $0.000001 です。時間は、クライアントが報告した `duration_ms` に関係なく、サーバーの時計で run の開始から終了までを測ります。予算は run の最初の `run.begin` で設定してください。

セッションで予算超過のアクティブな run を最初に見つけた `run.get` は、メンターヒント `run_over_budget` も返します。プラグインはこれをアドバイスとしてモデルに渡します。終了した run でも `over_budget` は報告されますが、ヒントは付きません。他のメンターヒントと同様、ヒントはセッションごとに 1 回だけ伝えられ、セッションを送らない呼び出し元（SDK、素の HTTP）は名前空間とウェブプロセスごとに 1 つのセッションを共有します。そのため、判断にはヒントではなく `over_budget` を使ってください。

制限事項：

- **プロンプトの合間のみ。** Claude Code プラグインはプロンプトのターンをそのプロンプトの終了時に送信するため（エージェントは `cost.record_turns` で自分で送信することもできます）、コスト予算はプロンプトの合間にチェックされ、単一のヘッドレスプロンプト（`claude -p`）の支出は終了後にしか表示されません。それらのターンは、エージェント自身の `run.complete` の後に届いた場合でも集計されます。呼び出しのコストは戻った後にしか分からないため、run は何かが気づく前に、最後のターンのコスト分だけ `budget_usd` を超えることがあります。
- **クライアントの時計。** この集計ウィンドウは、クライアント側のターン時刻とサーバー側の run 時刻を比較します。時計が数分ずれていると、run の境界付近のターンが誤った run に割り当てられます。ターンは送信時の時刻を保持するため、再送されたターンは最初に属した run に残ります。時刻なし（または解析できない時刻）で送られたターンは、その `message_id` が記録された時刻、新規であれば現在時刻になります。最初の送信から一瞬のうちに再送した場合は、現在時刻として記録されることがあります。`cost.record_turns` は、サーバーの時計より 5 秒を超えて先の時刻を持つ受信ターンを `turns_dated_ahead` として返すため、自分でターンを送る呼び出し元は時計の進みに気づけます。Claude Code プラグインは応答を読まずにターンを送信します。
- **1 つのセッションに複数の run。** 同じセッションで同時に開いている run は同じターンを参照します。入れ子の run は両方に計上され、コスト予算を必要とする並行ワーカーには、それぞれ専用のセッションが必要です。
- **価格未設定のモデル。** 価格表にないモデルのターンのコストは 0 です。`run.get` はそれらを `unpriced_turns` で数え、`budget_usd` には計上しません。
- **MCP。** MCP ツールが提供するのは `budget_seconds` のみです。モデルは、ターンが記録されるセッション ID を知らないためです。
- **リトライ。** 再送された `run.begin` は、他のリトライと同様に run の行を置き換えます。再指定されなかったセッションと予算は保持され、0 以外の新しい予算は古いものを置き換え、0 は既存の値を保持します。新しい `budget_usd` は `session_id` と一緒に送る必要があります（ない場合は 422）。新しい `budget_seconds` には他に何も必要ありません。run を別のセッションに移す再送 begin は、古いセッションのターン用に設定されたコスト予算を破棄し、時間予算は保持して、その旨を `next` で伝えます。予算が削除されるのはこの場合だけです。`run.begin` が送ったものと異なる `run_id` を返した場合は、その `run_id` か同じ `session_id` で再送してください。そうしないと、リトライは新しい run を開きます。

<a id="recalled-is-not-relied-on"></a>

## 取得されたことは、依拠されたことを意味しない

`run.complete` では 2 つのリストが送られます。

- **`memories_used`** — エージェントが実際に依拠したキーです。各キーには `used` 使用イベントが記録され、結果が肯定的な場合は `evidence` イベント、否定的な場合は `harm` イベントも記録されます。`outcome` の既定値は `status` から決まります。`completed` は肯定的、`failed` は否定的、`cancelled` はどちらでもありません。
- **`recalled_keys`** — 実行中に Loom のツールが返したキーです（Claude Code と Codex のプラグインが `loom_search` と `memory.get` の結果から収集して保持し、セッション終了時に送信します）。これらは `run.get` のために記録されるだけで、評価の対象にはなりません。

返されたものの使われなかった行は何も得ません。検索でヒットしたすべての行を評価すると、上位に表示されたものが無条件に優遇されてしまうためです。`usage_fold` ワーカーは、これらのイベントを強化項にまとめます。この強化項は[メモリ検索](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)の順位付けに使われます。`harm` イベントは獲得済みのブーストを取り消しますが、その行の書き込み時の基準値が下限となります。そのため、1 回の不適切な実行によって行が本来の関連度を下回ることも、削除されることもありません。

`memories_used` に指定されたプロシージャのキーには、プロシージャ台帳にも行が追加されます（パスは空で、`ok` は outcome から決まります）。エージェント自身が `procedure.record_run` を呼び出す場合は、実行が二重に数えられないよう、そのキーを `memories_used` に含めないでください。詳しくは[プロシージャと学習内容](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)を参照してください。

<a id="reflections"></a>

## リフレクション

`run.complete(reflection=...)` は、`what_worked`、`what_failed`、`assumptions`、`lessons`、`improvements`（それぞれ最大 20 項目）、`confidence`、そして教訓の根拠となる `evidence` キーを記録します。リフレクションは `reflection:<run_id>` というキーで、タイプ `reflection` の 1 件のメモリとして保存されます。

- 埋め込みが作成されるため、後続の実行は `memory.search(memory_type="reflection")` で見つけられます。ただし、フィルタなしの検索や `memory.ask` の対象からは除外されます。
- `what_failed` と `lessons` には、少なくとも 1 つの `evidence` キーが必要です（指定がない場合は 422 になります）。
- `expires_in_days`（デフォルトは 90、`0` は無期限）が経過すると失効します。ただし、`memory.verify` または肯定的な結果によって更新された場合は失効しません。
- `role`（planner、executor、critic、または独自の名前）を指定して開始した実行は、リフレクションに `role:<role>` タグを付けます。これにより、同じロールのエージェントが `memory.search(role=...)` を実行したときに、そのリフレクションが優先されます。

<a id="what-happens-to-the-rows-a-run-wrote"></a>

## 実行が書き込んだ行はどうなるか

`run.complete` の `retain` は、その実行（run）の下で書き込まれたメモリの扱いを決めます。

| `retain`         | 効果                                                                                                                                          |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `keep` (default) | 何も変更されません。                                                                                                                          |
| `demote`         | 信頼度がその時点で下げられます。                                                                                                              |
| `expire`         | `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS` 日後（デフォルトは 7）を有効期限として `on_expire=demote` とともに設定し、`memory_expiry_sweep` ワーカーがそれを適用します。 |

ピン留め済み、検証済み、昇格済み、ロック済み、エピソード、リフレクションの各行は、どのモードでも保持されます。また、どのモードでも行が論理削除（tombstone）されることはありません。デフォルト値は `LOOM_MEMORY_RUN_COMPLETE_RETAIN` で決まります。MCP セッションの暗黙の run は常に keep になります。これは、run を閉じるトランスポートが作業内容を判断できないためです。

<a id="graded-runs"></a>

## 評価付きの run

`status` は run が終了したことを示し、評価（grade）はその結果が妥当だったかどうかを示します。評価は、CI が通ったときやレビュアーがプルリクエストを却下したときなど、数日後に付くことがよくあります。`run.grade` は `grade`（`pass` または `fail`）、`source`（`maintainer`、`ci`、`reviewer` など、自由なテキスト）、`reason`、`ref`（URL、finding key、check run id）を受け取り、1 つの `run_id`、または 1 つの `session_id`（たとえば `pr_1234`）に属するすべての終了済み run を対象にします。再評価すると判定は置き換わります。同じ source からの同じ判定では何も変わりません。アクティブな run は対象外です。

評価付きの run の `memories_used` に含まれるメモリは、すべてその評価に対して集計されます。この集計値が、`memory.proposals` で試用中の各ファクト（`memory.set(proposed=true)` の行）の横に表示されます。Loom が集計値に基づいて提案を決定するのは、名前空間の所有者が `proposal_policy.mode` を `auto` に設定した場合だけです。どちらのモードでも、書き込み権限を持つ任意のエージェントが `memory.proposal_decide` で決定できます。[メモリの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)を参照してください。

`run.list(grade="ungraded")` は、まだ判定が付いていない終了済みの run を一覧します。

MCP セッションには、要求しなくても暗黙の run が付きます。最初の run スコープのツール呼び出しで run が開始され、`DELETE /mcp` または `mcp_session_run_sweep` ワーカーがそれを閉じます（[MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)を参照）。sweep が閉じるのは、`environment='mcp'` で `metadata.implicit` が true、かつ id が `mcp-` に 40 桁の 16 進文字を続けた形（トランスポートだけが作る形）のアクティブな run です。run のトークンによる呼び出し（トークンを記録しない run では、誰の呼び出しでも）が `LOOM_MCP_SESSION_RUN_IDLE_SECONDS` の間その run に届かなかったとき、sweep はそれを閉じます。run は呼び出し元のデフォルト名前空間に置かれるため、その読み手に見える内容もその名前空間に限られます。goal はラベル `mcp session (<agent>)` であり、呼び出しのテキストは入りません（読み取りのクエリは、呼び出し元が読める任意の名前空間に及ぶ可能性があるためです）。`memories_written` には、その名前空間への書き込みだけが載ります（それ以外の書き込みにも、その `run_id` は付きます）。run を閉じても何も判定しません。outcome は `none` で、サーバーのデフォルトの retain モードにかかわらず、メモリは保持されます。したがって、2 つの close が競合しても（2 台のホストでの sweep や `DELETE`）、最悪の場合、同じ completion が 2 回書き込まれるだけです。

run が複数の web プロセスにまたがる仕組み。run の id は、呼び出し元の org、トークンの subject、`Mcp-Session-Id` のハッシュです。そのため、サーバーのどのプロセスも同じセッションに対して同じ run を指し、同じセッション id を送っても他のトークンがその run を指すことはできません。プロセスは、セッションで最初に受理された run スコープの呼び出しで run を登録し、begin 行をバックグラウンドで書き込みます（await する insert では、ClickHouse Cloud 上でその呼び出しが数秒止まってしまいます）。ゲートに拒否された呼び出しは run 行を書きません。行が見つからなかった 2 つのプロセスがそれぞれ最初の begin を書いた場合は、開始が早いほうとその所有者が優先されます（最初の begin の行バージョンは開始時刻から逆算して減っていき、以降の書き込みはすべてそれを上書きします）。各プロセスは、キャッシュした run を台帳と照合する頻度を 1 分に最大 1 回とし、その間に `DELETE` や sweep が閉じていた場合は run を再オープンします。`DELETE` は、どのプロセスに届いても id によって run を閉じ、close はその id への書き込みを、誰が閉じるかに関係なくすべて集計します。begin 行が書き込まれる前に別のプロセスへ届いた `DELETE` は、閉じる対象が見つかりません。その後 begin が書き込まれて open の run ができ、アイドルになった時点で sweep がそれを閉じます。短いリトライでは数秒かかる begin をカバーできず、run を一度も持たなかったすべてのセッションの `DELETE` を遅らせてしまうためです。再オープンされた run は元の `started_at` を保つため、sweep に閉じられた後に再オープンされた run の `duration_ms` は、セッションの最初の呼び出しから、空白期間も含めて数えた値になります。消去（erasure）によって行が削除され、チェックで行がまったく見つからなかった場合は、その時点で開始する新しい run が始まります。そのため、消去より前の日付の run が戻ってくることはありません。

<a id="operations"></a>

## オペレーション

| オペレーション | ロール | 内容 |
| -------------- | ------ | ---- |
| `run.begin`    | writer | run を開始します：`goal`（必須）、`user_request`、`expected_outcome`、`run_id`（空または `-`：自動生成）、`session_id`、`role`、`model`、`metadata`。同じ `run_id` を再送すると行が置き換えられます。MCP トランスポートの形式の ID（`mcp-` と 40 桁の 16 進数）には、別の ID が割り当てられます。 |
| `run.complete` | writer | run を一度だけ終了します：`status`、`memories_used`、`recalled_keys`、`outcome`、`reflection`、`retain`。冪等ではありません。2 回完了すると使用イベントが 2 回追加されます。 |
| `run.get`      | viewer | 1 件の run を、有効な派生データとともに返します。 |
| `run.list`     | viewer | run の 1 ページ分を返します。`agent`、`session_id`、`status`、`grade`、`q`、`memory_activity` で絞り込めます（`with` はメモリを利用または書き込んだ run を残し、`without` は何も使わなかった run を残します。絞り込みは `limit` の前に適用されます）。 |
| `run.grade`    | writer | 終了済みの run、またはセッションの run に `pass` / `fail` の評価を付けます。 |

`run.list` は `order_by`（開始時刻、所要時間、利用したメモリ数のいずれかを昇順または降順で指定します。デフォルトは `started_desc`、新しい順です）と `offset`（最大 100,000）でページングします。`q` は、ゴール、run id、エージェントのいずれかに指定したテキストを含む run を、大文字小文字を区別せずに残します。`total` は絞り込みに一致するすべての run の件数です。2 ページ目以降の満杯のページでは、そのページだけでは判断できないため null になります。

フィールドの全一覧は[オペレーションリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#run)を参照してください。`memory.outcome_run` は、キーを指定せずにまとめて評価する呼び出し元向けに、run の下で呼び出された各メモリについて helped / hurt の結果を 1 件ずつ記録します。

<a id="example"></a>

## 例

Python SDK の `run.scope` は、ブロックに入るときに実行（run）を開き、抜けるときに閉じます。ブロック内で `memory.ask` の回答が引用したキーと、`used()` に渡したキーが `memories_used` になります。例外が発生した場合、実行は `failed`（outcome は negative）として閉じられ、その後に例外が伝播します。ブロック内の書き込みには、その実行の id が付与されます。

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    async with agent.run.scope(goal="Answer ticket 4411: refund policy for annual plans") as run:
        answer = await agent.memory.ask("what is the refund policy for annual plans?")
        run.used("policy/refunds/annual")  # relied on beyond what the answer cited
        run.final_response = answer["answer"]

    replay = await agent.run.get(run.run_id)
    print(replay["run"]["status"], replay["memories_written"], replay["recalled"])

    # Later, once the customer confirmed the answer was right:
    await agent.run.grade("pass", run_id=run.run_id, source="support-lead")
```

HTTP では、ライフサイクルは 2 回の呼び出しで構成されます。

```bash
curl -sX POST "$LOOM_BASE_URL/v1/run.begin" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","agent":"triage-bot","goal":"Answer ticket 4411"}'
# → {"run_id":"…","status":"active","started_at":"…","next":"…"}

curl -sX POST "$LOOM_BASE_URL/v1/run.complete" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","run_id":"…","status":"completed",
       "memories_used":["policy/refunds/annual"]}'
```

`Agent(auto_run=True)`（またはクライアント環境の `LOOM_AUTO_RUN=on`）を指定すると、最初のメモリ操作の呼び出しで実行が遅延的に開かれ、以降のすべての呼び出しがその実行に紐づけられ、`Agent` を閉じるときに outcome `none` で完了されます。そのため、評価の加点も減点も行われません。実行下での書き込みはその実行にスコープされ、別の実行の行を言い換えた内容は統合されず `duplicate` として保存されるため、この設定はデフォルトでは無効です。

<a id="settings"></a>

## 設定

| 設定                                                          | 効果                                                                                                                  |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `LOOM_MEMORY_RUN_COMPLETE_RETAIN`                             | 呼び出しで `retain` モードが指定されていない場合に使われるモード（`keep`）。                                          |
| `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS`                        | `retain=expire` が有効期限を何日先に設定するか（7 日）。                                                              |
| `LOOM_SEARCH_TRACE_LOG_ENABLED`                               | すべての検索を `run_id` ごとに記録します。`recalled` はこの記録から導出されます。クエリテキストをログに残すため、デフォルトではオフです。 |
| `LOOM_USAGE_FOLD_ENABLED`                                     | used / evidence / harm イベントをランキングに反映する `usage_fold` ワーカー。                                         |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`, `…_HALF_LIFE_DAYS` | 検索で、利用実績が行をどの程度、どのくらいの期間押し上げるか。                                                        |

[設定：重複排除と有効期限](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#dedup-and-expiry)、
[ワーカー](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#workers)、
[メモリ検索](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch) も参照してください。

<a id="limits"></a>

## 制限

- `memories_used` と `recalled_keys` は、それぞれ最大 500 個のキーを受け付けます。`goal` は最大 2,000 文字です。
- `memories_used` のキーのうち、有効な行に解決されないものは何も得られません。レスポンスの `used_applied` は、解決されたキーの数を示します。
- `run.complete` は冪等ではありません。完了を再試行すると使用イベントが二重に計上されるため、完了は一度だけ行ってください。
- SDK の `run.scope` は、ledger をベストエフォートで書き込みます。`run.complete` が失敗しても、ブロック自体の例外が隠されることはありません。そのため、完了に失敗した run は `active` のままになります。
- `turns` と `cost_usd` は、run に `session_id` がない場合、またはクライアントがターンの使用量を報告しない場合は 0 になります。これらは、run が開いている間に行われたターンだけを集計します。

<a id="related"></a>

## 関連ドキュメント

- [長時間実行のトレースと再開](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — `run_id` をキーとするワーキングメモリ内のチェックポイントと、実行内容をトレースする方法。
- [検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md) — 強化がランキングにどのように反映されるか。
- [タスクと意思決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md) — `decision.outcome` は、意思決定を行った実行が根拠にしたメモリにクレジットを付与します。
- [メモリアーキテクチャ](../architecture/memory-architecture.md) — 使用イベントのフォールドと、その背後にあるテーブル。
