<!-- i18n: source=docs/use-cases/agent-memory.md blob=ef7ede85eb9ef02954cc9bdd80508b69b4b006f0 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI エージェントのための長期メモリと共有メモリ

Loom は、AI エージェントが共有できる長期メモリを提供する、セルフホスト型の MCP サーバーおよび HTTP API です。あるエージェントがセッション内で保存した事実は、次のセッションや別のエージェント、あるいはダッシュボードを見ている人が見つけられます。このページでは、その仕組みと始め方を説明します。

<a id="what-you-get"></a>

## できること

- **名前空間内のキーごとに 1 つの主張。** `memory.set` は事実を保存し、`memory.get` はキーを指定して読み出し、`memory.search` と `loom_search` は意味で検索します。名前空間は共有の単位であり、デフォルトでは、メンバー全員がその中のすべての行を読めます。
  [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **訂正できるメモリ。** 変更すると、履歴付きの新しいバージョンが作られます。正規化後に同じになる事実は保存済みの行にマージされ、言い換えただけの同じ事実は重複として保存され、矛盾は確認対象としてフラグが付き、固定（pin）された事実への書き込みは拒否されます。
  [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **答えを控える検索。** 類似度の下限を超えるものがない場合、`memory.search` は理由コード付きで `abstain=true` を返し、弱いヒットにはそれぞれ `below_threshold` のラベルを付けます。`loom_search` はそれらを除外します。[検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **コーディングエージェントの長期メモリを、エージェント間で共有。** Claude Code と Codex のプラグインは、セッション開始時に関連するコンテキストを読み込み、実行中のセッションを記録します。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode、goose、Zed などの MCP クライアントも同じサーバーに接続するため、あるエージェントが保存した事実を他のエージェントが見つけられます。
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [各クライアントの設定](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#other-editors-and-agents)
- **自分のエージェント向けのユーザー別メモリ。** Python と TypeScript の SDK は、エンドユーザーごとにメモリを書き込み、読み出します。[はじめてのエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 計測結果

LongMemEval-S の 100 問のペアで、同じ `gpt-5` をリーダーとして使った場合、Loom が取得したメモリからの回答は正答率 94%、履歴全体からの回答は 90% でした。リーダーのプロンプトトークンは 91% 少なくなっています（メモリ構築の初回コストを除く）。正答率の差は統計的に確立されたものではありません。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## 始める

[クイックスタート](../getting-started/quickstart.md)では、ローカルスタックを起動し、HTTP、Python、MCP で最初のメモリを保存します。[Loom が向かない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)では、インプロセスのメモリライブラリのほうが適しているケースを説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
