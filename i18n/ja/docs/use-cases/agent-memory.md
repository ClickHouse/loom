<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI エージェントのための長期記憶と共有メモリ

Loom は、セルフホスト型の MCP サーバーおよび HTTP API です。AI エージェントが共有できる長期記憶を提供します。あるエージェントがセッション中に保存した事実は、次のセッション、別のエージェント、あるいはダッシュボードを使う人が検索して見つけられます。このページでは、その仕組みと始め方を説明します。

<a id="what-you-get"></a>

## 提供される機能

- **名前空間内のキーごとに1つの主張。** `memory.set` は事実を保存し、`memory.get` はキーを指定してそれを読み戻します。`memory.search` と `loom_search` は意味で検索します。名前空間は共有の単位であり、デフォルトでは、メンバー全員がその名前空間内のすべての行を読めます。
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **訂正できるメモリ。** 変更を加えると、履歴を伴う新しいバージョンが作られます。正規化後に同じになる事実は保存済みの行にマージされ、言い換えただけの同じ事実は重複として保存され、矛盾はレビュー対象としてフラグが付けられ、固定(pin)された事実への書き込みは拒否されます。
  [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **回答を控える検索。** 類似度のしきい値を超えるものがない場合、`memory.search` は理由コード付きで `abstain=true` を返し、弱いヒットにはそれぞれ `below_threshold` のラベルを付けます。`loom_search` はそれらを除外します。[Search and ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **コーディングエージェント間で共有されるメモリ。** Claude Code と Codex のプラグインは、セッション開始時に関連するコンテキストを読み込み、実行中のセッションを記録します。Cursor などの MCP クライアントも同じサーバーに接続できます。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **自作エージェント向けのユーザー別メモリ。** Python と TypeScript の SDK は、エンドユーザーごとにメモリを書き込み、読み取ります。[Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 計測結果

同じ `gpt-5` をリーダーとして使い、LongMemEval-S の 100 問のペアで比較したところ、Loom が取得した記憶から得た回答のスコアは 94% で、履歴全体から得た回答の 90% を上回りました。リーダーのプロンプトトークン数は 91% 少なくなっています(記憶の構築に一度だけかかるコストは除く)。ただし、精度の差は統計的には確認されていません。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## はじめに

[クイックスタート](../getting-started/quickstart.md)では、ローカルスタックを起動し、HTTP、Python、MCP で最初のメモリを保存します。[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)では、インプロセスのメモリライブラリのほうが適しているケースを説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
