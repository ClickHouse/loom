<!-- i18n: source=docs/architecture/document-extraction.md blob=43e95a2510b1d98965d48e5a1b079a7c345a8d5c model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-extraction.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-extraction.md), which is authoritative.

<a id="document-extraction"></a>

# Extracción de documentos

Un original subido se analiza y se convierte en una única estructura, el artefacto de extracción, mediante un servicio extractor: un proceso independiente con
sus propias dependencias, nunca el servidor de Loom. El servidor envía los bytes del original al servicio al que se enruta su formato,
lee el artefacto de vuelta como entrada no confiable y corta fragmentos estructurales a partir de sus bloques, de modo que una sentencia SQL, una clave TOML
y una sección de Markdown se citan mediante un localizador dentro de la fuente. Loom incluye un extractor de referencia
(`extractor/loom_extractor/`) para Markdown, HTML, texto plano, SQL, TOML, CSV, PDF, DOCX, PPTX, XLSX y, con
`pyarrow` instalado, Parquet; un operador puede ejecutar otro que respete el mismo
[contrato de comunicación](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md). `knowledge.ingest` extrae de este modo un original subido
([ingesta](#ingestion)), y la sincronización de fuentes de documentos lo invoca para los archivos PDF, `.pptx` y `.docx` que contiene una
carpeta registrada ([sincronizar una fuente](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#syncing-a-source)); `knowledge.add` recibe
texto que ya extrajo el llamador.

<a id="the-service-boundary"></a>

## El límite del servicio

| Lado              | Contiene                                                                                                                                                                                 |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Servidor de Loom  | el contrato (`server/knowledge/domain/extract/`), las comprobaciones sobre lo que devuelve un extractor, el enrutamiento por formato, el chunker estructural y el cliente (`server/knowledge/app/extractors.py`) |
| Servicio extractor | detección de formato, decodificación estricta y todos los parsers; sin token de Loom, sin credenciales de base de datos y sin acceso a buckets: recibe bytes y devuelve JSON                |

Analizar un archivo significa ejecutar un parser sobre una entrada que controla quien la sube. Mantener eso fuera del
proceso del servidor mantiene un defecto del parser, una entrada patológica o las dependencias de un parser lejos del
proceso que guarda las credenciales de ClickHouse, las claves de firma y los datos de los inquilinos, y permite que un
operador incorpore su propia utilidad para un formato. `tests/unit/test_package_contracts.py` comprueba que ningún
módulo del servidor importa el extractor, sqlglot o pypdf, y que el plano de conocimiento no importa ninguna
biblioteca de parsers.

**Enrutamiento.** El servidor determina el formato a partir de lo que indica quien llama (un formato declarado; si no,
la extensión del archivo; si no, el tipo de medio; si no, `text`) sin leer los bytes, y envía el original al servicio
que `LOOM_DOCUMENT_EXTRACTORS_JSON` indica para ese formato, o para su tipo de medio cuando este corresponde a ese
formato, y si no, a `LOOM_DOCUMENT_EXTRACTOR_URL`. Un formato se extrae solo cuando el `/v1/capabilities` de ese
servicio lo declara; la declaración se vuelve a leer pasado un minuto. Un nombre `.doc`, `.xls` o `.ppt` se rechaza
antes de cualquier llamada. Una clave de ruta es un nombre de formato o un tipo de medio que corresponde a uno,
comparado sin sus parámetros y sin distinguir mayúsculas; cualquier otra clave se rechaza al cargar la configuración.
Lo que un despliegue anuncia es lo que dicen estas declaraciones: `knowledge.formats` informa un formato como
disponible cuando un servicio al que puede ir un archivo de ese formato lo declara, y como desconocido mientras ese
servicio no se puede leer (un formato servido solo por la ruta de un tipo de medio se lee cuando la subida lleva ese
tipo de medio). Lee los servicios en conjunto en 5 s; los formatos de uno que no puede leer en 5 s quedan como
desconocidos hasta por un minuto, hasta que una lectura de sus capacidades tenga éxito.

**Fallo cerrado.** Un formato sin servicio configurado, un servicio que no lo declara, que no es alcanzable, que tarda
más de `LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S` en una llamada completa, que rechaza el token del servidor (`401`, `403`,
`407`), que responde `408`, `429`, `format_unavailable`, un `5xx` o cualquier otra cosa que no sea `200` ni `4xx` (una
redirección, un `204`) provoca `ExtractorUnavailable`, y la fuente puede reintentarse. Una fuente que el servicio
rechaza provoca `ExtractionFailed` con el código del contrato (`format_mismatch`, `encoding`, `too_large`, `timeout`,
`resource_limit`, ...), igual que una respuesta que incumple el contrato (`invalid_artifact`). No hay alternativa en el
mismo proceso: sin extractor configurado, no se extrae ningún original.

**Tokens.** El servidor envía a cada servicio el token que `LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON` guarda para su URL, y
si no `LOOM_DOCUMENT_EXTRACTOR_TOKEN`, de modo que el servicio propio de un operador no necesita tener el token que
acepta el extractor de referencia.

**Salida no confiable.** El servidor pide la respuesta sin comprimir (`Accept-Encoding: identity`), trata una
comprimida como servicio no disponible y lee un artefacto hasta `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES`,
rechazando uno más largo como `resource_limit`. Después `parse_artifact` lo comprueba antes de trocearlo, generar
embeddings o publicarlo: la versión del esquema, el formato solicitado, el tipo de cada bloque, un id único y un padre
que sea un bloque anterior, como máximo 10,000 claves de 1,024 caracteres y 64 entradas de ruta de sección de 500
caracteres por bloque (de modo que los chunks cortados de un bloque largo comparten sus claves y el trabajo por chunk
sigue siendo lineal), spans de localizador que sean rangos, recuentos de cobertura dentro de su total, cadenas que sean
Unicode válido, números que sean JSON (sin `NaN` ni `Infinity`; enteros de 64 bits con signo (-2^63 a 2^63-1), otros
números dentro de un double), la forma de cada tabla (un id único, como máximo 16,384 columnas tipadas con nombres de
como máximo 500 caracteres, filas tan largas como sus columnas, con escalares o errores de celda, y una hoja y filas
que sean un localizador) y la profundidad de anidamiento en `attrs` y `options` (16; el parser TOML de referencia omite
el `attrs.value` de un valor anidado a más de 8, cuyo texto conserva el bloque). `complete` se recalcula a partir de
los recuentos, los fallos y las pérdidas, y se rechaza un artefacto cuyo indicador no coincide. Un campo que el
contrato no nombra se descarta. El servidor direcciona el artefacto por el SHA-256 de su propia serialización, no de
los bytes del servicio.

**Aislamiento en el extractor de referencia.** Cada llamada `/v1` lleva `Authorization: Bearer <token>`; el servicio se
niega a iniciar sin un token de al menos 16 caracteres. Cada extracción se ejecuta en un proceso hijo y, antes de que
el hijo lea la fuente:

| El hijo                                  | Cómo                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| no tiene entorno                         | se inicia solo con `PYTHONPATH`, `PATH` y `LC_ALL`, de modo que el token no está en él                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| no puede leer el servicio ni a otro hijo | el servicio y cada hijo son no volcables (`PR_SET_DUMPABLE`), de modo que un proceso del mismo usuario no puede leer su entorno, memoria ni archivos abiertos a través de `/proc` o ptrace                                                                                                                                                                                                                                                                                                                                                         |
| tiene recursos acotados                  | espacio de direcciones (`LOOM_EXTRACTOR_MEMORY_MB`), segundos de CPU, un tamaño de archivo de 0 (puede crear archivos vacíos, no escribir en uno), sin volcado de memoria                                                                                                                                                                                                                                                                                                                                                                          |
| no inicia procesos                       | `RLIMIT_NPROC` 0, de modo que nada de lo que inicie un parser sobrevive a la extracción; un hijo que sigue en ejecución tras un timeout, una desconexión o una cancelación se mata con su grupo de procesos antes de liberar su slot                                                                                                                                                                                                                                                                                                                |
| no abre sockets                          | un filtro seccomp establecido tras no-new-privileges hace que `socket`, `socketpair` y `io_uring_setup` fallen con EPERM, y también `ptrace`, `process_vm_readv`, `process_vm_writev` y `pidfd_getfd` (x86_64 y aarch64; el servicio se niega a iniciar en otra arquitectura), así que no alcanza ninguna dirección, incluido el servidor de Loom en su red. Un hermano concurrente sigue siendo escribible a través de `/proc/<pid>/mem` hasta que es no volcable, de modo que con `LOOM_EXTRACTOR_CONCURRENCY` mayor que 1 un hijo tomado podría aún modificar uno antes de filtrarse a sí mismo |
| no gana privilegios al hacer exec        | `PR_SET_NO_NEW_PRIVS` en el servicio y en cada hijo, de modo que ningún binario setuid ni capacidad de archivo eleva uno                                                                                                                                                                                                                                                                                                                                                                                                                          |
| carga solo el paquete                    | se ejecuta en `/` con `-P`, de modo que ningún directorio actual ni de script está en `sys.path`; el paquete y sus dependencias se instalan como solo lectura para el usuario del servicio, ya que un archivo propio en `sys.path` podría cambiar lo que importa cada hijo posterior                                                                                                                                                                                                                                                              |
| no muestra ninguna solicitud             | su línea de comandos contiene solo sus límites; el nombre del archivo y las opciones llegan por stdin                                                                                                                                                                                                                                                                                                                                                                                                                                             |

El servicio mata a un hijo al llegar al timeout de tiempo real, cuando su llamador se desconecta o cuando su salida
supera el límite del artefacto, y lee una fuente solo cuando uno de sus slots de `LOOM_EXTRACTOR_CONCURRENCY` está
libre, de modo que ese límite cubre también los originales retenidos en memoria. Por defecto hay un slot: un hijo es
legible por otros procesos de su usuario desde su inicio hasta que se hace no volcable, así que un hijo tomado por una
fuente hostil podría abrir en ese momento la fuente de un hijo concurrente. Escale con réplicas del servicio, no con
slots. El kernel no sujeta al límite de procesos a root, ni a un proceso con `CAP_SYS_RESOURCE` o `CAP_SYS_ADMIN`, así
que el servicio se niega a iniciar como root o con cualquiera de esas capacidades permitidas. A un hijo no se le
niegan las lecturas del sistema de archivos en su proceso: lo que las niega es el contenedor en el que se ejecuta. El
servicio se niega a iniciar en una arquitectura que su filtro de sockets no cubre. La configuración está en
[`extractor/README.md`](https://github.com/ClickHouse/loom/blob/main/extractor/README.md).

<a id="the-artifact"></a>

## El artefacto

| Parte      | Contiene                                                                                                                                                                                                                                                                                                |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `blocks`   | la estructura de la fuente en orden: `heading`, `paragraph`, `list`, `code`, `sql_statement`, `toml_table`, `toml_key`, ...; cada uno con su texto, localizador, ruta de sección y padre                                                                                                                |
| `tables`   | las tablas que contiene una fuente: cada una con un nombre, columnas tipadas, sus filas de valores, un número de filas de encabezado y un localizador (CSV, XLSX, PDF, Parquet)                                                                                                                         |
| `coverage` | la unidad de la que se compone el formato (`lines`, `statements`, `keys`, `chars`, `pages`, `paragraphs`, `slides`, `rows`), cuántas hay, cuántas se extrajeron, cada unidad fallida con su motivo y cada capacidad perdida                                                                              |
| `warnings` | hechos que un lector debería ver, como un dialecto SQL ambiguo                                                                                                                                                                                                                                          |
| `options`  | las sobrescrituras con las que se ejecutó el análisis (`sql_dialect` solo para SQL, `encoding` por el nombre de su códec)                                                                                                                                                                               |
| `attrs`    | datos del formato (`dialect`, `dialect_source`, `dialect_candidates`; el `title` de una página HTML o de un PDF; las `pages` y `furniture` de un PDF, las `slides` de una presentación, las `sheets` de un libro; `images_unread`, las páginas de PDF o diapositivas de PPTX con texto cuyas imágenes, gráficos, diagramas u objetos incrustados no se leen) |

El `id` de un bloque es `b<ordinal>`, estable dentro de su artefacto. Las `keys` son los nombres exactos por los que un
lector puede buscar el bloque: los objetos que una sentencia SQL crea, escribe y lee, y sus columnas (para una
sentencia que sqlglot no puede analizar, cada nombre que contienen sus tokens), cada nombre calificado también por su
última parte (`events` para `analytics.events`), sin comillas (`my.table` para ``db.`my.table` ``; una parte entre
comillas conserva sus puntos); la ruta completa de una tabla o clave TOML y su forma sin índices.

Un localizador establece solo los campos que corresponden: `line_start`/`line_end` (desde 1) en todos los formatos de
texto, `char_start`/`char_end` (desplazamientos en el texto decodificado) en Markdown, texto plano, SQL y los bloques de
texto de un archivo TOML no válido, `key_path` para TOML, `page_start`/`page_end` (desde 1) para las páginas de un PDF y
las diapositivas de una presentación PPTX, y `sheet` y `row_start`/`row_end` (contando el encabezado) para las hojas de
un libro y las filas de un archivo CSV, y para las filas de una tabla de PDF con sus páginas. Un DOCX no tiene páginas
propias (la paginación es del renderizador), así que sus bloques no llevan localizador y una cita nombra su ruta de
sección. El rango de un encabezado Markdown abarca su línea o líneas de origen, marcado incluido, mientras que su texto
es el título limpio, de modo que una ventana de un encabezado largo cita el rango completo del encabezado. Un fragmento
cortado de varios bloques abarca sus líneas, caracteres, páginas y filas, y conserva una hoja cuando todos nombran la
misma.

El artefacto se serializa como JSON compacto y ordenado con un `schema_version` y se direcciona por el SHA-256 de esos
bytes. Un cambio de analizador o de opción produce un artefacto nuevo de la misma revisión de la fuente; un artefacto
nunca se edita. Un lector rechaza un `schema_version` que no conoce. `parser_version` nombra aquello de lo que depende un
análisis: la versión de sqlglot para SQL, la de pypdf para PDF (y la de Tesseract cuando el OCR leyó una página o una
diapositiva), y la versión de Python para HTML, TOML, CSV y XLSX, cuyos analizadores se distribuyen con ella (`csv` y su
detector de formato; `datetime.fromisoformat` para una fecha de XLSX escrita como texto).

<a id="formats-in-the-reference-extractor"></a>

## Formatos en el extractor de referencia

La fidelidad con que los lectores de PDF, DOCX y PPTX conservan el texto, los encabezados, las tablas y las páginas de un documento, frente a una línea base de modelo de diseño, se mide en los [resultados de análisis de documentos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing).

`detect_format` lee los bytes antes que el nombre del archivo. Un PDF, un contenedor DOCX, PPTX o XLSX (por las partes que hay dentro del zip) y un archivo Parquet se reconocen por su contenido; un nombre que declara un formato que los bytes no son se rechaza (`format_mismatch`), y un archivo binario heredado de Office se rechaza (`unsupported_format`), igual que un nombre `.doc`, `.xls` o `.ppt` salvo que se declare un formato. El texto se decodifica de forma estricta: UTF-8, UTF-16 o UTF-32 según su marca de orden de bytes, o una codificación declarada, y la marca de orden de bytes de UTF-8 se descarta cuando el texto es UTF-8, declarado o no, igual que la marca de un códec de orden de bytes declarado (`utf-16-le`). Un byte no decodificable es un fallo `encoding` que indica su desplazamiento, y un byte NUL en texto UTF-8 no declarado (tras una marca de orden de bytes o sin ella) es binario; ninguno se reemplaza ni se indexa.

| Formato    | Analizador                                                            | Bloques                                                                      |
| ---------- | --------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Markdown   | un analizador de bloques según las reglas de CommonMark para encabezados, bloques de código y bloques HTML | encabezados, párrafos, listas, código delimitado, front matter               |
| Texto plano | un divisor de párrafos                                               | un párrafo por cada serie de líneas no vacías (una línea con `#` es texto, no un encabezado) |
| HTML       | el analizador de la biblioteca estándar                               | encabezados, párrafos, elementos de lista, tablas, citas, definiciones, `pre` |
| SQL        | `sqlglot`, nunca se ejecuta                                           | un bloque por sentencia                                                      |
| TOML       | `tomllib` y un escáner de líneas                                      | un bloque por cada encabezado de tabla y por cada clave                      |
| PDF        | `pypdf`: las posiciones de sus fragmentos de texto y las líneas que dibuja; OCR | por página: encabezados, párrafos, listas, código, tablas; el mobiliario de página se omite |
| DOCX       | los lectores de zip y XML de la biblioteca estándar                   | encabezados, párrafos, listas, tablas, notas al pie y notas finales          |
| PPTX       | los lectores de zip y XML de la biblioteca estándar                   | por diapositiva: su título como encabezado, cada forma de texto, tablas, notas del orador |
| CSV        | el `csv` de la biblioteca estándar                                    | una tabla; sus filas como bloques de hasta 20 filas                          |
| XLSX       | los lectores de zip y XML de la biblioteca estándar                   | por hoja: una tabla, su nombre como encabezado, sus filas como bloques de hasta 20 |
| Parquet    | `pyarrow`, a partir de los bytes en memoria                           | una tabla; sus filas como bloques de hasta 20 filas                          |

**Markdown.** Los encabezados son ATX, setext y `<hN>` de una línea. Un párrafo sobre un subrayado setext (con sangría de hasta tres espacios) es un encabezado; un elemento de lista, una cita o una ruptura temática sobre `---` no lo es. Un bloque delimitado conserva las líneas de su cuerpo sin las vallas; una valla se cierra con una serie sola de su carácter de al menos la misma longitud, y su lenguaje es la primera palabra de la cadena de información, y el resto se conserva como `info` (un bloque vacío también la conserva). Dentro de un párrafo solo una viñeta o un elemento numerado con 1 inicia una lista, así que una línea ajustada que empieza por `2019.` sigue siendo prosa. Una ruptura temática (`***`, `- - -`) es marcado: termina el párrafo o la lista que tiene encima y no es texto de ningún bloque. Un bloque HTML es un bloque de párrafo, tal como está escrito, y nunca el título de un encabezado (condiciones de inicio de CommonMark): el que se abre con un comentario, `<?`, `<![CDATA[`, `<!` (`<!DOCTYPE html>`) o una etiqueta de texto sin procesar (`<pre>`, `<script>`, `<style>`, `<textarea>`) termina en la línea que contiene su marca de cierre (`-->`, `</pre>`), de modo que la línea siguiente puede ser un título setext; el que se abre con una etiqueta de nivel de bloque (`<p
align="center">`, `<div>`) o una línea con solo etiquetas (`</div>`) termina en una línea en blanco. Todos salvo la línea de solo etiquetas terminan un párrafo anterior, y una línea de solo etiquetas dentro de un párrafo es su texto. Un encabezado ATX vacío (`# #`) es marcado que termina el párrafo anterior, mientras que uno cuyo título queda en nada al limpiarlo (`# <img src="logo.svg">`) sigue siendo un párrafo. El front matter YAML inicial (líneas `key:` entre marcadores `---`, y un `#` al inicio de línea seguido de un espacio en blanco es un encabezado, no un comentario) es un bloque. Los bloques llevan rangos de líneas y de caracteres, y la cobertura cuenta las líneas no vacías, cada una de las cuales cae en un bloque o es un delimitador de valla o de front matter, una ruptura temática o un encabezado ATX vacío (se cuenta a partir de los rangos de los bloques, como salvaguarda frente a un cambio del analizador que descarte una línea).

Estas reglas son propias del extractor (`extractor/loom_extractor/markdown.py`). El divisor de encabezados de `knowledge.add`, que divide en secciones el texto que extrajo quien llama, conserva sus propias reglas y puede seccionar el mismo documento de otra manera.

El título de un encabezado lleva como máximo sus primeros 500 caracteres a las rutas de sección que hay debajo de él, con un corte marcado como indican los límites del contrato más abajo; el bloque del encabezado lo conserva entero y se divide en ventanas como cualquier bloque más largo que un fragmento.

**HTML.** `blockquote`, `dd` y `dt` son párrafos, y un `<p>` que abre un elemento de lista, y los `<p>` que le siguen, son el texto del elemento. Un bloque cuya etiqueta de cierre se omite se cierra en el bloque siguiente, en el final de su contenedor o en el final del documento (un contenedor dentro de un bloque lo deja abierto; `br`, `hr` y el inicio de un contenedor separan palabras), y un `head` sin cerrar se cierra en el primer elemento o texto del cuerpo. El rango de líneas de un bloque va de su primera línea de texto a la última. `script`, `style`, `template`, `noscript`, `svg`, `title` y `head` se descartan de los bloques (el texto de `<title>` es el `attrs.title` del artefacto); la cobertura cuenta los caracteres visibles fuera de ellos y cuántos contienen los bloques (sin contar las barras verticales que unen las celdas de una tabla), una salvaguarda del mismo tipo. Ninguna cuenta lo que un bloque pierde dentro de sí, como las etiquetas en línea eliminadas. Un elemento oculto que queda abierto oculta el resto del documento; el texto que hay dentro se cuenta como no leído, con un fallo que nombra los elementos abiertos.

**Tablas HTML.** Una `<table>` es un bloque, una fila por línea con sus celdas unidas por una barra vertical y `attrs.table` con sus `rows` y `columns`, igual que una tabla DOCX, salvo que se descartan las filas sin texto. Una celda tras el final de una fila inicia la fila siguiente, y el inicio o el final de un grupo de filas (`thead`, `tbody`, `tfoot`) termina la fila abierta. Sus leyendas, con cualquier encabezado o lista que contengan, y su texto fuera de toda celda (entre filas) son párrafos anteriores a ella, en su orden, como los muestra aparte un navegador; una tabla dentro de una leyenda, como un encabezado o una lista allí, se lee como texto de la leyenda.

Las celdas se colocan en columnas como las coloca HTML: un intervalo (`colspan`, `rowspan`) se lee como HTML lee un entero no negativo, y una celda que un `rowspan` arrastra hacia abajo ocupa sus columnas en las filas inferiores, como celda vacía allí, hasta que termina su grupo de filas. Una columna que solo llenan celdas vacías y celdas que abarcan varias columnas es un espaciador y se descarta, salvo que se arrastre hacia ella una celda con texto. La colocación cuenta las celdas de cada fila y las celdas arrastradas hasta ella; una tabla que supera el millón de estas conserva todas las celdas. Una tabla que queda con una sola columna no contiene cuadrícula: sus filas son un párrafo sin `attrs.table`, igual que una página envuelta en una tabla (un correo).

Una tabla cuya celda contiene un encabezado, una lista, un `pre`, una tabla o un segundo párrafo maqueta una página en su lugar (un correo, una página compuesta en las celdas de una tabla): sus celdas, leyendas y texto fuera de toda celda se leen como los bloques que contienen, en orden, y una tabla dentro de una es una tabla. Un encabezado, lista, elemento de lista, definición, `pre` o tabla fuera de toda celda termina la tabla, con su `</table>` omitido o no, y las filas posteriores se leen como párrafos, de modo que una tabla que queda abierta no retiene el resto de la página. El final de un elemento en cuyo interior está la tabla no es el final de la tabla, ya que HTML trata una tabla como un ámbito propio: lo que sigue fuera de toda celda se muestra antes de la tabla, como lo muestra un navegador, y una leyenda que queda abierta lo retiene. El texto de cada párrafo o contenedor fuera de toda celda es suyo.

**Sentencias SQL.** Una sentencia se divide según los tokens del dialecto, de modo que un `;` dentro de una cadena o un comentario no la termina. Las líneas de comando propias de un cliente (`GO`, una `/` sola, `DELIMITER ;;`, comandos con barra invertida) no son SQL: un script que las contiene se divide igualmente por sus tokens `;`, y una sentencia que una línea de comando une es un bloque `UNKNOWN` que la cobertura registra como fallido. Un cuerpo que el cliente envía entero (el `BEGIN … END` de un procedimiento) se corta en sus `;` internos: la cabecera conserva el `CREATE` y su nombre, cada sentencia del cuerpo es un bloque propio con sus propias lecturas y escrituras, y el fragmento de cierre (`END`) es `UNKNOWN`; un cuerpo T-SQL sin `BEGIN … END` puede leerse como completo. No se divide según las reglas propias de un cliente; declara el dialecto y espera que tales scripts informen de la pérdida de `sql_structure`. Su bloque contiene su tipo (`CREATE MATERIALIZED VIEW` distinto de `CREATE VIEW`), el objeto creado, los objetos escritos (la tabla `TO` de una vista, la tabla sobre la que se construye un índice, lo que elimina un `DROP` o `TRUNCATE`, el destino de un `MERGE`), las tablas leídas (una CTE no lo es), sus columnas y sus comentarios (uno tras un `;` en su línea pertenece a esa sentencia). La cobertura cuenta sentencias, no bloques que solo tienen comentarios, y nombra la sentencia fallida por el id de su bloque.

**Dialectos SQL.** Un dialecto declarado se usa tal como se da, solo por su nombre; uno desconocido, o uno con ajustes de sqlglot tras una coma, es `invalid_option`. En caso contrario, cada dialecto candidato lee una ventana del script: el script entero cuando tiene como máximo 50,000 caracteres, o si no un prefijo hasta su `;` número 50 dentro de los primeros 50,000 caracteres, y un prefijo más corto cuando su tokenizador rechaza uno (un corte puede caer dentro de un cuerpo entrecomillado). Un dialecto que tokeniza una ventana más larga queda primero, de modo que un dialecto que rechaza el script no puede ganar con un corte más corto de él; después gana la mayor proporción del texto de sus primeras 50 sentencias convertida en árboles sintácticos (una proporción, para que un dialecto que divide un cuerpo entrecomillado en muchas sentencias pequeñas no gane), y en caso de empate gana el primero según un orden fijo (ClickHouse primero). Un empate entre dialectos que analizan algo se informa como `sql_dialect_ambiguous` con los candidatos empatados, y el script se divide con el primero de ellos cuyo tokenizador lo lee entero; cuando ninguno de los dialectos que leen más del script analiza nada, se usa el primero de ellos con `dialect_source = "default"` y sin candidatos. La detección lee como máximo 50,000 caracteres por ventana y como máximo unas siete ventanas por dialecto, así que un script cuya primera sentencia es más larga, o cuyo primer cuerpo entrecomillado supera todos los cortes, puede detectarse mal: declara su dialecto. Una sentencia que el dialecto elegido no puede convertir en árbol sintáctico sigue siendo un bloque buscable de tipo `UNKNOWN`, se lista en `coverage.failed` y establece `coverage.lost =
["sql_structure"]`; un script que el tokenizador no puede dividir es un bloque así.

**TOML.** `tomllib` decide la validez, los tipos y los valores; el escáner que lo acompaña encuentra líneas, comentarios y el texto original del valor, incluidos los valores que ocupan varias líneas (cadenas multilínea, arrays, tablas en línea). El texto de un bloque de clave es `<full
path> = <value text>` con sus comentarios, escrito para que se lea por sí solo; su rango de líneas apunta al origen. Los comentarios sobre una tabla o clave pasan a su texto y a su rango de líneas; una serie de comentarios que ninguna tabla ni clave sigue es un párrafo propio, y un comentario dentro de un array multilínea queda en el texto de esa clave. Un archivo no válido, incluido uno con un entero fuera de 64 bits con signo, se conserva como bloques de párrafo con `coverage.lost = ["toml_types"]` y el motivo, que nombra la línea o la ruta de la clave cuando puede. El `attrs.value` de una clave es el valor JSON, salvo que una fecha o una hora es su texto ISO y un flotante infinito o NaN es su texto (`inf`, `nan`), sea cual sea su `value_type`.

**PDF.** Una página se lee tal como está maquetada, a partir de la posición que pypdf informa para cada fragmento de texto que muestra y de las líneas que dibuja (`extractor/loom_extractor/pdflayout.py`). El ancho de un fragmento proviene de los anchos de su fuente (`/Widths`, o el `/W` de una fuente CID a través de su mapa ToUnicode, o las métricas de las fuentes estándar), sin el espaciado de caracteres, el espaciado de palabras ni la escala horizontal del estado del texto; una pieza de un operador de texto (cortada donde cambia su fuente, como una ligadura de otra fuente) continúa desde el final de la pieza anterior. Los fragmentos sobre una misma línea base forman una línea, cortada en segmentos donde hay un hueco mayor que el tamaño de la fuente; dos fragmentos que la geometría junta se separan cuando la palabra que formarían no está en la capa de texto de la página y ambas partes sí lo están; un fragmento dibujado dos veces en el mismo lugar (una negrita simulada, una sombra) se lee una vez. Una página se gira de modo que la dirección en que corre la mayor parte de sus fragmentos de texto se lea de izquierda a derecha, así que una página mostrada de lado (`/Rotate`, o contenido dibujado girado) se lee como cualquier otra; el texto en otra dirección se conserva como líneas propias, un párrafo cada una, tras las de la propia página (`attrs.orientation`). Después, en orden:

- un medianil en medio de la página que cruzan como máximo una veinteava parte de las líneas, con texto que llena su columna a cada lado, divide la página en dos columnas, leídas primero la izquierda y luego la derecha entre las líneas de ancho completo;
- una cuadrícula de líneas horizontales y verticales que se encuentran (líneas trazadas o cajas finas rellenas, incluidos los form XObjects) es una tabla con bordes, cuyas celdas son el texto dentro de cada caja, con las filas y columnas vacías descartadas; una caja alrededor de texto no es una tabla;
- una serie de líneas cortadas en segmentos que se sitúan en dos o más bandas de la página es una tabla sin bordes: una línea con texto en la primera banda abre una fila, una sin él se añade a las celdas de la fila anterior (una celda ajustada); un marcador de lista separado del texto de su elemento (`•`, `1.`, la viñeta de una fuente de símbolos) no es una columna;
- el mobiliario de página, una línea que ninguna tabla contiene como fila, en la décima parte superior o inferior de una página, que contiene una letra y cuyo texto, sin contar los dígitos, se repite allí en al menos dos páginas y dos quintos de ellas, o un número de página solo, se omite de los bloques (un candidato en cualquiera de los extremos de una tabla sin bordes, salvo que sea un número de página solo, es una de sus filas únicamente cuando llena todas las columnas, como hace el encabezado propio de la tabla repetido en la parte superior de cada página, o, a menos de una vez y media el espaciado de filas de la tabla, abre una fila o, en la parte superior, llena todas las columnas menos la primera, un encabezado sobre una columna de nombres de fila; así que un encabezado de página corrido arriba, un número de página abajo y un encabezado repetido con una celda que abarca dos columnas no lo son); el artefacto nombra cada tipo (una entrada por un pie de página cuyo número de página cambia) con su primer texto y el número de páginas en que aparece (`attrs.furniture`, los primeros 200 tipos) y advierte de que lo hizo;
- el resto son párrafos (líneas de un mismo estilo, divididas donde el hueco supera en más de un 30% el interlineado más común del documento para ese tamaño), elementos de lista (con un marcador primero) y código (una fuente monoespaciada, con su sangría conservada), y encabezados: un párrafo de hasta tres líneas y veinte palabras (uno numerado que se lee como elemento de lista, una línea), con un tamaño al menos un 12% mayor que el del texto del cuerpo (el tamaño en que está la mayoría de los caracteres) o en negrita con el 95% de ese tamaño o más, y que no termina como termina una oración salvo que esté numerado. Los tamaños mayores tienen rango superior; dentro de un mismo estilo, la numeración (`2.1`) da el nivel.

Cuando el PDF tiene un esquema (marcadores), sus entradas son los encabezados en su lugar: cada una, en el orden del esquema, toma la primera línea posterior a la del encabezado anterior que contiene su título (o un párrafo cuyo texto es su título), con su párrafo dividido en torno a ella, o queda justo después de ese encabezado cuando ninguna línea lo contiene; un encabezado de mayor tamaño que todos los del esquema (un título que el esquema omite) queda por encima de ellos, y los niveles del esquema quedan debajo de él en todas las páginas. Una tabla cuyas filas continúan en la parte superior de la página siguiente en las mismas columnas es una sola tabla, a lo largo de tantas páginas como continúe, con la repetición de su fila de encabezado en la página siguiente descartada.

Una tabla es lo que es la de una fuente tabular (**Tablas** más abajo): una tabla tipada en `tables`, con su primera fila como encabezado, su localizador formado por sus páginas y filas (el encabezado es la fila 1), y sus filas como bloques de líneas `column: value` citados por las páginas y filas que contienen, de modo que un resultado cita sus filas y un conjunto de datos contiene sus valores. Una celda se tipa como un campo CSV solo cuando su valor se lee de vuelta tal como lo muestra la página, así que `1.50` y una versión `1.10` siguen siendo texto. Una página se lee como su capa de texto (su texto dividido en las líneas en blanco, los encabezados del esquema en las líneas que contienen sus títulos) cuando su lectura maquetada contiene menos del 99.8% de las letras y dígitos que contiene su capa de texto (contados sin espaciado ni mayúsculas), cuando contiene más de 5,000 líneas, o cuando no se puede leer su maquetación, con una advertencia que la nombra y dice por qué, de modo que una maquetación que el lector interpreta mal nunca descarta texto. Una página sin texto que dibuja una imagen (un objeto de imagen, o uno dentro de un formulario) se lee con OCR (más abajo) donde el extractor lo tiene, y en caso contrario es una unidad fallida: su texto necesita OCR, por lo que un PDF con una página así no está completo y se publica solo con `allow_partial`; una imagen dibujada en línea, en el contenido de una página o de un formulario, no se lee, y una página así es una unidad fallida que lo dice (una página con texto cuya imagen en línea cubre una quinta parte de ella, sin que su propio texto la llene, es parcial, igual que una página sin texto cuya imagen en línea está junto a imágenes que el OCR lee; una imagen en línea de menos de un punto de ancho no cuenta). Una página con texto cuyo dibujo o maquetación no se puede leer, o que nombra un objeto de imagen que el archivo no contiene o dibuja un formulario anidado dentro de otros cuatro (más profundo de lo que el lector recorre), es parcial cuando puede dibujar una imagen, y una página sin texto en ese estado es una unidad fallida, nunca el archivo. Una imagen que una página tiene en sus recursos y nunca dibuja no cuenta. Una página con texto que dibuja una imagen que cubre una quinta parte de la página o más (una captura de pantalla, una foto de una pizarra) tiene su imagen leída con OCR, cada una de sus líneas como un bloque de párrafo propio tras el texto de la página (`attrs.ocr`), nunca una tabla ni un encabezado, o, sin OCR, no está completa: la página se lee, y una unidad fallida dice que el texto de la imagen necesita OCR. Una imagen que el propio texto de la página ya llena en una décima parte (la capa de texto de un escaneo, dibujada sobre su imagen) no se vuelve a leer. Una página con texto que dibuja una imagen menor, que el OCR deja (una de menos de un punto de ancho o alto no cuenta, ni tampoco una que el propio texto de la página llena), se lee por su texto y se nombra en `attrs.images_unread` (su número) y en una advertencia para el archivo: el texto dibujado solo dentro de una imagen así no se extrae; una página parcial por sus imágenes se nombra en cambio por su motivo, que nombra lo que la detuvo, no cada imagen que contiene. Una página que pypdf no puede leer es una unidad fallida, no el archivo; los encabezados del esquema de una página fallida siguen abriendo sus secciones. Un carácter al que el mapa de una fuente no da ningún carácter Unicode (un sustituto aislado) se lee como U+FFFD, con una advertencia que nombra su página. Un PDF que solo se abre con contraseña es `unsupported_format`.

**OCR.** La imagen del extractor instala las bibliotecas de Tesseract y Leptonica y el modelo inglés de Tesseract (`LOOM_EXTRACTOR_OCR_LANGUAGES` nombra otros que la imagen contenga; el servicio se niega a arrancar cuando falta uno, y avisa una vez cuando las bibliotecas no están instaladas); el proceso hijo de extracción las carga en su propio proceso mediante ctypes (`extractor/loom_extractor/ocr.py`), ya que no puede iniciar ningún programa, con OpenMP limitado a un hilo, y con la primera imagen que lee. Una compilación con `OCR=off`, o `LOOM_EXTRACTOR_OCR=off`, no lee ninguna imagen, y las reglas anteriores dicen qué queda entonces incompleto. Una imagen de PDF se lee como el archivo que contiene (JPEG, JPEG 2000, fax CCITT, que pypdf envuelve como TIFF) o sus muestras en bruto (gris, RGB, CMYK por su negro, o un índice de gris o RGB, de 1 u 8 bits) como un PNM; JBIG2, que solo decodifica un programa aparte, no se lee, y lo dice. Una imagen tiene como máximo 40 M de píxeles, decodificada o escalada, y su tamaño se lee de su propia cabecera antes de decodificarla (el de un GIF recorriendo sus bloques en busca de su pantalla y cada fotograma, ya que cada uno se decodifica a su propio tamaño), y una imagen de PDF mostrada con menos de un punto de ancho (un espaciador, un píxel de seguimiento) no se lee, ya que no contiene texto; una mostrada a menos de 200 píxeles por pulgada se escala primero a unos 300. La imagen se lee en gris, y Tesseract fija su propio umbral; una mayormente oscura (texto claro sobre una pantalla oscura) se lee invertida a clara. Antes de leer las palabras, los trazos de más de media pulgada de ancho o un tercio de pulgada de alto (la cuadrícula de una tabla con bordes; como máximo 300 píxeles de ancho y 210 de alto, como a 600 píxeles por pulgada, así que una letra más alta que eso, unos 36 puntos a 300 píxeles por pulgada o 18 a 1,200, pierde su asta con ellos, y una región oscura más gruesa que una décima de pulgada, como el fondo de un encabezado, no es un trazo, ni lo es un trazo junto a ella a esa misma distancia (una regla que toca una fila de encabezado oscura) ni los huecos entre las letras claras que contiene) se borran de la imagen (los tamaños son los de 600 píxeles por pulgada como máximo, de modo que una captura mostrada con finura se lee a su propio tamaño y su limpieza sigue siendo barata), y son las reglas de la página para sus tablas con bordes, ya que Tesseract lee las celdas de una tabla con bordes como una imagen; una palabra junto a una regla así pierde un trazo que queda en su borde (`[Owner`, `rota-8|`), una palabra formada solo por barras verticales se descarta (una línea de cuadrícula demasiado clara para encontrarla), y las demás palabras se leen tal cual (`timeout = 30`, `__init__`). Cada palabra cae en la página donde se dibuja la imagen, con el tamaño en puntos que Tesseract lee de la altura de la x de su línea, que da a los 300 píxeles por pulgada a que lee y que se convierte al tamaño al que se muestra la imagen (de él salen los encabezados, con un paso del 8% en lugar del 12%, ya que el OCR lee puntos enteros, y los tamaños de menos de 6 puntos, a los que se reducen los tamaños del OCR en una imagen mostrada con finura, se comparan en décimas de punto en lugar de medios), de modo que la lectura de maquetación anterior forma párrafos, encabezados y tablas de una página escaneada, y sus bloques se marcan con `attrs.ocr`. Un archivo lee como máximo `LOOM_EXTRACTOR_OCR_MAX_IMAGES` imágenes (30), cada imagen una sola vez por muchas veces que la muestren páginas o diapositivas, como máximo 50 en una página entre las imágenes que lee con OCR, y el reconocimiento de una imagen dura como máximo 20 segundos; una página cuya imagen supera cualquiera de estos límites, o cuyo dibujo es demasiado grande para reunir todas las imágenes, o que el OCR no puede leer, es parcial (o, sin texto propio, una unidad fallida) y nombra el motivo. El artefacto lista las páginas leídas con OCR (`attrs.ocr_pages`; `attrs.ocr_slides` en una presentación), y `parser_version` nombra la versión de Tesseract cuando leyó alguna. El motor de OCR añade unos 44 MB a la imagen (12.5 MB comprimida). El OCR lee lo que una imagen muestra como texto, no la estructura de un gráfico ni de un diagrama.

**DOCX y PPTX.** Ambos son zips de partes XML, leídos con la biblioteca estándar: ninguna biblioteca de XML o de imágenes de terceros se ejecuta sobre un archivo hostil. Una parte debe estar almacenada o comprimida con deflate (los métodos que escribe OOXML) y declarar como máximo 256 MiB descomprimida, y las partes de un paquete como máximo 512 MiB en total; una parte se descomprime solo hasta lo que declara, de modo que una bomba zip se detiene ahí. Una parte que declara un DOCTYPE (en cualquier codificación) se rechaza, así que no se expande ninguna entidad. Una parte que no se puede leer (dañada, por encima del límite, que no es XML, en una codificación que el analizador XML no lee) hace fallar la unidad a la que pertenece: una diapositiva, las notas de una diapositiva, los estilos, las notas al pie; la parte del documento en sí hace fallar el archivo como `corrupt`. Un encabezado DOCX es un párrafo de un estilo `Heading N` o `Title`, o uno con un nivel de esquema propio o a lo largo de la cadena `basedOn` de su estilo, y decide el más cercano establecido (el nivel 9, texto del cuerpo, no es un encabezado sea cual sea el estilo); una vez que un párrafo `Title` se lee como encabezado, tiene profundidad 1 (sea cual sea el nivel de esquema que lleve) y todo otro encabezado posterior queda un nivel por debajo del suyo (`Heading 1` en la profundidad 2), de modo que las secciones quedan bajo el título, y un título posterior vuelve a ser de profundidad 1; un párrafo numerado o de un estilo `List` es un elemento de lista, y una serie de ellos es un bloque de lista. El texto insertado se lee, y una eliminación con control de cambios (sus ejecuciones, filas y celdas) y el texto movido en su lugar antiguo no se leen; el texto de un control de contenido o de un elemento XML personalizado se lee en su sitio, en el cuerpo, las filas de una tabla y sus celdas, y una opción de contenido alternativo una vez. Los encabezados de página, los pies de página y los comentarios no son el cuerpo y no se leen; las notas al pie y las notas finales siguen al cuerpo (`attrs.note`). Un documento incrustado (`altChunk`) es una unidad fallida, igual que un documento de imágenes, gráficos, diagramas u objetos incrustados y sin texto. Un documento de texto advierte cuando contiene lo que un dibujo muestra y el lector no lee, en su cuerpo, sus tablas o sus notas: una imagen (en una imagen, un grupo o el relleno de una forma, el relleno de imagen de una forma VML, o el contenido de respaldo de un contenido alternativo, donde un dibujo que no conoce, como un modelo 3D, conserva su imagen; el texto dibujado allí es el de la opción, leído), texto dibujado (WordArt, el de un lienzo bloqueado), un gráfico, un diagrama o un objeto incrustado; el fondo de una página no se lee ni se nombra. Una eliminación con control de cambios se omite, y un cuadro de texto (su texto se lee), una línea o una forma simple no advierten. Una presentación PPTX se lee en el orden de sus diapositivas (la lista de diapositivas de la presentación, no los nombres de las partes): el título de una diapositiva es un encabezado bajo el que quedan sus demás bloques, cada forma de texto (las agrupadas en su lugar, y una forma en contenido alternativo mediante su primera opción) es un bloque, y las notas del orador son un bloque marcado `attrs.notes`; una diapositiva oculta (`show` falso) se lee y se marca `attrs.hidden`. Una imagen que cubre una quinta parte de su diapositiva o más se lee con OCR en un bloque de párrafo marcado `attrs.ocr` tras las formas de la diapositiva (una imagen que muestran varias diapositivas se lee una vez, y las imágenes se descomprimen dentro de un límite propio, aparte de las partes del paquete), y sin OCR, o para una imagen enlazada en lugar de contenida en el archivo, la diapositiva es una unidad fallida que dice por qué. El tamaño de una imagen es el suyo, escalado por cada grupo en que se encuentra, o si no el de su marcador de posición en el diseño de la diapositiva (por índice, luego por tipo) y luego en su patrón; el que no se indica en ninguna parte se toma como el de la diapositiva, y una presentación que no indica tamaño de diapositiva tiene el predeterminado de 10 por 7.5 pulgadas. Una diapositiva cuyas imágenes, gráficos, diagramas u objetos incrustados llevan todo lo que muestra, sin texto, es una unidad fallida, salvo que el OCR haya leído sus imágenes; una cuyo único texto es lo que leyó el OCR es también una unidad fallida cuando contiene un gráfico, un diagrama o un objeto incrustado. También lo es un id de diapositiva cuya relación no nombra ninguna parte de diapositiva, una diapositiva anidada más profundamente de lo que el lector recorre, y una diapositiva cuyas relaciones no se pueden leer, o cuya imagen toma su tamaño de un marcador de posición de un diseño o patrón que no se puede leer (su texto se sigue leyendo). Una diapositiva de texto con una imagen que el OCR no lee (una de menos de una quinta parte de la diapositiva, una forma, grupo o celda de tabla rellenos con una imagen, una imagen como fondo propio de la diapositiva, o la imagen de respaldo de un dibujo que el lector no conoce, como la tinta, o de una ecuación, cuyas matemáticas no lee; un fondo que da su diseño, patrón o tema no se lee), un gráfico, un diagrama o un objeto incrustado se lee por su texto y se nombra en `attrs.images_unread` (su número) y en una advertencia para el archivo, salvo que la diapositiva haya fallado por lo que muestra: su fallo nombra lo que la detuvo, no todo lo que contiene. Una tabla, en cualquiera de los dos formatos, es un bloque, una fila por línea con sus celdas unidas por una barra vertical, y `attrs.table` con sus `rows` y `columns`.

**Tablas (CSV, XLSX, Parquet).** Una fuente tabular es `tables` en el artefacto: cada una con un encabezado (la primera fila con un valor, o dos en XLSX, más abajo; el esquema de un archivo Parquet), sus columnas tipadas por todos los valores que contienen (`string`, `number`, `bool`, `date`, `datetime`, `time`, `duration` o `mixed`; las celdas vacías y los errores no cuentan; una columna de fechas y fechas con hora es `datetime`), y sus filas de valores. Las mismas filas son bloques, de veinte en veinte y de como máximo 1,000 caracteres (una fila más larga es un bloque aparte, por debajo de los 1,500 caracteres predeterminados de un fragmento, de modo que un fragmento cita las filas que contiene), cada fila una línea de pares `column: value`, citada por hoja y filas, de modo que las ramas léxica y vectorial las buscan como cualquier pasaje; un encabezado sin fila de texto debajo (sin filas, o un archivo Parquet cuyos valores se omitieron todos) es un bloque propio cuando nombra una columna, y un bloque empieza y termina en una fila con texto. El nombre de una columna tiene como máximo 500 caracteres de sus celdas de encabezado (uno cortado más corto es una unidad fallida, `<table> header`). El texto de fila de un flotante es lo que muestra una hoja, hasta 15 cifras significativas (`0.1+0.2` se lee `0.3`), y la tabla conserva su valor exacto. En CSV y XLSX un número que no es un número JSON (un entero que supera 64 bits con signo, un infinito, un NaN) se lee como flotante o como texto, y un valor que ninguna otra regla lee (un índice de estilo de 5,000 dígitos, una fecha redondeada más allá del año 9999) se lee como número o como texto, sin hacer nunca fallar el archivo; Parquet lee cada uno según el tipo de su columna (más abajo).

**CSV.** Para un nombre `.tsv` el delimitador es un tabulador, sin comillas. En caso contrario es una coma, un punto y coma, un tabulador o una barra vertical (un archivo separado por tabuladores enviado sin ese nombre se analiza, de modo que lee las comillas como lo hace CSV), deducido a partir de los campos entrecomillados en las líneas completas de los primeros 4 KiB del archivo, o si no a partir del recuento de delimitadores en las líneas completas de sus primeros 64 KiB (cada muestra se corta a mitad de línea solo cuando su primera línea la supera); un campo se entrecomilla con `"`, con sus comillas duplicadas, sea cual sea la deducción del analizador, y un valor es texto salvo que esté escrito como un número simple, una fecha ISO o una fecha y hora ISO (con su zona conservada). Una fila contiene solo sus campos no vacíos. Un campo de más de un megabyte, un registro de más de 8 MiB o un entrecomillado que el lector no puede cerrar hace fallar las filas desde ahí.

**XLSX.** El libro, sus estilos, sus cadenas compartidas y sus hojas de cálculo se leen a medida que se descomprimen, sin conservarlos nunca como árbol; las relaciones del libro se leen enteras, de una parte de como máximo 4 MiB (por encima de eso, o si no es XML, el archivo se rechaza como `corrupt`: sin ellas no se puede encontrar ninguna hoja), y nombran las cadenas compartidas y los estilos. Una celda que nombra una cadena compartida que el libro no contiene se lee vacía, y el artefacto pierde `shared_strings`. El nombre de una hoja tiene como máximo 500 caracteres (uno cortado más corto es una unidad fallida, `sheet names`). Las hojas de cálculo se leen en el orden del libro (una hoja de gráfico o de diálogo no contiene celdas; una hoja de macros, o una hoja de cualquier otro tipo, no se lee y falla sola), cada una una tabla; un libro Strict Open XML se rechaza como `unsupported_format`; una hoja cuya relación no nombra ninguna parte falla sola. Una celda se lee tal como la contiene el libro: una cadena compartida o en línea sin su lectura fonética (una formada solo por espacios en blanco está vacía, como un campo CSV), un número, un booleano, un error como `{"error": "#DIV/0!"}`, o una fecha cuando su formato numérico lo es (los formatos integrados 14-17 y 22, los 27-31, 36 y 50-58 del Excel de Asia oriental, o un código personalizado de como máximo 255 caracteres, el límite del propio Excel, con una letra de fecha), del sistema de 1900 con el 1900-02-29 de Excel leído como texto, o el de 1904, al segundo más cercano. Un formato de hora del día se lee como `time`, y un formato de tiempo transcurrido como `[h]:mm` como `duration`. Una fórmula se lee como su valor en caché; una sin él se lee vacía, y el artefacto pierde `formula_values`. Un valor de más de un megabyte, en línea o compartido, y uno más allá de la columna 16,384, se omite y se cuenta, igual que una celda escrita dos veces en una misma posición (su valor anterior se pierde). El primer valor de un rango combinado llena cada una de sus celdas, de modo que un encabezado combinado sobre varias columnas nombra cada una; un encabezado de dos filas, un rango combinado a lo ancho de columnas de la primera que agrupa las celdas de texto de la segunda bajo él (`2025` sobre `H1` y `H2`), nombra cada columna a partir de ambas (`2025 / H1`; una sin nada encima toma el nombre de la segunda fila), y `header_rows` es 2; la segunda fila de encabezado cuenta contra las filas que contiene una tabla. Cualquier otra celda de esa segunda fila, sea texto o no, que no esté combinada hacia abajo desde la primera la convierte en datos. Un rango que el presupuesto de celdas dejó sin extender no agrupa nada; los números o fechas bajo un rango (`2024 | 2025` escritos como números) se leen también como datos. Un título combinado sobre toda la primera fila cumple también la regla, y encabeza todos los nombres (`Sales 2025 / Region`). Lo mismo ocurre con una primera fila de datos que es toda texto bajo combinaciones que cubren todas las columnas que llena (`Name` sobre `Alice | Smith`): nada la distingue de los nombres bajo los grupos, así que se lee como la segunda fila de encabezado, y las columnas conservan esos nombres hasta que se cambie la hoja. Un rango combinado no llena ninguna fila más allá de la última contenida, y una combinación que añadiría filas más allá de las que contiene una tabla se detiene ahí y es una unidad fallida. Una fila o celda que no nombra ninguna posición toma la siguiente a la anterior. Una hoja oculta se lee y se marca `attrs.hidden`, y una hoja dañada, o una cuya raíz no es una hoja de cálculo (otro espacio de nombres), falla sola; las cadenas compartidas o los estilos que no se pueden leer hasta el final conservan lo leído, y el artefacto pierde `shared_strings` o `number_formats`.

**Parquet.** pyarrow lee el archivo a partir de los bytes en memoria, sin sistema de archivos, sin hilos y con su grupo de memoria del sistema: sus columnas a partir del esquema (las primeras 16,384, conservadas por posición ya que un nombre puede repetirse; el resto es una unidad fallida, `<name> columns`) y sus filas un grupo de filas cada vez, en lotes de como máximo 1,024 filas y no más valores de los que admite el presupuesto de celdas, contados por columnas hoja (cada campo de una estructura cuenta una). Los nombres no son ninguna fila del archivo, así que la tabla no tiene fila de encabezado (`header_rows` 0) y sus filas se cuentan desde 1. La forma de leer una columna se decide una vez a partir de su tipo, de modo que cada lote de ella se lee igual. Un entero es un número, igual que un decimal y un entero que supera 64 bits con signo (un `uint64`), cada uno escrito con sus dígitos exactos, ya que un número JSON se lee de vuelta como un double (el artefacto y sus pasajes conservan todos los dígitos; las primeras 24 columnas de número o duración de un conjunto de datos conservan nueve decimales, así que un `decimal(38,18)` se redondea ahí, y el resto un double, [Conjuntos de datos](#datasets)); un flotante es un número, y el valor de una columna `float32` o `float16` de nivel superior es su decimal más corto en su propio tipo, un NaN no es ningún valor (la marca de un número ausente en una columna de flotantes), igual que un nulo; un booleano, una fecha, una marca de tiempo (con su zona conservada, ninguna cuando la columna no tiene), una hora del día y una duración se leen como las escriben los otros lectores, con una unidad de nanosegundos pasada al microsegundo (también dentro de una lista, estructura o mapa, no una vista de lista, que Arrow no convierte a ningún otro tipo de elemento); el texto es texto, un UUID es su texto, y una lista, estructura o mapa es su JSON (los bytes que contiene, en base64); otro tipo de extensión se lee como lo que almacena, un `bool8` como un booleano. Los bytes no son texto, y ellos, un infinito (de modo que una columna de flotantes sigue siendo un número), un valor de más de 1,048,576 caracteres y un valor que Python no puede contener (una fecha posterior al año 9999) se omiten y se cuentan (`<name> values`, con cada causa nombrada, sobre las filas leídas); un lote con un valor que Python no puede contener se divide por mitades hasta que cada parte se convierte, de modo que solo se omite ese, y pasados 16 de ellos en una columna (en todo el archivo) el resto de la columna se omite sin leer, contado como tal. Una fila de nulos, NaN o texto en blanco no es una fila; un archivo cuyo esquema no tiene columnas solo tiene filas en blanco, contadas a partir de sus metadatos, y un grupo de filas sin filas se pasa por alto; un recuento de filas que un pie declara por debajo de cero es ninguno, y un pie cuyos grupos de filas declaran 2^63 filas o más se rechaza como `corrupt`. Las páginas se decodifican de una en una, así que el tamaño de un grupo de filas no decide lo que se conserva; las filas más allá del presupuesto de la tabla no se leen: las posteriores al lote en que se agotó, y más allá de cuatro millones de valores o 256 MiB decodificados o 100,000 grupos de filas leídos, cargados antes de que un lote se lea en Python (al menos un valor por fila, cada elemento de una lista, estructura o mapa hasta sus hojas, una entrada de diccionario una vez por cada valor que la nombra y el valor de una vista una vez por cada vista (esas dos en una columna leída en Python: una columna de bytes nunca se copia), de modo que un archivo de filas en blanco, de listas largas o de valores largos queda también acotado; las filas desde ahí son su propia unidad fallida, `past what one file decodes`), se cuentan a partir de los metadatos, nunca se decodifican; un grupo de filas cuyas páginas no se decodifican, o que contiene más o menos filas de las que declara su pie (las filas se numeran y cuentan por el pie), hace fallar las filas desde ahí (`<name> rows N-`). Los límites del proceso hijo acotan una decodificación (una que supera el límite de tiempo rechaza el archivo, `timeout`, y una que hace fallar al hijo es `resource_limit`), y su entorno limita las bibliotecas nativas a un hilo y al asignador del sistema, que permite el límite de espacio de direcciones. Una compilación sin pyarrow no declara Parquet.

**Límites.** Un presupuesto acota todas las tablas de un artefacto, en tres partes: lo que contiene una fuente, los bytes que escriben sus tablas y el texto que indexan. Una fuente contiene como máximo 1,000,000 de celdas no vacías en total, incluidas las celdas combinadas, con su texto dentro de 256 MiB contados a cuatro bytes por carácter, y 100,000 filas por tabla; una combinación demasiado grande para las celdas restantes se omite y las deja para el resto, y un libro lee como máximo 1,000,000 de rangos combinados, un límite al tiempo que tardan (más allá de él, cada uno se cuenta como no extendido, sin leer). Maquetar las filas gasta como máximo 40 MiB, contados como los deletrea el JSON del artefacto: los valores de cada fila, incluidos los nulos, su línea de bloque con los nombres de columna, con los escapes incluidos, los campos propios de cada bloque y el nombre de la hoja, y las unidades fallidas; y el texto que contienen los bloques de tabla es como máximo 12 M de caracteres, con cada bloque cargado con 760 más, ya que un bloque se indexa como máximo en un fragmento (los bloques bajo un mismo encabezado se agrupan en menos) y uno más largo que un fragmento en uno por cada paso, de modo que las tablas de un artefacto se indexan como máximo en 15,800 fragmentos, por debajo de los 20,000 que una fuente publica por defecto; una hoja cuya tabla no cabe no deja ningún encabezado. Estos presupuestos suponen los valores predeterminados: un despliegue que eleve el límite de entrada de 64 MiB del extractor (un CSV se lee en un tiempo proporcional a su tamaño, 34 s para 64 MiB de líneas en blanco, frente a los 120 s del hijo) puede ver cómo un CSV grande agota el tiempo de CPU, y uno que reduzca el límite de respuesta del extractor por debajo de unos 41 MiB, o el tamaño de fragmento, el paso de fragmento (por debajo de 760 caracteres) o el tope de fragmentos del servidor, rechaza una tabla grande (`resource_limit`, `too_large`) en lugar de cortarla. Las filas más allá de cualquiera de ellos son una unidad fallida, contadas, con sus celdas comprobadas solo en busca de un valor, y nunca maquetadas; un encabezado que supera los bytes hace fallar su tabla, y una vez agotado el presupuesto (sus celdas, bytes o texto) las hojas posteriores son una única unidad fallida, nunca leída; lo que queda de las celdas sigue extendiendo las combinaciones de la hoja en que cae el corte, de modo que un encabezado combinado nombra sus columnas. Las partes de un libro se descomprimen hasta como máximo 512 MiB en total, igual que las de un DOCX (una parte en flujo se lee hasta ese total sea cual sea el tamaño que declare, y se corta ahí, quedando las filas leídas antes; una etiqueta de más de 16 MiB, sus atributos incluidos, un anidamiento de más de 64 niveles, o más de 4,096 nombres distintos de elementos y atributos, o uno de más de 1,024 caracteres, corta solo su parte de la misma manera), y contienen como máximo 25 M de elementos en total, lo que acota el tiempo de lectura; la hoja en que se agota el límite conserva las filas leídas antes de él. Un libro se lee por sus primeras 10,000 hojas (el resto es una unidad fallida) y 65,536 estilos y formatos numéricos (más allá, el artefacto pierde `number_formats`), y sus cadenas compartidas contienen como máximo 256 MiB, cada una contada a cuatro bytes por carácter y el objeto que ocupa (`lost: shared_strings` más allá). Una hoja de 100,000 por 20 números (62 MB descomprimida, 4 MB comprimida en zip) se extrae en unos 9 segundos bajo los límites del hijo.

**Límites del contrato.** Antes de responder, el extractor de referencia ajusta cada bloque a los límites del contrato: una clave de más de 1,024 caracteres se omite y se conservan las primeras 10,000, y la ruta de sección conserva sus primeras 64 entradas, cada una cortada a 500 caracteres. Un bloque que cortó se marca (`attrs.keys_truncated`, `attrs.section_truncated`) y el `coverage.lost` del artefacto nombra lo que perdió (`exact_keys`, `section_path`); el texto y el localizador del bloque siguen conteniéndolo todo.

<a id="qualification"></a>

## Calificación

El extractor de referencia declara un formato solo cuando ese formato supera los umbrales siguientes, medidos por separado para cada
formato y segmento, de modo que un buen promedio no pueda ocultar uno fallido. `tests/unit/test_extraction_gates.py` lee esta tabla, puntúa
el extractor de referencia con fixtures etiquetados y falla un segmento que quede por debajo de su umbral, y también falla cuando las
capacidades del extractor de referencia declaran un formato sin una fila aquí o afirman algo más allá de los formatos y sus opciones. PDF, DOCX
y PPTX se puntúan con el corpus del banco de análisis (`bench/docparse/corpus`, sus etiquetas de referencia y
`bench/docparse/score.py`); los demás formatos, con fixtures etiquetados construidos en la prueba.

| Formato  | Segmento                          | Métrica                  | Umbral |
| -------- | --------------------------------- | ------------------------ | ------ |
| pdf      | capa de texto, todos los segmentos | recall de texto          | 0.97   |
| pdf      | capa de texto, todos los segmentos | orden de lectura         | 0.95   |
| pdf      | capa de texto, todos los segmentos | F1 de encabezados        | 0.95   |
| pdf      | capa de texto, todos los segmentos | nivel de encabezado      | 0.95   |
| pdf      | capa de texto, todos los segmentos | F1 de celdas de tabla    | 0.95   |
| pdf      | capa de texto, todos los segmentos | localizador              | 0.90   |
| pdf      | escaneado                         | unidades fallidas        | 1.00   |
| pdf      | todos los segmentos               | pérdida anunciada        | 1.00   |
| docx     | documentos                        | recall de texto          | 0.98   |
| docx     | documentos                        | F1 de encabezados        | 0.98   |
| docx     | documentos                        | nivel de encabezado      | 0.98   |
| docx     | documentos                        | F1 de celdas de tabla    | 0.98   |
| pptx     | presentaciones                    | recall de texto          | 0.95   |
| pptx     | presentaciones                    | F1 de encabezados        | 0.98   |
| pptx     | presentaciones                    | localizador              | 0.90   |
| pptx     | presentaciones                    | notas                    | 1.00   |
| pptx     | presentaciones                    | pérdida anunciada        | 1.00   |
| markdown | fixtures                          | recall de texto          | 1.00   |
| markdown | fixtures                          | precisión de texto       | 1.00   |
| markdown | fixtures                          | F1 de encabezados        | 1.00   |
| markdown | fixtures                          | nivel de encabezado      | 1.00   |
| markdown | fixtures                          | localizador              | 1.00   |
| html     | fixtures                          | recall de texto          | 1.00   |
| html     | fixtures                          | precisión de texto       | 1.00   |
| html     | fixtures                          | F1 de encabezados        | 1.00   |
| html     | fixtures                          | nivel de encabezado      | 1.00   |
| html     | fixtures                          | filas de tabla           | 1.00   |
| text     | fixtures                          | recall de texto          | 1.00   |
| text     | fixtures                          | precisión de texto       | 1.00   |
| text     | fixtures                          | localizador              | 1.00   |
| sql      | fixtures                          | sentencias               | 1.00   |
| sql      | fixtures                          | localizador              | 1.00   |
| sql      | fixtures                          | nombres                  | 1.00   |
| toml     | fixtures                          | rutas de claves          | 1.00   |
| toml     | fixtures                          | localizador              | 1.00   |
| csv      | fixtures                          | celdas tipadas           | 1.00   |
| csv      | fixtures                          | encabezados              | 1.00   |
| xlsx     | fixtures                          | celdas tipadas           | 1.00   |
| xlsx     | fixtures                          | encabezados              | 1.00   |
| parquet  | fixtures                          | celdas tipadas           | 1.00   |
| parquet  | fixtures                          | encabezados              | 1.00   |

Las métricas:

- **recall de texto**, **precisión de texto**, **orden de lectura**, **F1 de encabezados**, **nivel de encabezado**, **F1 de celdas de tabla** y
  **localizador** son las del evaluador del banco: la proporción de las palabras de la referencia que contiene el análisis, de las palabras del análisis que
  contiene la referencia, de los bloques de la referencia en orden, el F1 sobre los encabezados emparejados por su texto, la proporción de los encabezados
  emparejados en su nivel (por rango), el F1 sobre las celdas de tabla, y la proporción de los bloques de la referencia citados en su página o diapositiva. El nivel de
  encabezado no se evalúa en las presentaciones: todos los títulos de diapositiva del corpus están en un mismo nivel. Para Markdown, HTML, text y SQL,
  el localizador es la proporción de bloques o sentencias de la referencia encontrados con exactamente su rango de líneas; para TOML, la de las claves cuyo
  rango de bloque contiene su línea.
- **unidades fallidas**: la proporción de páginas de un archivo escaneado que el artefacto lee con OCR (`attrs.ocr_pages`) o informa como
  fallidas, nunca leídas como vacías; con el OCR desactivado, toda página es una unidad fallida.
- **pérdida anunciada**: por categoría del banco, la proporción del texto de la referencia dibujado dentro de imágenes cuya página o diapositiva
  el artefacto lee con OCR o nombra como no leída, en `coverage.failed` o `attrs.images_unread`.
- **notas**: la proporción de notas del orador conservadas como notas, en su diapositiva.
- **filas de tabla**: la proporción de filas de una tabla HTML conservadas como una sola fila, con sus celdas en orden.
- **sentencias**, **nombres**, **rutas de claves**: la proporción de sentencias SQL encontradas completas, de los objetos que definen o leen
  entre sus claves, y de rutas de claves TOML encontradas como claves.
- **celdas tipadas** y **encabezados**: la proporción de celdas de una tabla leídas con su valor y tipo, y de sus columnas leídas
  con sus nombres, sobre el recuento de la referencia o el de la extracción, el que sea mayor, de modo que una fila, columna o tabla
  adicional cuenta como un fallo.

Las pruebas se ejecutan con el OCR desactivado, por lo que dan el mismo resultado esté o no instalado el motor de OCR.

Lo que no está calificado, y por tanto el extractor de referencia no lo afirma: las palabras que lee el OCR (lee una página escaneada y una
imagen grande donde la imagen del extractor incluye el motor, y las pruebas de escaneado y de pérdida anunciada cuentan como leída una página que
leyó, pero ningún umbral mide todavía su texto), el texto dentro de imágenes más pequeñas, gráficos y diagramas (nombrados en
`attrs.images_unread` y en las advertencias del artefacto, nunca leídos), y una tabla de Markdown con barras verticales como tabla tipada (sus filas
permanecen como un solo párrafo, tal como se escribieron).

<a id="retrieval-floors"></a>

## Umbrales de recuperación

Estos umbrales controlan `knowledge.search` sobre los documentos extraídos, no lo que declara el extractor de referencia.
`tests/integration/test_knowledge_retrieval_floors.py` ingiere runbooks en Markdown, esquemas SQL, un archivo TOML, tablas CSV,
tablas HTML y un PDF, con escrituras de texto junto a ellos, y plantea consultas etiquetadas en tres segmentos; el objetivo de cada consulta
debe ser el primer resultado (**encontrado primero**):

- **identificadores exactos**: un código de error, una tabla, una vista, una ruta de clave y un número de formulario.
- **preguntas sobre tablas**: una fila en cada bloque de una tabla CSV lo bastante larga como para dividirse en varios, una fila de otro CSV con
  las mismas columnas y una fila de cada una de dos tablas HTML con las mismas columnas.
- **recuperación filtrada**: filtros `formats`, `block_types` y `tags` que conservan cada uno documentos competidores; todos los resultados están
  dentro del filtro (**conservado en el filtro**).

Un resultado cuenta cuando proviene de la fuente esperada y muestra el texto esperado en su pasaje o en los encabezados bajo los que
se encuentra el pasaje (un código de error suele ser el encabezado de su sección). Los umbrales se miden y se comprueban con el embedder
falso de n-gramas que ejecutan las pruebas de integración, el mismo modelo que genera los embeddings de los fragmentos y de las consultas, de modo que el ranking no
varía entre ejecuciones; con otro embedder la prueba se omite. Un código como `M-41` no llega ni a la rama léxica (sus
partes son más cortas que la longitud mínima de término) ni a la rama de clave exacta, por lo que con el embedder falso se encuentra mediante la rama
vectorial sobre el encabezado de su sección.

| Segmento                | Métrica                | Umbral |
| ----------------------- | ---------------------- | ------ |
| identificadores exactos | encontrado primero     | 1.00   |
| preguntas sobre tablas  | encontrado primero     | 1.00   |
| recuperación filtrada   | encontrado primero     | 1.00   |
| recuperación filtrada   | conservado en el filtro | 1.00   |

<a id="structural-chunks"></a>

## Chunks estructurales

`chunk_artifact` corta los chunks a partir de bloques en lugar de ventanas de tamaño fijo sobre texto aplanado:

- los bloques consecutivos bajo un mismo padre (la sección de un encabezado, una tabla TOML con sus claves) se agrupan en un solo chunk hasta el
  tamaño de chunk;
- una sentencia SQL es un chunk por sí sola, de modo que un resultado cita una única sentencia;
- un bloque más largo que el tamaño de chunk se divide en ventanas, y cada ventana conserva su sección y, si el bloque tiene desplazamientos de caracteres, su propio rango de líneas y caracteres (una ventana HTML, o la de un archivo TOML válido, cita las líneas de su bloque);
- el texto del encabezado no se repite en el texto del chunk: la ruta de la sección viaja junto a él; un encabezado sin texto y sin ninguna subsección debajo es un chunk por sí solo.

Cada chunk lleva los ids de los bloques de los que se cortó, su tipo (`mixed` cuando difieren), el localizador combinado de
su rango (un campo como `key_path` se conserva solo cuando todos los bloques coinciden) y la unión de sus claves, y registra
la revisión del documento y el formato con el que se escribió.

<a id="ingestion"></a>

## Ingesta

`knowledge.ingest` ejecuta un trabajo (`server/knowledge/app/ingest_run.py`, worker `knowledge_ingest`) sobre un original que `POST /v1/knowledge.upload` almacenó. La subida determina el formato a partir del nombre del archivo y del tipo de medio, y nunca lee los bytes; el trabajo lee el original y lo envía al extractor a través del cliente descrito arriba, comprueba el artefacto, lo almacena como un original propio (direccionado por contenido), corta chunks estructurales con el contexto de cada chunk (`chunk_context`: el título, la descripción y la ruta de secciones), los incrusta reutilizando el vector de una entrada que este espacio de nombres ya incrustó con el mismo modelo, y publica la siguiente revisión de la fuente: decide la revisión (y comprueba una esperada), inserta los chunks escritos con ella y, a continuación, bajo el claim de la fuente, rechaza la publicación si la fuente cambió desde que se decidió la revisión, escribe la fila del documento y retira los chunks de la versión anterior. El claim cubre esas pocas sentencias, no la inserción de los chunks, de modo que sus límites de tiempo se cumplen con una fuente de cualquier tamaño. Una fuente que el extractor rechaza hace fallar el trabajo con su código; un formato que ningún extractor configurado acepta lo hace fallar con `format_unavailable`; un artefacto que incumple el contrato lo hace fallar con `invalid_artifact`; un extractor al que no se puede llegar, que agota el tiempo de espera o que responde con un estado que no es ni un resultado ni un rechazo devuelve el trabajo a la cola con una espera creciente (de una hora como máximo) hasta `LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS`. No se publica nada antes de que se hayan ejecutado todos los pasos lentos, así que un trabajo fallido o cancelado deja en servicio la revisión anterior. La fila del trabajo registra la versión de chunks que inserta una publicación antes de escribir la fila del documento; cuando una ejecución se detiene entre ambos pasos, su reintento, su fallo o su siguiente ejecución retira esos chunks. La fila del documento indica el trabajo que la escribió (`ingestion_id`, que se conserva con una edición de metadatos y con un borrado) y una nueva ejecución busca cualquier fila de versión de la fuente que lo indique, de modo que una escritura de texto posterior no lo oculta. Una ejecución que se detuvo a mitad de la publicación (todavía en `processing` en la etapa de publicación, que se guarda junto con su versión de chunks), cuando ya se ha producido una escritura posterior de la fuente (una escritura de texto, una edición de metadatos o un borrado) y no se encuentra la fila del trabajo (una fusión pudo haberla absorbido), falla con `revision_conflict` en lugar de publicar quizá su archivo más antiguo sobre esa escritura; un trabajo que un reintento devolvió a pendiente no se juzga así. Una inserción del documento que falló de forma transitoria (por su propio tiempo de espera o porque el plazo del claim de la fuente la cortó) aún puede completarse, así que la ejecución se detiene allí como lo haría una caída y el trabajo queda en `processing` en su etapa de publicación, con sus chunks activos: la ejecución deja que el lease del trabajo expire en lugar de liberarlo, de modo que ningún worker toma el trabajo hasta que el TTL del lease haya transcurrido según el reloj del servidor, más allá del límite propio de la inserción (la ventana de inicio de un envío en curso, el límite del lado del servidor de una sentencia con claim y un margen de commit), y para entonces la inserción ya se ha completado o se ha detenido: la siguiente ejecución lo termina como publicado si su fila llegó a escribirse (sea lo que sea que se haya escrito después: un borrado o una escritura de texto prevalece), lo hace fallar como superado, como se indicó antes, o retira los chunks; una nueva ejecución de un trabajo que ya publicó termina sin volver a publicar ni retirar los chunks que publicó. Una cancelación es una fila propia (`loom.knowledge_ingest_cancels`), nunca una versión de la fila del trabajo, que solo escribe el worker que tiene el lease del trabajo; el worker la lee antes de la publicación y cuando toma el trabajo, y un trabajo cuya propia publicación se encuentra termina como publicado sea cual sea la cancelación que llegó después. Una pasada ejecuta un trabajo bajo `LOOM_KNOWLEDGE_INGEST_MAX_PASS_SECONDS`, atendiendo los trabajos de una fuente de uno en uno en el orden en que se solicitaron (solo se ofrece el trabajo abierto más antiguo de una fuente, uno antiguo antes que cualquiera posterior), y los espacios de nombres por turnos (primero el atendido hace más tiempo), y una ejecución cortada por ese plazo o por un proceso detenido cuenta para los intentos del trabajo.

<a id="faults"></a>

## Fallos

Cada fallo de la tabla se produce cuando llega la siguiente revisión de una fuente que ya se sirve: el job termina en el estado indicado, su
`error_code` y su `error_message` explican el motivo, y la fila de documento y los chunks de la revisión anterior son los que se siguen sirviendo. Un
fallo transitorio devuelve el job a `pending` con `retrying` hasta que se agota `LOOM_KNOWLEDGE_INGEST_MAX_ATTEMPTS`; entonces
falla con `retries_exhausted`. Los casos son
`tests/unit/test_knowledge_ingest.py::test_a_fault_on_the_next_revision_fails_typed_and_the_earlier_one_keeps_serving`.

| Fallo                                                                                           | El job termina                             |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------ |
| Extractor inaccesible                                                                           | reintentado, luego `failed` `retries_exhausted` |
| Extractor más lento que `LOOM_DOCUMENT_EXTRACTOR_TIMEOUT_S`                                     | reintentado, luego `failed` `retries_exhausted` |
| Extractor responde comprimido (cualquier `Content-Encoding`)                                    | reintentado, luego `failed` `retries_exhausted` |
| Extractor rechaza la fuente al superar su propio límite de tiempo                               | `failed` `timeout`                         |
| No hay extractor configurado, o el servicio no tiene un parser para ella                        | `failed` `format_unavailable`              |
| Artefacto mayor que `LOOM_DOCUMENT_EXTRACTOR_MAX_ARTIFACT_BYTES`                                | `failed` `resource_limit`                  |
| Artefacto que no es JSON, fuera del contrato, un bloque bajo un padre inexistente, un locator más allá del original | `failed` `invalid_artifact`     |
| El almacén de objetos falla al leer el original o al escribir el artefacto                      | reintentado, luego `failed` `retries_exhausted` |
| Embedder caído o fuera de su plazo                                                              | reintentado, luego `failed` `retries_exhausted` |
| Defecto del embedder (cualquier otro error)                                                     | `failed` `internal`                        |
| El almacén rechaza la fila de documento después de los chunks                                   | `failed` `internal`                        |

Un original que ya no está en el almacén falla con `original_gone` (en el mismo archivo).
`tests/integration/test_knowledge_ingest_faults_live.py` ejecuta los casos de extractor caído, un bloque bajo un padre inexistente, fallo de lectura del almacén de objetos, embedder caído y fila de documento rechazada después de los chunks contra un servidor y su
ClickHouse, y lee la revisión anterior de nuevo mediante `knowledge.get` y `knowledge.search`.

<a id="service-levels"></a>

## Niveles de servicio

Los niveles a los que un despliegue somete las operaciones de conocimiento bajo carga mixta: lecturas interactivas en su p95 y p99, y el tiempo que pasa desde `knowledge.ingest` hasta que el trabajo de un documento pequeño termina en estado ready. El p99 de cada operación es su presupuesto de latencia (`OP_BUDGETS_P99_MS` en `server/settings/catalog.py`), que se verifica con una prueba unitaria.

| Medida                      | p95 (ms) | p99 (ms) |
| --------------------------- | -------- | -------- |
| `knowledge.search`          | 150      | 250      |
| `knowledge.query`           | 1,000    | 1,500    |
| `knowledge.ingest`          | 40       | 60       |
| ingesta de documento pequeño | 20,000   | 30,000   |

`bench/knowledge_load.py` ejecuta la carga mixta contra un servidor y la contrasta con esta tabla: ingestas de documentos pequeños, un PDF grande y una importación de CSV a la vez, con búsquedas y consultas estructuradas sobre lo ya publicado, y con los trabajos abiertos del espacio de nombres muestreados para medir la profundidad de la cola. Las llamadas llegan a un ritmo fijo, hayan respondido o no las anteriores; `--tenants` ejecuta la carga en varias organizaciones a la vez, ya que los límites de tasa de un espacio de nombres (`server/core/ratelimit.py`) acotan lo que envía un solo tenant. Informa de p50, p95 y p99 por operación, de errores y de llamadas rechazadas (429, 503), y termina con un código distinto de cero cuando una medida supera su nivel.

La ingesta está acotada para todo el despliegue: un solo proceso ejecuta el worker de ingesta (el supervisor que mantiene el lease de los workers con `LOOM_BACKGROUND_WORKERS=auto`) y ejecuta un trabajo a la vez, de modo que el despliegue no termina los trabajos más rápido que si se ejecutaran uno tras otro, sin importar cuántas réplicas atiendan, y un trabajo en cola detrás de un archivo grande espera a que este termine. Tras una pasada que ejecutó un trabajo, la siguiente comienza después de `LOOM_KNOWLEDGE_INGEST_INTERVAL_SECONDS` (0: de inmediato); una pasada que no encontró ningún trabajo espera `LOOM_KNOWLEDGE_INGEST_IDLE_INTERVAL_SECONDS` (15 s), por lo que en un despliegue sin actividad un documento espera hasta ese tiempo antes de que empiece su trabajo. El nivel para documentos pequeños tiene en cuenta esa espera, y solo se cumple mientras los documentos pequeños lleguen más despacio de lo que el worker los termina.

<a id="datasets"></a>

## Conjuntos de datos

Una tabla del artefacto (una hoja, un archivo CSV o Parquet, la tabla de un PDF) se convierte además en un conjunto de datos tipado
(`server/knowledge/app/datasets.py`), de modo que sus valores pueden leerse como valores y no como pasajes. Dos tablas contienen todos los
conjuntos de datos, sea cual sea la forma de los archivos, por lo que ninguna carga crea una tabla:

- `loom.datasets`, el catálogo: una fila ReplacingMergeTree por (fuente, conjunto de datos, versión del documento), leída con `FINAL`,
  con el esquema, su estado (`inferred`, `needs_clarification`, `approved`), lo que necesita una respuesta, las filas almacenadas y
  rechazadas, si la extracción conservó todos los valores de la tabla y la instantánea que lo sirve.
- `loom.dataset_rows`, las filas: los valores de una columna se guardan en la ranura tipada que le asigna el esquema, 72 en total: texto y horas
  en `s1`..`s24` (`Nullable(String)`), números y duraciones (en segundos) en `n1`..`n24` (`Nullable(Decimal128(9))`,
  las columnas numéricas que exceden esas, en `f1`..`f8`, `Nullable(Float64)`), fechas y fechas con hora en `t1`..`t8`
  (`Nullable(DateTime64(6, 'UTC'))`), booleanos en `b1`..`b8`. Una columna que excede las ranuras de su tipo no se almacena y lo indica
  (`past_capacity`). La clave es `(org, namespace, dataset_id, snapshot_id, row_id)`.

Una columna toma el tipo del extractor; una columna `mixed` se guarda como texto. Una fila con un error de celda, o con un valor que el tipo de
su columna no puede contener, se rechaza entera y se cuenta, con sus primeros 20 motivos, de modo que todos los valores de un conjunto de datos están tipados: un
número de 10^29 o más (tras redondear a nueve decimales) en una ranura `n`, uno que un double no puede contener en una ranura `f`, un
instante fuera de 1900-2299 (lo que admite `DateTime64`; una zona horaria se convierte a UTC). Un conjunto de datos es `complete` solo cuando la
extracción conservó todos los valores de su tabla: ninguna de sus filas, combinaciones, valores o celdas es una unidad fallida, no se perdió ninguna
cadena compartida, y el artefacto no perdió ninguna de las capacidades de las que depende un valor (valores de fórmulas, cadenas compartidas, formatos
numéricos). El esquema pide una respuesta (`needs_clarification`) para una columna mixta, un nombre generado (una celda de encabezado vacía), un
nombre repetido (un extractor distinto del de referencia puede enviarlo; el conjunto de datos lo nombra `<name> (2)`, como lo hace el extractor de
referencia, ya que una columna se direcciona por su nombre), un encabezado cuyos nombres se leen todos como número o como fecha
(la primera fila puede ser un dato; no se pregunta en una tabla cuyos nombres no son una fila de su fuente, `header_rows` 0, como el esquema de un
archivo Parquet), y las columnas que ninguna ranura contiene (una respuesta por tipo, que nombra la primera de ellas). Las respuestas
sobre columnas individuales se detienen en 100; a partir de ahí, el tipo `inferred` de una columna indica el resto. La inferencia se ejecuta fuera del bucle de
eventos, ya que una tabla puede tener 16,384 columnas de ancho. El id de un conjunto de datos es el mismo en todas las revisiones de su fuente: una hoja por
su nombre, la tabla de un archivo CSV o Parquet y las tablas de un PDF por sus ids (las de un PDF en el orden en que las contienen sus páginas, de modo que una
tabla añadida antes de otra da un nuevo conjunto de datos a la posterior), y una hoja que un extractor dividió en más de una tabla
por su nombre y el id de la tabla.

El trabajo de ingesta escribe cada conjunto de datos después de los fragmentos y antes del reclamo de la fuente: una fila de catálogo vacía, de modo que una ejecución que
se detiene a medias encuentra las filas que escribió, sus filas en lotes de 5,000 bajo una nueva instantánea (tipadas fuera del bucle de eventos, el
trabajo se vuelve a leer antes de cada una, de modo que un borrado que lo eliminó detiene el resto), y luego su fila de catálogo, con la versión del documento
que toma la fila del trabajo. Nada las lee hasta que esa fila se escribe: un conjunto de datos se sirve mientras la fila del documento vivo de la fuente
tiene la versión con la que se publicó, de modo que la fila del documento es el interruptor tanto para los fragmentos como para los conjuntos de datos, y una escritura de texto o un borrado
de la fuente deja de servirlos de inmediato. Una edición de metadatos
(`knowledge.update_metadata`) los conserva: bajo el reclamo de la fuente escribe cada fila de catálogo con su nueva versión, la
misma instantánea, antes de su fila de documento. Una vez escrita la fila, se borran las instantáneas de la fuente de versiones anteriores; una
ejecución que se detiene antes borra las suyas. Una revisión de una fuente cuyo conjunto de datos vivo estaba aprobado conserva los tipos aprobados
cuando sus columnas tienen los mismos nombres en el mismo orden; una aprobación que llega después de que la publicación leyó los conjuntos de datos vivos (toma primero el
reclamo) devuelve el trabajo a la cola para ejecutarse de nuevo de inmediato, sin contar el intento, de modo que se conserva y no se pierde.

`knowledge.dataset_approve` aprueba el esquema de un conjunto de datos, cambiando el tipo de las columnas que nombra. Un cambio de tipo vuelve a tipar las filas
desde el artefacto almacenado bajo una nueva instantánea, escrita antes de tomar el reclamo de la fuente (el mismo reclamo que toma la publicación de una ingesta);
bajo el reclamo, la aprobación comprueba que el documento es el que leyó y la instantánea la que revisó el
llamante, escribe la fila de catálogo que cambia a la nueva instantánea (con su versión marcada allí, posterior a la fila que
reemplaza) y borra las filas de la que reemplazó. Una aprobación que descubre que otra escritura movió la fuente, que no puede
tomar el reclamo o que falla antes de enviar su fila de catálogo no escribe ninguna fila de catálogo y borra sus propias filas; una cuyo
insert de catálogo falla una vez enviado las conserva, ya que la fila pudo haberse escrito. La fila de catálogo es la escritura del aprobador: su `actor`
y `approved_by` nombran al aprobador y su `source_class` es `asserted`; las filas siguen siendo de quien las subió (`user_id`,
su propio `source_class`), tal como las encuentra un borrado de usuario.

Las instantáneas se eliminan mediante `DELETE FROM` ligero con `lightweight_deletes_sync = 0`: nada lee una instantánea cuando ninguna fila de
catálogo la sirve, de modo que ninguna solicitud espera por una. `knowledge.delete` borra los conjuntos de datos de las versiones del documento que retira,
hasta la que leyó; una ingesta que se ejecuta mientras tanto escribe una versión posterior, que deja intacta para esa ingesta (un
borrado no toma reclamo, por lo que puede ocurrir entre los lotes de la ingesta). Las filas de una instantánea transferida conservan la versión con la que
se escribieron, de modo que un borrado conserva las filas de cualquier instantánea que una fila de catálogo más allá de su límite aún sirva; una edición cuya
transferencia llega después de que el borrado leyó el catálogo puede revivir el documento sobre el borrado, cuando su versión es la posterior:
sus fragmentos quedan marcados como eliminados y sus conjuntos de datos se sirven con sus filas borradas, como lo deja un borrado sin reclamo que compite con una edición,
y un borrado de la fuente resuelve ambos. Un borrado de una fuente ya borrada vuelve a borrar lo que queda de sus
versiones retiradas (una instantánea de aprobación que se escribió mientras se ejecutaba el primer borrado, o una que dejó la compilación N-1). Una escritura de texto
sobre una fuente ingerida deja su instantánea sin servir hasta la siguiente ingesta o borrado de la fuente. El borrado por erradicación elimina las filas de ambas
tablas del ámbito junto con los documentos (`documents.tombstone`).

<a id="queries"></a>

### Consultas

`knowledge.query` (`server/knowledge/app/query.py`) calcula sobre conjuntos de datos con un plan tipado: hasta cuatro conjuntos de datos del
espacio de nombres del llamante, cada uno bajo un alias; filtros sobre sus columnas, todos los cuales deben cumplirse; una combinación interna por igualdad de
cada conjunto de datos posterior al primero con uno anterior; agrupaciones (una fecha o fecha con hora por día, mes o año, en UTC); y agregados
(`count`, `count_distinct`, `sum`, `avg`, `min`, `max`), o las propias filas con sus ids de fila. El plan se comprueba
contra los esquemas de los conjuntos de datos (`server/knowledge/domain/query.py`): cada columna que nombra es una que un esquema almacena, cada
valor de filtro se lee como el tipo de su columna (como se lee el valor de una fila, pero un decimal con más de nueve decimales se rechaza, no se redondea;
una columna numérica guardada como double se compara como tal), y cada comparación, combinación, agrupación y agregado es de los que permiten los
tipos de sus columnas; las claves de combinación deben estar en el mismo tipo de ranura, ya que un decimal y un double no tienen un tipo común. Se compila
en una sola sentencia de solo lectura sobre `loom.dataset_rows` (`server/knowledge/store/dataset_query.py`) construida solo a partir de nombres de ranura,
nombres de resultado `c0`, `c1`, ... y parámetros tipados, de modo que nada de lo que escribió un llamante es SQL. Cada conjunto de datos es una subconsulta
limitada a su instantánea y a la org y el espacio de nombres del llamante, con sus propios filtros, antes de cualquier combinación, de modo que una fila de otro
tenant o instantánea nunca llega a un recuento. Una suma de una columna decimal se calcula en `Decimal256`, que ninguna fila de ningún conjunto de datos
desborda, y un promedio es esa suma sobre el recuento, dividida a 18 decimales y redondeada a nueve (el `avg` de ClickHouse sobre un
decimal es un double, y una división decimal trunca); las columnas numéricas de un conjunto de datos que exceden sus primeras 24 columnas numéricas o de duración se guardan, y por tanto se suman y promedian, como doubles.

Cada conjunto de datos se lee en la instantánea que sirve el documento vivo de su fuente; un conjunto de datos cuyo esquema necesita una respuesta
(`needs_clarification`) se rechaza hasta que se apruebe. La sentencia se ejecuta bajo los límites con forma de llamante como el usuario lector de solo lectura
donde `LOOM_CH_READER_USER` está definido (`ch.caller_shaped`), de modo que un plan que excede las filas, el tiempo o la memoria del despliegue para una solicitud falla (`422`) en lugar de devolver parte de un agregado. Antes de liberar las filas,
cada conjunto de datos debe seguir siendo servido por la instantánea leída (una publicación o una aprobación con cambio de tipo sirve otra y borra
las filas leídas sin esperar; un borrado por erradicación no sirve ninguna; una edición de metadatos la conserva): una reemplazada o borrada mientras se ejecutaba la sentencia
responde `409 PRECONDITION_FAILED`, para ejecutarla de nuevo, como ocurre con una publicación que llega mientras la consulta busca el conjunto de datos.
El resultado nombra cada instantánea leída en su recibo, con el plan tal como se validó, la sentencia y sus parámetros, y es
`complete: false`, indicando el motivo, cuando la extracción de un conjunto de datos dejó parte de su tabla fuera o rechazó filas. No se almacena nada: el recibo es la respuesta.

`knowledge.synthesize` con `datasets` (`server/knowledge/app/synthesize.py`, el prompt y la lectura de la respuesta en
`server/knowledge/domain/answer_plan.py`) pide una sola vez al modelo un plan de `knowledge.query` a partir de la tarea y del alias, nombre, número de filas y
columnas almacenadas de cada conjunto de datos, o la pregunta que necesitaría hacer (`ambiguous`). Los conjuntos de datos son los del llamante: los `datasets`, `org` y `namespace` propios de un plan se descartan. El plan se valida como una entrada de
`knowledge.query` y lo ejecuta su manejador, de modo que se comprueba contra los esquemas, se fija, se limita y se revalida como cualquier consulta; un plan rechazado por cualquiera de los dos se propone una vez más con el motivo, y luego la respuesta se abstiene (`invalid_plan`).
Las columnas del resultado y sus primeras 50 filas, con lo que puede no cubrir, son una nota más para la reducción, citada como
`[knowledge.query#1]` y fundamentada como una cita de fragmento (`ground_citations`), de modo que se elimina la cita de un cálculo que no
se ejecutó. Los nombres de columnas, los nombres de conjuntos de datos y la tarea son entrada no confiable para el plan, que no concede nada: la consulta se ejecuta en el ámbito del llamante, como el usuario lector donde se haya definido uno.

<a id="dependencies"></a>

## Dependencias

Los parsers del extractor de referencia necesitan `sqlglot` para SQL, `pypdf` (y `cryptography`, para un PDF cifrado con AES) para
PDF, `pyarrow` para Parquet, y la biblioteca estándar en el resto de los casos. Se instalan con el servicio
(`extractor/pyproject.toml`), y en un entorno de desarrollo mediante el extra `extractor`; la imagen del servidor no incluye ninguno
de ellos. Su imagen (`extractor/Dockerfile`, `ghcr.io/clickhouse/loom-extractor`) instala el paquete y sus dependencias
fijadas (`extractor/uv.lock`) en un venv cuyo propietario es root y ejecuta el servicio como un usuario que no es propietario de nada en él, de modo que
ningún archivo en el `sys.path` de un proceso hijo es escribible por el hijo. El stack de compose y el chart de Helm lo ejecutan separado del
servidor: Compose en una red interna compartida únicamente con el servidor, el chart detrás de una NetworkPolicy que admite solo los
pods de Loom y no permite ningún egress, sin otra credencial que su token; en ambos, una extracción no abre ningún socket mientras se ejecuta sola (`LOOM_EXTRACTOR_CONCURRENCY` 1, el valor por defecto) ([despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images),
[Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md#document-extractor)).

<a id="invariants"></a>

## Invariantes

- El contenido de un formato de texto se guarda en bloques; lo que un parser no pudo estructurar se registra en `coverage` (unidades fallidas, capacidades perdidas) y nunca se descarta en silencio. Solo se omiten los elementos HTML enumerados arriba, por diseño.
- El localizador de un bloque apunta al texto fuente del que proviene.
- Un artefacto se direcciona por el SHA-256 de sus bytes y nunca se edita; un cambio en un parser incrementa el `PARSER_VERSION` de ese parser, de modo que su salida es un artefacto nuevo.
- La extracción nunca ejecuta lo que analiza.
- El servidor no analiza ningún original: cada formato va a un servicio extractor, y nada de lo que este devuelve se usa antes de que `parse_artifact` lo acepte.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

| Qué                                 | Dónde                                                                                           |
| ----------------------------------- | ----------------------------------------------------------------------------------------------- |
| El artefacto                        | `server/knowledge/domain/extract/artifact.py` (`Artifact`, `Block`, `Locator`, `Coverage`)      |
| Comprobaciones de una respuesta     | `server/knowledge/domain/extract/validate.py` (`parse_artifact`)                                |
| Enrutamiento de formatos            | `server/core/document_formats.py` (`format_of`)                                                 |
| Llamadas a los extractores          | `server/knowledge/app/extractors.py` (`extract`, `available_formats`)                           |
| Fragmentos estructurales            | `server/knowledge/domain/extract/structural.py` (`chunk_artifact`)                              |
| Esquemas y tipado de datasets       | `server/knowledge/domain/datasets.py` (`infer`, `typed_rows`)                                   |
| Publicación y aprobación de datasets | `server/knowledge/app/datasets.py`, filas en `server/knowledge/store/datasets.py`              |
| Consultas de datasets               | `server/knowledge/app/query.py`, el plan en `domain/query.py`, SQL en `store/dataset_query.py`  |
| El servicio de referencia           | `extractor/loom_extractor/service.py`, una extracción por proceso `child.py`                    |
| Detección y decodificación de formato | `extractor/loom_extractor/source.py` (`detect_format`, `decode`, `extract`)                   |
| SQL                                 | `extractor/loom_extractor/sql.py` (`extract_sql`, `detect_dialect`)                             |
| TOML                                | `extractor/loom_extractor/tomlfile.py`                                                          |
| Markdown, texto plano, HTML         | `extractor/loom_extractor/text.py`, reglas de encabezados en `markdown.py`                      |
