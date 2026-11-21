<!-- i18n: source=docs/reference/extractor-contract.md blob=539b2933c9373bff7d858f2d0b746a85d2d5e253 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md), which is authoritative.

<a id="extractor-contract"></a>

# Contrato del extractor

Un extractor es un servicio HTTP al que el servidor de Loom envía un archivo original y del que recibe su artefacto de extracción. Esta página describe la versión 1 del contrato entre ambos: qué debe servir un servicio, ya sea el extractor de referencia de Loom (`extractor/loom_extractor/`) o uno propio de un operador. Por qué el análisis se ejecuta fuera del servidor, y cómo el servidor lee una respuesta, se explica en [extracción de documentos](../architecture/document-extraction.md).

<a id="configuring-the-server"></a>

## Configurar el servidor

| Ajuste                                       | Significado                                                                                                                                                                                                                                                                  |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_DOCUMENT_EXTRACTOR_URL`                | la URL base a la que va un formato cuando ninguna ruta lo nombra                                                                                                                                                                                                             |
| `LOOM_DOCUMENT_EXTRACTORS_JSON`              | `{format or media type: base URL}`; la clave de un formato prevalece sobre la de su tipo de medio (que se aplica solo al formato de ese tipo de medio), y ambas sobre la URL anterior; una clave que no nombra ningún formato, o una segunda grafía de un mismo tipo de medio, se rechaza al cargar |
| `LOOM_DOCUMENT_EXTRACTOR_TOKEN`              | el token bearer que se envía a un servicio que no tiene uno propio entre los de abajo                                                                                                                                                                                        |
| `LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON`        | `{base URL: token}`: el token propio de un servicio, de modo que ningún servicio conozca el token que acepta otro; cada URL debe ser una de las que nombran los dos ajustes anteriores                                                                                        |
| `LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S`          | segundos que puede tardar una llamada en total: leer las capacidades, o enviar el original y leer la respuesta                                                                                                                                                               |
| `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES` | la respuesta más grande que lee el servidor; una más larga se rechaza como `resource_limit`                                                                                                                                                                                  |

Los nombres de formato son `markdown`, `html`, `text`, `sql`, `toml`, `pdf`, `docx`, `pptx`, `xlsx`, `csv` y `parquet`. Por
ejemplo, para enviar los PDF al servicio propio de un operador y todo lo demás al extractor de referencia:

```bash
LOOM_DOCUMENT_EXTRACTOR_URL=http://extractor:8790
LOOM_DOCUMENT_EXTRACTORS_JSON='{"pdf": "http://pdf-extractor:8080"}'
```

<a id="endpoints"></a>

## Puntos de conexión

Cada llamada incluye `Authorization: Bearer <token>`. Un servicio responde con `401` cuando falta el token o es incorrecto.

<a id="get-v1capabilities"></a>

### `GET /v1/capabilities`

Lo que extrae el servicio. El servidor extrae un formato solo cuando el servicio al que se enruta lo lista aquí, y vuelve a
leer esta información después de un minuto.

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

| Campo              | Significado                                                                         |
| ------------------ | ----------------------------------------------------------------------------------- |
| `contract_version` | `1`; el servidor trata un servicio de otra versión como no disponible               |
| `max_bytes`        | el original más grande que acepta el servicio; el servidor no envía uno mayor       |
| `formats`          | cada formato que extrae el servicio y las opciones que acepta                       |

<a id="post-v1extract"></a>

### `POST /v1/extract`

Los bytes del original son el cuerpo (`Content-Type: application/octet-stream`). La consulta indica lo que pide el servidor:

| Parámetro     | Significado                                                                    |
| ------------- | ------------------------------------------------------------------------------ |
| `format`      | el formato con el que el servidor enrutó el archivo                            |
| `name`        | el nombre del archivo, que puede estar vacío                                   |
| `encoding`    | opcional: el nombre de un códec para un archivo de texto que no puede indicar el suyo |
| `sql_dialect` | opcional, solo SQL: el dialecto con el que se analiza                          |

Un servicio rechaza una opción que no acepta (`invalid_option`). Comprueba el contenido contra `format` y rechaza un
archivo que es de otro formato (`format_mismatch`) en lugar de leerlo como el formato solicitado.

`200` devuelve el JSON del artefacto. El servidor envía `Accept-Encoding: identity`, y un servicio no debe comprimir una
respuesta: el servidor trata una respuesta comprimida, sea cual sea su estado, como si el servicio no estuviera disponible. Una fuente que el servicio
no puede extraer produce un `422` con un cuerpo de error:

```json
{ "error": { "code": "format_mismatch", "message": "the file is pdf, not markdown" } }
```

| Código               | Significado                                                              |
| -------------------- | ------------------------------------------------------------------------ |
| `unsupported_format` | el archivo tiene un formato que ningún extractor admite                  |
| `format_unavailable` | este servicio no extrae el formato                                       |
| `format_mismatch`    | el contenido es de un formato distinto al solicitado                     |
| `encoding`           | el texto no se decodifica                                                |
| `corrupt`            | el archivo está dañado, o el analizador falla con él                     |
| `invalid_option`     | una opción es desconocida o tiene un valor que el servicio rechaza       |
| `too_large`          | el original supera el `max_bytes` del servicio                           |
| `timeout`            | la extracción tardó más de lo que permite el servicio                    |
| `resource_limit`     | la extracción superó un límite de memoria o de salida                    |

El servidor conserva como código del fallo un código de hasta 40 letras minúsculas y `_`, y interpreta cualquier otro, o un cuerpo de error
de más de 64 KiB, como `refused`. Trata `401`, `403`, `407`, `408`, `429`, cualquier `5xx`, cualquier otra respuesta que no sea
`200` ni `4xx` (una redirección, un `204`), `format_unavailable` y una llamada que supera su timeout como si el servicio no estuviera disponible,
de modo que la fuente pueda reintentarse más tarde, y cualquier otro `4xx` como una fuente rechazada. Un `200` cuyo artefacto incumple
las reglas siguientes se rechaza como `invalid_artifact`.

<a id="the-artifact"></a>

## El artefacto

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

| Campo                                  | Tipo                     | Regla                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| -------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `schema_version`                       | entero                   | `1`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `format`                               | cadena                   | el `format` solicitado                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `parser`, `parser_version`             | cadenas no vacías        | lo que generó el artefacto; un cambio de parser produce una versión nueva                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `options`, `attrs`                     | objetos                  | anidados como máximo 16 niveles                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `warnings`                             | lista de cadenas         |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `coverage.unit`                        | cadena no vacía          | de qué se compone el formato: `lines`, `statements`, `keys`, `chars`, `pages`, `paragraphs`, `slides`, `rows`, ...                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `coverage.total`, `coverage.extracted` | enteros                  | `0 <= extracted <= total`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `coverage.failed`                      | lista de `{unit, reason}` | cada unidad no extraída                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `coverage.lost`                        | lista de cadenas         | capacidades que el artefacto no puede ofrecer, como `sql_structure`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `coverage.complete`                    | booleano, opcional       | si se envía, igual a `extracted == total` sin fallos ni pérdidas                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `blocks`                               | lista                    | en orden de lectura                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `blocks[].id`                          | cadena no vacía          | única en el artefacto                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `blocks[].type`                        | cadena                   | `heading`, `paragraph`, `list`, `code`, `sql_statement`, `toml_table` o `toml_key`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `blocks[].text`                        | cadena                   |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `blocks[].parent`                      | cadena, opcional         | el id de un bloque anterior que lo contiene                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `blocks[].depth`                       | entero, opcional         | `0` o más; el nivel de un encabezado                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `blocks[].section_path`                | lista de cadenas         | los encabezados por encima del bloque; como máximo 64, cada uno de como máximo 500 caracteres                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `blocks[].keys`                        | lista de cadenas         | nombres exactos por los que se puede buscar el bloque; como máximo 10,000, cada uno de como máximo 1,024 caracteres                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `blocks[].attrs`                       | objeto                   | datos del formato (`table_id` en un bloque de filas: el id de la tabla de la que provienen sus filas; `table` en un bloque de tabla DOCX, PPTX o HTML: sus `rows` y `columns`; `orientation` en un bloque PDF de texto que corre en otra dirección que el de su página; `ocr` en un bloque leído de una imagen)                                                                                                                                                                                                                                                                                                                                                                              |
| `blocks[].locator`                     | objeto                   | `line_start`/`line_end` (desde 1, ambos `0` o `1 <= start <= end`), `char_start`/`char_end` (desplazamientos en el texto decodificado, ambos `-1` o `0 <= start <= end`), `key_path`, `page_start`/`page_end` (las páginas de un PDF o las diapositivas de una presentación, desde 1, ambos `0` o `1 <= start <= end`), `sheet` (el nombre de la hoja de un libro, como máximo 500 caracteres), `row_start`/`row_end` (las filas de una hoja o de un archivo CSV, contando el encabezado, las filas de un archivo Parquet desde 1, o las filas de una tabla PDF, con su encabezado como fila 1; desde 1, ambos `0` o `1 <= start <= end`) |

Una fuente tabular (un archivo CSV, las hojas de un libro, un archivo Parquet) y un PDF que contiene tablas también incluyen `tables`, junto a los bloques de filas por los que se buscan:

| Campo                          | Tipo                   | Regla                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tables`                       | lista, opcional        | cada tabla de la fuente                                                                                                                                                                                                                                                                                                                                                                    |
| `tables[].id`, `tables[].name` | cadenas                | el id único y no vacío; el nombre: el de la hoja, el del archivo, o la página y el lugar de una tabla PDF (`page 3 table 1`)                                                                                                                                                                                                                                                               |
| `tables[].columns`             | lista de `{name, type}` | como máximo 16,384; un nombre de como máximo 500 caracteres; un tipo `string`, `number`, `bool`, `date`, `datetime`, `time`, `duration` o `mixed` (lo que es cada valor que contiene; las fechas y las fechas con hora juntas son `datetime`)                                                                                                                                              |
| `tables[].rows`                | lista de listas        | cada una tan larga como `columns`; cada valor una cadena, un número, un booleano, null, o un error de celda `{"error": "#DIV/0!"}`; una fecha o una hora es texto ISO, una duración `H:MM:SS` con horas más allá de un día (`36:00:00`), un decimal, o un entero más allá de 64 bits con signo, en una columna `number` sus dígitos exactos (`"12.345"`) |
| `tables[].header_rows`         | entero, opcional       | `0` o más, `1` si se omite: las filas situadas sobre los valores que dan nombre a las columnas; `0` para una fuente cuyos nombres no son una fila de ella (un esquema de Parquet), cuyas filas se cuentan entonces desde 1                                                                                |
| `tables[].locator`             | objeto                 | como el de un bloque: la hoja (o las páginas de un PDF) y las filas que abarca la tabla                                                                                                                                                                                                                |
| `tables[].attrs`               | objeto                 | datos del formato (`hidden` para una hoja oculta)                                                                                                                                                                                                                                                       |

