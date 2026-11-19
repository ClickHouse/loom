<!-- i18n: source=docs/architecture/documentation-translation.md blob=f3c39bd59c115e29a52e9ce3c79a91bd7e85666a model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/documentation-translation.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/documentation-translation.md), which is authoritative.

<a id="documentation-translation-requests"></a>

# Solicitudes de traducción de la documentación

El generador de traducciones divide el texto en inglés preparado en solicitudes acotadas y luego verifica la traducción ensamblada antes de escribirla. El inglés sigue siendo la fuente de verdad.

<a id="request-planning"></a>

## Planificación de solicitudes

`english_requests` asigna anclas de encabezado con un único contador para toda la página. Prefiere cortes seguros en encabezados de segundo nivel y empaqueta las secciones demasiado grandes en fragmentos de origen de como máximo 16 KiB. Los fragmentos reconstruyen exactamente el inglés con anclas cuando se unen con saltos de línea.

El planificador usa los rangos de origen de los tokens de CommonMark para elementos de lista, bloques de código y filas de tabla. El código delimitado e indentado, el código en línea, los elementos HTML completos y los comentarios permanecen juntos. El encabezado de una tabla permanece con su delimitador. Una unidad indivisible demasiado grande se rechaza antes de la primera llamada al modelo de la página. La estimación de costos usa la misma lista de solicitudes que la traducción.

La extensión de listas del sitio acepta continuaciones de dos espacios, incluidos los elementos ordenados. Su analizador no tiene mapas de origen, por lo que un escaneo de indentación protege esos elementos junto con los rangos de CommonMark. Los elementos de ambos dialectos permanecen indivisibles.

Algunas tablas de la extensión del sitio son código indentado en CommonMark. El rango de código permanece indivisible, por lo que un bloque demasiado grande con esa ambigüedad se rechaza antes de la admisión del proveedor. Mover la tabla fuera de su contenedor de extensión resuelve la ambigüedad en el inglés.

<a id="original-unit-reconciliation"></a>

## Reconciliación de unidades originales

`reconcile_completed_units` es una API de planificación pura para conservar las solicitudes completadas a través de ediciones del inglés. Acepta contenedores `PreparedSource` histórico y actual, y un manifiesto completo de `OriginalUnit` con su resumen (digest). Ambos contenedores incluyen enlaces preparados y anclas de toda la página. Cada rango original y cada hash de origen deben coincidir con el contenedor histórico; los espacios entre rangos contienen exactamente una cadena vacía o LF. El resumen del manifiesto vincula estos metadatos, incluida la referencia de cada respuesta seleccionada.

El llamador autentica el plan original completo y los estados de finalización contra su libro de solicitudes, su caché y los recibos de las respuestas seleccionadas. Un archivo de caché ausente no prueba nada. Una unidad original con `reply_ref=None` atestigua explícitamente que no hay respuesta completada después de resolver los intentos inciertos. Una respuesta completada con un fallo confirmado conserva su referencia y un `UnitFault`; no puede convertirse en trabajo sin terminar omitiendo esa referencia. El llamador también proporciona los rechazos de todo el par configuración regional/origen a través de las versiones del origen.

El plan conserva únicamente unidades completadas enteras, únicas, monótonas e idénticas byte a byte, en límites actuales seguros para el analizador. Una carga útil puede poseer su salto de línea final o dejar un LF para el ensamblado; las cargas contiguas permanecen contiguas. Cada disposición lleva su `SourceContext` actual, la procedencia original, los identificadores de fallos y los separadores exactos. Las unidades completadas pueden superar el límite de la solicitud cuando se reutilizan sin una llamada al proveedor. Los huecos sin terminar y el inglés insertado usan el planificador acotado fuera de los rangos conservados. `chunks.split` acepta un `source_context` para que un fragmento de tabla sin encabezado use los límites de fila de la tabla actual completa.

Las disposiciones distinguen entre reutilización, corrección con la misma entrada, entrada modificada y trabajo sin terminar. Las ediciones del origen y los fallos que afectan a una unidad original comparten una única disposición de entrada modificada. Los candidatos repetidos, los rangos superpuestos o reordenados, los límites inseguros, los fallos desconocidos y los rechazos del par devuelven conflictos. Una unidad completada modificada entre varias unidades originales sin correspondencia necesita cortes homólogos demostrados; el planificador no adivina una partición. El reemplazo de una unidad completada que abarca toda la página no está admitido. La reutilización exacta de la página completa sigue siendo posible, pero la API no resuelve todas las correspondencias de una página modificada.

Tanto `chunks.split` como `reconcile_completed_units` aceptan `bounded_new_request_bytes`, un entero de 1 a 16384, sin contar booleanos. El valor predeterminado conserva los cortes de 16 KiB. El límite se aplica a toda solicitud prospectiva, incluidas las correcciones con entrada modificada y con la misma entrada. Las unidades completadas reutilizadas permanecen enteras incluso por encima de ese límite. Una unidad completada modificada o con fallos que no cabe devuelve un conflicto en lugar de dividirse.

Un límite menor puede refinar el trabajo sin terminar tras una respuesta truncada. El llamador autentica la solicitud resuelta, el rango de origen, la respuesta y el uso, todos los estados de finalización y rechazo, y las entradas posteriores del libro. Su paquete de ejecución vincula el límite seleccionado y el plan actual exacto, incluidos los rangos homólogos y las referencias completadas conservadas. Un límite por sí solo no establece esta evidencia, no levanta una retención ni admite una llamada. Un prefijo de respuesta truncada no es una unidad completada, y una entrada más pequeña no garantiza una respuesta completa.

