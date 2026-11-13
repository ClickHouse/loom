<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=ecd38c904b18ebd717798fb62bc55f139071429b model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# C++、Python、TypeScript、Go、Rust、Java、インフラコードのコードインテリジェンス

Loom のコードインデックスは、リポジトリに含まれる言語について、コーディングエージェントの構造に関する質問に答えます。シンボルがどこで定義されているか、誰が呼び出しているか、変更が何に影響するかです。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定する必要があります。すべての言語で高速ティアを利用できます。これは tree-sitter 文法または手書きのスキャナーで解析します。C/C++、Python、TypeScript、Go では、コンパイラと同じ方法で名前を解決するコンパイラティアを追加できます。拡張子とエッジの完全な一覧は[言語サポート表](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)にあり、コンパイラティアのセットアップは[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)にあります。

回答には、その根拠となったティアが示されます（コンパイラティアは `tier: "scip"`、高速ティアは `"treesitter"`）。また、`code.coverage` は名前空間が言語とティアごとに何を保持しているかを報告します。Loom を使ったことのない言語で呼び出し元や影響範囲を信頼する前に、これを確認してください。

<a id="c-and-c"></a>

## C と C++

シンボル、呼び出し、インクルード、継承、オーバーライドは高速ティアから得られます。コンパイラティア（`scip-clang`）は、プロジェクトのビルドからオーバーロード、テンプレート、名前空間を解決します。そのため、`DB::IMergeTreeDataPart::getBytesOnDisk` のような修飾名に対して `uses_brief: true` を指定して `code.symbol` を呼ぶと、唯一の定義、呼び出し箇所の数、そして上位の呼び出し箇所がそれを囲む関数とともに返されます。それぞれに `tier: "scip"` が付きます。コンパイラティアにはプロジェクトの CMake ビルド設定が必要です。

<a id="python"></a>

## Python

呼び出し、インポート、継承、オーバーライドは高速ティアから得られ、ノートブックのコードセルも含まれます。コンパイラティア（`scip-python`）はモジュールをまたいで名前を解決します。Loom 自身のリポジトリでは、`writer_actor` に対して `code.symbol` を呼ぶと、`server/memory/app/provenance.py` にある定義がシグネチャと docstring とともにコンパイラティアで返され、`uses_brief: true` を指定すると呼び出し箇所も加わります。

<a id="typescript-and-javascript"></a>

## TypeScript と JavaScript

呼び出し（`new` を含む）、インポート、継承、オーバーライドは高速ティアから得られ、Vue、Svelte、Astro コンポーネントのスクリプトブロックも同様です。Angular の `.component.html` テンプレートは、そのコンポーネントとともに 1 つの単位としてインデックスされます。コンパイラティア（`scip-typescript`）は、プロジェクトの `tsconfig.json`、`jsconfig.json`、`package.json` を通じて名前を解決します。

<a id="go"></a>

## Go

呼び出し、インターフェースの埋め込み、構造的な実装は高速ティアから得られ、Go の文字列内の SQL はテーブル参照になります。コンパイラティア（`scip-go`）は、モノレポを含め、パッケージをまたいで名前を解決します。

<a id="rust-and-java"></a>

## Rust と Java

Rust では呼び出し、`use` インポート、trait 実装が得られます。Java では、インポートで名前を束縛した呼び出しと、Java 文字列内の SQL が得られます。どちらも高速ティアのみです。同じファイル内で束縛された呼び出しの信頼度は 1.0、囲んでいるクラス経由では 0.8、リポジトリ全体の名前一致では 0.6 です。束縛できない呼び出しは推測せずに除外され、`code.impact` はファイル単位で、対応付けられたテストと共変更のパートナーを追加します。

<a id="sql-and-migrations"></a>

## SQL とマイグレーション

`.sql` ファイルは、デフォルトで検索と grep のためにインデックスされます。`LOOM_CODE_SQL_STATEMENT_RECORDS` を有効にすると、各文がテーブル参照を持つレコードになるため、エージェントはどのマイグレーションやクエリがテーブルを読み書きするかを尋ねられます。すでにインデックス済みの名前空間で有効にするには、SQL ファイルに対して `loom code backfill --paths-file` を実行する必要があります（[スイッチ](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)）。

<a id="infrastructure-as-code"></a>

## Infrastructure as code（インフラストラクチャのコード化）

Terraform と HCL、Kubernetes マニフェスト、Helm チャートと Go テンプレート、Jsonnet、CMake、Protocol Buffers、シェルスクリプトは、参照、インクルード、呼び出しとともにインデックスされます。そのため、エージェントは Helm の `include` からその `define` へ、あるいは Terraform の参照からその参照先へたどれます。Dockerfile、Makefile、Jenkinsfile、CI YAML のジョブは、`LOOM_CODE_HOST_ISLANDS` を有効にするとシンボルになります。名前空間がすでにインデックス済みの場合は、これらのファイルのバックフィルが必要です。

<a id="start"></a>

## はじめに

1. [リポジトリをインデックスする](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)。どのコンパイラティアが実行されるかは[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)にあります。
2. `coding` ツールセットで[コーディングエージェントを接続する](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。
3. 構造に関する質問をします。頼りにする言語については `code.coverage` を確認してください。

<a id="related"></a>

## 関連項目

- [コーディングエージェント向けのコードインデックス](code-intelligence.md)
- [コード操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [言語サポートの追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
