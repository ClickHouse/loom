<!-- i18n: source=docs/reference/mcp.md blob=6a2cb653387481ccd3b42d2036cf7323fc58a763 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md), which is authoritative.

<a id="mcp-reference"></a>

# MCP リファレンス

Loom は、その操作を MCP ツールとして `<loom>/mcp` で提供します。streamable HTTP に対応した MCP クライアントであれば、URL とベアラートークンで接続できます。ツール一覧は [HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md) と同じ操作レジストリから生成されます。ツール呼び出しは、`POST /v1/<op>` と同じ認可、レート制限、監査、冪等性の経路で実行されます。トランスポートは `server/transport/mcp.py` で、ツール呼び出しは `mcp_call.py` と `mcp_outcome.py`、ベアラートークンの検証は `mcp_auth.py`、一覧表示は `mcp_tools.py`、スキーマの整形は `mcp_schemas.py`、プロトコルとセッションの定数は `mcp_config.py`、厳選された説明文は `mcp_descriptions.py` にあります。

特定のクライアント（Claude Code、Codex、Cursor、VS Code、Claude Desktop など）を `loom install` または手動で設定する方法は、[MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)を参照してください。

<a id="endpoint"></a>

## エンドポイント

| リクエスト                       | 動作                                                                                                                                                                |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /mcp`                      | JSON-RPC リクエスト 1 件、または 1 〜 64 件のバッチを受け付けます。応答はイベントストリームではなく JSON ボディです。通知には本文なしの `202` を返します。                |
| `DELETE /mcp` + `Mcp-Session-Id` | セッションを終了し、そのセッションの暗黙の run を閉じます。`run.complete` として、組織の予算の範囲内で呼び出し元ごとにレート制限されます。上限を超えると `Retry-After` 付きの `429` を返します。 |

URL のクエリパラメータで、その接続で `tools/list` が表示する内容を選択します。

- `?toolset=` は、一覧に表示するツールの構成を選びます（[ツールセット](#toolsets)）。デフォルトは `LOOM_MCP_TOOLSET`（`core`）です。
- `?schema_profile=` は、一覧の形式を選びます。`standard`（整えられた説明と入力スキーマ。デフォルトで、`LOOM_MCP_SCHEMA_PROFILE`）、`full`（`outputSchema` を追加）、`compact`（最初の段落と引数ツリー）があります。

どちらも未知の値を指定した場合はデプロイメントのデフォルトにフォールバックするため、タイプミスによって一覧が気付かないうちに広がったり狭まったりすることはありません。`?toolset=` の値は、セッションのプライマー（`initialize` の instructions、`server/discover`、`loom` プロンプト、`loom://primer`、`loom_activate`）の内容にも影響します。プライマーは、ツールセットに op 専用のツールがあればそのツール名を示し、なければ「(via loom_op)」と付けた op 名を示します。`loom_op` がそこへルーティングしない op は省略されます。

対応するメソッドは `initialize`、`notifications/initialized`、`notifications/cancelled`、`ping`、`tools/list`、`tools/call`、`prompts/list`、`prompts/get`（`loom` プロンプト：セッションのプライマー）、`resources/list`、`resources/read`（`loom://primer`）、`server/discover` です。一覧の結果には `ttlMs` と `cacheScope` のヒントが含まれ、クライアントはこれをもとにキャッシュできます。

**プロトコルバージョン。** `2025-11-25`、`2025-06-18`、`2025-03-26`、`2024-11-05` に対応しています（`MCP_SUPPORTED_PROTOCOL_VERSIONS`）。`initialize` は、クライアントのバージョンがこれらのいずれかであればそのバージョンで応答し、それ以外の場合は `2025-11-25` で応答します。`MCP-Protocol-Version` ヘッダーが他のリビジョンを指定している場合は HTTP 400 と JSON-RPC エラー `-32022` を返し、`data.supported` に対応バージョンの一覧が入ります。`server/discover` は、同じバージョン一覧に加えて、capabilities、サーバー識別情報、instructions を返します。

<a id="authentication"></a>

## 認証

`LOOM_MCP_AUTH_MODE` が、どのメソッドにベアラーが必要かを決めます。

| モード                | ベアラーが必要なメソッド                                      |
| -------------------- | ------------------------------------------------------------- |
| `required`（デフォルト） | 通知を除くすべてのメソッド                                    |
| `discovery_only`     | `tools/call` のみ。それ以外のメソッドはすべて公開              |
| `open`               | なし。無効なベアラーは無視されます（信頼できるネットワーク専用） |

