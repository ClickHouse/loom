<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# Búsqueda de documentos y RAG para agentes

Loom indexa tus documentos (Markdown y texto, y archivos PDF, `.docx` y `.pptx` que convierte su
servicio de extracción) y permite que los agentes los busquen con citas a través de MCP, HTTP o
los SDK. Es la mitad de recuperación de la generación aumentada por recuperación (RAG): tu agente o
modelo redacta la respuesta a partir de los pasajes que devuelve Loom.

<a id="what-you-get"></a>

## Qué obtienes

- **Fragmentos que siguen la estructura del documento.** `knowledge.add` almacena el texto y,
  opcionalmente, el archivo original; Loom lo divide por sus encabezados, aplica ventanas a cada sección e
  incrusta cada fragmento. [Documentos de conocimiento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **Citas.** Un resultado indica el `source_uri` de su documento y el pasaje, de modo que un agente puede citar
  y enlazar la fuente.
- **Una sola búsqueda en memoria, documentos y conversaciones.** `loom_search` ejecuta las búsquedas de memoria,
  conocimiento, perfil y conversaciones en una sola llamada, a través de los espacios de nombres de memoria
  del solicitante. [Búsqueda unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Fuentes de documentos.** Los administradores de un espacio de nombres pueden registrar fuentes de las que Loom extrae documentos;
  el worker de sincronización necesita `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`.
  [Fuentes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **Un umbral de similitud.** `loom_search` descarta los resultados de conocimiento por debajo de un umbral de similitud
  en lugar de entregar al modelo un pasaje débil. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **Control de acceso en cada pasaje.** Los documentos viven en espacios de nombres con roles de lector, escritor y
  administrador. [Espacios de nombres y uso compartido](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## Medición

En un corpus incluido en el repositorio de archivos PDF, DOCX y PPTX (27 archivos reservados para evaluación), el extractor de Loom,
con OCR activado (su valor predeterminado), obtuvo 0.999 de recall de texto y 0.991 de F1 de celdas de tabla en la
ejecución medida ([análisis de documentos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)).
La calidad de la recuperación sobre documentos solo se mide en parte;
[cobertura y limitaciones conocidas](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) indica qué falta todavía.

<a id="start"></a>

## Primeros pasos

La [guía de inicio rápido](../getting-started/quickstart.md) ejecuta el stack con el servicio de extracción.
Añade un documento desde la página Library del dashboard, con `knowledge.add` para texto,
o, para un archivo que convierte el extractor, con `knowledge.upload` y luego `knowledge.ingest`
([ingesta de archivos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
