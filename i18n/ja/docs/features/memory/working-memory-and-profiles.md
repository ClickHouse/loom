<!-- i18n: source=docs/features/memory/working-memory-and-profiles.md blob=bedd8394f653eb3b5cd589b4bd7aed2749be116b model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md), which is authoritative.

<a id="working-memory-and-profiles"></a>

# ワーキングメモリとプロファイル

エージェントに検索ではなく毎回そのまま渡されるテキストを保持するプレーンが 2 つあります。

- **ワーキングメモリ**：リース、カーソル、チェックポイント、「どこまで進めたか」のための小さな名前付きブロックです。ブロックは埋め込み、ランク付け、検索の対象になりません。
- **プロファイル**：ユーザーまたはエージェントに関する常時ロードされるコンテキスト（ペルソナ、設定、フォーカス）で、1 つのプロンプトフラグメントとしてレンダリングされます。

後のセッションが問い合わせて見つけられるべき情報には `memory.set` を使います。問い合わせなしで伝えるべき情報には、これらのプレーンを使います。ファクトとして書かれたスクラッチパッドの行は、すべての検索と競合チェックで本物のファクトと競合します。

<a id="working-memory"></a>

## ワーキングメモリ

ブロックは `loom.working_memory` に保存され、識別タプルに `run_id` と `block_key`（デフォルトは `scratchpad`、最大 80 文字）を加えたものがキーになります。ブロックは再起動後も残り、TTL もないため、長時間の実行の再開ポイントとして使えます（[トレースと再開](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)）。ブロックが消えるのは、書き込み側が削除したときか、消去の対象になったときだけです。

| Op                       | ロール | 動作                                                                            |
| ------------------------ | ------ | ------------------------------------------------------------------------------- |
| `working_memory.set`     | writer | ブロックの内容を置き換えます。`char_limit` が適用されます。                     |
| `working_memory.append`  | writer | `addition` を `separator`（デフォルトは改行）で連結して追記します。             |
| `working_memory.replace` | writer | ブロック内の `old` をすべて `new` に置き換えます。                              |
| `working_memory.delete`  | writer | ブロックを論理削除（トゥームストーン）します。                                  |
| `working_memory.get`     | viewer | ブロックを 1 つ読み取ります。`fill_ratio` も返します。                          |
| `working_memory.render`  | viewer | スコープ内のブロック（または最大 20 個の指定した `block_keys`）を XML または Markdown として出力します。 |

