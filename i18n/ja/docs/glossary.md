<!-- i18n: source=docs/glossary.md blob=8dd4bdacf4b7cf0e994f5fcf1af3353f19f26540 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md), which is authoritative.

<a id="glossary"></a>

# 用語集

Loom の用語をアルファベット順に並べ、それぞれ一度だけ定義します。コード上の識別子はバッククォートで示します。

**Abstain（棄権）** — 最良のヒットが、エンベッダー向けに調整された abstain ゲートを下回る場合に、`memory.search` が最も近い弱い一致を返す代わりに、理由コードとともに `abstain=true` を返すこと。「何も保存されていない」場合と「無関係なものが 0.3 のスコアを得た」場合を区別します。[検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)を参照してください。

**Actor（アクター）** — 行を書き込んだ主体を示す、サーバーが付与するカラム。`human:<subject>` または `process:<subject>` の形式で、種別はトークンから取得されます。リクエストボディからは読み取られません。[アイデンティティと名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)を参照してください。

**Agent（エージェント）** — アイデンティティタプルの 3 番目の要素で、読み書きを行うプログラムの名前（`claude-code`、`support-bot`）です。作成者を記録するものであり、読み取りの境界ではありません。名前空間のすべてのメンバーが、すべてのエージェントの行を読み取れます。

**Attestation（証明）** — 行が検証済みであることを記録する `verified_*` カラム。編集してもこれらは残り、古い状態としてマークされます。読み手は、証明されたコンテンツハッシュを比較して信頼するかどうかを判断します。[履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)を参照してください。

**Audit log（監査ログ）** — `loom.audit_log`：監査対象の操作ごとに、コンテンツをハッシュ化した行が 1 つ記録され、ハッシュで連結された 1 時間ごとのチェックポイントにまとめられます。行にはコンテンツではなくハッシュが含まれます。[監査](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md)を参照してください。

