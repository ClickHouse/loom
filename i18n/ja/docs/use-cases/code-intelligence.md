<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスをコーディングエージェントに MCP 経由で提供します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読み込まなくても、その答えとなる行を受け取れます。利用するには、リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。インデックスの構築方法と最新の状態に保つ仕組みについては、[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

<a id="what-you-get"></a>

## 提供される機能

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行番号を添えて回答します。各回答には、その根拠となるコミットとインデックスの階層も示されます。[Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と完全一致による検索。** `code.search` は仕組みが実装されている場所を探し、`code.grep` は正確なメッセージや識別子を探します。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらを使うには PR 履歴を有効にする必要があります（`LOOM_CODE_PR_HISTORY_ENABLED`）。レビューコメントとオープンな PR には、GitHub トークンも必要です。それぞれに何が必要かは [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) を参照してください。
- **確認できる鮮度。** インデックスはマージ済みのコミット分だけブランチより遅れます。古い回答にはその旨が示されます。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に表示します。Cursor やその他の MCP クライアントは `?toolset=coding` を使います。[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリでは、Loom を使った Claude Code は、36 件の概念的な質問（ペア実行 10 回）のうち 96.4% で、3 つの候補の中から正しいファイルを挙げました。Loom を使わない場合は 91.4% で、モデルコストは 21% 低くなりました（エージェントのモデル利用料のみで、Loom 自身のモデル呼び出しは含みません）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で、変更されたファイルを上位 10 件の結果に含めました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス済みの全ファイルを対象にした BM25 は 57%、`.py` ファイルのみを対象にした BM25 は 79% でした。`.py` のみの場合との差は誤差の範囲内です。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を行うと、そのリポジトリでインデックスが有効になります。階層については[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

このページの op の一部は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記されています。
