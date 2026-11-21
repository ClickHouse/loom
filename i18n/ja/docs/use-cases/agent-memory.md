<!-- i18n: source=docs/use-cases/agent-memory.md blob=e9322c4d7b4b3395943f3caead94d6436cd0954b model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# AI エージェントのための長期メモリと共有メモリ

Loom は、AI エージェントが共有できる長期メモリを提供する、セルフホスト型の MCP サーバーおよび HTTP API です。あるエージェントがセッションで保存した事実は、次のセッション、別のエージェント、またはダッシュボードを使う人が見つけられます。このページでは、その仕組みと始め方を説明します。

<a id="what-you-get"></a>

## 利用できる機能

- **名前空間内でキーごとに 1 つの主張。** `memory.set` は事実を保存し、`memory.get` はキーで読み出し、`memory.search` と `loom_search` は意味で検索します。名前空間は共有の単位であり、デフォルトでは、メンバー全員がその中のすべての行を読めます。
  [メモリ](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **訂正できるメモリ。** 変更すると履歴付きの新しいバージョンが作られます。正規化後に同じになる事実は保存済みの行にマージされ、言い換えただけの同じ事実は重複として保存され、矛盾は確認対象としてフラグが付き、固定された事実への書き込みは拒否されます。
  [競合と拒否](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [履歴と出所](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **回答を控える検索。** 類似度の下限を超えるものがない場合、`memory.search` は理由コード付きで `abstain=true` を返し、弱いヒットには `below_threshold` のラベルを付けます。`loom_search` はそれらを除外します。[検索と質問](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **コーディングエージェント向けの長期メモリを、エージェント間で共有。** Claude Code と Codex のプラグインは、セッション開始時に関連するコンテキストを読み込み、実行中のセッションを記録します。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode、goose、Zed などの MCP クライアントも同じサーバーに接続でき、名前空間上のエージェントは、他のエージェントがそこに保存した事実を見つけられます。
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **対象のコードに紐づくメモリ。** コーディングエージェントが得た教訓は、名前を挙げた定義にひも付き、リネームや編集があっても追従し、エージェントがそのコードを読むときに表示されます。[コードに紐づくメモリ](memory-linked-to-code.md)
- **自分のエージェント向けのユーザー別メモリ。** Python と TypeScript の SDK で、エンドユーザーごとにメモリを書き込み、読み出せます。[最初のエージェント](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## 計測結果

LongMemEval-S の 100 組の質問で、同じ `gpt-5` リーダーを使った場合、Loom が取得したメモリに基づく回答のスコアは 94% で、履歴全体に基づく回答の 90% を上回りました。リーダーのプロンプトトークンは 91% 少なくなっています（メモリの構築にかかる 1 回限りのコストは除く）。ただし、精度の差は統計的に確立されたものではありません。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="a-research-assistant-that-remembers-your-reading-preferences"></a>

## 閲覧の好みを覚えているリサーチアシスタント

[クイックスタート](../getting-started/quickstart.md)の後、ダッシュボードの[クライアント設定](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#how-do-i-give-cursor-or-another-mcp-client-shared-memory)から Claude Desktop または Cursor を接続します。ツールは標準の LLM で動作します。ツールを呼び出すモデルは、お使いのチャットアプリが提供します。

1. アシスタントに次のように伝えます。「`loom_memory_set` を使って、私のリサーチの好みを `example/research/source-policy` に保存してください。査読済み論文を閲覧リストに含めます。」
2. ツールのレスポンスに `memory_id` が含まれていることを確認します。ダッシュボードの **Memories** を開き、値と書き込み元を確認します。ツールが書き込みを拒否した場合は、報告された問題を解決してから先に進んでください。
3. 新しいチャットを開始します。アシスタントに次のように伝えます。「`loom_memory_get` を使って `example/research/source-policy` を読み出し、私の閲覧リストに入れるべき情報源を教えてください。」ツールが `found: true` と、保存した好みを返すことを確認します。
4. 終わったら、アシスタントにこのサンプルのキーを削除するよう依頼し、ダッシュボードで有効なメモリとして表示されなくなったことを確認します。

2 つ目のチャットは、1 つ目のチャットのやり取りを使わずに、保存された好みを読み出します。これは永続性とクライアントの接続を確認するもので、アシスタントが論文を評価できるか、リサーチの質問に答えられるかを試すものではありません。論文そのものを検索する場合は、[ドキュメント検索](document-search.md)を使ってください。

<a id="start"></a>

## 始める

[クイックスタート](../getting-started/quickstart.md)では、ローカルのスタックを起動し、HTTP、Python、MCP で最初のメモリを保存します。[Loom が向かない場合](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)では、インプロセスのメモリライブラリのほうが適しているケースを扱っています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で試験的と記載されています。

[Microsoft Agent Framework のコンテキストプロバイダー](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)を使うと、実行前にユーザーの事実を呼び出し、実行後に会話を記録できます。

[Claude API のメモリツール](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#claude-api-memory-tool)のファイルツリーを Loom に保存すると、名前空間内のエージェントがその内容を共有できます。

[OpenAI Agents のセッション履歴](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#openai-agents-sdk)を Loom に保持すると、エージェントの実行やハンドオフをまたいで引き継げます。

ビジュアルなワークフローには、[n8n ノード](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)で、AI Agent を Loom の会話履歴、永続メモリ、ナレッジ検索に接続できます。
