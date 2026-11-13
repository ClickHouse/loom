<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスをコーディングエージェントに MCP 経由で提供します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読み込む代わりに、その答えとなる行を受け取れます。利用するには、リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。インデックスの構築方法と最新状態の維持方法については、[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

<a id="what-you-get"></a>

## できること

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行を添えて回答し、各回答にはその根拠となるコミットとインデックスの tier が示されます。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と、完全一致の文字列検索。** `code.search` はある仕組みがどこにあるかを見つけ、`code.grep` は正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらを使うには PR 履歴を有効にする必要があります（`LOOM_CODE_PR_HISTORY_ENABLED`）。レビューコメントとオープンな PR には、さらに GitHub のトークンが必要です。それぞれに何が必要かは [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) を参照してください。
- **確認できる鮮度。** インデックスはブランチに対してマージ済みコミット分だけ遅れ、古い回答はその旨を明示します。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` toolset で接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に表示します。Cursor やその他の MCP クライアントでは `?toolset=coding` を使います。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリでは、Loom を使った Claude Code は、概念的な質問 36 問（ペアでの実行 10 回）のうち 96.4% で、3 つのファイルの中から正しいファイルを挙げました。Loom を使わない場合は 91.4% でした。モデルコストは 21% 低くなっています（エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは除きます）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で、変更されたファイルを上位 10 件の結果に含めていました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス済みの全ファイルに対する BM25 は 57%、`.py` ファイルのみに対する BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified file localization](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を行うと、そのリポジトリでインデックスが有効になります。ティアについては[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)で説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と表示されています。