フィールドの一覧：[操作リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#working_memory)。

<a id="size-limits"></a>

### サイズ制限

| 制限                 | 値                                                                        |
| -------------------- | ------------------------------------------------------------------------- |
| デフォルトの `char_limit` | 1 ブロックあたり 4,096 文字                                          |
| `char_limit` の最大値 | 16,384 文字                                                              |
| 制限を超えた場合     | 422 `WORKING_MEMORY_LIMIT_EXCEEDED`（`value_length` と `char_limit` を含む） |
| render あたりのブロック数 | `limit` 1–100（デフォルトは 20）                                     |

書き込みは必ず `value_length` と `fill_ratio`（`len(content) / char_limit`）を返すため、呼び出し側は書き込みが拒否される前に内容を削ることができます。`replace` で `old` のテキストが存在しない場合は 422 `SUBSTRING_NOT_FOUND` を返し、存在しないブロックに対して行った場合は `WORKING_MEMORY_BLOCK_NOT_FOUND` を返します。存在しないブロックに対する `working_memory.get` は `found=false` を返します。

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.working_memory.set(
        "cursor: ticket 4812\nlease: holds refunds queue", block_key="state", run_id="run-7"
    )
    await bot.working_memory.append("step 3 done", block_key="state", run_id="run-7")
    print(await bot.working_memory.render(run_id="run-7"))
    # <working-memory block="state">cursor: ticket 4812 ...</working-memory>
```

<a id="profiles"></a>

## プロファイル

プロファイルのセクションは、`memory_type='profile'` を持つメモリ行で、キーはセクションの `label` です。`profile.set`、`profile.append`、`profile.replace` は `memory.set` の薄いラッパーで、この型と `char_limit` を固定します。1 つのセクションを読むには、ラベルをキーにして `memory.get` を使います。Python SDK ではこれを `agent.profile.get(label)`、`agent.profile.list()`、`agent.profile.delete(label)` としてラップしています（これらは SDK のメソッドであり、ワイヤー op ではありません）。

| Op                 | ロール | 動作                                                                                         |
| ------------------ | ------ | -------------------------------------------------------------------------------------------- |
| `profile.set`      | writer | `label` のセクションを作成または置換します（競合する書き込みには `if_match_sha256` を使います）。 |
| `profile.append`   | writer | セクションに追記します。他のクライアントが先に書き込んでいた場合は、compare-and-swap で再試行します。 |
| `profile.replace`  | writer | セクション内の部分文字列を書き換えます（該当がない場合は 422 `SUBSTRING_NOT_FOUND`）。       |
| `profile.render`   | viewer | すべてのセクション（または指定した `labels`、その順序）を 1 つの XML または Markdown フラグメントとしてレンダリングします。 |
| `profile.activity` | viewer | どのクライアントがいつプロファイルを読んだかを返します（`lookback_days`、上限は 30）。       |

フィールド一覧：[操作リファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#profile)。

- **上限。** `char_limit` のデフォルトは 8,192 文字で、値の正規 JSON に対して計測します。これより長い場合は 422 `CHAR_LIMIT_EXCEEDED` を返します。
- **スコープ。** `profile.render` は、デフォルト（`scope="namespace"`）では名前空間内のすべてのエージェントのセクションを読みます。そのため、ダッシュボードで書いたペルソナはすべての MCP クライアントに届きます。`scope="this_agent"` を指定すると、呼び出し元自身のセクションだけを対象にします。`read_scope` が `own_user` または `own_session` の場合は、そのユーザー（またはセッション）のセクションと、名前空間レベルのセクション（`user_id` が `-`）だけがレンダリングされます。
- **検索。** プロファイル行は `memory.search` の対象外です。`loom_search` ツールにはプロファイル用のレッグがあり、質問がユーザーに関するものである場合に、意味に基づいてセクションをスコアリングします。各セクションのベクトルは、書き込みのたびにリクエストパスの外で更新されます。[統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)を参照してください。
- **事実のプロファイルへの統合。** `profile_consolidation` ワーカー（デフォルトでは無効、`LOOM_PROFILE_CONSOLIDATION_ENABLED`）は、モデル呼び出しによって、ユーザーの好みや決定に関するメモリをバージョン付きのセクション（ラベル `consolidated_profile`）に統合します。保護属性でタグ付けされた行は除外されます。出自（provenance）は `derived` です（`source_trace` は `profile.fold:<label>:<memory ids>` で、読み取った事実を示します）。

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.profile.set("persona", "Terse. Cite ticket ids. Never promise refunds.")
    await bot.profile.append("preferences", "Prefers EUR invoices.", user_id="u-42")
    system = await bot.profile.render(user_id="u-42", read_scope="own_user")
```

<a id="structured-profiles"></a>

## 構造化プロファイル

最初の返信の前にユーザーを把握していなければならないチャットボットは、自由形式のテキストではなく、運用者が定義した形式の 1 つのオブジェクトを読み取ります。管理者が名前空間にプロファイルスキーマを設定すると、`structured_profile` ワーカーがユーザーのメモリから、その形式で各ユーザーのプロファイルを最新に保ちます。`profile.structured` はステータスとともにプロファイルを返します。読み取りでモデルが呼び出されることはありません。

| Op                     | ロール | 内容                                                                                                                                                       |
| ---------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile_schema.set`   | admin  | 名前空間のスキーマ（`json_schema`）を設定します。監査対象です。以前のスキーマは置き換えられます。                                                         |
| `profile_schema.get`   | viewer | スキーマ、その `schema_sha256`、設定した人と日時を返します。                                                                                               |
| `profile_schema.clear` | admin  | スキーマを削除します。監査対象です。構築済みのプロファイルはメモリ行として残ります。                                                                       |
| `profile.structured`   | viewer | 1 ユーザーのプロファイル：`profile`、`status`、`built_at`、`memory_id`、`version`（および、同じ値を文字列にした `version_text`。JavaScript でも正確です）。 |

スキーマは、1〜64 個の `properties` を持つ JSON Schema オブジェクトで、サイズは最大 16 KiB です。各プロパティには `type`（`string`、`number`、`integer`、`boolean`、または、これらのいずれかを `items` に持つ `array`。任意の `maxItems` は最大 50）と、モデルが読む `description` が必要です。完全な回答に含まれる値は最大 256 個で、プロパティごとに 1 個、配列は `maxItems` 個として数えるため、モデルの出力上限に収まります。文字列では `enum`、`pattern`、`minLength`、`maxLength` を指定でき、数値では `minimum` と `maximum` を指定できます。これらは[型付きレコード](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records)が使う制約です。すべてのプロパティはすべてのプロファイルに含まれ、メモリから確定できない場合は `null` になります。そのため、スキーマに `required` はありません。`additionalProperties` に指定できるのは `false` のみです。`title` はどの階層でも受け付けられ、無視されます。

```python
schema = {
    "type": "object",
    "properties": {
        "preferred_name": {"type": "string", "description": "What the user asks to be called"},
        "timezone": {"type": "string", "description": "IANA time zone, e.g. Europe/Berlin"},
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages they speak",
        },
    },
}
async with Agent(org="acme", namespace="support", agent="chat", api_key=ADMIN_TOKEN) as admin:
    await admin.client.profile_schema.set({"json_schema": schema})

