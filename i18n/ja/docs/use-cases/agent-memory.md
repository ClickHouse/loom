<!-- i18n: source=docs/use-cases/agent-memory.md blob=7278a12f3a940bfba3a0dead9a5e37fa445bb68a model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AIエージェントのための長期記憶と共有メモリ

Loom は、AI エージェントが共有する長期メモリを提供する、セルフホスト型の MCP サーバーおよび HTTP API です。あるエージェントがセッション中に保存した事実は、次のセッション、別のエージェント、あるいはダッシュボードを見ている人が見つけられます。このページでは、その仕組みと始め方を説明します。

<a id="what-you-get"></a>

## 提供される機能

- **名前空間内でキーごとに 1 つの主張。** `memory.set` は事実を保存し、`memory.get` はキーで読み戻し、`memory.search` と `loom_search` は意味で検索します。名前空間は共有の単位であり、デフォルトでは、メンバー全員がその中のすべての行を読み取れます。
  [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **訂正できるメモリ。** 変更は履歴を伴う新しいバージョンになります。正規化後に同じになる事実は保存済みの行にマージされ、言い換えられた同じ事実は重複として保存され、矛盾はレビュー対象としてフラグが付けられ、固定された事実に対する書き込みは拒否されます。
  [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [履歴と出所](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **棄権する検索。** 類似度の下限を超えるものがない場合、`memory.search` は理由コードとともに `abstain=true` を返し、弱いヒットにはそれぞれ `below_threshold` のラベルを付けます。`loom_search` はそれらを除外します。[検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **コーディングエージェント間のメモリ。** Claude Code と Codex のプラグインは、セッション開始時に関連するコンテキストを読み込み、実行中のセッションを記録します。Cursor などの MCP クライアントも同じサーバーに接続できます。[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **自分のエージェント向けのユーザー単位のメモリ。** Python と TypeScript の SDK は、エンドユーザーごとにメモリを書き込み、読み取ります。[最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 計測結果

同じ `gpt-5` をリーダーに使った、LongMemEval-S の 100 組の質問では、Loom が取得したメモリからの回答のスコアは 94% で、履歴全体からの回答の 90% を上回りました。リーダーのプロンプトトークンは 91% 少なくなっています（メモリを構築する一度きりのコストを除く）。ただし、精度の差は統計的に確立されたものではありません。
[Loom の有無による比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="start"></a>

## 始める

[クイックスタート](../getting-started/quickstart.md) では、ローカルスタックを起動し、HTTP、Python、MCP で最初のメモリを保存します。[Loom が適さない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit) では、インプロセスのメモリライブラリのほうが適しているケースを扱います。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) で experimental と記載されています。
