<!-- i18n: source=docs/features/conversations-and-sessions.md blob=b2079f3bf2ad65d5e3c485f2587db24b35257342 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md), which is authoritative.

<a id="conversations-and-sessions"></a>

# 会話とセッション

Loom は 3 種類のセッション状態を保持し、それぞれ独自のプレーンに置きます。

| プレーン           | 保持するもの                                                               | 埋め込み・検索                                                              | 書き込み元                                                                  |
| ------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| 会話プレーン       | スレッドの発話をそのまま記録したもの（`loom.conversations`）               | ユーザーとアシスタントの発話、およびコンパクションの要約。`conversation.search` で検索します | `conversation.append`、プラグインのキャプチャフック、`loom ingest-transcript` |
| セッションブリーフ | 1 つの作業の流れを要約した、再開可能な状態（`loom.session_briefs`）        | いいえ                                                                      | `session.brief_save`、コンパクション時とセッション終了時のコーディングプラグイン |
| メモリ             | 発言内容から抽出された永続的な事実                                         | はい。`memory.search` で検索します                                          | `memory.set`、`memory.set_from_messages`                                    |

発話を保存してもメモリは作成されません。会話を事実に変換するには、[`memory.set_from_messages`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) に会話を送信してください。オーナーは、抽出済みのメモリを失うことなく、トランスクリプトの記録をオフにできます。また、ブリーフが想起時に事実と競合することはありません。

<a id="conversations"></a>

## 会話

<a id="recording-is-the-owners-choice"></a>

### 記録するかどうかはオーナーが決める

`conversation.append` は、名前空間で会話ログが有効になっている場合にのみターンを保存します。このスイッチ（`loom.namespaces` の `conversation_logging_enabled`）はデフォルトでオフで、フェイルクローズします。行のない名前空間は記録されません。オーナーは `conversation.set_logging(enabled=true)`（admin）またはダッシュボードの Conversations ページにある **Record conversations** スイッチでオンにします。

ログがオフのとき、append は受理されますが何も保持されません。レスポンスは `stored: false, reason: "logging_disabled"` を返します。クライアントがオーナーの選択を上書きすることはできません。埋め込みの実行中にターンが削除された場合、append は遅れて届いたベクトルを破棄し、`stored: false, reason: "deleted"` を返します。

<a id="writing-turns"></a>

### ターンの書き込み

`conversation.append` は `thread_id`、`role`（`user`、`assistant`、`system`、`tool`）、`content` を受け取り、任意で `msg_id`、`reasoning`（モデルの推論を専用のカラムに保持）、`user_id`、`run_id`、`metadata`、`tokens` を受け取ります。スレッドは最初の append で作成されます。`conversation.create` は `title`、`metadata`、`run_id` を事前に付与するだけです。

- **`msg_id` で冪等です。** `(thread_id, msg_id)` がすでに書き込まれているターンは再度書き込まれません（`reason: "duplicate"`）。`msg_id` がない場合、サーバーはスレッド、ロール、内容のハッシュから導出します。そのため、クライアントがターンを再ログしても、トランスクリプトが二重になることはありません。
- **user ターンと assistant ターンは埋め込まれます。** 対象は先頭 2,048 文字までで、埋め込み行はこの文字列を保持します。再埋め込みは、ベクトルの生成元となったテキストから始まります。以前のビルドで書かれた行は 240 文字を保持しており、再埋め込みはそれをもとに行われます。`system` ターンと `tool` ターンは、コンパクションの要約を除き、インデックスされずにトランスクリプトに残ります。
- **埋め込みはフェイルオープンです。** 埋め込みに失敗してもターンは保存され、エラーは埋め込み行に記録されます。再試行は行われません。そのターンはベクトル検索の対象外となり、部分文字列によるフォールバックでのみ見つかります。
- **high と判定されるターンは埋め込まれません。** テキストに high の形（貼り付けられた API キー、`password=` の代入、カード番号、SSN、記載された PIN）が含まれるターンは、そのまま保存されますが、埋め込み行は作られず、テキストがエンベッダーに送られることもありません。`conversation.search` は、ベクトル検索でも部分文字列のフォールバックでも、そのターンを返しません。`conversation.threads` は、そのようなターンが最後にあるスレッドを、空の抜粋（および high と判定されるタイトルは空）で一覧表示します。このとき、抜粋として表示される 240 文字と、それに続く 1,024 文字を確認します。タイトル生成処理は、そのようなターンをモデルに送りません。最初の 8 件の user ターンと assistant ターンがすべて high と判定されるスレッド、または生成されたタイトルが high と判定されるスレッドは、タイトルなしのままとなり、メタデータに `title_refused` が記され、タイトル生成処理が再度問い合わせることはありません。そのようなターンが 8 件に満たないスレッドは、マークされないまま、通常のターンを待ちます。`conversation.update` または `conversation.create` による新しい `metadata` はこのマークを置き換え、タイトル生成処理はもう一度だけ問い合わせて、スレッドに再びマークを付けます。`conversation.get` と `conversation.list` は、引き続きそのターンをスレッド内で返します（[機密度](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)）。

