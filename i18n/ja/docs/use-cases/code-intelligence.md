<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスをコーディングエージェントに MCP 経由で提供します。エージェントはファイル全体を読み込む代わりに、コードの構造に関する質問を投げて、その答えとなる行だけを受け取れます。利用するには、リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と最新の状態に保つ仕組みについては、[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

<a id="what-you-get"></a>

## 得られるもの

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行を添えて答えを返します。各回答には、その根拠となるコミットとインデックスのティアも示されます。
  [Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と、文字列の完全一致による検索。** `code.search` は仕組みの所在を見つけ、`code.grep` は正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR の履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を扱えます。これらには PR 履歴の有効化(`LOOM_CODE_PR_HISTORY_ENABLED`)が必要です。レビューコメントとオープン中の PR には、さらに GitHub トークンも必要です。
  [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) に、それぞれの必要条件が記載されています。
- **確認できる鮮度。** インデックスは、マージ済みコミット分だけブランチより遅れます。古い回答にはその旨が示されます。[Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に含みます。Cursor などの MCP クライアントは `?toolset=coding` を使います。
  [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリにおいて、Loom を併用した Claude Code は、36 件の概念的な質問(10 回のペア実行)のうち 96.4% で、3 つの候補ファイルの中から正しいファイルを挙げました。Loom なしでは 91.4% でした。モデルコストは 21% 低くなっています(エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは含みません)。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で、変更されたファイルを上位 10 件以内にランク付けしました(ファイルの特定であり、issue の解決ではありません)。比較として、インデックス済みの全ファイルに対する BM25 は 57%、`.py` ファイルのみに対する BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を行うと、そのリポジトリのインデックスが有効になります。階層については[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)で説明しています。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