ループバック以外の `LOOM_BIND_HOST` 上のサーバーは、`LOOM_ALLOW_INSECURE=1` でない限り `open` では起動を拒否します。認証をオフにしたデプロイ（`LOOM_AUTH_REQUIRED=false`）は `open` と同じ動作になります。ゲートされたメソッドでベアラーがない、または無効な場合は、HTTP `401` と `WWW-Authenticate: Bearer realm="loom"` が返ります（トークンが送信されていた場合は `error="invalid_token"` も付きます）。失効リストが未読み込みのためにトークンをまだ検証できない場合は、代わりに `Retry-After` 付きの HTTP `503` が返り、クライアントはトークンを保持したまま再試行します。**JSON-RPC エラーではなく 401 である理由：** クライアントは「認証が必要」というフローや OAuth ディスカバリを HTTP ステータスで判定します。JSON-RPC のエラー本文では、パースエラーやツールエラーとして扱われてしまいます。トークンは通常の Loom JWT です。詳しくは[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)を参照してください。

ベアラーは `Authorization` ヘッダーからのみ読み取られます。`/mcp` の URL に `?token=` として渡されたトークンは無視され、リクエストは未認証として扱われます。リクエストラインはプロキシやアクセスログに残るため、URL 内のトークンは平文で記録されてしまうからです。

<a id="oauth"></a>

### OAuth

`LOOM_OAUTH_ENABLED=true` の場合にのみ提供されます。Loom は、トークンを貼り付ける代わりにブラウザーのサインインで接続するクライアント向けの OAuth 2.1 認可サーバーです（`server/auth/oauth.py`）。

- ディスカバリは `/.well-known/oauth-authorization-server` と `/.well-known/oauth-protected-resource` にあります。
- 動的クライアント登録は `/oauth/register` で行います。パブリッククライアントが対象で、PKCE `S256` が必須です。
- `/oauth/authorize` と `/oauth/token` は、アクセストークンとローテーションされるリフレッシュトークンを発行します。リフレッシュが成功するのは、リクエストがトークンの発行先の `client_id` を指定している場合のみです。
- アクセストークンはサインインしたユーザー向けの Loom JWT なので、ユーザーが後から参加した名前空間も同じ接続で利用できます。
- サインインには Loom のパスワードログインを使います。`LOOM_OAUTH_LOGIN_PROVIDER=oidc` を設定すると、外部のアイデンティティプロバイダーに委任されます（`LOOM_OIDC_ISSUER`、`LOOM_OIDC_CLIENT_ID`、`LOOM_OIDC_CLIENT_SECRET`）。サインインを委任すると、パスワードによるセルフサインアップも無効になります。
- リモートのデプロイでは HTTPS で提供し、`LOOM_OAUTH_ISSUER` を公開用の `https://` ベース URL に設定する必要があります。
- 登録するリダイレクト URI は絶対 URI で、フラグメントを含んではいけません。使えるのは `https`、ループバックホストへの `http`、またはアプリスキーム（`cursor://…`）です。サインインページには、認可の送信先ホストが表示されます。
- リフレッシュのたびに、アカウントが引き続き有効かを再確認し、ロールを再導出します。そのため、解放されたアカウントや `LOOM_PLATFORM_ADMINS` から削除されたサブジェクトは、リフレッシュしてもアクセス権を維持できません。

この背後にあるトークンモデルについては、[セキュリティ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)を参照してください。

<a id="sessions-and-identity"></a>

## セッションと ID

