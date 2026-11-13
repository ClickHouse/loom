<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=797ba5c0830f3cf220afabc07d4a9e4709260b92 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# C++、Python、TypeScript、Go、Rust、Java、インフラストラクチャコード向けのコードインテリジェンス

Loom のコードインデックスは、リポジトリに含まれる各言語について、コーディングエージェントの構造に関する問い（シンボルがどこで定義されているか、誰が呼び出しているか、変更が何に影響するか）に答えます。リポジトリが追加済みで、`LOOM_CODE_INGEST_ENABLED=true` が設定されている必要があります。すべての言語で高速ティアが使え、tree-sitter の文法または手書きのスキャナーで解析します。C/C++、Python、TypeScript、Go では、コンパイラと同じ方法で名前を解決するコンパイラティアを追加できます。対応する拡張子とエッジの一覧は[言語サポート表](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)にあり、コンパイラティアのセットアップは[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)にあります。

回答には、その根拠となったティアが示されます（コンパイラティアは `tier: "scip"`、高速ティアは `"treesitter"`）。また、`code.coverage` は名前空間が保持している内容を言語とティアごとに報告します。Loom を使ったことのない言語で callers や impact を信頼する前に、これを確認してください。

<a id="c-and-c"></a>

## C と C++

シンボル、呼び出し、インクルード、継承、オーバーライドは高速ティアから取得します。コンパイラティア（`scip-clang`）は、プロジェクトのビルドからオーバーロード、テンプレート、名前空間を解決します。そのため、`DB::IMergeTreeDataPart::getBytesOnDisk` のような修飾名に対して `uses_brief: true` を指定して `code.symbol` を呼び出すと、唯一の定義、呼び出し箇所の数、そして上位の呼び出し箇所とそれを囲む関数が返され、それぞれに `tier: "scip"` が付きます。コンパイラティアには、プロジェクトの CMake ビルド設定が必要です。

<a id="python"></a>

## Python

呼び出し、インポート、継承、オーバーライドは高速ティアから取得されます。ノートブックのコードセルも対象です。コンパイラティア（`scip-python`）はモジュールをまたいで名前を解決します。Loom 自身のリポジトリでは、`writer_actor` に対する `code.symbol` は、コンパイラティアで `server/memory/app/provenance.py` にある定義をシグネチャと docstring とともに返し、`uses_brief: true` を指定すると呼び出し箇所も追加されます。

<a id="typescript-and-javascript"></a>

## TypeScript と JavaScript

呼び出し（`new` を含む）、import、継承、オーバーライドは高速ティアから取得されます。Vue、Svelte、Astro コンポーネントの script ブロックも同様です。Angular の `.component.html` テンプレートは、対応するコンポーネントとともに 1 つの単位としてインデックスされます。コンパイラティア（`scip-typescript`）は、プロジェクトの `tsconfig.json`、`jsconfig.json`、`package.json` を通じて名前を解決します。

<a id="go"></a>

## Go

呼び出し、インターフェース埋め込み、構造的な実装は高速ティアから取得され、Go 文字列内の SQL はテーブル参照になります。コンパイラティア（`scip-go`）は、モノレポを含め、パッケージをまたいで名前を解決します。

<a id="rust-and-java"></a>

## RustとJava

Rustでは、呼び出し、`use` インポート、トレイト実装が対象になります。Javaでは、呼び出し（インポートが名前を束縛します）と、Javaの文字列内のSQLが対象になります。どちらも高速ティアのみです。同じファイル内で束縛された呼び出しの信頼度は 1.0、外側のクラスを介して束縛された場合は 0.8、リポジトリ全体での名前一致による場合は 0.6 です。束縛できない呼び出しは推測せずに除外され、`code.impact` はマップされたテストと co-change パートナーをファイル単位で追加します。

<a id="sql-and-migrations"></a>

## SQL とマイグレーション

`.sql` ファイルは、デフォルトで検索と grep のためにインデックスされます。`LOOM_CODE_SQL_STATEMENT_RECORDS` を有効にすると、各ステートメントがテーブル参照を持つレコードになり、エージェントはどのマイグレーションやクエリがテーブルを読み書きするかを問い合わせられます。すでにインデックス済みの名前空間で有効にするには、SQL ファイルを対象にした `loom code backfill --paths-file` が必要です（[スイッチ](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)）。

<a id="infrastructure-as-code"></a>

## コードとしてのインフラ

Terraform と HCL、Kubernetes マニフェスト、Helm チャートと Go テンプレート、Jsonnet、CMake、Protocol Buffers、シェルスクリプトは、参照、include、呼び出しとともにインデックスされます。そのためエージェントは、Helm の `include` からその `define` へ、また Terraform の参照からそれが指す対象へたどることができます。Dockerfile、Makefile、Jenkinsfile、CI YAML のジョブは、`LOOM_CODE_HOST_ISLANDS` を有効にするとシンボルになります。名前空間がすでにインデックス済みの場合は、これらのファイルのバックフィルを行った後に反映されます。

<a id="start"></a>

## はじめに

1. [リポジトリをインデックスする](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)。どのコンパイラティアが実行されるかについては、[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)を参照してください。
2. `coding` ツールセットで[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。
3. 構造に関する質問をします。使用している言語については `code.coverage` を確認してください。

<a id="related"></a>

## 関連ドキュメント

- [コーディングエージェント向けのコードインデックス](code-intelligence.md)
- [コード操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [言語サポートの追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
