<!-- i18n: source=docs/use-cases/code-intelligence.md blob=af7c5d7d9b5c5333fc328a23fcfef0116c9b06ce model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスを MCP 経由でコーディングエージェントに提供します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読まなくても、答えとなる行を得られます。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と最新に保つ方法は [コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) に、言語ごとに得られる内容は [言語別のコードインデックス](code-intelligence-by-language.md) にあります。

<a id="what-you-get"></a>

## 得られるもの

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行を添えて答えます。各回答には、根拠となるコミットとインデックスのティアも示されます。[コード操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と完全一致の文字列検索。** `code.search` は仕組みが存在する場所を見つけ、`code.grep` は正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR 履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらには PR 履歴を有効にする設定（`LOOM_CODE_PR_HISTORY_ENABLED`）が必要です。レビューコメントとオープンな PR には GitHub トークンも必要です。それぞれに必要なものは [履歴、PR、リリース](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) を参照してください。
- **確認できる鮮度。** インデックスはマージ済みコミットの分だけブランチに遅れ、古い回答にはその旨が示されます。[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグインと MCP クライアント。** Claude Code プラグインは `coding` ツールセットで、Codex プラグインは `core,coding,tracker,tasks` で接続し、どちらもコードツールを一覧表示します。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode などの MCP クライアントは `?toolset=coding` を使います。
  [コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [各クライアントの設定](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md#other-editors-and-agents)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の末尾で最大 3 つのファイルを挙げるよう指示しました。Loom を使った場合、概念に関する 36 問（10 回のペア実行）のうち 96.4% で正しいファイルを挙げました。Loom なしでは 91.4% で、Loom ありの実行はコストが 21% 低くなりました（エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは含みません）。
SWE-bench Verified の 200 件の課題では、`code.search` は 82% で変更対象のファイルを上位 10 件に入れました（課題の解決ではなく、ファイルの特定です）。比較として、インデックス済みの全ファイルに対する BM25 は 57%、`.py` ファイルのみに対する BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。
[Loom ありとなし](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) で、リポジトリのインデックスを有効にします。ティアについては [コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) で説明しています。

このページの一部の操作は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) で experimental と表示されています。
