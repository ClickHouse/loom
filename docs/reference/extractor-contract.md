# Extractor contract

An extractor is an HTTP service the Loom server sends an original file to and gets its extraction artifact back from.
This page is version 1 of the contract between them: what a service, Loom's reference extractor
(`extractor/loom_extractor/`) or one of an operator's own, must serve. Why the parsing runs outside the server, and how
the server reads a response, is in [document extraction](../architecture/document-extraction.md).

## Configuring the server

| Setting                                      | Meaning                                                                                                                                                                                                                                           |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_DOCUMENT_EXTRACTOR_URL`                | the base URL a format goes to when no route names it                                                                                                                                                                                              |
| `LOOM_DOCUMENT_EXTRACTORS_JSON`              | `{format or media type: base URL}`; a format's key wins over its media type's (which applies to that media type's format only), either over the URL above; a key that names no format, or a second spelling of one media type, is refused at load |
| `LOOM_DOCUMENT_EXTRACTOR_TOKEN`              | the bearer token sent to a service with none of its own below                                                                                                                                                                                     |
| `LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON`        | `{base URL: token}`: a service's own token, so no service holds the token another accepts; each URL must be one the two settings above name                                                                                                       |
| `LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S`          | seconds one call may take as a whole: reading the capabilities, or sending the original and reading the response                                                                                                                                  |
| `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES` | the largest response the server reads; a longer one is refused as `resource_limit`                                                                                                                                                                |

The format names are `markdown`, `html`, `text`, `sql`, `toml`, `pdf`, `docx`, `pptx`, `xlsx`, `csv` and `parquet`. For
example, to send PDFs to an operator's own service and everything else to the reference extractor:

```bash
LOOM_DOCUMENT_EXTRACTOR_URL=http://extractor:8790
LOOM_DOCUMENT_EXTRACTORS_JSON='{"pdf": "http://pdf-extractor:8080"}'
```

## Endpoints

Every call carries `Authorization: Bearer <token>`. A service answers a missing or wrong token with `401`.

### `GET /v1/capabilities`

What the service extracts. The server extracts a format only when the service it is routed to lists it here, and reads
this again after a minute.

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

| Field              | Meaning                                                                       |
| ------------------ | ----------------------------------------------------------------------------- |
| `contract_version` | `1`; the server treats a service of another version as unavailable            |
| `max_bytes`        | the largest original the service takes; the server does not send a larger one |
| `formats`          | each format the service extracts and the options it takes                     |

### `POST /v1/extract`

The original's bytes are the body (`Content-Type: application/octet-stream`). The query names what the server asks for:

| Parameter     | Meaning                                                        |
| ------------- | -------------------------------------------------------------- |
| `format`      | the format the server routed the file as                       |
| `name`        | the file's name, which may be empty                            |
| `encoding`    | optional: a codec name for a text file that cannot say its own |
| `sql_dialect` | optional, SQL only: the dialect to parse with                  |

A service refuses an option it does not take (`invalid_option`). It checks the content against `format` and refuses a
file that is another format (`format_mismatch`) rather than reading it as the one asked for.

`200` carries the artifact JSON. The server sends `Accept-Encoding: identity`, and a service must not compress an
answer: the server treats a compressed one, whatever its status, as the service being unavailable. A source the service
cannot extract is a `422` with an error body:

```json
{ "error": { "code": "format_mismatch", "message": "the file is pdf, not markdown" } }
```

| Code                 | Meaning                                                 |
| -------------------- | ------------------------------------------------------- |
| `unsupported_format` | the file is a format no extractor supports              |
| `format_unavailable` | this service does not extract the format                |
| `format_mismatch`    | the content is another format than the one asked for    |
| `encoding`           | the text does not decode                                |
| `corrupt`            | the file is damaged, or the parser fails on it          |
| `invalid_option`     | an option is unknown or has a value the service refuses |
| `too_large`          | the original is over the service's `max_bytes`          |
| `timeout`            | the extraction took longer than the service allows      |
| `resource_limit`     | the extraction passed a memory or output limit          |

The server keeps a code of up to 40 lowercase letters and `_` as the failure's code and reads any other, or an error
body over 64 KiB, as `refused`. It treats `401`, `403`, `407`, `408`, `429`, any `5xx`, any other answer that is not
`200` or `4xx` (a redirect, a `204`), `format_unavailable` and a call past its timeout as the service being unavailable,
so the source can be tried again later, and any other `4xx` as the source refused. A `200` whose artifact breaks the
rules below is refused as `invalid_artifact`.

## The artifact

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

| Field                                  | Type                     | Rule                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`                       | integer                  | `1`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `format`                               | string                   | the `format` asked for                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `parser`, `parser_version`             | non-empty strings        | what made the artifact; a parser change makes a new version                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `options`, `attrs`                     | objects                  | nested at most 16 deep                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `warnings`                             | list of strings          |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `coverage.unit`                        | non-empty string         | what the format is made of: `lines`, `statements`, `keys`, `chars`, `pages`, `paragraphs`, `slides`, `rows`, ...                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `coverage.total`, `coverage.extracted` | integers                 | `0 <= extracted <= total`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `coverage.failed`                      | list of `{unit, reason}` | each unit not extracted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `coverage.lost`                        | list of strings          | capabilities the artifact cannot serve, such as `sql_structure`                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `coverage.complete`                    | boolean, optional        | if sent, equal to `extracted == total` with no failures and no losses                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `blocks`                               | list                     | in reading order                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `blocks[].id`                          | non-empty string         | unique in the artifact                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `blocks[].type`                        | string                   | `heading`, `paragraph`, `list`, `code`, `sql_statement`, `toml_table` or `toml_key`                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `blocks[].text`                        | string                   |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `blocks[].parent`                      | string, optional         | the id of an earlier block that encloses it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `blocks[].depth`                       | integer, optional        | `0` or more; a heading's level                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `blocks[].section_path`                | list of strings          | the headings above the block; at most 64, each at most 500 characters                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `blocks[].keys`                        | list of strings          | exact names the block can be looked up by; at most 10,000, each at most 1,024 characters                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `blocks[].attrs`                       | object                   | format facts (`table_id` on a row block: the id of the table its rows are from; `table` on a DOCX, PPTX or HTML table block: its `rows` and `columns`; `orientation` on a PDF block of text that runs in another direction than its page's; `ocr` on a block read from an image)                                                                                                                                                                                                                                                        |
| `blocks[].locator`                     | object                   | `line_start`/`line_end` (1-based, both `0` or `1 <= start <= end`), `char_start`/`char_end` (offsets into the decoded text, both `-1` or `0 <= start <= end`), `key_path`, `page_start`/`page_end` (a PDF's pages or a deck's slides, 1-based, both `0` or `1 <= start <= end`), `sheet` (a workbook's sheet name, at most 500 characters), `row_start`/`row_end` (a sheet's or a CSV file's rows, the header counted, a Parquet file's rows from 1, or a PDF table's rows, its header row 1; 1-based, both `0` or `1 <= start <= end`) |

A tabular source (a CSV file, a workbook's sheets, a Parquet file) and a PDF that holds tables also carry `tables`, beside the row blocks they are searched by:

| Field                          | Type                   | Rule                                                                                                                                                                                                                                                                                                    |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tables`                       | list, optional         | each table of the source                                                                                                                                                                                                                                                                                |
| `tables[].id`, `tables[].name` | strings                | the id unique and not empty; the name: the sheet's, the file's, or a PDF table's page and place (`page 3 table 1`)                                                                                                                                                                                      |
| `tables[].columns`             | list of `{name, type}` | at most 16,384; a name at most 500 characters; a type `string`, `number`, `bool`, `date`, `datetime`, `time`, `duration` or `mixed` (what every value it holds is; dates and date-times together are `datetime`)                                                                                        |
| `tables[].rows`                | list of lists          | each as long as `columns`; each value a string, a number, a bool, null, or a cell error `{"error": "#DIV/0!"}`; a date or a time is ISO text, a duration `H:MM:SS` with hours past a day (`36:00:00`), a decimal, or an integer past signed 64 bits, in a `number` column its exact digits (`"12.345"`) |
| `tables[].header_rows`         | integer, optional      | `0` or more, `1` when left out: the rows above the values that named the columns; `0` for a source whose names are no row of it (a Parquet schema), whose rows then count from 1                                                                                                                        |
| `tables[].locator`             | object                 | as a block's: the sheet (or a PDF's pages) and the rows the table spans                                                                                                                                                                                                                                 |
| `tables[].attrs`               | object                 | format facts (`hidden` for a hidden sheet)                                                                                                                                                                                                                                                              |

A server that does not know `tables` drops it, as any field the contract does not name, and still publishes the row
blocks.

Every string is valid Unicode, and every number is JSON (no `NaN` or `Infinity`): an integer is signed 64-bit (-2^63 to
2^63-1, so not a `UInt64` past it), any other number within a double (not `1e999`). The server refuses an artifact that
breaks any rule, and drops a field the contract does not name, so a service can send one without a version change. A
change an older server cannot read raises `schema_version`. A block's keys and section path travel with every chunk cut
from it, so they are bounded by count and length as well as by the artifact's size; the reference extractor leaves out a
longer key, keeps the first 10,000 and the first 64 section entries cut to 500 characters, and marks the block
`attrs.keys_truncated` or `attrs.section_truncated` and the artifact's `coverage.lost` (`exact_keys`, `section_path`),
so it does not report itself complete. A character, here and in `char_start`/`char_end`, is a Unicode code point of the
decoded text (not a UTF-16 unit or a byte). A line or character range lies within the original: the server refuses an
artifact with a `char_end` past the original's size in bytes or a `line_end` past that size plus one, bounds that hold
in any encoding.
