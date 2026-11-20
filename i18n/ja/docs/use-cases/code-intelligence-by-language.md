<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=414b69b208ddf6610590f99678d2dc8e9bff8780 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# C++、Python、TypeScript、Go、Rust、Java、インフラコード向けのコードインテリジェンス

Loom のコードインデックスは、リポジトリに含まれる言語について、コーディングエージェントの構造に関する質問に答えます。シンボルがどこで定義されているか、誰が呼び出しているか、変更が何に影響するかです。リポジトリが追加済みで、`LOOM_CODE_INGEST_ENABLED=true` が設定されている必要があります。どの言語でも高速ティアを使えます。高速ティアは tree-sitter の文法または手書きのスキャナーで解析します。C/C++、Python、TypeScript、Go では、コンパイラと同じ方法で名前を解決するコンパイラティアを追加できます。対応する拡張子とエッジの一覧は[言語サポート表](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)にあり、コンパイラティアのセットアップは[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)にあります。

回答には、その根拠となったティアが示されます（コンパイラティアは `tier: "scip"`、高速ティアは `"treesitter"`）。また `code.coverage` は、名前空間が保持しているデータを言語別・ティア別に報告します。Loom を使ったことのない言語で `code.callers` や `code.impact` の回答を信頼する前に、これを確認してください。

<a id="c-and-c"></a>

## C と C++

シンボル、呼び出し、インクルード、継承、オーバーライドは高速ティアから得られます。コンパイラティア（`scip-clang`）はプロジェクトのビルドからオーバーロード、テンプレート、名前空間を解決します。そのため、`DB::IMergeTreeDataPart::getBytesOnDisk` のような修飾名に対して `uses_brief: true` を指定して `code.symbol` を呼ぶと、唯一の定義、呼び出し箇所の数、主要な呼び出し箇所とそれを囲む関数が返り、それぞれに `tier: "scip"` が付きます。コンパイラティアにはプロジェクトの CMake ビルド設定が必要です。

<a id="python"></a>

## Python

呼び出し、インポート、継承、オーバーライドは高速ティアから得られ、ノートブックのコードセルも含まれます。コンパイラティア（`scip-python`）はモジュールをまたいで名前を解決します。Loom 自身のリポジトリで `writer_actor` に対して `code.symbol` を呼ぶと、`server/memory/app/provenance.py` 内の定義がシグネチャと docstring とともにコンパイラティアで返され、`uses_brief: true` を付けると呼び出し箇所も加わります。

<a id="typescript-and-javascript"></a>

## TypeScript と JavaScript

呼び出し（`new` を含む）、インポート、継承、オーバーライドは高速ティアから得られ、Vue、Svelte、Astro コンポーネントのスクリプトブロックも同様です。Angular の `.component.html` テンプレートは、そのコンポーネントとともに 1 つの単位としてインデックスされます。コンパイラティア（`scip-typescript`）は、プロジェクトの `tsconfig.json`、`jsconfig.json`、`package.json` を通じて名前を解決します。

<a id="go"></a>

## Go

呼び出し、インターフェースの埋め込み、構造的な実装は高速ティアから得られ、Go の文字列内の SQL はテーブル参照になります。コンパイラティア（`scip-go`）は、モノレポを含め、パッケージをまたいで名前を解決します。

<a id="rust-and-java"></a>

## Rust と Java

Rust では呼び出し、`use` インポート、トレイト実装が得られます。Java では、インポートによる名前の束縛つきの呼び出しと、Java の文字列内の SQL が得られます。どちらも高速ティアのみです。同じファイル内で束縛された呼び出しの信頼度は 1.0、外側のクラスを通じた場合は 0.8、リポジトリ全体での名前一致による場合は 0.6 です。束縛できない呼び出しは推測せず除外します。`code.impact` はファイル単位で、対応付けられたテストと共変更の相手を加えます。

<a id="sql-and-migrations"></a>

## SQL とマイグレーション

`.sql` ファイルは、デフォルトで検索と grep の対象としてインデックスされます。`LOOM_CODE_SQL_STATEMENT_RECORDS` を有効にすると、各ステートメントがテーブル参照つきのレコードになり、エージェントはどのマイグレーションやクエリがテーブルを読み書きするかを尋ねられます。すでにインデックス済みの名前空間で有効にする場合は、SQL ファイルに対して `loom code backfill --paths-file` が必要です（[スイッチ](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)）。

<a id="infrastructure-as-code"></a>

## Infrastructure as Code

Terraform と HCL、Kubernetes マニフェスト、Helm チャートと Go テンプレート、Jsonnet、CMake、Protocol Buffers、シェルスクリプトは、参照、インクルード、呼び出しとともにインデックスされます。そのためエージェントは、Helm の `include` からその `define` へ、あるいは Terraform の参照からその参照先へとたどれます。Dockerfile、Makefile、Jenkinsfile、CI YAML のジョブは、`LOOM_CODE_HOST_ISLANDS` を有効にするとシンボルになります。名前空間がすでにインデックス済みの場合は、それらのファイルのバックフィルが必要です。

<a id="start"></a>

## はじめに

1. [リポジトリをインデックスする](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)。どのコンパイラティアが実行されるかは[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)を参照してください。
2. `coding` ツールセットで[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。
3. 構造に関する質問をします。利用する言語については `code.coverage` を確認してください。

<a id="related"></a>

## 関連ドキュメント

- [コーディングエージェント向けのコードインデックス](code-intelligence.md)
- [コード操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [言語サポートの追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
