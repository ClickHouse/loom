<!-- i18n: source=docs/reference/extractor-contract.md blob=539b2933c9373bff7d858f2d0b746a85d2d5e253 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md), which is authoritative.

<a id="extractor-contract"></a>

# 추출기 계약

추출기는 Loom 서버가 원본 파일을 보내면 추출 아티팩트를 돌려주는 HTTP 서비스입니다.
이 페이지는 서버와 추출기 사이의 계약 버전 1입니다. Loom의 참조 추출기(`extractor/loom_extractor/`)이든 운영자가 직접 만든 추출기이든, 서비스가 반드시 제공해야 하는 내용을 설명합니다. 파싱을 서버 밖에서 실행하는 이유와 서버가 응답을 읽는 방식은 [문서 추출](../architecture/document-extraction.md)에서 다룹니다.

<a id="configuring-the-server"></a>

## 서버 설정

| 설정                                         | 의미                                                                                                                                                                                                                                              |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_DOCUMENT_EXTRACTOR_URL`                | 어떤 경로도 지정하지 않은 형식이 향하는 기본 URL                                                                                                                                                                                                  |
| `LOOM_DOCUMENT_EXTRACTORS_JSON`              | `{format or media type: base URL}`. 형식의 키가 미디어 타입의 키보다 우선하며(미디어 타입의 키는 해당 미디어 타입의 형식에만 적용됨), 둘 다 위의 URL보다 우선합니다. 어떤 형식도 가리키지 않는 키나 같은 미디어 타입을 두 번 표기한 키는 로드 시 거부됩니다 |
| `LOOM_DOCUMENT_EXTRACTOR_TOKEN`              | 아래에 자체 토큰이 없는 서비스로 보내는 bearer 토큰                                                                                                                                                                                               |
| `LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON`        | `{base URL: token}`. 서비스별 자체 토큰이므로 어떤 서비스도 다른 서비스가 받아들이는 토큰을 가지지 않습니다. 각 URL은 위의 두 설정 중 하나에 있는 URL이어야 합니다                                                                                  |
| `LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S`          | 한 번의 호출 전체에 허용되는 시간(초). 기능 정보를 읽는 시간, 또는 원본을 보내고 응답을 읽는 시간입니다                                                                                                                                           |
| `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES` | 서버가 읽는 응답의 최대 크기. 이보다 긴 응답은 `resource_limit`로 거부됩니다                                                                                                                                                                      |

형식 이름은 `markdown`, `html`, `text`, `sql`, `toml`, `pdf`, `docx`, `pptx`, `xlsx`, `csv`, `parquet`입니다. 예를 들어 PDF는 운영자가 직접 운영하는 서비스로 보내고 나머지는 모두 참조 추출기로 보내려면 다음과 같이 설정합니다.

```bash
LOOM_DOCUMENT_EXTRACTOR_URL=http://extractor:8790
LOOM_DOCUMENT_EXTRACTORS_JSON='{"pdf": "http://pdf-extractor:8080"}'
```

<a id="endpoints"></a>

## 엔드포인트

모든 호출에는 `Authorization: Bearer <token>`이 포함됩니다. 서비스는 토큰이 없거나 잘못된 경우 `401`로 응답합니다.

<a id="get-v1capabilities"></a>

### `GET /v1/capabilities`

서비스가 추출하는 대상을 알려 줍니다. 서버는 라우팅된 서비스가 여기에 나열한 포맷만 추출하며, 1분 후에 이 정보를 다시 읽습니다.

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

| 필드               | 의미                                                                          |
| ------------------ | ----------------------------------------------------------------------------- |
| `contract_version` | `1`입니다. 서버는 다른 버전의 서비스를 사용할 수 없는 것으로 처리합니다       |
| `max_bytes`        | 서비스가 받는 원본의 최대 크기입니다. 서버는 이보다 큰 원본을 보내지 않습니다 |
| `formats`          | 서비스가 추출하는 각 포맷과 그 포맷이 받는 옵션입니다                         |

<a id="post-v1extract"></a>

### `POST /v1/extract`

원본의 바이트가 본문입니다(`Content-Type: application/octet-stream`). 쿼리는 서버가 요청하는 내용을 지정합니다.

| 매개변수      | 의미                                                              |
| ------------- | ----------------------------------------------------------------- |
| `format`      | 서버가 파일에 대해 라우팅한 포맷                                  |
| `name`        | 파일 이름이며, 비어 있을 수 있습니다                              |
| `encoding`    | 선택 사항: 자체적으로 인코딩을 밝히지 못하는 텍스트 파일의 코덱 이름 |
| `sql_dialect` | 선택 사항, SQL 전용: 파싱에 사용할 방언                           |

서비스는 받지 않는 옵션을 거부합니다(`invalid_option`). 또한 내용을 `format`과 대조하여, 다른 포맷의 파일이면 요청된 포맷으로 읽지 않고 거부합니다(`format_mismatch`).

`200`은 아티팩트 JSON을 담습니다. 서버는 `Accept-Encoding: identity`를 보내며, 서비스는 응답을 압축해서는 안 됩니다. 서버는 상태와 관계없이 압축된 응답을 서비스를 사용할 수 없는 것으로 처리합니다. 서비스가 추출할 수 없는 소스는 오류 본문과 함께 `422`로 응답합니다.

```json
{ "error": { "code": "format_mismatch", "message": "the file is pdf, not markdown" } }
```

| 코드                 | 의미                                                    |
| -------------------- | ------------------------------------------------------- |
| `unsupported_format` | 어떤 추출기도 지원하지 않는 포맷의 파일입니다           |
| `format_unavailable` | 이 서비스는 해당 포맷을 추출하지 않습니다               |
| `format_mismatch`    | 내용이 요청된 포맷과 다른 포맷입니다                    |
| `encoding`           | 텍스트를 디코딩할 수 없습니다                           |
| `corrupt`            | 파일이 손상되었거나 파서가 처리하지 못합니다            |
| `invalid_option`     | 옵션을 알 수 없거나 서비스가 거부하는 값을 가졌습니다   |
| `too_large`          | 원본이 서비스의 `max_bytes`를 초과합니다                |
| `timeout`            | 추출이 서비스가 허용하는 시간보다 오래 걸렸습니다       |
| `resource_limit`     | 추출이 메모리 또는 출력 한도를 넘었습니다               |

서버는 소문자와 `_`로 이루어진 최대 40자의 코드를 실패 코드로 보존하며, 그 외의 코드나 64 KiB를 넘는 오류 본문은 `refused`로 읽습니다. 서버는 `401`, `403`, `407`, `408`, `429`, 모든 `5xx`, `200`이나 `4xx`가 아닌 그 밖의 응답(리다이렉트, `204`), `format_unavailable`, 그리고 타임아웃을 넘긴 호출을 서비스를 사용할 수 없는 것으로 처리하므로 소스를 나중에 다시 시도할 수 있습니다. 그 밖의 `4xx`는 소스가 거부된 것으로 처리합니다. 아래 규칙을 어기는 아티팩트를 담은 `200`은 `invalid_artifact`로 거부됩니다.

<a id="the-artifact"></a>

## 아티팩트

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

| 필드                                   | 타입                     | 규칙                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`                       | 정수                     | `1`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `format`                               | 문자열                   | 요청한 `format`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `parser`, `parser_version`             | 비어 있지 않은 문자열    | 아티팩트를 만든 주체. 파서가 바뀌면 새 버전이 된다                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `options`, `attrs`                     | 객체                     | 중첩은 최대 16단계                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `warnings`                             | 문자열 목록              |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `coverage.unit`                        | 비어 있지 않은 문자열    | 형식을 이루는 단위: `lines`, `statements`, `keys`, `chars`, `pages`, `paragraphs`, `slides`, `rows`, ...                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `coverage.total`, `coverage.extracted` | 정수                     | `0 <= extracted <= total`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `coverage.failed`                      | `{unit, reason}` 목록    | 추출되지 않은 각 단위                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `coverage.lost`                        | 문자열 목록              | 아티팩트가 제공할 수 없는 기능 (예: `sql_structure`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `coverage.complete`                    | 불리언, 선택             | 보내는 경우 `extracted == total`이면서 실패와 손실이 없는 것과 같아야 한다                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `blocks`                               | 목록                     | 읽는 순서대로                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `blocks[].id`                          | 비어 있지 않은 문자열    | 아티팩트 내에서 고유                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `blocks[].type`                        | 문자열                   | `heading`, `paragraph`, `list`, `code`, `sql_statement`, `toml_table` 또는 `toml_key`                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `blocks[].text`                        | 문자열                   |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `blocks[].parent`                      | 문자열, 선택             | 이 블록을 감싸는 앞선 블록의 id                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `blocks[].depth`                       | 정수, 선택               | `0` 이상. 제목의 수준                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `blocks[].section_path`                | 문자열 목록              | 블록 위에 있는 제목들. 최대 64개, 각각 최대 500자                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `blocks[].keys`                        | 문자열 목록              | 블록을 조회할 때 쓰는 정확한 이름. 최대 10,000개, 각각 최대 1,024자                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `blocks[].attrs`                       | 객체                     | 형식별 정보 (행 블록의 `table_id`: 행이 속한 표의 id. DOCX, PPTX, HTML 표 블록의 `table`: 해당 표의 `rows`와 `columns`. 페이지와 다른 방향으로 흐르는 텍스트의 PDF 블록의 `orientation`. 이미지에서 읽은 블록의 `ocr`)                                                                                                                                                                                                                                                                                                                  |
| `blocks[].locator`                     | 객체                     | `line_start`/`line_end` (1부터 시작, 둘 다 `0` 또는 `1 <= start <= end`), `char_start`/`char_end` (디코딩된 텍스트에서의 오프셋, 둘 다 `-1` 또는 `0 <= start <= end`), `key_path`, `page_start`/`page_end` (PDF의 페이지 또는 덱의 슬라이드, 1부터 시작, 둘 다 `0` 또는 `1 <= start <= end`), `sheet` (워크북의 시트 이름, 최대 500자), `row_start`/`row_end` (시트나 CSV 파일의 행으로 헤더를 포함해 센다. Parquet 파일의 행은 1부터, PDF 표의 행은 헤더 행을 1로 센다. 1부터 시작, 둘 다 `0` 또는 `1 <= start <= end`) |

표 형식 소스(CSV 파일, 워크북의 시트, Parquet 파일)와 표가 들어 있는 PDF는 검색에 쓰이는 행 블록과 함께 `tables`도 포함한다:

| 필드                           | 타입                   | 규칙                                                                                                                                                                                                                                                                                                         |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `tables`                       | 목록, 선택             | 소스의 각 표                                                                                                                                                                                                                                                                                                 |
| `tables[].id`, `tables[].name` | 문자열                 | id는 고유하며 비어 있지 않아야 한다. name은 시트 이름, 파일 이름, 또는 PDF 표의 페이지와 위치(`page 3 table 1`)                                                                                                                                                                                              |
| `tables[].columns`             | `{name, type}` 목록    | 최대 16,384개. name은 최대 500자. type은 `string`, `number`, `bool`, `date`, `datetime`, `time`, `duration` 또는 `mixed` (담고 있는 모든 값의 종류이며, 날짜와 날짜-시간이 함께 있으면 `datetime`)                                                                                                            |
| `tables[].rows`                | 목록의 목록            | 각각 `columns`와 길이가 같다. 각 값은 문자열, 숫자, 불리언, null, 또는 셀 오류 `{"error": "#DIV/0!"}`. 날짜와 시간은 ISO 텍스트, 기간은 하루를 넘는 시간을 허용하는 `H:MM:SS`(`36:00:00`), 소수, 또는 부호 있는 64비트를 넘는 정수이며, `number` 열에서는 정확한 자릿수 문자열(`"12.345"`)이다 |
| `tables[].header_rows`         | integer, 선택 사항      | `0` 이상이며, 생략하면 `1`: 열 이름을 이루는 값 위에 있는 행. 이름이 소스의 행이 아닌 소스(Parquet 스키마)에는 `0`이며, 이 경우 행은 1부터 센다                                                                                                                                                                                                                        |
| `tables[].locator`             | object                 | 블록의 locator와 같음: 표가 걸쳐 있는 시트(또는 PDF의 페이지)와 행                                                                                                                                                                                                                                 |
| `tables[].attrs`               | object                 | 형식별 정보(숨겨진 시트에는 `hidden`)                                                                                                                                                                                                                                                              |

`tables`를 모르는 서버는 계약에 명시되지 않은 다른 필드와 마찬가지로 이 필드를 버리고, 행 블록은 그대로 게시합니다.

모든 문자열은 유효한 Unicode여야 하며, 모든 숫자는 JSON 숫자여야 합니다(`NaN`이나 `Infinity` 불가). 정수는 부호 있는 64비트(-2^63에서 2^63-1까지이므로 그 범위를 넘는 `UInt64`는 불가)이고, 그 밖의 숫자는 double 범위 안에 있어야 합니다(`1e999` 불가). 서버는 규칙을 하나라도 어기는 아티팩트를 거부하며, 계약에 명시되지 않은 필드는 버리므로 서비스는 버전을 바꾸지 않고도 그런 필드를 보낼 수 있습니다. 이전 서버가 읽을 수 없는 변경은 `schema_version`을 올립니다. 블록의 키와 섹션 경로는 그 블록에서 잘라낸 모든 청크와 함께 전달되므로, 아티팩트의 크기뿐 아니라 개수와 길이에도 제한이 있습니다. 참조 추출기는 더 긴 키를 제외하고, 처음 10,000개의 키와 처음 64개의 섹션 항목(각각 500자로 자름)만 유지합니다. 그리고 해당 블록에 `attrs.keys_truncated` 또는 `attrs.section_truncated`를, 아티팩트의 `coverage.lost`에 (`exact_keys`, `section_path`)를 표시하여 스스로를 완전하다고 보고하지 않게 합니다. 여기와 `char_start`/`char_end`에서 문자는 디코딩된 텍스트의 Unicode 코드 포인트입니다(UTF-16 단위나 바이트가 아님). 줄 또는 문자 범위는 원본 안에 있어야 합니다. 서버는 `char_end`가 원본의 바이트 크기를 넘거나 `line_end`가 그 크기에 1을 더한 값을 넘는 아티팩트를 거부하며, 이 한계는 어떤 인코딩에서도 성립합니다.