Un servidor que no conoce `tables` lo descarta, como a cualquier campo que el contrato no nombra, y aun así publica los bloques de filas.

Toda cadena es Unicode válido y todo número es JSON (sin `NaN` ni `Infinity`): un entero es de 64 bits con signo (-2^63 a 2^63-1, por lo que no es un `UInt64` más allá de ese límite), y cualquier otro número cabe en un double (no `1e999`). El servidor rechaza un artefacto que incumpla cualquier regla y descarta un campo que el contrato no nombra, de modo que un servicio puede enviarlo sin un cambio de versión. Un cambio que un servidor más antiguo no pueda leer aumenta `schema_version`. Las claves de un bloque y la ruta de sección viajan con cada fragmento cortado de él, por lo que están acotadas por número y longitud, además de por el tamaño del artefacto; el extractor de referencia omite una clave más larga, conserva las primeras 10,000 y las primeras 64 entradas de sección recortadas a 500 caracteres, y marca el bloque con `attrs.keys_truncated` o `attrs.section_truncated` y el `coverage.lost` del artefacto (`exact_keys`, `section_path`), para que no se declare completo. Un carácter, aquí y en `char_start`/`char_end`, es un punto de código Unicode del texto decodificado (no una unidad UTF-16 ni un byte). Un rango de líneas o de caracteres queda dentro del original: el servidor rechaza un artefacto con un `char_end` mayor que el tamaño del original en bytes o un `line_end` mayor que ese tamaño más uno, límites que se cumplen en cualquier codificación.
