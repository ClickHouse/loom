<!-- i18n: source=docs/features/integrations/clickstack.md blob=008f7906fbb2d60f24b7f99be4e0ee44382e64f9 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md), which is authoritative.

<a id="clickstack-and-hyperdx"></a>

# ClickStack と HyperDX

Loom は常に OpenTelemetry を出力します。HyperDX のソース、ダッシュボード、消去ステップについては設定が必要です。

Loom は操作ごとにスパンを出力し、ログとメトリクスも出力します。スパンには `loom.operation`、`loom.org`、`loom.namespace`、`loom.agent`、`loom.request_id` が含まれ、存在する場合は `loom.user_id` / `enduser.id` も含まれます。デフォルトでは、Loom はこれらを自身の ClickHouse サービス上の `default.otel_*` テーブルに、コレクターのスキーマで直接挿入するため、HyperDX は実行すべきコレクターなしでそれらを読み取れます。`LOOM_CLICKSTACK_DATABASE` を設定すると、ここおよび以下のすべてのクエリで、テーブルは `default` ではなくそのデータベースに置かれます。カラム構成は `schemas/otel/clickstack_otel_tables.sql` にあります。ClickStack がスキーマを変更した場合は、このファイルも合わせて更新する必要があります。

モデル呼び出しのスパンには、OpenTelemetry GenAI 属性（[テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#traces-and-logs)）が含まれます。これらは、HyperDX の LLM ビューがプロバイダー、操作、トークン使用量、プロンプトキャッシュの割合、コストを読み取る際に使うキーで出力されます。

<a id="choose-the-export-path"></a>

## エクスポート経路の選択

| `LOOM_OTEL_EXPORTER` | 動作                                                                                                                                      |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `ch`                 | 同じサービス上の `default.otel_traces`、`otel_logs`、`otel_metrics_sum`、`otel_metrics_histogram`、`otel_metrics_gauge` に挿入します |
| `otlp`               | `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` に送信します（ヘッダーは `LOOM_OTEL_EXPORTER_OTLP_HEADERS`）。コレクターや別のバックエンド向けです |
| 空（デフォルト）     | エンドポイントが設定されている場合は `otlp`、それ以外は `ch` になります                                                                   |

`ch` モードでは、Loom はまず `default.otel_traces` が存在することを確認します。存在しない場合、エクスポーターは1行のログを出力した後、自身を無効にします。テーブルを作成するには、次を実行します。

```bash
clickhouse-client --multiquery < schemas/otel/clickstack_otel_tables.sql
```

`GET /v1/health?include=otel` で、解決されたエクスポーターを確認できます。その他の OTel 設定（エクスポート間隔、テンポラリティ、ゲージのハートビート、サービス名）については、[テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#exporters)を参照してください。

<a id="point-hyperdx-at-the-tables"></a>

## HyperDX をテーブルに向ける

行はすでにサービス内にあるため、コレクターと取り込みキーの手順は省略できます。HyperDX の 3 つの**ソース**を作成します。`otel_traces` に対するトレース、`otel_logs` に対するログ、`otel_metrics_*` に対するメトリクスです。`deploy/clickstack_sources.sh` は、HyperDX API を通じてこれらを作成し、相関も設定します（ホスト型 ClickStack は、コンソールのセッション cookie である `HDX_COOKIE` で認証します）。各ソースのフィールドマッピングは、[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) を参照してください。

<a id="dashboards-and-alerts"></a>

## ダッシュボードとアラート

[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md) がレシピ集です。

- **Loom — Memory Health**：検索品質の推移（`loom.search.top_similarity`）、操作とトランスポートごとの呼び出し数とエラー数、システム状態、コストとトークン、検索プレーンごとのレイテンシ、書き込み結果、リクエストのパネル（レート、レイテンシ、エラー、最も遅いリクエスト、テナントごとのトラフィック、トレースのログ）。
- **Loom — Code Plane**：インデックスのラグ、ティアのラグ、その他の `loom.code.*` ゲージ。JSON 定義は `deploy/dashboards/code_plane.json` です。
- ブートストラップの順序と保存済み検索。

独自のタイルを作成するときの注意点が 2 つあります。

- **カウンターは累積値です。** `LOOM_OTEL_METRICS_DELTA_TEMPORALITY=true` を設定しない限り、カウンターの行はその時点までの累計です。レートを求めるには合計ではなく、連続するポイント間の差分が必要です。この設定とダッシュボードは一緒に切り替えてください。
- **HyperDX では空の結果が 0 として扱われます。** アラートの最小サンプル数のガードは、行なしではなく正常範囲内の値を返す必要があります。そうしないと、データがないだけでアラートが発火します。

メトリクス名と推奨しきい値は[メトリクスカタログ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#metrics)にあります。

<a id="erasure"></a>

## 消去

`LOOM_CLICKSTACK_DATABASE` が必要です。ClickStack にはユーザーデータを削除するための管理 API がないため、バックエンド（`server/erasure/app/backends.py` の `ClickStackBackend`）は、そのデータベース内の OTel テーブルに対して軽量な `DELETE FROM` 文を実行します。

| テーブル                                                                         | 検索する属性カラム        |
| -------------------------------------------------------------------------------- | ------------------------- |
| `otel_logs`                                                                      | `LogAttributes`           |
| `otel_traces`                                                                    | `SpanAttributes`          |
| `otel_metrics_gauge`, `_sum`, `_histogram`, `_exponential_histogram`, `_summary` | `Attributes`              |
| `hyperdx_sessions`                                                               | `ResourceAttributes`      |

- **ユーザースコープ** は、`LOOM_CLICKSTACK_USER_ID_KEYS` のいずれかのキー（デフォルトは `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id`）に OR で一致します。SDK ごとにキー名が異なるためです。Loom のリクエストスパンは `enduser.id` を持ち、モデル呼び出しスパンは `loom.user_id` と `langfuse.user.id` を持ちます。Langfuse が設定されている場合は、呼び出し元が解決された後にそのリクエストが開く他のすべてのスパンも同様です。これらはいずれも `loom.org` を持つため、下記の org ガードが適用されます。
- **名前空間スコープ** は `loom.namespace` または `service.namespace` に一致します。Loom のエクスポーターは `service.namespace` を定数 `loom` に設定し、テナントは `loom.namespace` に格納します。`service.namespace` は同じ ClickStack に書き込む他のエミッターをカバーするためのもので、その値 `loom` に一致することはありません。
- **org ガード。** _別の_ org の `loom.org` が付いた行は消去されません。`loom.namespace` に一致した行は、消去を実行する org の `loom.org` を持っている必要があります。Loom の op メトリクスとドメインメトリクス（カウンターとヒストグラム）は `loom.namespace` を持ちますが org を持たないため、名前空間の消去では対象になりません。これらにメモリの内容は含まれませんが、`loom.namespace` ラベルは名前空間名であり、個人用名前空間の場合はユーザーのメールアドレスに由来します。コレクターテーブル自体に TTL がある場合は、その TTL で削除されるまで残ります。`loom.org` タグがなく、ユーザーキーまたは `service.namespace`（他のエミッター）に一致した行は、対象のユーザーのデータを含む可能性があるため消去されます。

軽量削除が書き込むのは行の存在マスクだけなので、行はすべての読み取りから直ちに見えなくなり、バイトは次回のマージで削除されます。`ALTER TABLE … DELETE` は、影響を受けるすべてのパートを書き直します。述語は、属性マップの値に bloom filter がないテーブル（メトリクスのテーブルにはありません）をすべてスキャンします。`loom doctor` がそれらのテーブルを示します。

このステップは、削除の前に行数を数え、その件数を報告します。テーブルまたはデータベースが存在しない場合は、消去対象なしとして扱います。それ以外の失敗ではステップは fail closed となり、`erasure_recovery` ワーカーが再試行します。

デフォルトに頼る前に、実際のデータがどのユーザー ID キーを持っているかを確認してください。

```sql
SELECT DISTINCT arrayJoin(mapKeys(LogAttributes))
FROM default.otel_logs WHERE TimestampTime > now() - INTERVAL 1 DAY
```

このステップが属するサガについては、[保持と消去](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)を参照してください。

<a id="settings"></a>

## 設定

| 設定                               | デフォルト                                                 | 効果                                         |
| ---------------------------------- | ---------------------------------------------------------- | -------------------------------------------- |
| `LOOM_OTEL_EXPORTER`               | `""`                                                       | `ch`、`otlp`、または自動                     |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` | `""`                                                       | コレクターのベース URL                       |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`  | `""`                                                       | カンマ区切りの `K=V` ヘッダー                |
| `LOOM_OTEL_ENABLED`                | `true`                                                     | マスタースイッチ                             |
| `LOOM_CLICKSTACK_DATABASE`         | `""`                                                       | OTel テーブルのデータベース。消去を有効にする |
| `LOOM_CLICKSTACK_USER_ID_KEYS`     | `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id` | ユーザー単位の消去が照合する属性キー         |

<a id="related"></a>

## 関連ドキュメント

- [テレメトリ](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)：出力される内容と、メトリクスの全カタログ。
- [モニタリング](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md)。
- [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)：同じスパンをモデル呼び出しのトレースとして扱います。
