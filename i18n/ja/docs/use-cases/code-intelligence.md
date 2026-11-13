<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスをコーディングエージェントに提供します。インデックスは MCP 経由で利用でき、エージェントは構造に関する質問を投げるだけで、ファイル全体を読み込む代わりに、答えとなる行を受け取れます。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と最新の状態に保つ方法については、[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

<a id="what-you-get"></a>

## 提供される機能

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行番号を添えて回答し、各回答にはその根拠となるコミットとインデックスのティアが示されます。
  [Code operations](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と、完全一致の文字列検索。** `code.search` は仕組みがどこにあるかを探し、`code.grep` は正確なメッセージや識別子を探します。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR 履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を提供します。これらには PR 履歴を有効にする設定（`LOOM_CODE_PR_HISTORY_ENABLED`）が必要です。レビューコメントとオープン中の PR には GitHub トークンも必要です。
  [History, PRs and releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) に、それぞれの要件が記載されています。
- **確認できる鮮度。** インデックスはブランチに対してマージ済みコミット分だけ遅れ、古い回答にはその旨が示されます。
  [Freshness](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧表示します。Cursor やその他の MCP クライアントは `?toolset=coding` を使用します。
  [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Loom を使った Claude Code は、36 件の概念的な質問（10 回のペア実行）のうち 96.4% で、3 つの候補ファイルの中から正しいファイルを挙げました。Loom を使わない場合は 91.4% でした。モデルコストは 21% 低くなっています（エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは含みません）。
SWE-bench Verified の 200 件の issue では、`code.search` が上位 10 件の結果に変更対象のファイルを含めた割合は 82% でした（ファイルの特定であり、issue の解決ではありません）。比較対象は、インデックス済みの全ファイルに対する BM25 が 57%、`.py` ファイルのみに対する BM25 が 79% です。`.py` のみとの差は誤差の範囲内です。
[With and without Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified file localization](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)を行うと、そのリポジトリでインデックスが有効になります。ティアの説明は[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)を参照してください。

このページの一部の op は、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と記載されています。
