<!-- i18n: source=docs/features/entity-graph.md blob=f2344ba078b13ea0d97951af2212b5da3b9af8bb model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md), which is authoritative.

<a id="entity-graph"></a>

# エンティティグラフ

Loom は、メモリの行と同じ ClickHouse データベース内に、メモリの行と並べてエンティティグラフを保持します。エンティティがノードです。同じメモリの中で言及された 2 つのエンティティは、共起エッジでつながります。抽出処理は、型付きの `(subject, relation, object)` トリプルを 2 つ目のエッジ集合として追加します。グラフはメモリの射影であり、すべてのエッジは、それを生成した行にさかのぼれます。「billing service につながっているものは何か」のように、ベクトル検索では答えられない問いに使います。

エンティティコミュニティはデフォルトでは無効です（`LOOM_ENTITY_COMMUNITIES_ENABLED`）。

<a id="what-the-graph-holds"></a>

## グラフが保持するもの

| テーブル                                                                 | 保持するもの                                                                                                                            |
| ------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.entity_edges` (+ `entity_edge_memories`, `entity_edge_rows`)       | 共起の観測ごとに 1 行（`co_occur = 1`）。読み取り側がペアごとに合計します。ペアごとのソースキーと、それを挙げた行および値を保持します。 |
| `loom.entity_triples` (+ `entity_triple_memories`, `entity_triple_rows`) | `confidence` と、`adapter_name` としての抽出モデルを持つ型付きトリプル。トリプルごとのソースキーと、それを述べた行および値を保持します。 |
| `loom.entity_degree`                                                     | 各エンティティの共起次数の合計（`SummingMergeTree`）。ハブルールがこれを読み取ります。                                                  |
| `loom.entity_canonical_map`                                              | メンションのテキスト → 正規エンティティ。                                                                                               |
| `loom.entity_communities`                                                | レベルごと、エンティティごとのコミュニティ ID。                                                                                         |
| `loom.entity_vectors`, `loom.entity_summaries`                           | 検索のエンティティ系統が読み取る埋め込み。                                                                                              |
| `loom.derived_facts`                                                     | 単位と日付を持つ数値ファクト。検索での計算による回答に使います。                                                                        |
| `memory.link` edges                                                      | エージェントまたは人が明示するエッジ（`supports`、`depends_on`、`caused` など）。グラフモードの検索が読み取ります。                     |

エッジとトリプルは、`valid_from` / `valid_to`（ソースとなるメモリの `valid_at` / `valid_to` に由来）と、それらを無効化する `invalid_at` スタンプを持ちます。`as_of` を指定した読み取りでは、その時点で有効な観測だけを合計します。

グラフを ClickHouse に置いているのは、エッジを対応する行と同じリクエストで書き込み、同じ saga で消去し、ネットワークを介さずにメモリと結合できるようにするためです。その代償として、ウォークは上限付きの集計クエリの連続になるため、すべてのウォークに上限が設けられています。

<a id="how-the-graph-is-built"></a>

## グラフの構築方法

最初にメモリ行が書き込まれ、グラフの行はそのメモリ行からの射影です。射影に失敗した場合は `loom.memory_projection_failures` に記録され、行が射影に失敗した値を保持している間に限り再試行されます。書き込み自体が失敗することはありません。感度が `high` のバルクエントリは何も射影しません。感度が `high` の `memory.set` は、書き込み側が宣言したエンティティのエッジのみを射影し、テキストは射影しません。

1. **エンティティ。** 書き込み側が `memory.set` で `entities` を渡すか、`memory.set_from_messages` がエンティティを抽出します。`entity_extractor` ワーカー（`LOOM_ENTITY_EXTRACT_ENABLED`、デフォルトで有効）は、エンティティなしで書き込まれた行に、書き込みパスの外でタグを付けます。このワーカーには実際の LLM プロバイダーが必要です。エンティティのテキストは、書き込み時と読み取り時で同じ方法で正規化されます（冠詞と所有格を除去し、主要名詞を単数形にし、一人称を `user` に対応付けます）。そのため、"The Billing Service's" と "billing service" は 1 つのノードになります。`commit:<sha>` や `error:<name>` という名前は識別子であり、同じ方法で正規化されますが、単数形化はされません。`error:Timeouts`、またはバッククォートや引用符で囲んだ同じ名前は、ノード `error:timeouts` になります。コロンの直後が空白またはマークアップの場合（`Error: the build failed`、``error:`Timeouts` ``）は、通常の文章として扱われます。代名詞、感動詞、軽動詞、2 文字のトークンは除外されます。書かれたままの状態、または正規化後（NFKC は `ﬁ` を `fi` にします）に 256 文字を超えるテキストも、書き込み時と `entity_filter` で除外されます。ポイント書き込みでは、そのようなファクトの主語または目的語は拒否されます。
2. **エッジ。** メモリのエンティティの全ペアが 1 件のオブザベーション行になります。1 メモリあたり最大 100 ペアです。
3. **トリプル。** 抽出されたファクトのリレーションがトリプルになります。1 メモリあたり最大 8 件です。動詞は `lower_snake` に正規化されます。`namespace.register_ontology`（管理者用）は、推奨するエンティティタイプ、動詞、属性を、カーディナリティとともに登録します。この集合に含まれない名前は `ontology_warnings` を生成しますが、拒否されることはありません。
4. **置き換えによるトリプルのクローズ。** ある行が別の行を置き換えると、古い行のオープンなトリプルのうち、新しい行が裏付けていないものに `invalid_at` が設定されます。`one` リレーション（勤務先、所在地）では、新しい値が現行のまま残り、古い値は新しい値の開始時にクローズされます。このクローズの実行中に古い値を再度述べる書き込みがあった場合、その値はオープンのままになります。
   - 値は開始日で順序付けられ（日付のない値は到着時に開始します）、同じ日ならマイクロ秒単位の到着順で決まります。これにより、競合する書き込み側同士でも現行値が 1 つに定まります。1 回の書き込み内では、後から述べられた値の方が新しい値です。
   - 1 回の書き込み内では、過去形の日付なしの値（`lived_in`、`worked_at`）は、日付付きの値より古いものとして扱われます。順序は問いません。10 月 1 日からの Paris と並んだ "user lived_in london" では、Paris が現行のままです。
   - 値は最新の行で判定されます。"London from Oct 15, Paris from Oct 14, London from Oct 13" では London が現行のままです。
   - 自身の終了日付きで述べられた値（"London until December"）は、早期にクローズされることはありません。その終了日まで、新しい値と並んで現行のままです。
   - Undo（`memory.restore`）は、復元された値を、復元時点から、またはファクト自身の後の開始日（"moving to Berlin on Dec 1"）から現行にします。もう一方の値はクローズされますが、その行は有効なままです。後の日付の値は引き続き優先され、Undo の後に述べられた値も同様です。Undo を行き来しても重みは増えません。
   - 失敗したクローズは、それを引き起こした書き込みまたは Undo の時点を基準に再実行されるため、その後に述べられた値は現行のまま残ります。

   Undo がファクトとエッジをどのように再度述べ、重み付けし、再実行されるかについては、[メモリアーキテクチャ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md#undo)を参照してください。

ポイント書き込み（`memory.set`、`memory.update`、`memory.update_by_id`）は、呼び出し元が `relations` で渡したトリプル（最大 8 件）を、HTTP と SDK の型付き操作で書き込みます（MCP の一覧にはありません）。抽出は行いません。自動抽出はバルクパスで実行されます。`memory.set_from_messages`、またはリレーションを供給するバルク書き込み側が対象です。標準のフェイクプロバイダーでは、渡したエンティティだけがエッジを形成します。

<a id="operations"></a>

## オペレーション

| オペレーション | ロール | 答える内容 |
| ---------------------------- | ------ | ------------------------------------------------------------------------------------------------------------- |
| `facts.list` | viewer | `subject`、`object`、`relation`（AND）または `entity`（どちらかの端）で絞り込んだトリプル。 |
| `facts.related` | viewer | 1 つのエンティティと共起するエンティティ。強い順で、シードがどちら側にあるかも返す。 |
| `facts.traverse` | viewer | シードから N ホップ先までの近傍。 |
| `facts.path` | viewer | `max_hops` 以内での、2 つのエンティティ間の最短のエッジ連鎖。 |
| `facts.expire_by_adapter` | admin | 1 つのエクストラクタが書き込んだトリプルを廃棄（tombstone）する。メモリは残る。 |
| `memory.search`（graph モード） | viewer | `search_mode="graph"`：クエリのエンティティから 1 ホップ先にあるヒット（[検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)）。 |
| `memory.related` | viewer | 指定したメモリとエンティティを共有するメモリ。 |
| `memory.timeline` | viewer | あるエンティティのメモリを時系列に並べたもの（`entity=…`）。 |
| `memory.backlinks` | viewer | ある行のエンティティを共有するメモリ。 |
| `namespace.taxonomy` | viewer | 名前空間の上位エンティティ。計算済みの場合はコミュニティも返す。 |

フィールド一覧：[ファクト](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/facts.md)、[メモリ](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/memory.md)。graph モード以外では、`entity_filter` が指定したエンティティに関する行をブーストし、検索のエンティティレッグが、クエリ内で別の言葉で言及されたエンティティに関する行を見つけます（[検索とランキング](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)）。

<a id="factslist"></a>

### `facts.list`

フィルタなしの場合、`co_occur` が上位のトリプルを返します。`sort_by="recency"` は `last_seen` で並べ替えます。

| フィールド | 効果 |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `include_sources` | トリプルごとに最大 10 件程度のソースメモリキーを追加する。 |
| `include_source_status` | 各ソース行を読み、トリプルが open なのに背後の行がすべて supersede 済みである場合に `stale` と印を付ける。 |
| `provenance` | `extracted`（両端がソーステキストに含まれる）、`inferred`、または `facet`（Loom が生成したナビゲーション用エッジ）。 |
| `as_of` | その時点で有効なトリプルだけを残す。 |

<a id="factstraverse"></a>

### `facts.traverse`

探索は展開の前に絞り込みます。各ホップはフロンティアを 1 つのクエリとして送り、有効期間内でペアごとの観測数を合計し、訪問済みのエンティティを除外して、各親につき最も強い `top_k_per_hop` 個のペアを残します。ホップ h のフロンティアは最大で K^h 個のエンティティです。各エンティティは、最初に到達したホップで、最も強い親を経由して 1 回だけ報告されるため、結果はツリーとして描画できます。

すべてを展開してから後で絞り込む再帰クエリ 1 本では、近傍全体がマテリアライズされます。大きな名前空間では、3 ホップの探索が ClickHouse のメモリ上限を超えることがあります。ホップごとに絞り込むことで、コストは答えのサイズに抑えられます。

| フィールド | デフォルト | 効果 |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `max_hops` | 2（1–4） | 探索の深さ。 |
| `top_k_per_hop` | 20（最大 100） | ホップごとに親 1 つあたり残す子の数。 |
| `min_co_occur` | 1 | ペアあたりの最小観測数。 |
| `max_nodes` | 400（最大 5,000） | 厳密な上限。到達すると `truncated=true` になる。 |
| `as_of` | 未設定 | その時点で有効な観測だけを残す。 |
| `labeled` / `relations` | false / 未設定 | 型付きトリプルをたどる（すべて、またはこれらの動詞のみ）。ノードは `via_relation`、`via_direction`、`via_provenance` を持つ。 |
| `hub_degree` | 0（オフ） | 総次数がこの値以上のノードをハブとする。 |
| `hub_share` | `LOOM_FACTS_TRAVERSE_HUB_SHARE` (0.01) | そのノードに到達したエッジが、ノードの次数に占める割合がこの値以下の場合、そのノードをハブとする。 |
| `rank_by` | `LOOM_FACTS_TRAVERSE_RANK_BY` (`cosine`) | ホップごとの絞り込み：`co_occur`、`specificity`（`via_co_occur / degree`）、または `cosine`（`via_co_occur / sqrt(degree)`）。 |

各ノードは `hop`、`cumulative_weight`、`via_co_occur`、`path`（シードからの連鎖）、`valid_from` / `valid_to`、`degree` を報告し、コミュニティが存在する場合は `community_id` も報告します。ノードはホップ順、次に重み順に並びます。

**ハブ。** 汎用的なエンティティ（"user"、"pr"）はあらゆるものと共起します。ハブは報告されます（`hub: true`、`hubs` に列挙）が、展開されません。シードは常に展開されるため、ハブの近傍を見るにはハブをシードにしてください。`hub_share` は名前空間の次数のスケールに依存しないため、デフォルトのルールになっています。`hub_degree` は名前空間ごとに固有の値です。設定する前に次数の分布の裾を確認してください。

<a id="factspath"></a>

### `facts.path`

同じエッジに対する、境界付きの双方向幅優先探索で、途中で出会います。`max_hops`（1–4、デフォルト 3）が全長の上限です。答えは `found`、`path`、`edges` で、各ペアの `co_occur` と有効期間を含みます。`truncated=true` は、答えが不完全な可能性があることを意味します。`found=false` かつ `truncated=false` は、`max_hops` 以内に接続がないことを意味します。

<a id="example"></a>

## 例

```python
from loom import Agent

