<!-- i18n: source=docs/development/adding-language-support.md blob=ee2dee0c86ab7bfb46f22017d1d71ab8005a2276 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md), which is authoritative.

<a id="add-language-support-to-the-code-index"></a>

# コードインデックスへの言語サポートの追加

コードインデックスは、リポジトリを 2 つのティアで読み取ります。

- **高速ティア**（tree-sitter 文法と手書きのスキャナ）は、すべてのコミットですべてのファイルに対して実行されます。チャンク、シンボル、コールエッジを生成します。
- **コンパイラティア**（変更されたファイルに対する SCIP インデクサ）は、インデクサが存在する場合に、コンパイラが保証する正確なシンボルとエッジを追加します。

高速ティアにその言語用のレーンがあれば、その言語はサポートされているとみなされます。[コードインデックス](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)では、各ティアの説明と、言語ごとに得られる情報の一覧を示しています。

<a id="the-one-rule-stay-outside-the-extractor-stamp"></a>

## 唯一のルール：エクストラクタスタンプに触れない

高速ティアの行には、その行を生成したコードのバージョンスタンプが付きます（`server/code/domain/extract/extractor_stamp.py`）。

- スタンプは、`STAGE_MODULES` の全モジュールの**コードトークン**（`logic_tokens`：コメント、空行、docstring を含まない。import は、パッケージパスではなく束縛される名前として数える）と、`STAMPED_GRAMMARS`（`tree-sitter`、`tree-sitter-cpp`、`tree-sitter-python`、`tree-sitter-bash`）にあるグラマーのインストール済みバージョンに対する sha256 です。
- スタンプが変わると、ingest ワーカーは、対象の言語ファミリーに属する、インデックス済みの全名前空間の全ファイルについて、変更されたステージ（`chunk`、`symbol`、`edge`、`guard`）を一定のペースでバッチ処理して再導出します（`server/code/app/index/ingest_pass.py` の `_maybe_rechunk`）。
- `MODULE_LANGUAGES` で1つのファミリーに対応付けられたモジュール（`lang_python.py`、`ts_*` モジュール）は、そのファミリーのファイルだけを対象にします。共有モジュール（`treesitter.py`、`chunker.py`）やコアの `tree-sitter` グラマーが変わると、全ファイルが対象になります。
- 大規模なデプロイでは、ingest に数時間かかります。

新しい言語は、スタンプが変わらない場所に追加します。

