<!-- i18n: source=docs/guides/extraction-pipeline.md blob=a8d7e423185b09c1fa986972aa01b8c1b70d77d8 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md), which is authoritative.

<a id="customize-the-extraction-pipeline"></a>

# 抽出パイプラインのカスタマイズ

`memory.set_from_messages` は、トランスクリプトを、1 つの状態を共有する順序付きのステップのリストで処理します。リクエストは `pipeline_steps` でこのリストを置き換えられます。ステップの並べ替えやスキップ、オペレーターが登録したインプロセスのステップの呼び出しが可能です。このページではステップのリストだけを扱います。抽出が何を行うかは[抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)を参照してください。

`pipeline_steps` は HTTP ボディのフィールドです。MCP ツールのスキーマには表示されず、Python SDK の `memory.set_from_messages` ヘルパーのパラメーターでもありません。HTTP または `call(...)` で送信してください。生成された op の入力型（`sdk/python/loom/ops.py`、`sdk/typescript/src/generated/ops.ts`）には含まれています。

<a id="the-default-pipeline"></a>

## デフォルトのパイプライン

`server/memory/app/pipeline.py` の `DEFAULT_PIPELINE`：

| ステップ                          | 処理                                                                                             |
| --------------------------------- | ------------------------------------------------------------------------------------------------ |
| `loom.fetch_namespace_categories` | 抽出器のために、名前空間のタグ分類を読み込みます。                                               |
| `loom.fetch_existing_memories`    | 最も近い既存のメモリ（最大 50 件）を抽出器に提示します。                                         |
| `loom.llm_extract`                | 抽出の呼び出しです。                                                                             |
| `loom.drop_recall_echoes`         | セッションに提供されたメモリを言い換えただけのファクトを破棄します。                             |
| `loom.drop_session_echoes`        | セッション自身の Loom 呼び出しが運んだ内容を言い換えただけのファクトを破棄します。               |
| `loom.drop_activity_narration`    | 永続的な成果のないツール操作の叙述を破棄します。                                                 |
| `loom.drop_non_durable`           | コーディングセッション：ステータスのスナップショット、後片付け、計画を破棄します。               |
| `loom.verify_durable`             | コーディングセッション：残ったファクトに対する検証パスです。                                     |
| `loom.hash_dedup`                 | 正規化ハッシュがすでに保存されているファクトを破棄します。                                       |
| `loom.bulk_write`                 | ボットのルール（`about="agent"`）を learnings キューに登録し、残りを `memory.bulk_set` で書き込みます。 |

コーディングセッションとは、`extra_tags` にコーディングエージェントのソースタグ（`source:claude-code`、`source:codex`、`source:cursor`）を持つトランスクリプト、またはトラジェクトリのリプレイです。2 つのコーディング用ステップは、それ以外のトランスクリプトをそのまま通過させます。

**空の `pipeline_steps` はこのリストを実行します。空でないものは、指定されたステップだけを指定した順序で**、1 つの共有状態に対して実行します。`loom.bulk_write` を省くと何も書き込まれませんが、レスポンスには抽出された内容が報告されます。

<a id="run-a-shorter-pipeline"></a>

## より短いパイプラインを実行する

新規と分かっているデータを一度だけインポートする場合は、重複排除のコンテキストなしで抽出と書き込みを行います。

```bash
curl -s "$LOOM_BASE_URL/v1/memory.set_from_messages" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' -d '{
  "org": "acme",
  "namespace": "support",
  "agent": "importer",
  "messages": [{ "role": "user", "content": "..." }],
  "pipeline_steps": [{ "step_id": "loom.llm_extract" }, { "step_id": "loom.bulk_write" }]
}'
```

これには実際の LLM プロバイダー（`LOOM_LLM_PROVIDER` とそのキー）が必要です。標準スタックのフェイクプロバイダーでは、意味のあるファクトは抽出されません。

<a id="step-behaviour"></a>

## ステップの動作

各ディスクリプターは `{"step_id": ..., ...}` の形式で、オブジェクト全体がステップの設定として渡されます。

- リクエストあたり**最大 16 ステップ**です。
- **`hook.*` ステップは拒否され**、422 `VALIDATION_FAILED` を返します。サーバーは呼び出し元に代わって外部への呼び出しを行いません。
- **未知のステップ ID** はスキップされ、レスポンスの `pipeline_errors` に `unknown_step:<id>` として報告されます。
- **フェイルオープンのステップ。** 取得ステップが失敗すると、エラーが `pipeline_errors`（たとえば `fetch_existing_failed:<error>`）に記録され、パイプラインは続行します。
- **`loom.llm_extract` はフェイルクローズです。** 抽出エラーはリクエストを 502 で中断するため、トランスクリプトに対して黙って `written=[]` が返されることはありません。ディスクリプターに `{"judge": true}` を指定すると judge ゲートが有効になります。デフォルトのリストでは、リクエストの `judge_extraction=true` が同じ効果を持ちます。
- **タイミング。** レスポンスの `stage_ms` には各ステップの実行時間（`step.<step_id>`）が含まれます。各ステップは、ステップ ID を属性に持つ `pipeline.step` という名前の OpenTelemetry スパン内でも実行されます。

<a id="register-an-in-process-step"></a>

## インプロセスのステップを登録する

オペレーターは、サーバープロセスが起動時にインポートするコードでステップを登録します。

```python
from server.memory.app.pipeline import WorkflowState, register_step


async def spam_filter(state: WorkflowState, config: dict) -> WorkflowState:
    state.extracted_facts = [
        f for f in state.extracted_facts if "spam" not in str(f.get("value", "")).lower()
    ]
    return state


register_step("operator.spam_filter", spam_filter)
```

以後、リクエストは `{"step_id": "operator.spam_filter"}` を指定できます。ディスクリプターのその他のキーは `config` として渡されます。ステップはインプロセスで実行され、`WorkflowState` 全体（ID、メッセージ、既存のメモリ、抽出されたファクト、カウンター）への読み取りと書き込みのアクセス権を持ちます。`register_step` は、同じ ID の既存の登録を置き換えるため、オペレーターのステップで組み込みのステップを置き換えることもできます。

`loom.bulk_write` を置き換えるステップは、書き込む内容から `about="agent"` のファクトを除外する必要があります。これらはボットのルールであり、組み込みのステップが learnings キューに登録します。

> **警告：** `extracted_facts` を破棄または書き換えるステップは、そのステップを指定するすべての呼び出し元が保存する内容を変えます。`loom.bulk_write` の後に置いたステップは、書き込まれた行を `state.written` で参照できますが、書き込みを取り消すことはできません。

Loom 本体に組み込みステップを追加するには、`server/memory/app/pipeline.py` に関数とその `register_step` 呼び出しを追加し、`DEFAULT_PIPELINE` に組み込み、前後で `make bench-quick` を実行します。デフォルトのリストはすべての抽出に影響するためです。

<a id="limits"></a>

## 制限

- ステップは順次実行されます。分岐や並列実行はありません。
- デフォルトのリストはグローバルです。名前空間ごとのパイプラインはありません。
- ステップ単位のリトライはありません。

<a id="related"></a>

## 関連項目

- [抽出](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)
- [Loom の拡張](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
- [メモリの書き込み](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