<a id="saving-single-messages"></a>

### 単一メッセージの保存

チャットクライアントは、名前空間のトランスクリプトを記録せずに、ユーザーが 1 件のメッセージを後で検索できるよう保存させることができます。`conversation.save_message`（writer）がそのオプトインで、`conversation.search_saved_messages`（viewer）がその検索です。保存されたメッセージは会話の行であり、ユーザーの個人コレクションのドキュメントにはなりません（[共有](../architecture/sharing.md#saved-messages)）。

クライアントがメッセージを保存してよいのは、すべてのレプリカがコンパクション中に保存状態を保持する場合に限られます。[ローリングアップグレードのルール](../architecture/rolling-upgrades.md#the-rules)を参照してください。

これらは `conversation.append` および `conversation.search` と、次の 6 点で異なります。

- **Record conversations スイッチは適用されません。** スイッチ（または `conversation.set_logging`）で記録がオフになっていても保存は行われ、保存によって記録がオンになることはありません。
- **メッセージにはフラグが付き、ラベルは付け替えられません。** トランスクリプト行とその埋め込み行は、`is_saved` と `saved_at`（ユーザーがメッセージを保存した時刻）を持ちます。トランスクリプトにすでにあるメッセージはその場でフラグが付けられ、`run_id` と内容は記録されたままです。
- **検索はユーザー単位です。** `conversation.search_saved_messages` は、指定した 1 つの名前空間内で `user_id` が所有するフラグ付きメッセージのみを返します。`conversation.search` は記録されたすべてのターンをランク付けし、デフォルトでは呼び出し元の名前空間全体に展開されます。
- **検索はハイブリッドです。** ベクトル側はメッセージの先頭 2,048 文字を埋め込み、BM25 側は全文を読みます。そのため、正確な用語（チケット ID、テーブル名）や、長い保存済み回答の奥にあるフレーズでも上位に出ます。`search_mode="hybrid"`（デフォルト）は両者を逆順位で融合し、`semantic` と `lexical` はどちらか一方だけを実行し、`lexical_weight`（0〜1）は BM25 側の重みを調整します。BM25 のヒットの抜粋は、一致した語を中心に切り出されます。対象は 1 人のユーザーの保存メッセージなので、統計はクエリごとに計算されます。
- **1 人のユーザーに固定されたトークンが使えます。** 1 つの資格情報で多数のユーザーにサービスを提供するインテグレーションは、`--read-scope own_user`（特定のユーザーには `--read-user-id`）を付けてトークンを発行します。以後、保存メッセージの op はそのユーザーとしてのみ動作します。別の `user_id` を指定したボディは 403 で拒否され、すべてのステートメントにトークンのユーザーが付きます。エンドユーザートークンは `namespace: "@me"` でこれらの op を呼び出します（[共有](../architecture/sharing.md#saved-messages)）。会話テーブルには `session_id` がないため、`own_session` トークンは拒否されます。他のすべての会話 op（`append`、`get`、`list`、`search`、`threads` など）は、スレッドに複数のユーザーが混在し、制限できるユーザー軸がないため、このようなトークンでは引き続き拒否されます。
- **所有者、削除、ロールは保存のたびに確認されます。** 別の `user_id` の下に保存されたメッセージを保存しようとすると、`indexed: false, reason: "owner_mismatch"` が返ります。トゥームストーン化されたメッセージには `reason: "deleted"` が返り、保存済みの system ターンや tool ターンは埋め込まれないため `reason: "unsupported_role"` が返ります。テキストが high と判定されるメッセージも埋め込まれず、`reason: "high_sensitivity"` が返り、新規には保存されません。以前のビルドで保存されたものは、`conversation.search_saved_messages` のベクトルヒットにもキーワードヒットにもならず、`conversation.list_saved_messages` は空の抜粋で一覧表示します（`conversation.threads` と同様に、表示する `conversation_search_excerpt_chars` 文字と、それに続く 1,024 文字を確認します）。これらの確認は、すでに保存済みのメッセージにも適用されます。

保存と保存解除は、append による埋め込み書き込み、コンパクションによる退避、スレッド削除と、更新可能なスレッドリースを共有します。append とコンパクションは、リースを取得する前にプロバイダーの処理を完了します。

保存メッセージの所有者はトランスクリプト行が決めます。保存は、誰も所有していない行、または呼び出し元が所有する行のみを、ミューテーション内で確保します。したがって、所有者のいない 1 件のメッセージを 2 人のユーザーが保存した場合、先のユーザーが確保し、後のユーザーには `owner_mismatch` が返ります。埋め込み行は、トランスクリプト行が呼び出し元のものとして読み戻された後にのみフラグが付けられます。

保存は安全に再試行できます。新規メッセージの同時保存でも、トランスクリプト行は 1 つしか保存されません。その挿入には、メッセージ ID から導出された重複排除トークンが付いているためです。各呼び出しはメッセージを読み直し、中断または同時の書き込みが残したものを修復します。欠けている埋め込み行を書き込み、直前の埋め込みが失敗していれば再度埋め込み、まだフラグが付いていない行にフラグを付けます。埋め込みに失敗した保存では、メッセージは保存済みとして格納され、`indexed: false, reason: "embed_failed"` が返ります。もう一度保存すると、埋め込みが再試行されます。同時の保存の後に埋め込みが到着したトランスクリプト append は、自身の埋め込み行にフラグを付けます。

`conversation.list_saved_messages`（viewer）は、クエリなしで 1 人のユーザーの保存メッセージを、最近保存したものから順にページ送りします（`limit` は最大 200、不透明な `cursor`、任意の `conversation_id`）。その `ts` はユーザーがメッセージを保存した時刻で、トランスクリプトのメッセージ時刻とは独立しています。保存時刻なしで書かれた行は、メッセージのタイムスタンプを使います。これは「Saved」パネルの最初のビューで、ユーザーが入力すると検索で絞り込まれます。`conversation.saved_status`（viewer）は 1 つの `conversation_id` と最大 500 件の `message_ids` を受け取り、そのうちこのユーザーが保存したものを返します。これにより、クライアントは会話を開いたときに、すべてのメッセージの保存状態を 1 回の呼び出しで描画できます。

`conversation.unsave_message`（writer）は、保存済みメッセージを所有者の検索から取り下げます。名前空間がトランスクリプトを記録している場合、オーナーが記録を選んでいるため、ターンはフラグが解除された状態でトランスクリプトに残ります。記録していない場合、そのターンは保存のためだけに存在していたので、軽量削除で取り除かれます（`removed: true`）。埋め込みはトゥームストーンを保持するため、古いバージョンが再び現れることはありません。呼び出し元のユーザーが保存していないメッセージや、別のユーザーが保存したメッセージには `found: false` が返り、どちらなのかは示されません。保存解除は冪等で、再度保存するとメッセージは復元されます。ユーザー単位の消去は、そのユーザーの他の会話行とともに保存メッセージも削除します。

<a id="reading-and-searching"></a>

### 読み取りと検索

| 操作                   | ロール | 内容                                                                                                                       |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------- |
| `conversation.threads` | viewer | 名前空間の最近のスレッドを新しい順に返します（`window_days`、`limit`、最大 100,000 までの `offset`。すべてのエージェントには `agent="*"`）。 |
| `conversation.get`     | viewer | 1 つのスレッドのメタデータを返します。`include_messages=true` でそのターンをページ送りします（末尾からは `order="desc"`）。 |
| `conversation.list`    | viewer | スレッドのターンを、カーソルによるページ送りで返します。`run_id` で絞り込むこともできます。                                |
| `conversation.search`  | viewer | 埋め込み済みターンに対するベクトル検索です。スレッド、run、ロールで絞り込むこともできます。                                |
| `conversation.update`  | writer | スレッドの `title`、`metadata`、`run_id` を変更します。                                                                    |

`conversation.search` はターンをコサイン類似度でランク付けし、`LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`（0.30）未満のものを除外し、デフォルトでは呼び出し元の名前空間全体に展開されます（`namespace_scope="all_mine"`）。エンベッダーが利用できないときは、op が応答できるように部分文字列一致にフォールバックします。ヒットにはターンの先頭 `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS` 文字（デフォルトは 240）が表示されます。ターン全体は `conversation.get` または `conversation.list` で読んでください。この op は MCP からは隠されており、エージェントは [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) の会話セクションを使います。

`memory.recall` は別の読み取りです。抽出が読んだターン（`loom.memory_evidence` に保持）を検索します。これは会話プレーンが記録しているかどうかにかかわらず存在します。[検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)を参照してください。

<a id="compacting-and-deleting"></a>

### コンパクションと削除

- **`conversation.compact`**（writer、LLM プロバイダーが必要）は、末尾の `keep_recent`（デフォルト 10）より前にある最も古いターンのうち、要約器のプロンプトに収まるものを要約します。要約は、`metadata.compacted_from` に要約された `msg_id` を列挙した、埋め込み済みの 1 件の `system` ターンとして書き込まれ、元のターンとその埋め込みはトゥームストーン化されます。その後 `conversation.search` は、元のターンの代わりに要約を見つけます。スレッド内の別のエージェントが、ターン検索がインデックスするのと同じテキストのターンを有効なまま保持している場合、そのターンは埋め込みを保持します（以下の delete を参照）。新しいターンが `min_to_compact`（デフォルト 4）件に満たないウィンドウは、そのまま残されます。以前の要約は、`min_to_compact` にも `messages_summarized` にも数えられません。ウィンドウの先頭にある要約は、別枠の許容量からプロンプトに取り込まれます。その残りに収まらない要約は有効なまま残り、失敗した実行が残したもの（そのターンがすべて有効でウィンドウ内にあり、最初のターンの `ts` にあるもの）は、読まれずに置き換えられます。ウィンドウのさらに奥にある要約は、通常のターンと同じようにプロンプトの予算を消費し、そのターンとともに置き換えられます。high と判定されるターンはウィンドウから除外されます。そのターンは有効なまま残り、要約器に送られることもないため、ウィンドウの最初のターンに置かれる要約は、スレッド内でそのターンより前に来ることがあります。`conversation.save_message` で保存されたターンも、要約もトゥームストーン化もされず、要約と最近のターンの間にそのまま残ります。埋め込めない要約では何もコンパクションされず（`reason: "embed-failed"`）、いずれにせよ high と判定される要約でも同様です（`reason: "summary-high-sensitivity"`）。そのような要約は埋め込まれないためです。`embed-failed` と違い、この拒否は再試行する価値がありません。ターンが有効な間はウィンドウが変わらないため、再試行しても同じ要約器の呼び出しに費用がかかり、同じ要約が得られる可能性が高いからです。保存済みターンのみ、または high と判定されるターンのみを含むウィンドウには、`reason: "nothing-to-compact"` が返ります。
- **`conversation.delete`**（admin）は、1 つのスレッドのターン、スレッド行、埋め込みをトゥームストーン化します。`confirm_thread_id` は `thread_id` と等しくなければなりません。スレッド内で別のエージェントもログしたターンは、そのエージェントが、検索がインデックスするのと同じテキスト（user ターンまたは assistant ターン、あるいは要約）の有効なコピーを保持している間、埋め込みを保持します。2 つのコピーが 1 つの埋め込み行を共有するためです。その行はそのコピーのエージェント、ロール、run、ユーザーを引き継ぐので、削除したエージェント自身の検索からは外れます（コンパクションも同様です）。ちょうど 240 文字の行（以前のビルドの切り詰め、またはその長さのターン）は、先頭 2,048 文字がそのエージェントのターンと一致するコピーに対してのみ保持され、照合するメッセージ行が残っていない場合はトゥームストーン化されます。削除は、そのターンについて自身のエージェントが有効なメッセージを保持しなくなったスレッドの行も完了させます。対象は、削除済みまたはコンパクション済みのもの（以前のビルドが残したもの）と、保持期間の TTL が切れたものです。個人や名前空間のデータをすべての場所から削除するには、[`erasure.request`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) を使います。

トゥームストーン化されたターンは、`erasure.export` を除くすべての読み取りから除外されます。これは、`include_tombstoned: false` を指定しない限りそれらを返します。ディスクから削除されるのは、オペレーターがテーブル TTL（`LOOM_RETENTION_CONVERSATIONS_DAYS`、0 = 永久に保持）を設定した場合に限られます。この TTL の対象は `loom.conversations` のみで、テキストを最大 2,048 文字保持するターンの埋め込み行には TTL がありません。メッセージが期限切れになると、そのスレッドの全体削除によって、その行はターンの有効なコピーに引き継がれるか、トゥームストーン化されます。消去リクエストはそれをトゥームストーン化し、実際に取り除くのは `purge` を伴う `namespace_list` の消去のみです。`erasure.export` は埋め込み行を返さないため、TTL で期限切れになったターンの抜粋は、対象者とともに消去されますが、エクスポートには含まれません。

<a id="thread-titles"></a>

### スレッドのタイトル

`conversation_titler` ワーカーは、タイトルのないスレッド（`conversation.create` なしの append で作られたもの）に、LLM が書いた短いタイトルを付けます。ダッシュボードの Conversations ページはこのタイトルを先頭に表示します。対象は、会話を記録する名前空間のうち、直近の `LOOM_CONVERSATION_TITLER_WINDOW_HOURS`（168）時間以内にアクティブで、少なくとも `LOOM_CONVERSATION_TITLER_MIN_TURNS`（4）件のターンを持つスレッドで、1 回のパスにつき最大 `LOOM_CONVERSATION_TITLER_MAX_THREADS_PER_PASS`（10）件です。デフォルトで有効（`LOOM_CONVERSATION_TITLER_ENABLED`）で、フェイク LLM では何もしません。

<a id="capturing-sessions-from-clients"></a>

## クライアントからのセッションのキャプチャ

| クライアント             | ターンが Loom に届く仕組み                                                                                                                                                                                                                                                    |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code プラグイン | Stop フック（`hooks/loom-conversation.mjs`）がセッションのトランスクリプトから新しいターンをそれぞれ追記します。`msg_id` にはトランスクリプトの uuid が使われます。`LOOM_CAPTURE_CONVERSATIONS=false` で無効になります。名前空間の記録可否の判定をキャッシュし、記録がオフの間は追記をスキップします。 |
| Codex プラグイン       | 同じキャプチャを行います。詳しくは [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) を参照してください。                                                                                                                                                                                                                 |
| Cursor             | Cursor の会話後フックから実行される `integrations/cursor/cursor-session-hook.sh` が、トランスクリプトを `loom ingest-transcript` に渡します。                                                                                                                                |
| その他のエージェント       | エクスポートしたトランスクリプトに対して `integrations/generic/ingest.sh` を実行します。                                                                                                                                                                                                            |
| 任意の MCP クライアント     | デフォルトの `core` 一覧には `conversation.append` が含まれず、モデルにターンの記録を促すものもありません。トランスクリプトフックでキャプチャするか、`loom_conversation_append` を一覧に含む `?toolset=all` をマウントしてください。                                                     |
| 独自のコード      | `conversation.append`、または下記の Python SDK の `Session` を使います。                                                                                                                                                                                                            |

`loom ingest-transcript --namespace NS --file session.md` は、`# user` と `# assistant` のセクションが交互に並ぶ Markdown ファイル、または `{role, content}` の JSON 配列を読み込み、各ターンを追記します（`--dry-run` はターン数を数えるだけで、`--thread-id` は固定の ID を設定します）。

プラグインは、セッション終了時にそのセッションのターンを `memory.set_from_messages(defer=true)` にも渡します。そのため、トランスクリプトが記録されているかどうかにかかわらず事実が抽出されます。また、ツールの操作を `observe.record` / `observe.crystallize` でエピソード的な観測として記録します（[抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)を参照）。フックごとの接続方法は [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) にあります。

<a id="session-briefs"></a>

## セッションブリーフ

セッションブリーフは、1 つの作業の流れについて、再開可能な状態をまとめたものです。目標、ステータス、決定事項、行き止まり、完了した作業、未解決の質問、次のステップ、主要なファイル、制約、参照先を含みます。コンパクション後、翌日、別のクライアント、あるいはチームメイトが読みます。埋め込みは作成されず、`memory.search` からも返されません。名前空間を読めるエージェントであれば、誰でも再開できます。

| オペレーション | ロール | 動作 |
| ---------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------- |
| `session.brief_save`   | writer | `events`（前回の保存以降に起きたこと）、エージェントが書いた `sections`、またはその両方からブリーフを更新（または新規作成）します。 |
| `session.brief_get`    | viewer | `brief_key` でブリーフを読み込みます。指定がなければ、セッションが最後に書いた最新のブリーフ（`from_session`）、それもなければ `project` の最新のブリーフを読み込みます。 |
| `session.brief_list`   | viewer | 名前空間のブリーフを新しい順に 1 行ずつ一覧表示します（呼び出し元のものだけにするには `mine_only`）。 |
| `session.brief_delete` | writer | 書き込んだ本人が削除を求めたときに、ブリーフを削除します。 |

**アドレス指定。** `brief_key` が空の場合は `auto/<project>/<branch>` になるため、同じ作業の流れにあるすべてのセッションが同じブリーフを更新します。project と branch のどちらも分からない場合は `session/<session_id>` になります。キーは `^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$` に一致する必要があります。1 ユーザー（またはセッション）に限定されたトークンは、ブリーフを `~<user>~<key>`（または `~<user>~<session>~<key>`）として保存し、`<key>` として参照します。名前空間全体を対象とするトークンは、そのプレフィックス付きでブリーフを一覧表示でき、同じ形式で取得や削除もできます。限定されたトークンは `~` を含むキーを送信できません。

**更新。** `events`（種類は `user`、`assistant`、`edit`、`command`、`error`、`read`、`search`、`tool`、`note`、`summary`。1 回の呼び出しで最大 4,000 件）を指定した保存は、サーバー側で要約されます。`LOOM_SESSION_BRIEF_LLM` が有効（デフォルト）の場合、`LOOM_SESSION_BRIEF_LLM_MODEL` で指定したモデルが、`LOOM_SESSION_BRIEF_WINDOW_CHARS` 文字のウィンドウごとに 1 回ブリーフを更新します。1 回の保存で処理するウィンドウは最大 `LOOM_SESSION_BRIEF_MAX_WINDOWS` 個です。そのモデルのキーがない場合はデプロイの LLM が書き込み、LLM がまったくない場合は決定論的ビルダーが目標、ステータス、ファイル、エラー、参照先を書き込みます（決定事項と行き止まりは含まれません）。レスポンスの `author` は `llm`、`deterministic`、`model`（呼び出したエージェントがセクションを書いた場合）のいずれかです。オペレーターの[抽出ポリシー](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#operator-extraction-policy)がある場合、ビルダーは実行されず、events からクライアントの要約も取得されません。モデル呼び出しが失敗したウィンドウはスキップされ、残りのウィンドウは更新されます。events だけの保存で呼び出せるモデルがない場合（未設定、`use_llm: false`、または最初のウィンドウを更新する前に組織のモデル呼び出しが無効化された場合）、何も書き込まず、`changed: false`、`reason: "not-revised"` を返します。一部のウィンドウを更新した後に組織が無効化された場合は、更新済みのウィンドウは残り、それ以降の更新は行われません。events だけの保存で、すべてのモデル呼び出しが失敗した、未読のスイッチで保留された、または解析できなかった場合は、クライアントが再送できるよう 503 で拒否されます。エージェントの `sections` も一緒に送る保存は、どちらの場合も既存のブリーフにそれらを上書きして書き込み、events からは何も取り込みません。
状態セクションは更新のたびに置き換えられます。決定事項、行き止まり、完了した項目は蓄積されます。すべてのリビジョンは `loom.session_brief_revisions` に保持され、`brief_get` の `revision=N` で 1 つを読み取れます。前回の保存と同じセッションおよび `event_cursor` を繰り返す保存は再試行とみなされ、何も書き込みません。`if_revision` を指定すると、保存は compare-and-set になります（不一致の場合は 409）。

**復元は本人の選択です。** コーディングプラグインは、コンパクション時とセッション終了時に、クライアント側のモデルトークンを使わずにブリーフを保存します。セッション開始時のプライマーは、保存済みのブリーフの名前を示すだけです。ブリーフの読み込みは、本人が `loom-session` スキルまたはダッシュボードの Conversations ページのブリーフセクションから求めたときに行われます。`brief_get(full=true)` は、クライアント自身の最後のコンパクション要約を、ブリーフの下にそのまま追加します。ブリーフが自動で読み込まれることはありません。どの作業の流れを再開するのかは、本人だけが知っているためです。

<a id="example"></a>

## 例

Python SDK の `Session` は、これらの op を 1 つの `(user, session)` スコープに結び付けます。`append` はターンをトランスクリプトに書き込み、`flush_every` ターンごとに `memory.set_from_messages` にも書き込みます。`resume` は、そのスコープに対する `context.pack` にトランスクリプトの末尾を加えたものです。

```python
from loom import Agent

async with Agent("dev", "local", "support-bot", base_url="http://localhost:7777") as bot:
    s = bot.session("thread-42", user_id="u1")
    resumed = await s.resume(query="resume the billing migration")
    await s.append(
        {"role": "user", "content": "We moved invoices to the new ledger."},
        {"role": "assistant", "content": "Noted; I'll update the export job."},
    )
    print(s.transcript_stored)  # False when the namespace does not record
    await s.close()  # flushes the buffered turns to extraction

    await bot.briefs.save(
        project="billing",
        branch="ledger-move",
        sections={
            "goal": "Move invoices to the new ledger",
            "next_steps": ["Update the export job"],
        },
    )
```

`close()` 時の抽出でファクトを見つけるには、実際の LLM プロバイダーが必要です。標準のフェイクプロバイダーでも、ターンは記録されます。

<a id="settings"></a>

## 設定

| 設定                                                                        | 効果                                                                                                                                                                      |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`                                   | `conversation.search` のコサイン類似度の下限（[ナレッジ → 会話](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)）。                                                     |
| `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS`                                    | 検索ヒットや保存済みメッセージの一覧に、ターンのどこまでを表示するか。即時に反映されます（[ナレッジ → 会話](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)）。                        |
| `LOOM_CONVERSATION_TITLER_*`                                                | タイトル生成ワーカーのオン/オフ、実行間隔、対象ウィンドウ、1 回あたりの上限（[ナレッジ → 会話](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)）。                                  |
| `LOOM_RETENTION_CONVERSATIONS_DAYS`, `LOOM_TIERING_CONVERSATIONS_WARM_DAYS` | トランスクリプトに対する、オプトイン方式の TTL とウォームボリュームへの移動（[保持](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#retention)、[ストレージティアリング](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#storage-tiering)）。 |
| `LOOM_SESSION_BRIEF_LLM`, `…_LLM_MODEL`, `…_WINDOW_CHARS`, `…_MAX_WINDOWS`  | ブリーフをどのように要約するか（[コンテキスト](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#context)）。                                                                                              |
| `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`                                         | 呼び出しがないまま、この時間が経過すると、MCP セッションの暗黙の実行を終了します（[MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#mcp)）。                                                           |

<a id="limits"></a>

## 制限事項

- **「追記したのに何も残っていない。」** 名前空間で記録がオフになっています。この場合、追記は `stored: false` を返します。`conversation.set_logging` でオンにしてください。
- **トランスクリプトはマイニングされません。** 記録されたターンが自動的にメモリになることはありません。
- **`conversation.search` はベクトル検索が優先されます。** エンベディングが捉えない正確な文言は、順位が低くなることがあります。`memory.recall` は、抽出が読み取ったターンに対して、字句検索と時間窓での読み取りを行います。
- **ブリーフには上限があります。** レンダリングされたブリーフは約 12,000 文字未満に収まります。各リストセクションにも上限があり（たとえば決定事項は 20 件、行き止まりは 12 件、次のステップは 8 件）、古い項目は削除されていきます。
- **ブリーフはロックではありません。** 同じブランチ上の 2 つのセッションは、同じブリーフを改訂します。これらが交互に混ざってはならない場合は、`if_revision` を渡してください。

<a id="related"></a>

## 関連ドキュメント

- [抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) — トランスクリプトをメモリに変換します。
- [ワーキングメモリとプロファイル](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md) — プラグインも書き込む、「どこまで進めたか」を記録するスクラッチパッドのブロックです。
- [実行](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) — セッションの書き込みと観測が加わる実行台帳です。
- [統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) — `loom_search` の会話セクションです。
- [トレースと再開](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — 長時間の実行を再開します。
- [メモリアーキテクチャ](../architecture/memory-architecture.md) — 会話テーブルとブリーフテーブルを内部から解説します。