**Belief revision（信念改訂）** — メモリのライフサイクルを捉える設計上の視点：Expand（`memory.set`）、Revise（`memory.update_by_id`）、Contract（`memory.delete`）、Reinforce（`memory.pin`）、Refuse（`blacklist.add`）。[書き込みの判定方法](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#how-a-write-is-judged)を参照してください。

**Blacklist（ブラックリスト）** — 一致する書き込みを拒否する、登録済みの部分文字列または正規表現（`422 BLACKLISTED`）。`sweep=true` を指定すると、既存の一致もトゥームストーンにします。

**Chunk（チャンク）** — ナレッジドキュメントまたはソースファイルの一部分で、個別に保存・埋め込みされます。これにより、検索でドキュメント全体ではなく該当する箇所を返せます。

**Code link（コードリンク）** — メモリが名指しする定義やファイルへのリンクで、リネームや編集を追跡します。状態は `fresh`、`moved`、`changed`、`unresolved`、`missing` のいずれかです。まだトランクにないコード名は、最大 `memory_code_links_pending_days`（14 日）待機します。[コードにリンクされたメモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md#memories-linked-to-code)を参照してください。

**Code namespace（コード名前空間）** — インデックス化された 1 つの git リポジトリ（そのチャンク、シンボル、エッジ、コミット、PR 履歴）を保持する、種別 `code` の名前空間。[コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

**Commit coverage（コミットカバレッジ）** — すべてのインデックスティアが特定のコミットを処理済みかどうか。`code.index_status(commit=...)` が答え、`code.coverage.watch` が真になったときに通知します。[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)を参照してください。

**Compiler tier（コンパイラティア）** — パーサーティアではなく、コンパイラ級のインデクサー（SCIP）から得られるコードインデックスのファクト。型情報によって解決された呼び出しエッジや参照です。

**Conflict（コンフリクト）** — 既存のメモリと意味的に近い書き込み（コサイン類似度 ≥ `LOOM_CONFLICT_THRESHOLD`）。両方の行が `conflicted` とマークされて相互にリンクされ、人またはエージェントが解決します。[コンフリクトと拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)を参照してください。

**Consolidation（統合）** — 同じ主題について断片化したメモリをマージし、系譜（`memory_belief_sources`、`support_type='derived_from'`）を記録するバックグラウンド処理。

**Content hash（コンテンツハッシュ）** — メモリの値の `content_sha256`。完全一致する重複を排除し、`if_match_sha256` 前提条件を支え、監査行のアンカーになります。

**Context pack（コンテキストパック）** — `context.pack`：ワーキングメモリ、プロファイル、関連するメモリ、手順、決定をまとめた上限付きのバンドルで、セッション開始時に一度だけ読み込まれます。[統合検索](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#contextpack)を参照してください。

**Dashboard（ダッシュボード）** — サーバーのルートにある、人向けの Web アプリケーション。メモリ、アクティビティ、ソース、レビューキュー、タスク、リポジトリ、設定を扱います。[ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)を参照してください。

**Decision（決定）** — 選択を記録する第一級のレコード（`decision.record`）。理由、却下された代替案、のちに結果（`decision.outcome`）を含みます。[タスクと決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)を参照してください。

**Entity edge（エンティティエッジ）** — `loom.entity_edges` にある、2 つのエンティティ間の重み付き共起リンクで、有効期間を持ちます。`facts.traverse` がたどるグラフです。[エンティティグラフ](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)を参照してください。

**Erasure saga（消去サガ）** — `erasure.request` が実行する、再開可能でフェイルクローズドな一連のステップ。対象者のデータをすべてのテーブル（および設定された外部バックエンド）から削除し、残存データの集計で終わります。[保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)を参照してください。

**Extraction（抽出）** — 会話メッセージを、LLM と品質フィルターを使って候補メモリに変換すること（`memory.set_from_messages`）。[抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)を参照してください。

**Extractor stamp（エクストラクタースタンプ）** — コードインデックスのエクストラクターモジュールのコードトークンのハッシュ。これが変わると、インデックス化済みのすべてのリポジトリが再チャンク化されます。コメントとドックストリングは対象外です。[コードインデックスのアーキテクチャ](architecture/code-index-architecture.md)を参照してください。

**FINAL** — `ReplacingMergeTree` テーブルを、読み取り時にキーごとの最新バージョンへ畳み込む ClickHouse の修飾子。Loom は `loom.memory` をこれで読み取ります。[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md)を参照してください。

**Fence（フェンス）** — `task.claim` がリースとともに返すトークン。以降の `task.progress`、`task.heartbeat`、`task.complete`、`task.fail` の呼び出しはこれを提示する必要があり、リースが失効した要求者は書き込めません。

**Grant（グラント）** — サブジェクトに名前空間上のロールを与える行（`grants.*`）。[トークンとグラント](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)を参照してください。

**Identity tuple（アイデンティティタプル）** — すべての行にある `(org, namespace, agent, user_id, session_id)`。最初の 3 つは必須で、後の 2 つのデフォルトは `'-'` です。[アイデンティティと名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)を参照してください。

**`ignored_args`** — サーバーが使用しなかったリクエストフィールド（未知のものや廃止されたもの）を示すレスポンスフィールド。これにより、クライアントは失敗せずにずれを把握できます。

**Inbox（インボックス）** — 名前空間が所有する、名前付きのタスクキュー（`task.*`、`inbox.*`）。[タスクと決定](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)を参照してください。

**Knowledge document（ナレッジドキュメント）** — `knowledge.add` で追加され、検索用にチャンク化・埋め込みされるドキュメント。メモリとは異なり、信念ではなく参照資料です。[ナレッジドキュメント](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)を参照してください。

**Learning（ラーニング）** — 人の判断を待って `loom.learnings_queue` に置かれる候補（`learnings.list`、`learnings.review`）。複数の書き込み元が裏付けるファクトは共有レイヤーへ、会話から生まれたファクトは名前空間のメモリ行へ、ボットが自身について述べたルールはその名前空間のナレッジへ、それぞれ昇格されます。

**`log_comment`** — Loom が呼び出し元の op、ワーカー、または CLI コマンドを設定する ClickHouse のクエリ設定。これにより `system.query_log` がすべてのステートメントの発信元を特定できます。

**`maturity`** — op が宣言する `stable`/`experimental` フィールド。コアのツールセットにもアトミックのツールセットにも含まれない experimental の op は、`all` の一覧から除外されますが、名前を指定すれば呼び出せます。[MCP ツールセット](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets)を参照してください。

**MCP tool（MCP ツール）** — op を Model Context Protocol に投影したもので、`loom_<op>` という名前（ドットはアンダースコアに置換。`loom_memory_set`）です。ほかに `loom_search` や `loom_op` などの合成ツールがあります。[MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)を参照してください。

**Memory（メモリ）** — `loom.memory` の 1 行：キー、値、そのアイデンティティタプル、タイプ、タグ、有効期間、来歴。[メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)を参照してください。

**Memory key（メモリキー）** — 名前空間とエージェントの範囲内でメモリを識別する、人が読める識別子。慣例として `<scope>/<kind>/<id>` の形式です。既存のキーへの書き込みは、それを置き換えます。

**Memory type（メモリタイプ）** — メモリの種類：`semantic`、`episodic`、`procedural`、`preference`、`profile` など。タイプによって、埋め込みの有無と検索のされ方が決まります。

**Mentor hint（メンターヒント）** — Loom が結果に添付する、または `mentor.brief` を通じて提供する短い使用上の提案。エージェントが十分に使いこなせていないプレーンや op を示します。

**Namespace（名前空間）** — ストレージ、共有、アクセスの単位：org 内の名前付きワークスペース。種別は `memory`、`code`、`bench` です。[名前空間と共有](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)を参照してください。

**Operation (op)（オペレーション）** — レジストリ（`server.operations.registry.ALL_OPERATIONS`）に宣言された 1 つのエントリ。`POST /v1/<op>`、MCP ツール、SDK を通じて提供されます。[オペレーションリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)を参照してください。

**Org（組織）** — 最上位のテナントで、アイデンティティタプルの最初の要素です。

**Pinned（ピン留め）** — `memory.pin` でマークされたメモリ。ピン留めされたメモリと競合する書き込みは `409 PINNED_CONFLICT` で拒否されます。

**Plane（プレーン）** — 独自の op とテーブルを持つ製品領域：メモリ、ナレッジ、会話、グラフ、コード、トラッカー、ラン・タスク、監査。

**Procedure（手順）** — 再利用可能なハウツー（`procedure.add` / `match`）。タイプ `procedural` のメモリとして保存されます。[手順とラーニング](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)を参照してください。

**Profile（プロファイル）** — エージェントまたはユーザーの継続的なコンテキスト（`profile.*`）で、`context.pack` が最初に読み込みます。

**Projection（プロジェクション）** — 書き込みから派生する行（埋め込み、グラフエッジ、サマリー）。失敗したプロジェクションは記録され、再試行されます（`ops.projection_failures.*`）。

**Read scope（読み取りスコープ）** — 1 つの名前空間内で読み取りを制限する `read_scope`：`namespace`（すべての行、デフォルト）、`own_user`、`own_session`。トークンのクレームは上限であり、リクエストは狭めることはできても広げることはできません。[アイデンティティと名前空間](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#read-scope-inside-a-namespace)を参照してください。

**Record（レコード）** — 登録済みのレコードスキーマ（`memory_schema.*`）で検証される構造化メモリ。トラッカーのレコードと混同しないでください。

**ReplacingMergeTree (RMT)** — Loom のほとんどのテーブルの基盤となる ClickHouse のエンジン。新しいバージョンの行が、マージ時または `FINAL` での読み取り時に古い行を置き換えるため、すべての書き込み経路はすべてのカラムを引き継ぐ必要があります。[ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md)を参照してください。

**Residue census（残存データ集計）** — 消去サガの最後のステップ：読み取り経路が参照するすべてのテーブルで、対象範囲内の有効な行を数えます。残存データが 1 つでもあれば、サガは失敗します。

**Role（ロール）** — 権限の低い順に `viewer`、`writer`、`admin`、`org_admin`、`platform_admin`。各 op は必要な最小のロールを宣言します。

**Run（ラン）** — `run.begin` と `run.complete` で区切られる、エージェントの 1 回の実行。何を想起し、何を書き込んだかを記録します。[ラン](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)を参照してください。

**Session（セッション）** — 会話または作業期間で、アイデンティティタプルの 5 番目の要素。**セッションブリーフ**（`session.brief_*`）は、あとで再開するために保存されるそのサマリーです。

**Shared layer（共有レイヤー）** — `(org, namespace, memory_key)` ごとに 1 つある正規の行で、エージェントが書き込んだ行から昇格されます（`memory.promote` / `memory.demote`、テーブル `loom.shared`）。もう一方のレイヤーの「プライベート」は、昇格されていないという意味であり、隠されているという意味ではありません。

**Source class（ソースクラス）** — サーバーが付与する行の来歴：`asserted`、`extracted`、`observed`、`derived`、`imported`、`external`。リクエストでは格下げのみ可能です。

**Tier（ティア）** — コードインデックスのファクトを生成したエクストラクター（パーサーまたはコンパイラ）。回答には、どのティアが裏付けているかが示されます。

**Tombstone（トゥームストーン）** — メモリが削除されたことを示す、より高いバージョンの行。`memory.restore` で猶予期間内なら元に戻せます。`memory.delete` で物理的に削除されるものはありません。

**Toolset（ツールセット）** — クライアントが要求する MCP の一覧（`core`、`coding`、`atomic`、`tasks`、`tracker`、`readonly`、`all`）。一覧に表示される内容が変わるだけで、トークンが呼び出せる内容は変わりません。

**Tracker（トラッカー）** — 設定されたソースから、課題トラッカーのレコード（課題、PR、CI ラン）を読み取るプレーン。重複チェック、bisect、テスト履歴に使われます。[トラッカー](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)を参照してください。

**Trust mode（トラストモード）** — インボックスの所有者が投稿者に対して設定する受け入れポリシー。Loom はこれを保存するだけで、自ら変更することはありません。

**Valid time（有効時間）** — メモリの `valid_at` / `valid_to`（エンティティエッジでは `valid_from` / `valid_to`）：ファクトが現実世界で真であった期間。Loom が記録するのは有効時間のみで、バイテンポラルではありません。

**Version（バージョン）** — `ReplacingMergeTree` が最大値を保持する、単調に増加するカラム。メモリへのすべての書き込み（削除を含む）が新しいバージョンになります。

**Worker（ワーカー）** — `server/workers/registry.py`（`_WORKERS`）に行として登録される、監視下のバックグラウンドループ。[ワーカー](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)を参照してください。

**Working memory（ワーキングメモリ）** — 現在のタスク用のスクラッチパッドブロック（`working_memory.*`）。埋め込みもランキングもされず、`context.pack` が永続メモリより先に読み込みます。[ワーキングメモリとプロファイル](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)を参照してください。
