<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=0ed7353714a116300c522e3520e5b1986b56ca81 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# C++、Python、TypeScript、Go、Rust、Java、インフラコード向けのコードインテリジェンス

Loom のコードインデックスは、リポジトリに含まれる言語について、コーディングエージェントの構造に関する問い（シンボルがどこで定義されているか、誰が呼び出しているか、変更が何に影響するか）に答えます。リポジトリを追加し、`LOOM_CODE_INGEST_ENABLED=true` を設定しておく必要があります。すべての言語で高速ティアを利用でき、tree-sitter の文法または手書きのスキャナーで解析します。C/C++、Python、TypeScript、Go では、コンパイラと同じ方法で名前を解決するコンパイラティアを追加できます。対応する拡張子とエッジの全一覧は[言語サポート表](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)にあり、コンパイラティアのセットアップは[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)にあります。

回答には、その根拠となったティアが示されます（コンパイラティアは `tier: "scip"`、高速ティアは `"treesitter"`）。また、`code.coverage` は名前空間が保持する内容を言語ごと、ティアごとに報告します。Loom を使ったことのない言語で呼び出し元や影響範囲を信頼する前に、この結果を確認してください。

<a id="c-and-c"></a>

## C と C++

シンボル、呼び出し、include、継承、オーバーライドは高速ティアから取得します。コンパイラティア（`scip-clang`）は、プロジェクトのビルドからオーバーロード、テンプレート、名前空間を解決します。そのため、`DB::IMergeTreeDataPart::getBytesOnDisk` のような修飾名に対して `uses_brief: true` を指定して `code.symbol` を呼び出すと、唯一の定義、呼び出し箇所の数、およびそれを囲む関数を含む主要な呼び出し箇所が返され、それぞれに `tier: "scip"` が付きます。コンパイラティアには、プロジェクトの CMake ビルド設定が必要です。

<a id="python"></a>

## Python

呼び出し、import、継承、オーバーライドは高速ティアから取得されます。ノートブックのコードセルも対象です。コンパイラティア（`scip-python`）は、モジュールをまたいで名前を解決します。Loom 自身のリポジトリでは、`writer_actor` に対する `code.symbol` が、コンパイラティアで `server/memory/app/provenance.py` 内の定義をシグネチャと docstring 付きで返し、`uses_brief: true` を指定すると呼び出し箇所も追加されます。

<a id="typescript-and-javascript"></a>

## TypeScript と JavaScript

呼び出し（`new` を含む）、import、継承、オーバーライドは高速ティアから取得されます。Vue、Svelte、Astro コンポーネントの script ブロックも同様です。Angular の `.component.html` テンプレートは、対応するコンポーネントとともに 1 つの単位としてインデックスされます。コンパイラティア（`scip-typescript`）は、プロジェクトの `tsconfig.json`、`jsconfig.json`、`package.json` を通じて名前を解決します。

<a id="go"></a>

## Go

呼び出し、インターフェースの埋め込み、構造的な実装は高速ティアから取得され、Go 文字列内の SQL はテーブル参照になります。コンパイラティア（`scip-go`）は、モノレポを含め、パッケージをまたいで名前を解決します。

<a id="rust-and-java"></a>

## Rust と Java

Rust では、呼び出し、`use` によるインポート、トレイト実装を扱います。Java では、インポートによる名前の束縛を伴う呼び出しと、Java 文字列内の SQL を扱います。どちらも高速ティアのみに対応しています。同じファイル内で束縛された呼び出しの confidence は 1.0、外側のクラスを経由した場合は 0.8、リポジトリ全体での名前一致による場合は 0.6 です。束縛できない呼び出しは推測せずに除外します。`code.impact` は、対応付けられたテストと、ファイル単位での共変更パートナーを追加します。

<a id="sql-and-migrations"></a>

## SQL とマイグレーション

`.sql` ファイルは、デフォルトで検索と grep 用にインデックスされます。`LOOM_CODE_SQL_STATEMENT_RECORDS` を有効にすると、各文がテーブル参照を持つレコードになるため、エージェントはどのマイグレーションやクエリがテーブルを読み書きしているかを問い合わせられます。すでにインデックス済みの名前空間で有効にするには、SQL ファイルに対して `loom code backfill --paths-file` を実行する必要があります（[スイッチ](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)）。

<a id="infrastructure-as-code"></a>

## コードとしてのインフラ

Terraform と HCL、Kubernetes マニフェスト、Helm チャートと Go テンプレート、Jsonnet、CMake、Protocol Buffers、シェルスクリプトは、参照、インクルード、呼び出しとともにインデックスされます。そのため、エージェントは Helm の `include` からその `define` へ、Terraform の参照からそれが指すものへとたどれます。Dockerfile、Makefile、Jenkinsfile、CI YAML のジョブは、`LOOM_CODE_HOST_ISLANDS` を有効にするとシンボルになります。名前空間がすでにインデックス済みの場合は、これらのファイルを backfill した後に反映されます。

<a id="start"></a>

## はじめに

1. [リポジトリをインデックスします](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)。リポジトリにビルドファイルがある場合、コンパイラ階層が起動します。
2. `coding` ツールセットで[コーディングエージェントを接続します](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)。
3. 構造に関する質問をして、利用している言語について `code.coverage` を確認します。

<a id="related"></a>

## 関連情報

- [コーディングエージェント向けのコードインデックス](code-intelligence.md)
- [コード操作](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [言語サポートの追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