- **セッション ID。** `initialize` は `Mcp-Session-Id` を割り当て、すべてのレスポンスがそれを返します。クライアントの `clientInfo.name` はこの ID に対してキャッシュされ、スラッグ化されて以降の呼び出しの `agent` になります（`Cursor` → `cursor`、未指定の場合は `claude-desktop`）。キャッシュはプロセス内に置かれ、サイズに上限があります。
- **ID のデフォルト値。** ツール呼び出しには通常、アクションの引数だけが含まれます。サーバーは `org` をトークンから、`namespace` と `agent` を呼び出し元のデフォルトワークスペースから補完します。明示的に指定した引数が優先されます。`code.*` の呼び出しは代わりにコードプレーンに解決され、`assistant` ツールセットでは、`namespace` または `agent` を指定しない読み取りはそのツールセットのルールに従います（[assistant ツールセット](#the-assistant-toolset)を参照）。トークンが操作を許可されていない `org` 引数は拒否されます（`ORG_MISMATCH`）。トークンは自身の org 内で操作でき、org 全体（`*`）のトークンまたはプラットフォーム管理者は任意の org で操作できます。これは HTTP 経由の場合と同じルールです（`authz.may_act_in_org`）。制限付きの名前空間は、プラットフォーム管理者を含め、他のすべての org を引き続き拒否します。`loom_search` は `namespace` 引数を取りません。呼び出し元のホーム名前空間と、メモリ種別が同じ他の名前空間を、ファンアウトの上限まで検索します。
- **コードスコープ。** クライアントは `X-Loom-Code-Scope: <org>/<namespace>` を送信できます（Claude Code プラグインは、チェックアウトが解決されたリポジトリを送信します）。これは名前空間を指定しないすべての `code.*` 呼び出しに適用され、認可はその名前空間が指定されたものとして実行されます。このヘッダーがある場合、`loom_search` は、呼び出し元に読み取り権限があれば、リポジトリのドキュメントと `decision` メモリもそのトランク用コード名前空間から読み取り、不具合の形をしたクエリに対してはトラッカーの重複チェックを実行します。
- **暗黙の run。** run は (org、トークンのサブジェクト、セッション) ごとに 1 つです。セッションで最初に run スコープのツール呼び出しが行われた時点で run 台帳に開始され、呼び出し元のデフォルト名前空間に記録されます。書き込みにはその `run_id` が付与されます（ただし、自身の run を指定する `run.*`、デプロイメントのコード ID として動作する `code.*`、`run_id` が行のスコープ指定または紐付けに使われる `working_memory.*` と `conversation.update` は除きます）。読み取りにはその ID がラベルとして付きますが、絞り込みは行われません。run は `DELETE /mcp` で終了するほか、`mcp_session_run_sweep` ワーカーが、その run に属するトークンの呼び出しを `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`（デフォルト 1800）の間確認できなかった場合にも終了します。バックグラウンドワーカーをどこでも実行していないデプロイメントでは、アイドル状態の run は開いたままになります。run は呼び出し回数を保持しません。その呼び出しは、`run_id` が一致する `agent_events` の行です。[実行（Runs）](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)を参照してください。

<a id="tool-list"></a>

## ツール一覧

`_MCP_HIDDEN_OPS` に含まれないレジストリのすべてのオペレーションがツールになります。

- **名前。** `loom_` に、ドットをアンダースコアに置き換えた op 名を続けたものです（`memory.set` → `loom_memory_set`）。
- **説明。** `TOOL_DESCRIPTIONS`（`server/transport/mcp_descriptions.py`）に厳選された文があればそれを使い、なければ op のレイテンシ予算、冪等性、最小ロールを含む 1 行のフォールバックを使います。厳選された文には、そのツールをいつ使うか、いつ使わないかが書かれており、選択はモデルに委ねられます。文中では他の op を op 名で参照し、各一覧はそれを読むクライアント向けに描画します（`server/transport/mcp_op.py` の `name_ops_for_listing`）。一覧に含まれる op はそのツール名（`loom_memory_update`）で参照されます。一覧に含まれない op は、`loom_op` が受け取る op 名のままとなり、その接続で `loom_op` がその op をルーティングする場合は、最初の言及の後ろに「(via loom_op)」が付きます。非表示の op、`readonly` での書き込み、`assistant` でのあらゆる op はルーティングされません。入力フィールドの説明、`full` プロファイルが保持する出力スキーマ、非表示 op の `instead` テキストも同じ方法で描画されます。op 名について書かれた `loom_op` 自身のテキストは、代わりに一覧ごとに組み立てられます。メンターテキストは、MCP 経由でも呼び出し元の一覧向けに描画されます（ツール結果の `hint`、`context.pack` の `mentor` セクション、`mentor.brief` のヒントとプレーンの `when`）。HTTP 経由では保存されたとおりに読み取られます。
- **入力スキーマ。** op の Pydantic 入力モデルから、サーバーが自分で埋めるフィールド（`org`、`agent`、`user_id`、`session_id`、`consumer`）、ダッシュボード専用フィールド（`server/transport/mcp_schemas.py` の `_HIDDEN_OP_PROPS`）、`MCP_HIDDEN` が付いたすべてのフィールドを除いたものです。`namespace` は任意のプロパティとして残ります。非表示フィールドも名前を指定すれば受け付けられます。
- **アノテーション。** `readOnlyHint`、`destructiveHint`、`idempotentHint` はレジストリ（ロール、冪等性、監査、名前に含まれる破壊的な動詞）から導出されます。`readOnlyHint` が true になるのは、冪等な viewer の op で、監査対象でも `writes` が付けられてもいないものだけです。そのため、読み取り専用ツールを自動承認するクライアントが、状態変更を確認なしに実行することはありません。これらはヒントであり、境界となるのは呼び出し時のロールチェックです。

`assistant` を除くすべてのツールセットで、3 つの合成ツールが一覧に表示されます。

| ツール          | 動作                                                                                                                                                                                                                 |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom_search`   | 1 回の呼び出しで、メモリ、プロファイル、ナレッジ、会話を並列に検索し、4 つのセクションすべてを返します。`explain: true` を指定すると、各メモリヒットがなぜその順位になったかが追加されます。                         |
| `loom_activate` | セッションのプライマー（プロファイル、設定、デフォルトのワークスペース）を返します。内容は `loom` プロンプトと同じテキストです。                                                                                     |
| `loom_op`       | `{op, arguments}` で、ロスターに載っていない任意の op を、一覧に載っているツールと同じ経路で実行します。`{op: "help", query}` は、一致する op を最大 15 件（`_CATALOG_LIMIT`）、必須引数と任意引数とともに一覧表示します。 |

`loom_search` はレジストリの `search.query` オペレーションです（HTTP では `POST /v1/search.query`。テキストサマリーを除いた同じセクションを返します）。MCP 経由では、他のすべてのレジストリツールと同じ経路（呼び出し元のデフォルト、セッション実行、認可、制限付きアクセスの監査、トラフィッククラス、履歴ポリシー、レート制限、冪等性）を通り、独自の一覧用スキーマ、寛容なオプション（範囲外の `top_k` や未知の `recipe` / `bodies` は拒否されず補正されます）、そして簡潔な結果と並ぶテキストサマリーを備えます。`Operation.mcp_tool` がこの名前を指定するため、2 つ目の `loom_search_query` ツールが一覧に載ることも、呼び出し可能になることもありません。詳しくは[統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#searchquery-over-http-and-the-sdks)を参照してください。

**非表示 op**（`server/operations/toolsets.py` の `_MCP_HIDDEN_OPS`）は、MCP では一覧に載らず、呼び出すこともできません。対象は、`loom_search` が置き換えるストア別の検索（`memory.search`、`knowledge.search`、`conversation.search`）、レビューキュー、スナップショットとリストア、ポリシー管理、監査・コストのダッシュボード、コードインデックスの取り込みです。これらを呼び出すと、コード `OP_NOT_ON_MCP` と代わりに使うべきものを添えた `isError` が返ります。HTTP と SDK からは引き続き利用できます。`assistant` ツールセットでは `memory.search` と `knowledge.search` が一覧に載り呼び出し可能で、他の非表示 op を呼び出すと `UNKNOWN_OP` が返ります。op を隠す基準は、一般的なエンドユーザー（オペレーターではなく）がチャットで使おうとするかどうかです。名前空間全体のビューは隠しつつ、呼び出し元に限定した派生版を MCP に残す場合があります。`namespace.list` は非表示で、`namespace.list_mine` は呼び出し可能です（`all` と `readonly` では一覧に載り、`assistant` を除く他のすべてのツールセットでは `loom_op` 経由で利用できます）。`memory.extraction_status`（名前空間内でキューに入っているすべてのバッチ）は非表示で、`memory.extraction_status_mine`（呼び出し元がキューに入れたバッチを `queue_id` で指定、1 回の呼び出しで最大 20 件）は、`assistant` を除くすべてのツールセット（`readonly` を含む）で、名前指定または `loom_op` 経由で呼び出せます。その書き込み側の対となる `memory.extraction_retry_mine` は、呼び出し元の失敗したバッチをもう一度再実行します。`loom_op` は、`readonly` と `assistant` を除くすべてのツールセットでこれをルーティングします。

<a id="toolsets"></a>

### ツールセット

ツールセットが決めるのは何が**一覧に載るか**です。非表示でないすべての op は、ツールセットに関係なく、ツール名または `loom_op` 経由で呼び出せます。例外は 2 つあります。`assistant` では自身の読み取り op だけが呼び出し可能です。また `readonly` を `?toolset=` で指定した場合、またはリクエストが指定しないときに `LOOM_MCP_TOOLSET` で指定した場合、`loom_op` は読み取り op だけをルーティングします。

| ツールセット | 一覧に載るもの                                                                                                                                                                                                                                                                                                                                                                                                                       | 用途                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------- |
| `core`       | メモリのループ：`memory.set/update/delete/pin/get/history/apply/lock`、`blacklist.add`、`context.pack`。他のすべての op は `loom_op` 経由で実行されます。`core` に載るツールに、会話ターンを記録するものはありません。コードツールは `LOOM_MCP_CODE_TOOLS=true` で追加されます。                                                                                                                                                     | チャットクライアント（デフォルト）             |
| `coding`     | `memory.set/update/get/delete`、`working_memory.set`、`context.pack`、コードツール（`code.search`、`grep`、`enclosing`、`ask`、`symbol`、`outline`、`callers`、`context_pack`）、`tracker.duplicate_check`、`code.bisect_candidates`、`code.pr_check`、`code.impact`。                                                                                                                                                                | コーディングエージェント                       |
| `tracker`    | `tracker.answer`、`binding`、`component`、`duplicate_check`、`flaky`、`item`、`links`、`release`、`similar`、`test_signal`、加えて `code.bisect_candidates`、`code.pr_check`、`code.impact`。                                                                                                                                                                                                                                          | 失敗したテスト、クラッシュ、PR を扱うセッション |
| `tasks`      | `task.pull/create/complete/fail/wont_do/defer/heartbeat/updates/list/get`、`inbox.list`。                                                                                                                                                                                                                                                                                                                                            | タスクインボックスを持つエージェントとして動くセッション |
| `atomic`     | `memory.set/get/delete/ask`、`working_memory.set/append`、`run.begin/complete`。                                                                                                                                                                                                                                                                                                                                                     | 最小構成の、ポリシーで形作られたエージェント   |
| `readonly`   | `all` の一覧から、`readOnlyHint` が false のツールをすべて除いたもの。`loom_op` は読み取り op だけをルーティングします。                                                                                                                                                                                                                                                                                                             | 読み取り専用の登録                             |
| `assistant`  | ユーザーに代わって Loom を調査するための固定の読み取りセット：`namespace.get/summary/inventory/tune/taxonomy/audit/validate/guide_get/guide_history/list_categories`、`mentor.brief/guide`、`help.search/get`、`grants.list`、`memory.inspect/search/ask/get/list/history/provenance/aggregate`、`facts.traverse/path`、`knowledge.search/get/list/inspect`、`code.search/symbol`、`insights.catalog/query`。合成ツールはありません。 | サインイン済みのユーザーのために動くチャットクライアント |
| `all`        | `maturity` が `stable` である非表示でないすべての op に、core または atomic のロスターが載せている `experimental` の op を加えたもの。どちらのロスターにも載っていない experimental の op は一覧から外れますが、名前指定または `loom_op` 経由では呼び出せます。コードツールは `LOOM_MCP_CODE_TOOLS=true` のときだけ含まれます。                                                                                                  | オペレーター                                   |

厳選されたロスターは組み合わせられます。`?toolset=coding+tracker`、`coding,tracker`、またはスペースとして届くエスケープされていない `+` を使います。組み合わせられるのは厳選されたロスターだけで（`all`、`readonly`、`assistant` は全体を表すため不可）、未知の部分が 1 つでもあると値全体が未知として扱われ、一覧はデプロイメントのデフォルトに戻ります。ロスターは `server/operations/toolsets.py` の frozenset です。`ops.capabilities` は各 op の `toolsets` を報告します。別のツールセットでの 2 つ目の MCP 登録（`?toolset=tracker`、`?toolset=tasks`）は、最初の登録を変えずにロスターを追加します。

**一覧が小さい理由。** 一覧に載るすべてのツールの名前、説明、スキーマは、すべてのセッションのすべてのターンでトークンとして消費され、一覧が長くなるほどツール選択の精度は下がります。`tests/unit/test_mcp_listing_budget.py` は、各ロスターをトークン上限以下に保ちます。op は一覧に載せるより、名前指定で呼び出せる状態にしておくほうを選んでください。一覧に載せる場合とその方法については、[オペレーションの追加](../development/adding-an-operation.md)を参照してください。

<a id="results"></a>

## 結果

`structuredContent`（および `content[0].text` 内の JSON）は op の出力で、回答に必要な部分だけに絞られています（`server/transport/lean_results.py`）。ヒットにはキー、内容、名前空間、日付、類似度、ソース、ステータスが含まれ、そのほかに件数、ページング、abstain 関連のフィールドが含まれます。スコアの内訳、説明、検索プランとコスト、ハッシュ、空のフィールドは除かれます。**理由：** 一部のクライアントは `structuredContent` をテキストブロックに加えてモデルにも渡します。診断情報を含む完全な形式では、短い回答でも数千トークンかかります。呼び出しに `explain: true` を指定すると完全な形式が返ります。HTTP、SDK、`assistant` ツールセットは常に完全な結果を受け取ります（`assistant` では、`memory.search` からヒットごとのスコア内訳を除いたものです）。

<a id="the-assistant-toolset"></a>

### assistant ツールセット

`?toolset=assistant` は、チャットクライアントがサインイン中のユーザーに代わって Loom を調べるための接続です。名前空間に何が入っているか、どう使われているか、そしてメモリ、ドキュメント、コードに対する引用付きの回答を扱います。値として指定できるのは `assistant` のみです（大文字小文字は問いません）。綴りを誤った場合や `assistant+core` のような組み合わせは解決されず、エラーなしでデプロイメントのデフォルトの一覧が返されます。保証を担うのはクライアントではなく、オペレーターが設定した URL と `viewer` トークンです。1 リリース古いレプリカもこの値を知らないため、デフォルトの一覧を返します。そのためローリングアップグレード中に接続を制限するのはトークンだけです。クライアントを `?toolset=assistant` に向けるのは、すべてのレプリカがこれに対応したビルドで動作してからにし、いずれにしても `viewer` トークンを渡してください。この接続では次のとおりです。

- **呼び出せるのは読み取りだけです。** それ以外のツール（`loom_op` と `loom_search` を含む）への `tools/call` は、ハンドラーが動く前に `isError` で応答します。トークンが許可しているかどうかは関係ありません。コードは `UNKNOWN_OP` です。ただし、トークンが読み取れない組織や名前空間を呼び出しが指定している場合は、アクセス拒否が返ります。こちらが先に確認されます。トークンは呼び出しごとに引き続き確認されるため、ユーザーに許されていない読み取りは、ほかの接続と同様に拒否されます。クライアントのトークンを `viewer` ロールで発行すると、同じ制限がほかのすべてのサーフェスでも有効になります。
- **呼び出しはコンシューマー `loom-assistant` として台帳に記録されます。** 呼び出しが何を送っても同じです。レジストリのツールへの拒否された呼び出しも含まれます（不明なツール名では行が書かれません）。`namespace.audit`、`namespace.summary`、`mentor.brief`、`knowledge.reads` は、このコンシューマーとダッシュボード（`loom-ui`）の読み取りを使用回数の集計から除外します。そのため、名前空間を見ても評価は変わりません（ダッシュボードからのメモリの変更は、ユーザーが名前空間を使ったものとして、`namespace.audit` と `namespace.summary` に引き続き数えられます）。`namespace.audit` は、除外した件数を `summary.inspector_calls` で報告します。`consumer` クレームがほかのコンシューマーを指すトークンは、そのクレームが保たれ、その呼び出しは集計されます。
- **名前空間を指定しない読み取りは、トークンの名前空間を読みます。** デフォルトは、トークンのクレームが指す名前空間のうち、読み取りが許されている最初のものです。書き込みが許されている最初のものではありません。assistant の回答用トークンは 1 つの名前空間向けに発行され、その対象者の個人名前空間が書き込み可能な名前空間になるためです。クレームがどの名前空間も指さないトークンでは、呼び出し元のデフォルトのワークスペースが保たれます。
- **読み取りは、どの行を読むかを指定します。** assistant は何も書き込まないため、クライアント側のエージェントは行を持ちません。そのため `agent` を指定しない読み取りは、エージェントで絞り込まれません。`memory.ask` はエージェントをまたいで読み、`mentor.brief` は名前空間で最も多く書き込んだ書き手についてブリーフを作ります。`memory.search`、`memory.list`、`memory.history` がすでにエージェントをまたいで読むのと同じです。`insights.query` は、`insights.catalog` が一覧するテーブルに対する SQL で、自由な分析上の問いに答えます。トークンの組織と名前空間の範囲は ClickHouse が保証します（[assistant の設計](https://github.com/ClickHouse/loom/blob/main/docs/architecture/assistant.md#the-analysis-op)）。特定の書き手の行を読む `memory.aggregate` と `memory.provenance` は、呼び出しが書き手を指定するまで、`agent` がないとして拒否されます。書き手は、検索ヒットの `agent`、`user_id`、`session_id` から分かります。一覧では、これらの引数がすべてのメモリ読み取りに用意され、`mentor.brief` には `agent` が用意されています。`memory.get` は、どのエージェントの行であっても取得できます。
- **読み取りは使用状況を記録しません。** `memory.search` は `auto_touch: false` で、`memory.ask` は `reinforce: false` で実行されます。呼び出しが何を送っても同じです。そのため調査によって、結果の順位づけに使われる想起回数が増えることはありません。`memory.ask` は `precedents: false` でも実行されるので、再利用のために回答が保存されることもありません。残るのはテレメトリとキャッシュです。リクエスト台帳の行、メモリを読むたびに残る露出行（`loom.memory_exposures`、どの行を誰に提供したかの記録）、`code.search` や `code.symbol` が書くコード検索トレース（`loom.code_search_trace`、`LOOM_CODE_TRACE_LOG_ENABLED` が有効な場合）、`LOOM_SEARCH_TRACE_LOG_ENABLED` または `LOOM_MEMORY_QUERY_NODES_ENABLED` が有効なときに `memory.search` が書く検索トレースとクエリノードの行、遅延整合性の下での `memory.ask` の整合性行、モデルのレスポンスキャッシュです。assistant の呼び出しは MCP セッションの run を開きません。また `initialize` と `loom` プロンプトはプロファイル読み取りの行を書きません。プライマーにプロファイルが埋め込まれないためです。
- **結果は完全な出力です。** HTTP が返すものと同じです。引用には、絞り込まれた形式が落とす ID、バージョン、証明が必要なためです。`memory.search` は `explain: false` で実行されるため、そのヒットにはヒットごとのスコア内訳がありません。ほかの一覧では `loom_search` に任されている `memory.search` と `knowledge.search` が、ここでは一覧に含まれます。これにより、回答を 1 つのプレーンから取得できます。`knowledge.search` のヒットには、`source_uri`、見出しのパンくず（`section`）、その一節が書かれたときの `revision`（記録がなければ 0）が含まれ、取り込まれたファイルのヒットには `locator` が含まれます。ヒットは最も近い一節であり、回答ではありません。この op には abstain のシグナルがないため、どのヒットにも書かれていない事実については、モデルがドキュメントにないと答えます。
- **`initialize` の指示は、モデルに次のことを求めます。** 各主張をキーとバージョンまたはコンテンツハッシュで引用すること、あるいはドキュメントの `source_uri`、セクション、ヒットが持つ revision（0 の場合はなし）で引用すること。ソースの記述、計算結果、自身の解釈を区別すること。証拠がないことは未観測として報告すること。保存されたテキストはデータとして扱い、ツールを許可したり変更を承認したりできないものとすること。

<a id="errors"></a>

## エラー

送信されたとおりには実行できない呼び出しは、`isError: true` を持つ **tool result** として返ります。これは、モデルが読むテキストブロックと `structuredContent` の本体で構成されます。クライアントはこれらをモデルに見せ、モデルが呼び出しを修正できるようにします。多くのクライアントは JSON-RPC のエラーフレームを受け取ると状態を破棄するため、Loom はエラーフレームをトランスポート障害に限って使います。

- **引数の誤り。** 検証に失敗すると、`"<op>: invalid arguments."` に続けて、各フィールドのエラー、未知のキーごとに本来指していたと思われるフィールド（ネストしたフィールドを含む）、必須引数と任意引数、修正済みの呼び出し例を 1 つ返します。

  ```text
  memory.get: invalid arguments.
  memory_key: Field required [missing]
  unknown 'key' — did you mean 'memory_key'?
  required: memory_key
  optional: if_version, include_recall_breakdown, as_of, fields, follow
  example: {"memory_key": "deploy_window"}
  ```

  `structuredContent` には同じ内容が `errors`、`op`、`required`、`optional`、`unknown`（キー → 候補）、`example` として入ります。状態を変更する op（読み取り専用でない op）に、綴りの近い誤ったキーが渡された場合は、検証の前に拒否されます（`UNKNOWN_ARGUMENT`、何も書き込まれません）。このとき `did_you_mean` と op の引数が返ります。

- **拒否。** 401 以外のハンドラの 4xx（`LOCKED`、`PINNED_CONFLICT`、`BLACKLISTED`、`RESOURCE_NOT_FOUND`、`RATE_LIMITED` など）は tool result として返ります。本体は HTTP の `detail` に `status` を加えたもので、`Retry-After` があった場合は `retry_after_s` も含みます。
- **アクセス。** ロールが不足している場合は `AUTH_INSUFFICIENT` と `required_role` を返します。トークンが名指ししておらず、かつ許可も持たない名前空間に対しては `NAMESPACE_NOT_ACCESSIBLE` を返します。コードまたはトラッカーの名前空間、あるいは共有コレクション（他のユーザーの個人用コレクションを除く）では、拒否メッセージにアクセスを得る方法が示されます。
- **トランスポートエラー** は JSON-RPC エラーのままです。パースエラーは `-32700`、不正なリクエストは `-32600`（認証の拒否には HTTP ステータスが付きます。形式の誤ったリクエストや、1〜64 の範囲外のバッチには 200 を返します）、メソッドが見つからない場合は `-32601`、未対応のプロトコルバージョンは `-32022`、サーバー障害は `-32000 "tool execution failed"` です。Loom が名前を付けている障害（`OVERLOADED`、`BACKEND_TIMEOUT`、`SCHEMA_PENDING`、`EMBED_TIMEOUT`）では、そのエラーにコードとメッセージが示され、`error.data` は HTTP の `detail` に `status`、`tool`、`request_id` を加えたものになります（待機時間を示すエラーでは `retry_after_s` も付きます）。それ以外の障害では `internal error` とリクエスト ID のみが示されます。

`code` を持つすべての拒否本体と、すべての障害の `error.data` には、HTTP エンベロープと同様に `category` と `retryable` も含まれます。テキストしかモデルに見せないホストのために、ツールの拒否テキストの末尾には同じ分類が言葉で付きます（`(auth: do not retry unchanged)`）。`loom_op` 自身の拒否では、これらのフィールドが JSON テキストに含まれます。コレクションではない名前空間に対する `NAMESPACE_NOT_ACCESSIBLE` の拒否が、コードまたはトラッカーのツールではないツールから、`loom_op` に到達できるツールセットで発生した場合は、`next` ステップも付きます（[エラーリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md#the-error-envelope-over-http)）。すべてのコードと、その原因および対処は[エラーリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)にあります。

<a id="security"></a>

## セキュリティ

`tools/call` は HTTP トランスポートのゲート（`server/transport/gates.py`）を実行します。

- **認可**：呼び出しごとに、op の `required_role` と名前空間のアクセスルールを確認します。
- **レート制限**：HTTP と同じバケットを使用します。MCP クライアントは再試行しないため、待機ヒントが `LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S`（デフォルトは 2 秒）以下で制限された呼び出しは、その時間だけ一度スリープし、同じバケットを再度確認します。ヒントがそれより長い場合、または 2 回目も拒否された場合は、`retry_after_s` を付けて `RATE_LIMITED` を返します（`LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S=0` の場合は待機しません）。
- **監査**：`audit=True` と宣言された op は、同じ `loom.audit_log` の行を書き込みます。
- **冪等性**：MCP には冪等性ヘッダーがないため、冪等な op は引数に `_idempotency_key`（または `idempotency_key`）を取ります。このキーは検証の前に取り除かれ、HTTP の `Idempotency-Key` とまったく同じようにリプレイされます。リプレイは認可の後にのみ行われ、キーを保存したプリンシパルに対してのみ行われます。

<a id="exporting-the-action-schema"></a>

## アクションスキーマのエクスポート

ポリシーゲートウェイは、レジストリから生成したスキーマ（`server/transport/action_schema.py`）を使って、Loom のツールを制御できます。

```bash
loom export-actions --format json  -o loom-tools.json    # tools/list manifest + a `loom` block per tool
loom export-actions --format cedar -o loom.cedarschema   # Cedar: one action per operation
```

JSON マニフェストは、すべての op について MCP の `tools/list` 形式（`name`、`description`、`inputSchema`、`outputSchema`）を持ち、さらにポリシーエンジンが必要とするものの MCP にはフィールドがない情報を収めた `loom` ブロックを含みます。内容は、op 名、HTTP パス、必要なロール、効果クラス（`read`、`write`、`destructive`。アノテーションと同じ分類器によるもの）、冪等性、監査フラグ、MCP での可視性、入力モデル名と出力モデル名です。Cedar スキーマでは、名前空間 `Loom` に、プリンシパル `Agent` とリソース `Namespace`、入力モデルと出力モデルごとに 1 つのレコード型、アクショングループ `Read`、`Write`、`Destructive`（`Destructive` は `Write` に含まれます）、そして各操作に対応するアクションをそれぞれのグループ内に宣言します。`-o -` を指定すると標準出力に書き出します。

<a id="related"></a>

## 関連情報

- [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)：Claude Code、Codex、Cursor などのクライアントを接続する方法。
- [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) と [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md)：サーバーを自動でマウントするプラグイン。
- [オペレーションリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)：すべての op とそのロール、種類、`all` で一覧表示されるかどうか。
- [エラーリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)。