async with Agent(org="acme", namespace="support", agent="chat") as bot:
    out = await bot.client.profile.structured({"user_id": "u-42"})
    # {"status": "ready", "profile": {"preferred_name": "Sam", "timezone": "Europe/Berlin",
    #  "languages": ["en", "de"]}, "built_at": "...", ...}
```

| `status`    | 意味                                                                                                                                                                                                                      |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ready`     | 現在のスキーマのもとで、ユーザーの現在のメモリから構築されています。ファクトのないユーザーでは、すべて `null` です。                                                                                                      |
| `stale`     | 構築後にメモリまたはスキーマが変更された、または構築時にそのファクトの 1 つが提供されませんでした。パスが再構築するまで、構築時のまま提供されます。                                                                       |
| `pending`   | 有効なプロファイルがありません。未構築、構築済みの行が期限切れ、墨消し済み（ワーカーが再構築するまで）、または `high` ラベル付き（キーが新しいバージョンに移るまで保留）のいずれかです。すべてのプロパティが `null` になります。 |
| `no_schema` | 名前空間にスキーマがない、または保存済みのスキーマがもう解析できません。`profile` は `{}` です。                                                                                                                          |

- **ストレージ。** プロファイルは、キーが `structured_profile`（`agent` は `-`）のユーザーの `profile` 行です。そのため `profile.render` と `context.pack` にも含まれ、[`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory) でそのバージョンを一覧できます。来歴は `derived` で、`source_trace` は `profile.fold:structured_profile:<memory ids>`、つまり構築元のメモリです。提供されるのはワーカー（その `actor`）が書き込んだ行だけです。呼び出し側がそのキーに書き込んだ行は、タグに関係なくプロファイルなし（`pending`）として読まれ、固定、ロック、または `high` ラベルが付いていない限り、ワーカーがその上から再構築します。
- **ソース。** ユーザーの現在の `semantic`、`preference`、`decision` のファクトで、自由形式の fold と同じフィルターが適用されます。`high` の行、重複（その正規の行が対象になります）、他人についてのファクト（`about:other`）、保護属性、保存済みの忘却リクエストは対象外です。各ファクトは、それが真になった日時（`valid_at`）、なければ最後に変更された日時で日付が付けられ、構築では確信度が高く、最近変更されたものから順に読みます。そのため、編集は最新の記述として読まれます。
- **陳腐化。** 各プロファイルは、スキーマのプロパティのハッシュ（`title` やプロパティの順序を変えても変わりません）、ソースのダイジェスト（各ソースの id、コンテンツハッシュ、確信度、構築が読み取り順序に使う日付である `valid_at`、なければ最後の変更）、およびその件数を持ちます。ダイジェストが変わるのは、ファクトの追加や編集、リバート、復元、ファクトの削除、確信度や日付の変更、そして行をソースに出入りさせる変更（`high` になる、タイプが変わる、`about:other`、`attribute:`、`forget` タグが付く・外れる、期限切れになる、`valid_to` を過ぎる）のときです。言い直しや強化でも変わります。ファクトがモデルの読む内容の最新の位置に移るためで、クールダウン後に 1 回の再構築が発生します。ソースのファクトに対する `memory.lock` や `memory.unlock` も同様です。内容が同じ新しいバージョンが書き込まれ、ファクトの日付がロックの時点になり、モデルが読む内容の最新の位置、および構築が読むファクトのうち最新のものへ移ります。別々のキーのもとで食い違う 2 つのファクトのうち、最後にロックまたはロック解除されたほうが、そのとき最新として読まれます。最後の変更を保持する想起やバックフィルでは変わりません。ダイジェストはすべてのソースを対象としますが、構築が読むのは確信度が高く新しい `LOOM_STRUCTURED_PROFILE_MEMORY_LIMIT`（100）件だけです。それより多くのファクトを持つユーザーでは、その範囲外の 1 件を変更しても、回答を変えられないモデル呼び出しが発生します。スキーマの変更は次のパスから再構築され、1 パスあたり最大 `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` 件のプロファイルです。削除（プロファイルが数えたよりソースが少ない場合）はクールダウンをスキップし、次に名前空間がスキャンされたときに再構築されます。パスに予算が残っている間は、最大 `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`（600）秒後です。`profile.structured` は直ちに `stale` を報告します。その他の変更は `LOOM_STRUCTURED_PROFILE_COOLDOWN_SECONDS`（600）後に再構築されます。読むべきファクトが足りないまま構築された場合（数えたファクトのうち、メモリ上限までの 1 つが提供されなかった。ソースを数えたレプリカより ClickHouse のレプリカが遅れている、または Postgres の場合は、そのファクトに対して Postgres が保持する変更がまだ ClickHouse に届いていない）、`stale` として読まれ、クールダウン後に再び構築されます。そのような変更がまだ届いていない間は、そのたびにモデル呼び出しが発生します。数えたファクトが 1 つも提供されなかった構築ではモデルを呼び出しません。提供済みのプロファイルをすべて `null` のものに置き換え、クールダウンのたびに書き直します。提供されたプロファイルがないユーザー（`pending`）には何も書き込みません。ファクトがすべてなくなったユーザーには、モデル呼び出しなしですべて `null` のプロファイルが書き込まれます。期限切れまたは墨消しされたプロファイル行はプロファイルなし（`pending`）として読まれ、再構築されます。`high` のプロファイル行もプロファイルなしとして読まれますが、ワーカーはそのまま残します。キーが新しいバージョンに移るまで、固定と同様に保持されます。
- **出力上限。** モデルが書けるのは、`LLM_MAX_TOKENS` の `structured_profile` エントリ（32。`LOOM_LLM_MAX_TOKENS_OVERRIDES` で変更）に、完全な回答についてのスキーマの見積もりを加えた量です。見積もりは、各キーとその句読点、および各値（プロパティごとに 1 個、配列は `maxItems` 個）で構成されます。値の大きさは `maxLength` または最長の `enum` 値を 1 トークン 2 文字として換算し、どちらもない文字列は 48 トークン、数値とブール値は 12 トークンです。`profile_schema.set` は、見積もりが 16,384 トークンを超えるスキーマを拒否します。見積もりより長い文字列は切り詰められることがあります。自由形式のプロパティには、余裕を持たせるために `maxLength` を指定してください。
- **検証。** モデルの回答は、書き込まれる前にスキーマに照らして検証されます。型が誤っている値、enum や制約の範囲外の値、有限でない数値（または 2^53 を超える整数）、スキーマで宣言されていないプロパティは破棄され（`null`）ます。
- **パス。** 1 回のパスで構築するプロファイルは最大 `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS`（20）件です。パスはスキーマを持つ名前空間を、前回のパスが止まった位置から順に処理し、各名前空間に均等な割り当てを与えます。構築するものがない名前空間は割り当てを消費しないため、パスは予算を使い切るか、すべてに到達するまで次の名前空間へ進みます。名前空間が使わなかった分は、割り当てをすべて使い切った名前空間に、すでにスキャンで一覧化されたユーザーから回されます。パスは、スーパーバイザーが放棄するまで `LOOM_STRUCTURED_PROFILE_MAX_PASS_SECONDS`（600）秒実行できますが、その時間の後半には新たなユーザーを取りません。処理中の構築には後半の時間が完了のために与えられ、到達しなかったユーザーは次のパスのために一覧に残ります。名前空間の 1 回のスキャンでは、割り当ての最大 10 倍まで一覧化し、以降のパスは、その一覧が尽きる、スキーマが変わる、または一覧が `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`（600）秒経過するまで、そこから取ります。遅れているユーザーをすべて見つけたスキャン（求めた数より少ない）は、一覧が尽きた後はその時間が経つまで繰り返されません。そのため、他に遅れているユーザーがいない間、名前空間のスキャンはその時間につき最大 1 回です。構築に失敗したユーザー（モデルのエラー、JSON オブジェクトではない回答）は `LOOM_STRUCTURED_PROFILE_BACKOFF_BASE_SECONDS`（300）秒待ち、その後は倍々に増えて `LOOM_STRUCTURED_PROFILE_BACKOFF_CAP_SECONDS`（21600）秒が上限です。待機中に行われる名前空間のスキャンからは除外され、待機が終わった後の最初のスキャンで再試行されます。つまり、失敗から最大で待機時間に `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`（600）秒を加えた時間後です。スキャンまたは保存済みスキーマが失敗した名前空間は、カウントされ、待機が終わるかスキーマが変わるまで同じようにバックオフします。待機中の名前空間はパスで順番を取りません。プロファイル行が拒否した構築（ロック、固定、`high` ラベル、ブラックリスト）や、出力上限でモデルが回答を打ち切った構築は、ユーザーのメモリ、プロファイル行（固定やロックの解除。墨消しされた行でも同様）、またはスキーマが変わるか、`LOOM_STRUCTURED_PROFILE_REFUSAL_RETRY_SECONDS`（21600）秒が経つまで再試行されません。そのため、自然に失効するロックは再試行されます。これらの待機と、スキャンの一覧はワーカーのプロセス内に保持され、別のプロセスがワーカーのリースを取ると最初からやり直しになります。スキーマを持つすべての名前空間に到達して遅れているユーザーが見つからなかったパスは、次のパスの前に `LOOM_STRUCTURED_PROFILE_IDLE_INTERVAL_SECONDS`（600）秒待ちます。ローテーションの一部にしか到達しなかったパスは、ワーカーの間隔で再び実行されます。
- **消去。** ファクトもプロファイル行もないユーザーには、構築は何も書き込みません。したがって、スキャンが一覧化した後に消去されたユーザーには、後のパスが到達しても新しい行は作られません。また、モデルが回答した時点で、読み取ったファクトが現在のものでなくなっていた場合（実行中に消去または削除された）も、構築は何も書き込みません。残るのは、そのチェックと書き込みの間の短い隙間です。消去 saga の実行中にそこで書かれた行は、最終的な集計が見つける有効な行となり、saga はそれに対してフェイルクローズします。ソースのファクトに対する `memory.redact` は、そのファクトをトレースに挙げたすべてのプロファイルバージョンを消去し、プロファイルキーがロックされている場合は拒否されます。失敗後に再試行された redact は、同じプロファイルキーに到達します。トレースにメモリ id がないプロファイルバージョン（ソースを記録しない構築、ローリングアップグレード中の古い構築が書く `profile.fold:<label>` のみのもの）には到達せず、ユーザーが消去されるまでテキストが残ります（[セキュリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。
- **スコープ。** 読み取りは常に 1 つの `user_id` に対するものです。ユーザーに固定されたトークン（`read_user_id`、エンドユーザーのトークン）は、本文に `user_id` があってもなくても、そのユーザーのプロファイルだけを読み取れ、別のユーザーは 403 です。1 つのセッションに限定されたトークン（`own_session`）は拒否されます（`session_id` がなければ `IDENTITY_INCOMPLETE`、あれば `AUTH_INSUFFICIENT`）。プロファイルはユーザーの複数のセッションにまたがるためです。
- **モデル呼び出し。** モデルを呼び出すのはワーカーだけで、スキーマを持つ名前空間に対してのみです。モデル呼び出しがオフになっている組織（[`org.set_model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#org)）では呼び出されず、設定済みのプロバイダーまたは推論ゲートウェイを通して行われます。

<a id="how-contextpack-loads-them"></a>

## `context.pack` によるロード方法

`context.pack` は、クエリに依存するセクションより前に `working_memory` を最初に、`profile` を2番目にレンダリングします。そのため、パックの先頭部分は質問が変わっても同じままで、クライアントはその直後にプロンプトキャッシュのブレークポイントを置けます（`stable_prefix_chars`、`stable_hash`）。ワーキングメモリは、範囲が最も狭いものから順に重ねられます。run 向けのパックには、セッションのブロック（`run_id` が `-`）も含まれ、セッション向けのパックには、エージェントのブロック（`session_id` が `-`、`run_id` が `-`）も含まれます。プラグインの「where was I」ノート（`session:<project>`）はエージェント層に置かれ、`session_block` を設定すると、他のプロジェクトの `session:*` ノートは除外されます。セクションの順序、予算、その他の `context.*` op については、[統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)を参照してください。

<a id="settings"></a>

## 設定

| 設定                            | 効果                                                                                                                                                        |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROFILE_CONSOLIDATION_*`  | プロファイルのフォールドワーカー（[統合](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)）。                                                                  |
| `LOOM_STRUCTURED_PROFILE_*`     | 構造化プロファイルワーカー（[構造化プロファイル](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#structured-profiles)）。                                                |
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | 保護属性を持つ行にタグを付け、フォールドの対象から除外します（[機密性と来歴](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#sensitivity-and-provenance)）。 |

<a id="limits"></a>

## 制限

- **上限を超える書き込みは、切り詰められずに拒否されます。** `fill_ratio` を監視してください。
- **ワーキングメモリはリコールではありません。** ブロックがランク付けや埋め込みの対象になることはありません。`context.pack` と render/get 系の op を除くと、ブロックを返すのは run または session を指定した呼び出しだけです。`memory.ask` はその run のブロックを合成プロンプトに含め、`memory.search` はその run と session のブロックを `scratchpad` に返します。どちらもリクエストの読み取りスコープ内で読み取ります。
- **プロファイルのセクションはラベルごとに 1 行です。** `profile.set` はその行を置き換えます。以前のバージョンは [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory) から引き続き読み取れます。
- **render は、`scope="this_agent"` を指定しない限り、既定ですべてのエージェントにまたがります。**

<a id="related"></a>

## 関連ドキュメント

- [メモリの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)：永続的なファクト。
- [会話とセッション](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)：セッションの要約。
- [統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)：`context.*` と `loom_search`。
