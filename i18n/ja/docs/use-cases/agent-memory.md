<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI エージェントのための長期メモリと共有メモリ

Loom は、AI エージェントが共有できる長期メモリを提供する、セルフホスト型の MCP サーバーおよび HTTP API です。あるエージェントがセッション中に保存した事実は、次のセッション、別のエージェント、またはダッシュボードを使う人が検索して見つけられます。このページでは、その仕組みと始め方を説明します。

<a id="what-you-get"></a>

## できること

- **名前空間内で、キー1つにつき主張1つ。** `memory.set` はファクトを保存し、`memory.get` はキーで読み戻し、`memory.search` と `loom_search` は意味で検索します。名前空間は共有の単位であり、デフォルトでは、メンバー全員がその中のすべての行を読めます。
  [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **訂正できるメモリ。** 変更すると、履歴を持つ新しいバージョンが作られます。正規化後に同じになるファクトは保存済みの行にマージされ、言い換えただけの同じファクトは重複として保存され、矛盾はレビュー対象としてフラグが付き、固定（pin）されたファクトへの書き込みは拒否されます。
  [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **棄権する検索。** 類似度のしきい値を超えるものがない場合、`memory.search` は理由コード付きで `abstain=true` を返し、弱いヒットにはそれぞれ `below_threshold` のラベルを付けます。`loom_search` はそれらを除外します。 [Search and ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **コーディングエージェント間で共有するメモリ。** Claude Code と Codex のプラグインは、セッション開始時に関連するコンテキストを読み込み、実行中のセッションを記録します。Cursor などの MCP クライアントも同じサーバーに接続します。 [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **自分のエージェント向けのユーザー別メモリ。** Python と TypeScript の SDK は、エンドユーザーごとにメモリを書き込み、読み取ります。 [Your first agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 計測結果

LongMemEval-S の 100 組のペアになった質問に対して、同じ `gpt-5` リーダーを使って比較したところ、Loom が取得したメモリからの回答は正答率 94%、履歴全体からの回答は 90% でした。リーダーのプロンプトトークンは 91% 少なくなっています（メモリを構築する初回のみのコストは除く）。ただし、正答率の差は統計的には確立されていません。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## はじめに

[クイックスタート](../getting-started/quickstart.md) では、ローカルスタックを起動し、HTTP、Python、MCP で最初のメモリを保存します。[When Loom does not fit](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) では、インプロセスのメモリライブラリのほうが適しているケースを説明します。

このページの一部の op は、[status page](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) で experimental と記載されています。
