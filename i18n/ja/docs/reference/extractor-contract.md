<!-- i18n: source=docs/reference/extractor-contract.md blob=539b2933c9373bff7d858f2d0b746a85d2d5e253 model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md), which is authoritative.

<a id="extractor-contract"></a>

# エクストラクターの契約

エクストラクターは、Loom サーバーが元のファイルを送信し、その抽出アーティファクトを受け取る HTTP サービスです。
このページは、両者間の契約のバージョン 1 です。サービスが提供しなければならないものを定めます。対象は、Loom のリファレンスエクストラクター（`extractor/loom_extractor/`）と、運用者が独自に用意するものです。パース処理をサーバーの外で実行する理由と、サーバーがレスポンスを読み取る方法については、[ドキュメント抽出](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-extraction.md)を参照してください。

<a id="configuring-the-server"></a>

## サーバーの設定

| 設定                                         | 意味                                                                                                                                                                                                                                              |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_DOCUMENT_EXTRACTOR_URL`                | どのルートにも名前が挙がっていない形式が送られる先のベース URL                                                                                                                                                                                    |
| `LOOM_DOCUMENT_EXTRACTORS_JSON`              | `{format or media type: base URL}`。形式のキーはそのメディアタイプのキーより優先され（メディアタイプのキーは、そのメディアタイプの形式にのみ適用される）、どちらも上記の URL より優先される。形式を指さないキーや、同じメディアタイプの二つ目の表記は、読み込み時に拒否される |
| `LOOM_DOCUMENT_EXTRACTOR_TOKEN`              | 下記の専用トークンを持たないサービスに送信されるベアラートークン                                                                                                                                                                                  |
| `LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON`        | `{base URL: token}`。サービス専用のトークンであり、あるサービスが別のサービスが受け付けるトークンを保持することはない。各 URL は上記の二つの設定のいずれかに挙げられているものでなければならない                                               |
| `LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S`          | 1 回の呼び出し全体に許される秒数。capabilities の読み取り、または元ファイルの送信とレスポンスの読み取りを含む                                                                                                                                      |
| `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES` | サーバーが読み取るレスポンスの最大サイズ。これを超えるレスポンスは `resource_limit` として拒否される                                                                                                                                               |

形式名は `markdown`、`html`、`text`、`sql`、`toml`、`pdf`、`docx`、`pptx`、`xlsx`、`csv`、`parquet` です。たとえば、PDF をオペレーター自身のサービスに送り、それ以外をリファレンス extractor に送るには、次のようにします。

```bash
LOOM_DOCUMENT_EXTRACTOR_URL=http://extractor:8790
LOOM_DOCUMENT_EXTRACTORS_JSON='{"pdf": "http://pdf-extractor:8080"}'
```

<a id="endpoints"></a>

## エンドポイント

すべての呼び出しに `Authorization: Bearer <token>` を付けます。トークンがない、または誤っている場合、サービスは `401` を返します。

<a id="get-v1capabilities"></a>

### `GET /v1/capabilities`

サービスが抽出できるものを返します。サーバーは、ルーティング先のサービスがここに一覧した形式だけを抽出し、1 分後にこの情報を再度読み込みます。

```json
{
  "contract_version": 1,
  "extractor": { "name": "loom-reference", "version": "1" },
  "max_bytes": 67108864,
  "formats": [
    { "format": "markdown", "options": ["encoding"] },
    { "format": "sql", "options": ["encoding", "sql_dialect"] }
  ]
}
```

| フィールド         | 意味                                                                          |
| ------------------ | ----------------------------------------------------------------------------- |
| `contract_version` | `1`。サーバーは別のバージョンのサービスを利用不可として扱います               |
| `max_bytes`        | サービスが受け付ける原本の最大サイズ。サーバーはこれより大きい原本を送りません |
| `formats`          | サービスが抽出する各形式と、その形式が受け付けるオプション                    |

<a id="post-v1extract"></a>

### `POST /v1/extract`

本文は原本のバイト列です（`Content-Type: application/octet-stream`）。サーバーが要求する内容はクエリで指定します。

| パラメーター  | 意味                                                             |
| ------------- | ---------------------------------------------------------------- |
| `format`      | サーバーがファイルのルーティングに使った形式                     |
| `name`        | ファイル名。空の場合があります                                   |
| `encoding`    | 任意。自身でエンコーディングを示せないテキストファイル向けのコーデック名 |
| `sql_dialect` | 任意、SQL のみ。パースに使う方言                                 |

サービスは、受け付けないオプションを拒否します（`invalid_option`）。また、内容を `format` と照合し、別の形式のファイルは、指定された形式として読まずに拒否します（`format_mismatch`）。

`200` は artifact の JSON を返します。サーバーは `Accept-Encoding: identity` を送信するため、サービスは応答を圧縮してはいけません。圧縮された応答は、ステータスに関わらず、サービスが利用不可であるものとして扱われます。サービスが抽出できないソースには、エラー本文付きの `422` を返します。

```json
{ "error": { "code": "format_mismatch", "message": "the file is pdf, not markdown" } }
```

| コード               | 意味                                                      |
| -------------------- | --------------------------------------------------------- |
| `unsupported_format` | どの抽出器もサポートしていない形式のファイル              |
| `format_unavailable` | このサービスはその形式を抽出しない                        |
| `format_mismatch`    | 内容が、指定された形式とは別の形式である                  |
| `encoding`           | テキストをデコードできない                                |
| `corrupt`            | ファイルが破損している、またはパーサーが処理に失敗する    |
| `invalid_option`     | オプションが未知であるか、サービスが拒否する値である      |
| `too_large`          | 原本がサービスの `max_bytes` を超えている                 |
| `timeout`            | 抽出にサービスの許容時間より長くかかった                  |
| `resource_limit`     | 抽出がメモリまたは出力の上限を超えた                      |

サーバーは、小文字の英字と `_` からなる最大 40 文字のコードを失敗のコードとして保持し、それ以外のコード、または 64 KiB を超えるエラー本文は `refused` として扱います。`401`、`403`、`407`、`408`、`429`、すべての `5xx`、`200` でも `4xx` でもないその他の応答（リダイレクトや `204`）、`format_unavailable`、およびタイムアウトを過ぎた呼び出しは、サービスが利用不可であるものとして扱い、そのソースは後で再試行できます。それ以外の `4xx` は、ソースが拒否されたものとして扱います。以下のルールに違反する artifact を含む `200` は、`invalid_artifact` として拒否されます。

<a id="the-artifact"></a>

## アーティファクト

```json
{
  "schema_version": 1,
  "format": "toml",
  "parser": "loom-toml",
  "parser_version": "1+python3.12.13",
  "options": {},
  "attrs": {},
  "warnings": [],
  "coverage": {
    "unit": "keys",
    "total": 1,
    "extracted": 1,
    "failed": [],
    "lost": [],
    "complete": true
  },
  "blocks": [
    {
      "id": "b0",
      "type": "toml_table",
      "text": "[server]",
      "section_path": ["server"],
      "locator": { "line_start": 1, "line_end": 1, "key_path": "server" },
      "keys": ["server"]
    },
    {
      "id": "b1",
      "type": "toml_key",
      "text": "server.port = 7777",
      "parent": "b0",
      "section_path": ["server"],
      "locator": { "line_start": 2, "line_end": 2, "key_path": "server.port" },
      "keys": ["server.port"],
      "attrs": { "value": 7777, "value_type": "integer" }
    }
  ]
}
```

| フィールド                             | 型                       | ルール                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| -------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`                       | 整数                     | `1`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `format`                               | 文字列                   | 要求された `format`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `parser`, `parser_version`             | 空でない文字列           | アーティファクトを生成したもの。パーサーが変わると新しいバージョンになる                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `options`, `attrs`                     | オブジェクト             | ネストは最大 16 階層                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `warnings`                             | 文字列のリスト           |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `coverage.unit`                        | 空でない文字列           | フォーマットを構成する単位：`lines`、`statements`、`keys`、`chars`、`pages`、`paragraphs`、`slides`、`rows`、...                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `coverage.total`, `coverage.extracted` | 整数                     | `0 <= extracted <= total`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `coverage.failed`                      | `{unit, reason}` のリスト | 抽出されなかった各単位                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `coverage.lost`                        | 文字列のリスト           | アーティファクトが提供できない機能（例：`sql_structure`）                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `coverage.complete`                    | 真偽値、省略可           | 送る場合は、`extracted == total` で、失敗も欠落もないことと等しくなければならない                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `blocks`                               | リスト                   | 読み順                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `blocks[].id`                          | 空でない文字列           | アーティファクト内で一意                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `blocks[].type`                        | 文字列                   | `heading`、`paragraph`、`list`、`code`、`sql_statement`、`toml_table`、`toml_key` のいずれか                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `blocks[].text`                        | 文字列                   |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `blocks[].parent`                      | 文字列、省略可           | そのブロックを囲む、より前のブロックの id                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `blocks[].depth`                       | 整数、省略可             | `0` 以上。見出しのレベル                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `blocks[].section_path`                | 文字列のリスト           | ブロックより上にある見出し。最大 64 個、各最大 500 文字                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `blocks[].keys`                        | 文字列のリスト           | ブロックを検索できる完全一致の名前。最大 10,000 個、各最大 1,024 文字                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `blocks[].attrs`                       | オブジェクト             | フォーマット固有の情報（行ブロックの `table_id`：その行が属するテーブルの id。DOCX、PPTX、HTML のテーブルブロックの `table`：その `rows` と `columns`。ページの向きとは別の方向に流れるテキストの PDF ブロックの `orientation`。画像から読み取ったブロックの `ocr`）                                                                                                                                                                                                                                                                      |
| `blocks[].locator`                     | オブジェクト             | `line_start`/`line_end`（1 始まり。どちらも `0`、または `1 <= start <= end`）、`char_start`/`char_end`（デコード後のテキストへのオフセット。どちらも `-1`、または `0 <= start <= end`）、`key_path`、`page_start`/`page_end`（PDF のページまたはデッキのスライド。1 始まり。どちらも `0`、または `1 <= start <= end`）、`sheet`（ワークブックのシート名。最大 500 文字）、`row_start`/`row_end`（シートまたは CSV ファイルの行でヘッダーを数える、Parquet ファイルの行は 1 から、または PDF テーブルの行でヘッダー行を 1 とする。1 始まり。どちらも `0`、または `1 <= start <= end`） |

