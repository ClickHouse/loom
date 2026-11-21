<!-- i18n: source=docs/guides/agent-integration.md blob=456e73fdf0db8acbbcc8465c94067cbbf581e556 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), which is authoritative.

<a id="integrate-loom-into-an-agent"></a>

# エージェントへの Loom の組み込み

このガイドでは、エージェントが Loom に何を送り、返答をどのように読むかを説明します。対象は、クライアントの範囲、アイデンティティ、何を書き込むか、どのように読み取るかです。前提となるのは、[メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)と[アイデンティティと名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)の知識です。プラグインが提供されているコーディングエージェント（[Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)、[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)）では、その大部分をプラグインが担います。

同じルールをあなた向けではなくエージェント向けに書いたものが、`loom` Agent Skill（`skills/loom/`）です。プラグインを使わないエージェントは、サーバーからこれを読み取ります。実験的な [`mentor.guide`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md#mentorguide) は、ループ、状態の種類ごとの書き込み先、返答の読み方（`part: "skill"`）、またはすべてのプレーンとその op（`part: "reference"`）を返します。これをエージェントのシステムプロンプトに含めてください。

```python
system_prompt = "You triage support tickets.\n\n" + await agent.guide()
```

MCP では、プライマーがこの呼び出しを示します。Agent Skills クライアントでは、`npx skills add ClickHouse/loom --skill loom` でインストールします。

<a id="1-choose-the-client-surface"></a>

## 1. クライアントサーフェスを選ぶ

| サーフェス                                                   | 使う場面                                                                                              | 得られるもの                                                                                                                                                                                 |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **MCP** (`/mcp`)                                             | エージェントがすでに MCP を扱える LLM クライアントである場合（Claude Code、Codex、Cursor、デスクトップアプリ）。 | `?toolset=` ごとに厳選されたツール一覧、モデル向けに書かれたツールの説明、接続から得られる ID のデフォルト値。呼び出すタイミングはモデルが決めます。                                       |
| **Python SDK** (`clickhouse-loom`, `from loom import Agent`) | エージェントが自前の Python プロセスである場合。                                                      | プレーンごとの型付きクライアント、リトライと冪等性キー、モデルのツールループ向けの `agent.tools()`、実行スコープ、フレームワークアダプター（LangGraph `LoomStore`、Strands `LoomMemoryStore`）。 |
| **TypeScript SDK** (`@clickhouse/loom`)                      | エージェントが Node またはブラウザ側のサービスである場合。                                           | `Datapack` / `Agent`、生成された op テーブルから型付けされる `agent.call(op, body)`、`AgentTools`、`Session`。                                                                              |
| **HTTP** (`POST /v1/<op>`)                                   | その他の言語、または単発のスクリプト。                                                                | すべての op を同じ JSON ボディで利用できます。リトライと `Idempotency-Key` ヘッダーは自分で処理します。                                                                                    |

4 つとも、1 つのレジストリから同じ op を提供します。選ぶ基準は、呼び出しを誰が決めるかです。モデルが決めるなら MCP、コードが決めるなら SDK を使います。多くのエージェントは両方を使います。開始時のコンテキストの読み込みと終了時の実行記録には SDK を、モデルが覚えるか調べるかを選ぶ場面には MCP ツールを使います。MCP では `?toolset=` で一覧を選びます（[MCP ツールセット](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)）。一覧に含まれない op も `loom_op` から呼び出せます。

<a id="2-choose-the-identity"></a>

## 2. アイデンティティを選ぶ

すべての行は、アイデンティティのタプル `(org, namespace, agent, user_id, session_id)` と `run_id` を持ちます。トークンが `org` と名前空間を固定し、残りは呼び出しごとに渡されます。

| フィールド   | 設定する値                                                        | 重要な理由                                                                                                      |
| ------------ | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `namespace`  | エージェントごとに 1 つ（`agent-<name>`）、またはエージェントのチームで共有する 1 つ | **メンバーシップが読み取りの境界です**：名前空間の読み取り権限を持つ者は、どのエージェントが書き込んだかにかかわらず、その中のすべての行を読み取れます。 |
| `agent`      | 書き込むエージェントの安定した名前                                | 分離ではなく、書き込み元を示します。MCP 経由では、クライアント名（`clientInfo.name`、スラッグ化したもの）がデフォルトになります。            |
| `user_id`    | エージェントが多数のユーザーに対応する場合の、対応中のエンドユーザー | `--read-scope own_user` で発行したトークンでは、あるユーザーのプロンプトが別のユーザーのメモリを取得することはできません。 |
| `session_id` | 会話またはジョブ                                                  | ワーキングメモリと `own_session` の読み取りをスコープし、トランスクリプトをまとめます。                                             |
| `run_id`     | タスクの 1 回の実行                                               | 1 回の実行の書き込み、検索、チェックポイント、トランスクリプトを結び付けます（[実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)）。               |

- **1 つのエージェント、多数のユーザー、1 つのトークン：** `--read-scope own_user` でトークンを発行し（または `--read-user-id` で固定し）、すべての呼び出しで `user_id` を渡します。スコープ付きの読み取りで `user_id` がない場合は 400 `IDENTITY_INCOMPLETE` を返し、範囲が広げられることはありません。
- **複数のエージェントで知識を共有する：** 全員が読み取る共有名前空間を 1 つ用意し、各エージェントが自身の `agent` で書き込みます。または、共有名前空間を読み取り、自身の名前空間に書き込むトークンを使います（`ns_write`）。[名前空間アクセス](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#namespace-access)を参照してください。
- **`agent` にシークレットや個人を特定できる識別子を入れないでください：** この値はすべての台帳の行に記録されます。

<a id="3-decide-what-to-write"></a>

## 3. 何を書き込むかを決める

まずプレーンを選びます。永続メモリは埋め込み、ランク付け、競合チェック、バージョン管理の対象です。ワーキングメモリはキーで読み取り、ランク付けは行いません。ナレッジはチャンク化したドキュメントを保持します。

| エージェントが持っているもの                                                                              | 書き込みに使うもの                                                                            | 使わないもの                          |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------- |
| 来週も真であり、別のセッションが必要とする事実（決定事項、制約、担当者、設定の好み）                      | `memory.set`：1 回の呼び出しにつき 1 つの事実、1 つの事実にキーを 1 つ                        | ワーキングメモリ                      |
| 現在の計画、未確定の前提、このタスクのチェックリスト                                                      | `working_memory.set` / `append`。`context.pack` が読み込む                                    | `memory.set`                          |
| 記憶しておく価値のあるトランスクリプト、メール、スレッド                                                  | `memory.set_from_messages`：サーバーが短いキーの事実を抽出する                                | 巨大な 1 つの `memory.set`            |
| ドキュメント（仕様書、ランブック、レポート、約 2 KB を超えるもの）                                        | Markdown として `knowledge.upsert`、さらに同じキーで `memory.set` に案内用の段落を 1 つ書く   | テキスト全体を保持する 1 つの `memory.set` |
| 識別子を持つ事実の表（番号付きの PR、id 付きのサービス）                                                  | 最初に 1 回 `memory_schema.register`、その後 `memory.record.write`                            | バッチごとに 1 つのメモリ行            |
| 協調状態（リース、フリーズフラグ、「現在どこまで進んだか」）                                              | `block_key` を指定した `working_memory.set`                                                   | ループ内の `memory.set` + `memory.get` |
| 再開用のチェックポイント                                                                                  | ワーキングメモリのブロック、または大きな blob には `context.offload`                          | ステップごとの永続メモリ              |
| 後で表示・検索したい会話                                                                                  | `conversation.create` / `conversation.append`                                                 | メモリ                                |
| 推論、あいさつ、途中のツール出力                                                                          | 何も書かない                                                                                  | すべて                                |

[調査エージェントのメモリ](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md)では、この表をケースを扱うエージェントに適用しています。チェックポイントについては[トレースと再開](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)を参照してください。

**後のセッションが信頼できるように、各事実を整える：**

- **主張を先に書く。** value には事実を平易なテキストで書き、出典の行をその後に続けます。埋め込まれるのは value の先頭部分だけです（抜粋は 1,200 文字で切り詰められます）。
- **読みやすいキー。** 形式は `<scope>/<kind>/<id>` で、ハッシュではなく `sre/case/1234/evidence/dns-sidecar` のようにします。ファセットは `tags`（`case:1234`、`kind:evidence`）と `entities` に入れます。
- **改訂は同じキーを書き換える。** Loom はバージョンを保持し（`memory.history`）、現在のバージョンを返します。`deploy_window_v2` のようなキーを新しく作らず、現在のキーを指すインデックス行も持たないでください。
- **出典を明記する。** 事実の読み取り元のファイル、URL、ドキュメントには `source_resource_id` を使います。
- **推論を事実として書かない。** 「A と B からの推論：C」と書き、`confidence` は 1.0 未満にします。
- **日付は解決しておく。**「昨日」ではなく「<YYYY-MM-DD> に」と書き、`valid_at` には観測した時点を設定します。
- **シークレットは書かない。** ユーザーが記憶してほしくない種類のものには、パターンを `blacklist.add` します。一致する書き込みは 422 `BLACKLISTED` で拒否されます。

ユーザーの発言（「デプロイは火曜日に行う」）が書き込みのきっかけです。先に検索しないでください。重複と競合はサーバーが処理します。

<a id="4-read-the-writes-answer"></a>

## 4. 書き込みの応答を読む

`memory.set` の応答は受領確認です。次の内容を読み取ります。

| フィールド                                | 意味                                                                                                                                               |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_id`, `version`, `content_sha256`  | 書き込みが永続化されたことを示します。これらがない場合は永続化されていません。保存されていないのに、保存したとユーザーに伝えてはいけません。      |
| `conflict_verdict: conflict`, `peers`     | 書き込みが既存のファクトと衝突しています（両方の行が `conflicted` とマークされます）。両方の値を示し、どちらが現在の値かを尋ねてください。黙って上書きしないでください。 |
| 409 `PINNED_CONFLICT`                     | 既存のファクトがユーザーによってピン留めされています。その旨を報告してください。新しいキーで再試行しないでください。                               |
| `duplicate_of`, `merged_into_key`, `noop` | そのファクトはすでに登録済みで、新たに保存されたものはありません。                                                                                 |
| `advisories`                              | 形式に関する警告です。base64、二重エンコードされた JSON、`LOOM_MEMORY_VALUE_ADVISORY_CHARS` を超える値などが対象です。                           |

複数の書き込み側が 1 つのキーで競合する可能性がある場合は、`memory.get` で取得した `if_match_sha256` を渡してください。ハッシュが古いと、上書きされずに拒否されます。

<a id="5-read-with-one-call-per-question"></a>

## 5. 質問ごとに1回の呼び出しで読み取る

1. **タスクごとにコンテキストを1回だけ読み込む。** `context.pack` は、トークン予算に収まるバンドルを返します。内容は、プロファイルのセクション、作業メモリ、`query` に関連するメモリ、手順で、必要に応じてナレッジ、会話、最近の実行（`include_knowledge`、`include_conversations`、`include_runs`）も含められます。プラグインはセッション開始時にこれを呼び出します。

   ```python
   pack = await agent.context.pack("fix the flaky ingest test", token_budget=2000)
   system_prompt = base_prompt + "\n\n" + pack.rendered
   ```

2. **質問ごとに検索を1回だけ行う。** MCP では、`loom_search` が1回の呼び出しでメモリ、プロファイル、ナレッジ、会話を対象に検索します。SDK では `agent.memory.search(query)` を使います。出典付きの回答が必要な場合は `agent.memory.ask(question)` を使います。回答にさらに多くの行（一覧や件数）が必要な場合にのみ、`next_page` でページを送ります。
3. **abstain は回答として扱う。** `abstain: true` は、類似度のゲートをどの候補も通過しなかったことを意味します。最も近い弱いヒットを返さず、「メモリにありません」と返答してください。`abstain_code` はどのゲートかを示します（[abstain コード](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)）。`pending_extraction` が 0 より大きい間は、遅延された抽出が実行されていないため、空の回答が存在しない証拠にはなりません。
4. **出所を尊重する。** ヒットの `source_class` は、そのコンテンツがどのようにメモリに入ったかを示し、`verified` は有効な証明（attestation）が対象にかかっているかどうかを示します。`memory.get` は `verification` を返します（`human`、`process`、証明後にコンテンツが変更された場合の `stale`、または `none`）。`memory.ask` は、デフォルトでは `external` と宣言され、証明されていない行を合成から除外します。

```python
hits = await agent.memory.search("refund window for EU customers", top_k=5)
if hits.abstain:
    reply = "I don't have that in memory."
else:
    reply = hits[0].content_excerpt
```

<a id="6-record-runs"></a>

## 6. 実行を記録する

実行（run）とは、タスクを 1 回実行したものです。目標、想起して依拠したメモリ、書き込んだ内容、結果を含みます。実行を記録しておくと、ランキングがどのメモリが役に立つかを学習できます（[実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)）。

```python
async with agent.run_scope(goal="Fix the flaky CI job") as run:
    answer = await agent.memory.ask("what broke CI last week?")
    run.used("ci/flaky/runner-image")
    run.final_response = "Pinned the runner image."
```

スコープは、開始時に `run.begin` を、終了時に `run.complete` を呼び出します。ステータスは `completed` または `failed` で、`memories_used` には、`memory.ask` の回答が引用したキーと `used()` に渡したキーが入ります。ブロック内の書き込みには、その `run_id` が付与されます。SDK を使わない場合は、`run.begin` と `run.complete(run_id, status=..., memories_used=[...])` を自分で呼び出してください。`memories_used` はエージェントが依拠したメモリであり、提示されたすべてのメモリではありません。想起されたキーは別に追跡されます。

プラグインを使わないエージェントも、完了したトランスクリプトを `memory.set_from_messages` に渡す（または `agent.session(...)` を閉じる）ようにしてください。これにより、サーバーが永続的な内容を抽出します。

<a id="7-follow-the-memory-skills-review"></a>

## 7. メモリスキルのレビューに従う

プラグインには `loom-memory` スキル（`integrations/claude-code-plugin/skills/loom-memory/`、Codex プラグインには生成されて含まれます）が同梱されており、このガイドの内容をモデル向けにまとめています。素の MCP 上で動作するコーディングエージェントは、[エージェント向け指示](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-instructions.md)ブロックを使います。このブロックの最後のメモリ行は、このレビューを短くしたものです。完全なレビューを実行するには、以下の質問をそのブロックの下に貼り付けてください。それ以外のエージェントは、質問を自分の指示にコピーします。完了を報告する前に、「はい」と答えられるものだけを書き込みます。

1. 今後のセッションに必要な決定、制約、担当者を、作成した、または把握したか。
2. 既存のメモリが誤っていた、または古くなっていたか。その場合は、同じキーで更新する。
3. ユーザーから訂正を受けたか。その場合は事実を更新する。却下された場合は `memory.delete` で削除し、再発しそうなら `blacklist.add` でそのパターンを登録する。
4. 今後の作業で把握しておくべき成果物（ファイル、PR、ダッシュボード）を作成したか。
5. 手間をかけて見つけた、自明ではない事柄があるか。
6. 書き込んだ推論が確認されたか。その場合は `confidence` を上げ、「Inferred」の接頭辞を外す。

その後、`namespace.validate` を実行し、書き込んだキーに言及している指摘に対応します。「保存した」と言えるのは、レシートに `memory_id` と `version` が含まれていた場合のみです。「検証済み」と言えるのは、人またはプロセスが確認を証明した場合のみです。チェックを実行できなかった場合は「ここでは未検証」と書きます。ほかに同梱されているスキルは、コードに関する質問（`loom-code`。どのクライアントでも使えるよう `skills/loom-code/` にもあります）、名前空間の再構成（`loom-optimize`）、セッションの復元（`loom-session`）、クライアントの状態確認（`loom-status`）を扱います。

<a id="mistakes-to-avoid"></a>

## 避けるべき間違い

`namespace.audit` はこれらをそれぞれ検出します。[名前空間の最適化](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)は、その検出結果を修正に変えます。

| 検出項目                              | 間違い                                                                                  | 代わりに                                                                                        |
| ------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `double_encoded_values`               | `json.dumps(json.dumps(doc))`：埋め込まれるテキストがエスケープされた引用符で始まります。 | テキスト、またはオブジェクトそのものを `value` として送信します。                               |
| `json_prefix_excerpts`                | 埋め込みの切り詰め長を超える JSON オブジェクト。ベクトルはその先頭部分から作られます。  | まずプレーンテキストとして登録し、ドキュメントは `knowledge.upsert` に送ります。                |
| `hash_keys`                           | 行の識別子に sha を使うこと：読めず、グループ化できず、改訂が新しいキーになります。     | `<scope>/<kind>/<id>`。改訂は同じキーのまま行います。                                           |
| `exact_key_store`                     | 完全一致のキーによる読み取りが検索を大きく上回る：その名前空間はオブジェクトストアです。 | `memory.search` で想起し、リースやポインタはワーキングメモリに置きます。                        |
| `coordination_as_memory`              | リース、ロック、凍結フラグをメモリとして保存し、ループで読み取ること。                  | `working_memory.set` / `get`：埋め込み、ランク付け、競合チェックは行われません。                |
| `hand_rolled_index_rows`              | エージェントが自分の行を見つけられないために、他のキーを列挙する行を作ること。          | `tags` と `memory.list prefix=`。改訂は `memory.link supersedes` でつなぎます。                 |
| `oversized_values`, `unembedded_rows` | ドキュメントをメモリとして保存すること。埋め込みモデルの上限を超えた値は埋め込まれません。 | `knowledge.upsert` と、1 段落のポインタ行を使います。                                           |
| `episodic_blobs`, `version_churn`     | 実行ダンプをエピソード行として保存し、ステップごとに書き換えること。                    | blob には `context.offload` を使い、実行ごとに短いエピソード行を 1 つ置きます。blob ではなく主張（claim）を改訂します。 |

監査では検出されないものがさらに 2 つあります。

- **ターンごとに 1 つの blob にまとめる。** 5 つの事実は、それぞれ別のキーで 5 回の `memory.set` 呼び出しにします。
- **稼働中のエージェントがまだ読んでいるキーを廃止する。** 読み取りは `found: false` を返し、クライアントがローカル状態にフォールバックして、2 つのストアが食い違うことがあります。移動には `memory.move` を使い（リダイレクトが残ります）、すべての読み取り側が移行するまで何も削除しないでください。

<a id="related"></a>

## 関連ドキュメント

- [ヘッドレスボット](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)
- [メモリの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- [フレームワーク](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)
- [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