| ファイル                                                       | スタンプ対象か | 置くもの                                                                                                    |
| -------------------------------------------------------------- | -------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/code/domain/extract/lang_<lang>.py`                    | いいえ         | レーン：シンボル、include、呼び出し箇所を含む `parse_<lang>(path, source) -> ParsedFile`。                  |
| `server/code/domain/extract/lang_detect.py`                    | いいえ         | `LANG_BY_EXTENSION`（およびシェルのルール）：どのファイルがこの言語かを定義します。                         |
| `server/code/app/index/ingest_parse.py`                        | いいえ         | `_PARSERS` 内の `_parse_<lang>` ラッパー。レーンに import がある場合は `ingest_resolve.py` に include 解決を追加します。 |
| `server/code/domain/extract/lane_units.py`                     | いいえ         | グラマーを使わないレーン向けの共有ヘルパー：`span_chunks` と、参照バインダーの `bind_sites`。               |
| `server/code/domain/extract/treesitter.py`                     | **はい**       | スタンプ対象のコア言語専用の `lang_for_path`。ここに拡張子を追加すると、全名前空間が再チャンク化されます。  |
| `chunker.py`、`edges_treesitter.py`、その他の `STAGE_MODULES`  | **はい**       | 共有のチャンク化とエッジ抽出。全員に対して抽出そのものが変わる場合にのみ変更してください。                  |

`lang_for_path` に挙がっていない言語は `lang_detect.py` で検出されるため、追加してもスタンプは変わりません。スタンプ対象モジュールのコメントと docstring は自由に編集できます。これらのモジュールはフォーマットの対象外です。フォーマッタが文字列の引用符を書き換える可能性があり、引用符はコードトークンだからです。

`server/code/domain/extract/` 配下のすべてのモジュールは、`STAGE_MODULES` のいずれかのステージに含まれるか、`tests/unit/test_code_extractor_stamp.py` の `NON_EXTRACTOR_MODULES` に理由付きで記載されている必要があります。新しいレーンモジュールは `NON_EXTRACTOR_MODULES` に追加してください。追加しないと、そのテストが失敗します。

<a id="1-choose-the-lanes-shape"></a>

## 1. レーンの形を選ぶ

| 形 | 例 | 用意するもの |
| ----------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Grammar lane（文法レーン） | `lang_typescript.py`, `lang_rust.py`, `lang_go.py`, `lang_java.py` | `pyproject.toml` に tree-sitter 文法の依存関係を追加します。シンボルと呼び出し箇所はツリーから取得します。チャンクとエッジは共有のチャンカーとエッジウォーカーが生成します。 |
| Tree-less lane（ツリーなしレーン） | `lang_proto.py`, `lang_gotemplate.py`, `lang_hcl.py`, `lang_k8s.py`, `lang_jsonnet.py` (`LANE_UNIT_LANGS`) | 手書きのスキャナー。`ParsedFile.tree` は `None` のままです。チャンクは `lane_units.span_chunks` から、エッジは `reference_sites` と `SITE_RULES` を `lane_units.bind_sites` で結び付けて生成します。 |
| Include-only lane（インクルード専用レーン） | `tla`, `flatbuffers`, `cmake`, `doc` (`_LANE_INCLUDE_LANGS`) | シンボルとインクルードを、シンプルなレーンリゾルバーで解決します。 |
| Embedded language（埋め込み言語） | Go と Java の中の SQL (`go_sql_units`, `java_sql_units`)、Dockerfile や Makefile の中のシェル領域 | ホストファイルのパースに結合されるユニット (`_join_island_symbols`)。通常はキルスイッチ設定の背後に置きます。 |
| Chunk-only（チャンクのみ） | `.css`、テキストファイル (`lang_text.py`) | 検出のみです。ファイルはチャンク化されて検索できますが、シンボルはありません。 |

文法レーンの文法は `STAMPED_GRAMMARS` に**含まれない**ため、アップグレードしてもスタンプは動きません。行の再導出はレーン自身の責任です。

<a id="2-write-the-parser"></a>

## 2. パーサーを書く

`parse_<lang>(path: str, source: bytes) -> ParsedFile` は、言語タグ、シンボル（`treesitter.Symbol`：修飾名、種別、範囲、シグネチャ）、インクルード、およびエッジ段階が必要とする情報を返します。最も近い既存のレーンに従ってください。

- **修飾名**は、その言語のモジュールパスに従います（Rust の場合：ファイルパスから得られるクレートパス。`mod.rs` / `lib.rs` はそのディレクトリの名前になります）。シンボル ID は修飾名と正規化されたシグネチャのハッシュなので、名前が不安定だと再インデックスのたびに行が入れ替わります。
- **インクルード**は、名前空間が保持するパス（`wanted_paths`、続いてレーン内の `resolve_imports` / `resolve_uses`。`ingest_resolve.py` から呼び出されます）に対して解決します。外部パッケージは推測で解決せず、何にも解決しません。
- **呼び出しエッジ**は、まず同一ファイル内で一意な名前を結び付け、次にリポジトリ内で一意な名前をより低い信頼度で結び付けます。名前が曖昧な場合は何も結び付けません（ツリーを持たないレーンでは `lane_units.bind_sites` がこれを行います）。
- **コンテナ種別**（クラス、impl ブロック）はレーンに属します。チャンカーの言語別テーブルに登録すると `chunker.py` が変更され、スタンプが変わります。

レーンを登録します。

```python
# server/code/app/index/ingest_parse.py
def _parse_rust(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust(path, source)


_PARSERS = {
    ...
    "rust": _parse_rust,
    ...
}
```

そして、`LANG_BY_EXTENSION` に拡張子（`".rs": "rust"`）を追加します。ラッパーはレーンを遅延インポートするため、オプションの文法はオプションのままです。新しい tree-sitter 文法は `bounded_parse._LOADERS` にも追加します。すべてのパースは最初にここを通ります。

<a id="3-test-the-lane"></a>

## 3. レーンをテストする

- **レーンテスト**（`tests/unit/test_code_lang_<lang>.py`）は、小さなインラインのソースをパースし、名前、種別、スパン、include、エッジを検証します。グラマーがない場合はスキップします：`pytest.importorskip("tree_sitter_rust")`。`test_code_lang_rust.py` と `test_code_lang_typescript.py` が手本です。
- **レーンコーパス。** `tests/fixtures/code_lanes/<lang>/` にフィクスチャを追加します。合成したもの、またはそれぞれのライセンスに従ったオープンソースの抜粋とし、`tests/fixtures/code_lanes/NOTICE.md` に記載します。`tests/unit/test_code_lanes_corpus.py` は、すべてのフィクスチャから言語、シンボル、ファセット、include、エッジを導出し、`tests/fixtures/code_lanes/expected.json` と比較します。再生成して差分を確認します：

  ```bash
  python -m tests.code_lanes.corpus --write
  git diff tests/fixtures/code_lanes/expected.json
  ```

  ここで数値が動くと、すべての名前空間の内容が変わります。必ず確認してください。

- **コストガード。** `ingest_parse._PARSERS` に追加したパーサーには、`tests/unit/lane_walks.py` に `SAMPLES` エントリが必要です。`tests/unit/test_code_regex_cost.py` は、生成した形状に対してレーンの処理時間を計測し、線形を超えるウォークがあれば失敗します。
- **スタンプテスト。** `tests/unit/test_code_extractor_stamp.py` で、新しいモジュールを `NON_EXTRACTOR_MODULES` に追加します。
- **検索。** `code.search` や `code.symbol` で良好に答えられるべきレーンについては、コードインテリジェンスのゲートを変更の前後で実行します。`make bench-code-intel-dry` は質問セットをオフラインで検証します。`make bench-code-intel` は、実際のエンベッダーを使うサーバーに対して実行します。偽のエンベッダーは使わないでください（[ベンチマークの実行](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)）。

<a id="4-bring-existing-namespaces-up-to-date"></a>

## 4. 既存の名前空間を最新の状態にする

スタンプの外にあるレーンは、すでにインデックス済みのものを何も変更しません。新しいコミットはそのレーンを使い、古いファイルは以前の行（多くはチャンクのみ）を保持します。対象を絞ったバックフィルで再導出します。

```bash
git -C <checkout of the repository> ls-files '*.rs' > rust-paths.txt
loom code backfill --org <org> --namespace <code-namespace> --paths-file rust-paths.txt
```

`--paths-file` は、ブランチの先頭にある、リポジトリからの相対パスで指定されたファイルだけを再取り込みし、ウォーターマークには触れません。サーバーの `.env` とリポジトリのミラーがある場所（workers コンテナなど）で実行してください（[コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)）。

> **警告:** 変更がスタンプ対象のモジュール（`chunker.py` の修正、`treesitter.py` の拡張など）に触れる場合は、PR にその旨を記載してください。その変更を取り込んだすべてのデプロイメントで、すべての名前空間にある対象ファミリーの全ファイルについて、影響を受けたステージが再導出されます。`chunker.py` の変更が動かすのは `chunk` のみです。`edges_treesitter.py` の変更は `edge`、`chunk`、`guard` を動かします。パーサーの変更はすべてのステージを動かします。

<a id="5-add-a-compiler-tier-optional"></a>

## 5. コンパイラティアを追加する（任意）

高速ティアのエッジが曖昧になる場合（オーバーロード、動的ディスパッチ、再エクスポート）で、SCIP インデクサが存在するときは、コンパイラティアが有効です。ティアの一覧は `server/code/domain/index/compiler_tiers.py` の `TIERS` にあります。各 `TierSpec` には、インデクサ（`scip-clang`、`scip-typescript`、`scip-go`、`scip-python`）、その `code_index_state` のウォーターマークキー（`scip_commit`、`scip_commit_ts`、`scip_commit_go`、`scip_commit_py`）、およびそれを進めるワーカーが記述されています。新しいティアには次のものが必要です。

1. コミット範囲内の変更ファイルに対してインデクサを実行し、その出力を Loom の JSON デルタに変換するハーネス：`scripts/scip_harness/`（`run_delta.sh`、`run_typescript_delta.sh`、`run_python_delta.sh`、`run_go_monorepo_delta.sh`、`scip_to_delta.py`）。
2. デルタワーカー（`server/code/workers/scip_ts_delta.py`、`scip_py_delta.py`、`scip_go_delta.py` が手本です）：その `run_pass` が `_run_repo` を `server/code/app/index/compiler_tier.py` の `run_tier` に渡します。これがエントリのループ、障害の分離、フルラン時のリセットを担当します。ワーカーには `LOOM_CODE_SCIP_<LANG>_DELTA_ENABLED` / `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS` の設定が付属します（[ワーカーの追加](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-worker.md)）。外部のプロデューサは代わりに `code.scip_ingest` 経由でアップロードし、`code_scip_apply` ワーカーが完結したバッチを適用します。
3. `code_scip_apply` の `watermark_key_for` 内のウォーターマークキーと `TierSpec` の行。これにより `code.index_status` とダッシュボードがそのティアを報告します。

コンパイラティアの行には `tier='scip'` が付きます。読み取り時には、これらを高速ティアの行と修飾名でマージし、コンパイラティアを優先します。インデクサがスキップするファイルや、デルタが反映される前のコミットは、引き続き高速ティアから読み取られます。

<a id="related"></a>

## 関連情報

- [コードインデックスの概要](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- [コードインデックスのアーキテクチャ](../architecture/code-index-architecture.md)
- [コードインデックスの運用](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
- [Loom の拡張](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
