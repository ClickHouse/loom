<!-- i18n: source=docs/use-cases/agent-memory.md blob=27f44a06830b04a8e513cbab521e16aef4bb7034 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AIエージェントのための長期メモリと共有メモリ

Loom は、AI エージェントが共有する長期メモリを提供する、セルフホスト型の MCP サーバーおよび HTTP API です。あるエージェントがセッションで保存した事実は、次のセッション、別のエージェント、またはダッシュボードを使う人が見つけられます。このページでは、その仕組みと始め方を説明します。

<a id="what-you-get"></a>

## できること

- **名前空間内でキーごとに 1 つの主張。** `memory.set` は事実を保存し、`memory.get` はキーで読み戻し、`memory.search` と `loom_search` は意味で検索します。名前空間は共有の単位であり、デフォルトでは、メンバー全員が名前空間内のすべての行を読み取れます。
  [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **訂正できるメモリ。** 変更すると履歴付きの新しいバージョンが作られます。正規化した結果が同じ事実は保存済みの行にマージされ、言い換えられた同じ事実は重複として保存され、矛盾はレビュー対象としてフラグが立てられ、固定（pin）された事実への書き込みは拒否されます。
  [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **棄権する検索。** 類似度のしきい値を超えるものがない場合、`memory.search` は理由コード付きで `abstain=true` を返し、関連の弱いヒットにはそれぞれ `below_threshold` のラベルを付けます。`loom_search` はそれらを除外します。[検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **コーディングエージェント向けの長期メモリを、エージェント間で共有。** Claude Code と Codex のプラグインは、セッション開始時に関連するコンテキストを読み込み、セッションの進行中にその内容を記録します。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode、goose、Zed などの MCP クライアントも同じサーバーに接続でき、名前空間に参加しているエージェントは、他のエージェントがそこに保存した事実を見つけられます。
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **自作エージェント向けのユーザーごとのメモリ。** Python と TypeScript の SDK で、エンドユーザーごとにメモリを書き込み、読み取れます。[はじめてのエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 計測結果

同じ `gpt-5` をリーダーとして使い、ペアにした 100 問の LongMemEval-S で比較したところ、Loom が取得したメモリからの回答は正答率 94% で、履歴全体からの回答の 90% を上回りました。リーダーのプロンプトトークンは 91% 少なくなっています（メモリ構築の一度きりのコストは除く）。ただし、正答率の差は統計的には確定していません。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## 始める

[クイックスタート](../getting-started/quickstart.md)では、ローカルスタックを起動し、HTTP、Python、MCP で最初のメモリを保存します。[Loom が向かない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)では、インプロセスのメモリライブラリのほうが適しているケースを説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
