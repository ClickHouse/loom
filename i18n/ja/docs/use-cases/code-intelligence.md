<!-- i18n: source=docs/use-cases/code-intelligence.md blob=4f52e242dcb612150541843af58b15f37c31a265 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# コーディングエージェント向けのコードインデックス

Loom は、git リポジトリの共有インデックスを MCP 経由でコーディングエージェントに提供します。エージェントは構造に関する質問を投げるだけで、ファイル全体を読み込む代わりに、答えとなる行を受け取れます。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。インデックスの構築方法と鮮度の保ち方は[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)、言語ごとに得られる情報は[言語別のコードインデックス](code-intelligence-by-language.md)を参照してください。

<a id="what-you-get"></a>

## 得られるもの

- **定義、呼び出し元、影響範囲。** `code.symbol`、`code.callers`、`code.impact` は、ファイルと行番号で答えを返し、各回答にはその根拠となるコミットとインデックスのティアが示されます。[コードオペレーション](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **意味による検索と、完全一致の文字列検索。** `code.search` は仕組みがどこにあるかを見つけ、`code.grep` は正確なメッセージや識別子を見つけます。
- **履歴とプルリクエスト。** レビューコメント付きのマージ済み PR 履歴、PR 作成前の重複チェック、リグレッションの bisect 候補を利用できます。これらには PR 履歴の有効化（`LOOM_CODE_PR_HISTORY_ENABLED`）が必要で、レビューコメントとオープン中の PR にはさらに GitHub トークンが必要です。それぞれに必要なものは[履歴、PR、リリース](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md)にまとめています。
- **目で確認できる鮮度。** インデックスはマージ済みコミットの分だけブランチより遅れ、古い回答にはその旨が示されます。[鮮度](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **プラグイン。** Claude Code プラグインは `coding` ツールセットで接続し、Codex プラグインは `core,coding,tracker,tasks` で接続します。どちらもコードツールを一覧に含みます。Cursor などの MCP クライアントは `?toolset=coding` を使います。[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## 計測結果

ClickHouse リポジトリで、Claude Code に各回答の最後に最大 3 つのファイルを挙げるよう指示しました。Loom を使った場合、36 件の概念的な質問（10 回のペア実行）のうち 96.4% で正しいファイルを挙げ、Loom なしの 91.4% を上回りました。また、Loom を使った実行のコストは 21% 低くなりました（エージェントのモデル利用料金であり、Loom 自身のモデル呼び出しは除きます）。
SWE-bench Verified の 200 件の issue では、`code.search` は 82% で変更されたファイルを上位 10 件に含めました（ファイルの特定であり、issue の解決ではありません）。これに対し、インデックス済みの全ファイルに対する BM25 は 57%、`.py` ファイルのみに対する BM25 は 79% でした。`.py` のみとの差は誤差の範囲内です。
[Loom あり・なしの比較](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[SWE-bench Verified のファイル特定](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## はじめに

[リポジトリの追加](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)で、リポジトリのインデックスを有効にします。ティアについては[コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)で説明しています。

このページの一部のオペレーションは、[ステータスページ](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)で experimental と表示されています。