`translate_text` acepta `initial_feedback` de confianza con un contexto de origen actual para las restricciones de entrada modificada. `request_system` incluye esa retroalimentación exacta en el SYSTEM canónico enviado al proveedor, de modo que la carga útil real de la solicitud vincula las restricciones. La retroalimentación se limita a 4096 bytes de ASCII imprimible y LF; una retroalimentación vacía conserva el SYSTEM predeterminado. El primer mensaje contiene solo el inglés actual. No se reproduce ninguna respuesta del asistente de una entrada anterior; un reintento mecánico puede usar solo la respuesta devuelta para la entrada actual. Una retroalimentación no válida o un contexto de origen que no coincide se rechazan antes de gastar o de llamar al proveedor.

Un plan de reconciliación no resuelve respuestas, no admite llamadas al proveedor ni escribe salida. Los consumidores autentican las respuestas resueltas y las validan contra sus contextos actuales, y luego validan la página ensamblada completa. Los invariantes de contenido y ambos dialectos de tabla se siguen aplicando. La publicación también requiere una revisión semántica y de completitud del idioma contra el inglés actual; el éxito estructural no elimina los fallos ni los rechazos confirmados.

<a id="assembled-validation"></a>

## Validación del ensamblado

La página ensamblada pasa los invariantes de contenido y las verificaciones de tablas existentes antes de que se cree su directorio o archivo de salida. Las verificaciones de tablas usan tanto el analizador de Markdown del sitio de documentación como CommonMark con su extensión de tablas: sus reglas de contenedor difieren. Conservan los grupos de tablas, las filas, las celdas, la alineación y si una celda está vacía. Las extensiones del sitio de listas, advertencias y código participan en la verificación. El texto de las celdas se mide después de la serialización y de la restauración del HTML sin procesar, para que el marcado vacío no pueda ocultar texto faltante y las etiquetas literales escapadas sigan siendo texto.

Las versiones de los paquetes del analizador están fijadas en los requisitos de traducción y en el extra de desarrollo. La versión fijada del analizador del sitio coincide con la del constructor de la documentación. Un límite de bytes de origen no garantiza que una respuesta traducida quepa en el límite de tokens de salida. Las respuestas truncadas siguen siendo fallos, y las verificaciones estructurales no establecen la calidad de la traducción ni la completitud de la prosa.

<a id="source-spelling-contracts"></a>

## Contratos de ortografía del origen

La validación de etiquetas distingue la ortografía del origen de las descripciones ordinarias. Las declaraciones de nombres específicos del origen protegen los tramos exactos de producto y proyecto. Una etiqueta de nombre de archivo debe coincidir con un archivo rastreado del repositorio en el destino de su enlace de origen; la etiqueta convencional `README` puede omitir el sufijo `.md`. Las declaraciones de campos de operación protegen los vocabularios de cadenas devueltos y una fórmula de campo documentada. Un campo de tipo cadena cuya descripción completa son alternativas de identificadores entre comillas también declara valores literales. Las comillas, las mayúsculas y la ortografía parecida a un nombre de archivo, por sí solas, no otorgan ninguna exención.

Estas declaraciones requieren evidencia de origen: identidades de paquete o proyecto, terminología explícita, destinos de archivo reales o el contrato de comunicación de la operación. Los cambios en esas declaraciones de origen requieren comprobar la atestación y sus casos de regresión específicos. Los calificativos ordinarios alrededor de un tramo protegido siguen sujetos a la validación de etiquetas.

Los analizadores existentes del sitio y de CommonMark proporcionan las celdas de las tablas, incluidas las barras verticales escapadas y las de código en línea. Cada solicitud acotada se verifica en su contenedor completo en inglés, sin enviar ese texto circundante al proveedor. Las celdas exclusivas del sitio sin mapas de origen se verifican en el ensamblado de la página. Los reintentos, el ensamblado y las verificaciones de la página almacenada comparten la misma regla: los tramos protegidos deben conservar su ortografía, su orden de aparición y su etiqueta o campo de origen. Este es un contrato de origen finito, no un clasificador de idioma ni una revisión semántica.

<a id="invariants"></a>

## Invariantes

- Cada fragmento de origen enviado a un proveedor se mantiene dentro del límite; todos los bytes de origen y las anclas de toda la página sobreviven al ensamblado. Las unidades completadas reutilizadas conservan sus cargas útiles completas.
- Una unidad indivisible se rechaza antes de la admisión del proveedor.
- La configuración del proveedor, los límites de tokens de salida, los reintentos y las verificaciones financieras acumuladas rigen cada solicitud de forma independiente de cómo se divida la página.
- La salida se escribe solo después de que la validación del ensamblado tenga éxito.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `scripts/i18n/translate.py`: `english_requests`, `request_chars`, `translate_page`.
- `scripts/i18n/translate.py`: `request_system`, `translate_text` y la retroalimentación inicial.
- `scripts/i18n/reconcile.py`: `reconcile_completed_units`, `OriginalUnit` y la vinculación del manifiesto.
- `scripts/i18n/chunks.py`: rangos protegidos, cortes de origen contextuales y celdas de tabla analizadas.
- `scripts/i18n/context.py`: declaraciones de ortografía del origen y proyección residual de etiquetas.
- `scripts/i18n/requirements.in`: dependencias de ejecución de la traducción.
