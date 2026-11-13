<!-- i18n: source=docs/use-cases/code-intelligence.md blob=71addec3496d26800b8e03234eb0c0adc578b52c model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスを MCP 経由でコーディングエージェントに提供します。エージェントはファイル全体を読む代わりに、構造に関する質問を投げ、その答えとなる行を受け取ります。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。インデックスの構築方法と鮮度の保ち方は[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)に、各言語で何が得られるかは[言語別のコードインデックス](code-intelligence-by-language.md)にあります。

<a id="what-you-get"></a>

## 得られるもの

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行を答えとして返し、その答えの根拠となるコミットとインデックスのティアも示します。[コード操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と完全一致の検索。** `code.search` は仕組みが実装されている場所を見つけ、`code.grep` は正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR 履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を扱います。これらには PR 履歴の有効化（`LOOM_CODE_PR_HISTORY_ENABLED`）が必要で、レビューコメントとオープン中の PR には GitHub トークンも必要です。それぞれに必要なものは[履歴、PR、リリース](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)にあります。
- **目で確認できる鮮度。** インデックスはブランチに対してマージ済みコミットの分だけ遅れ、古い答えにはその旨が示されます。[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグインと MCP クライアント。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に表示します。Cursor、GitHub Copilot、Gemini CLI、Cline、Kilo Code、OpenCode などの MCP クライアントは、URL に `?toolset=coding` を付けるとコードツールを一覧に表示します（Cursor には `loom install` が付与し、設定を書き込むそれ以外のクライアントには `--toolset coding` で付与します）。[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) · [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の末尾で最大 3 つのファイルを挙げるよう求めました。Loom ありでは、36 件の概念的な質問（10 回のペア実行）の 96.4% で正しいファイルを挙げ、Loom なしの 91.4% を上回りました。また、Loom ありの実行はコストが 21% 低くなりました（エージェントのモデル料金。Loom 自身のモデル呼び出しは除く）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で変更されたファイルを上位 10 件に入れました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス済みの全ファイルに対する BM25 は 57%、`.py` ファイルのみに対する BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。[Loom ありとなし](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) · [SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## 始める

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)で、リポジトリのインデックスを有効にします。ティアの説明は[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)にあります。

このページの一部の操作は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で実験的と記されています。