async with Agent("dev", "local", "sre-bot", base_url="http://localhost:7777") as agent:
    await agent.memory.set(
        "billing/depends_on",
        "The billing service reads invoices from the ledger database.",
        entities=["billing service", "ledger database"],
    )
    walk = await agent.facts.traverse("billing service", max_hops=2, top_k_per_hop=10)
    for node in walk["nodes"]:
        print(node["hop"], " > ".join(node["path"]), node["via_co_occur"])

    route = await agent.facts.path("billing service", "ledger outage")
    print(route["found"], route["path"], route["truncated"])
```

HTTP では、過去の時点のグラフに対して探索を実行できます。

```bash
curl -s localhost:7777/v1/facts.traverse -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"org":"dev","namespace":"local","seed_entity":"billing service","max_hops":2,"as_of":"'"$AS_OF"'"}'
```

MCP では、`op="facts.traverse"` を指定して `loom_op` を呼び出します。

<a id="maintaining-the-graph"></a>

## グラフの保守

| タスク | 方法 |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| 不適切なエクストラクタのトリプルをロールバックする | `facts.expire_by_adapter(adapter_name=…, max_confidence=…)`。メモリは残ります。 |
| グラフをエクスポートする | `loom entity-export --org O --namespace N --format graphml`（または `jsonld`。`--all` を指定すると無効化されたエッジも含まれます）。 |
| エンティティをコミュニティにクラスタリングする | `LOOM_ENTITY_COMMUNITIES_ENABLED=true` を設定します（下記）。 |
| グラフを表示する | ダッシュボードの Graph ビュー（[ダッシュボード](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)）。 |

`entity_communities` ワーカーは、名前空間ごとにラベル伝播を実行し、結果を `loom.entity_communities` に書き込みます。この結果は `namespace.taxonomy`、graph モードの拡張、`community_id` から参照されます。対象となるのは、統合（consolidation）が有効な名前空間と、直近の `LOOM_ENTITY_COMMUNITIES_WALKED_DAYS`（14）日以内にたどられた名前空間です。次数の高いエンティティから先に除外し（`LOOM_ENTITY_COMMUNITIES_HUB_DROP_FRACTION`）、1 つのコミュニティがエンティティの `LOOM_ENTITY_COMMUNITIES_GIANT_SHARE` を超える割合を占める間は、その割合を倍にしていきます。これにより、汎用的なトークンによってグラフが 1 つのコミュニティに融合することを防ぎます。

<a id="settings"></a>

## 設定

| 設定                                         | セクション                                                                                                                   |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_FACTS_TRAVERSE_HUB_SHARE`, `…_RANK_BY` | [名前空間の健全性、ナレッジグラフ、コミュニティ](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#namespace-health-knowledge-graph-communities) |
| `LOOM_ENTITY_COMMUNITIES_*`                  | 同じセクション。                                                                                                             |
| `LOOM_ENTITY_EXTRACT_*`                      | [統合処理](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)                                                                 |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED`              | [メモリ検索](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)                                                                   |

<a id="limits"></a>

## 制限事項

- **共起は関係ではありません。** 1 つのメモリに現れた 2 つのエンティティは、文の内容に関係なくリンクされます。関係が重要な場合は、型付きトリプル（`labeled`、`relations`）または `memory.link` を使用してください。
- **グラフの品質はエンティティの品質に左右されます。** エンティティを持たない行は、`entity_extractor` ワーカーが処理するまでエッジを追加しません。
- **探索には上限があります。** 2 つのエンティティが接続されていないと結論づける前に、`truncated` を確認してください。
- **置き換えられた行に対して、エッジの更新が遅れることがあります。** `facts.list(include_source_status=true)` を使うと、まだオープンなトリプルを確認できます。
- **有効性は有効時間（valid time）です。** `as_of` が読み取るのは事実が成り立っていた時点であり、Loom がその事実を知った時点ではありません。

<a id="related"></a>

## 関連項目

- [検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)：グラフモード。
- [メモリの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)：`entities` の渡し方。
- [抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)：抽出されたエンティティとトリプルの出どころ。
- [メモリのアーキテクチャ](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md)：プロジェクションライター。
- [ワーカーのリファレンス](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)。
