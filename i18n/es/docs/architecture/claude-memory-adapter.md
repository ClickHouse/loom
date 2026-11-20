<!-- i18n: source=docs/architecture/claude-memory-adapter.md blob=b72a628092f2f86d148e87a01ed0f9e99469b83f model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md), which is authoritative.

<a id="claude-api-memory-file-tree"></a>

# Árbol de archivos de memoria de la Claude API

El adaptador de memoria de la Claude API implementa las bases de la herramienta de memoria del framework en Python y los handlers de memoria en TypeScript mediante documentos de conocimiento de Loom. Los SDK del framework son dependencias opcionales. Ambas implementaciones usan el mismo URI de documento y la misma etiqueta de store, por lo que cada lenguaje lee los archivos creados por el otro.

Un archivo es `loom-memory://<store>/memories/<path>`. Los directorios son prefijos de URI, sin filas de directorio independientes. Las rutas se normalizan y se rechazan si no permanecen bajo `/memories`. Las lecturas de directorios usan `knowledge.list` con una etiqueta de store, un prefijo de URI y campos de URI proyectados; las lecturas de archivos usan `knowledge.get`. Las ediciones usan `knowledge.upsert`, y el borrado usa llamadas acotadas a `knowledge.bulk_delete`. Al renombrar, se copia cada archivo antes de eliminar su URI anterior. Un renombrado fallido puede dejar copias, y las ediciones simultáneas de lectura-modificación-escritura conservan la escritura posterior.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/claude_memory.py`: `AsyncLoomMemoryTool` y
  `LoomMemoryTool`.
- `sdk/typescript/src/integrations/claude-memory.ts`: `loomMemoryTool` y
  `loomMemoryHandlers`.

<a id="invariants"></a>

## Invariantes

Un store agrupa archivos; la autorización del espacio de nombres controla el acceso. Los árboles privados de usuario requieren espacios de nombres separados. El adaptador nunca interpreta el nombre de un store como una concesión de acceso. El actor, la procedencia de la fuente, las revisiones de documentos, la limpieza de blobs y el borrado siguen las operaciones de conocimiento existentes. No se añade estado persistente local, base de datos ni esquema. Las operaciones de directorio no son instantáneas atómicas.