表形式のソース（CSV ファイル、ワークブックのシート、Parquet ファイル）と、テーブルを含む PDF は、検索の対象となる行ブロックに加えて `tables` も持ちます。

| フィールド                     | 型                     | ルール                                                                                                                                                                                                                                                                                                  |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tables`                       | リスト、省略可         | ソースの各テーブル                                                                                                                                                                                                                                                                                      |
| `tables[].id`, `tables[].name` | 文字列                 | id は一意で空でないこと。name は、シート名、ファイル名、または PDF テーブルのページと位置（`page 3 table 1`）                                                                                                                                                                                          |
| `tables[].columns`             | `{name, type}` のリスト | 最大 16,384 個。name は最大 500 文字。type は `string`、`number`、`bool`、`date`、`datetime`、`time`、`duration`、`mixed` のいずれか（列の全ての値が何であるか。日付と日時が混在する場合は `datetime`）                                                                                                 |
| `tables[].rows`                | リストのリスト         | 各行は `columns` と同じ長さ。各値は文字列、数値、真偽値、null、またはセルエラー `{"error": "#DIV/0!"}`。日付と時刻は ISO テキスト、期間は `H:MM:SS` で 1 日を超える時間も含める（`36:00:00`）。小数、または符号付き 64 ビットを超える整数は、`number` 列では正確な桁（`"12.345"`）                      |
| `tables[].header_rows`         | integer、省略可         | `0` 以上。省略時は `1`：列に名前を付ける値の上にある行。名前がソースの行ではないソース（Parquet のスキーマ）では `0` とし、その場合、行は 1 から数える                                                                                                                                                                                                                        |
| `tables[].locator`             | object                 | ブロックのものと同じ：テーブルが及ぶシート（PDF ではページ）と行                                                                                                                                                                                                                                 |
| `tables[].attrs`               | object                 | フォーマット固有の情報（非表示シートには `hidden`）                                                                                                                                                                                                                                                              |

`tables` を知らないサーバーは、契約に定められていないほかのフィールドと同様にこれを破棄し、行ブロックは引き続き公開します。

すべての文字列は有効な Unicode でなければならず、すべての数値は JSON でなければなりません（`NaN` や `Infinity` は不可）。整数は符号付き 64 ビット（-2^63 から 2^63-1 まで。したがってそれを超える `UInt64` は不可）、それ以外の数値は double の範囲内（`1e999` は不可）です。サーバーは、いずれかの規則に違反するアーティファクトを拒否し、契約に定められていないフィールドは破棄するため、サービスはバージョンを変えずにそれを送れます。古いサーバーが読めない変更は `schema_version` を上げます。ブロックのキーとセクションパスは、そこから切り出されるすべてのチャンクに付随するため、アーティファクトのサイズに加えて、個数と長さでも制限されます。リファレンス抽出器は、長すぎるキーを除外し、先頭 10,000 個のキーと、500 文字に切り詰めた先頭 64 件のセクションエントリを残します。そして、ブロックに `attrs.keys_truncated` または `attrs.section_truncated` を、アーティファクトの `coverage.lost`（`exact_keys`、`section_path`）に印を付けるため、完全であると報告することはありません。ここでの文字、および `char_start`/`char_end` における文字は、デコード後のテキストの Unicode コードポイントです（UTF-16 単位でもバイトでもありません）。行や文字の範囲は原本の内側に収まる必要があります。サーバーは、`char_end` が原本のバイト数を超えるアーティファクト、または `line_end` がそのサイズに 1 を足した値を超えるアーティファクトを拒否します。この上限は、どのエンコーディングでも成り立ちます。
