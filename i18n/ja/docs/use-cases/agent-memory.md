<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AIエージェントのための長期メモリと共有メモリ

Loom は、AI エージェントが共有できる長期メモリを提供する、セルフホスト型の MCP サーバーおよび HTTP API です。あるエージェントがセッション中に保存した事実は、次のセッション、別のエージェント、またはダッシュボードを使う人が見つけられます。このページでは、その仕組みと始め方を説明します。

<a id="what-you-get"></a>

## できること

- **名前空間内でキーごとに 1 つの主張。** `memory.set` はファクトを保存し、`memory.get` はキーを指定して読み戻し、`memory.search` と `loom_search` は意味で検索します。名前空間は共有の単位です。デフォルトでは、メンバー全員が名前空間内のすべての行を読み取れます。
  [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **訂正できるメモリ。** 変更を加えると、履歴を持つ新しいバージョンが作られます。正規化後に同一になるファクトは保存済みの行にマージされ、言い換えただけの同じファクトは重複として保存され、矛盾はレビュー対象としてフラグが付き、固定されたファクトへの書き込みは拒否されます。
  [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [履歴と来歴](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **回答を控える検索。** 類似度の下限を超えるものがない場合、`memory.search` は理由コード付きで `abstain=true` を返し、弱いヒットにはそれぞれ `below_threshold` のラベルを付けます。`loom_search` はそれらを除外します。 [検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **コーディングエージェント間で共有するメモリ。** Claude Code と Codex のプラグインは、セッション開始時に関連するコンテキストを読み込み、セッションの実行中にその内容を記録します。Cursor などの MCP クライアントも同じサーバーに接続できます。 [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **独自のエージェント向けのユーザー単位のメモリ。** Python と TypeScript の SDK で、エンドユーザーごとにメモリを書き込み、読み取れます。 [はじめてのエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 計測結果

LongMemEval-S の 100 組の対応する質問に対し、同じ `gpt-5` リーダーを使って評価したところ、Loom が取得したメモリからの回答は正答率 94%、履歴全体からの回答は 90% でした。リーダーのプロンプトトークンは 91% 少なくなっています（メモリを構築する際の一度限りのコストを除く）。ただし、正答率の差は統計的に確定したものではありません。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## はじめに

[Quickstart](../getting-started/quickstart.md) では、ローカルスタックを起動し、HTTP、Python、MCP で最初のメモリを保存します。[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)では、インプロセスのメモリライブラリのほうが適しているケースを説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と表示されています。
