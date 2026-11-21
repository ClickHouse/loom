<!-- i18n: source=docs/architecture/haystack-adapter.md blob=c2cfa8a3d7cfb7c046a16c39b584aa93d1ea0420 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md), which is authoritative.

<a id="haystack-document-store-and-retriever"></a>

# Document store y retriever de Haystack

El adaptador opcional de Haystack 3.5 implementa su protocolo de document store y un componente de recuperación nativo a través del SDK síncrono de Loom. El SDK principal y el servidor no dependen del framework. Cada llamada HTTP tiene un ciclo de vida de cliente gestionado por el SDK; el adaptador no deja ningún servidor, base de datos local, caché ni proceso persistente en segundo plano.

<a id="invariants"></a>

## Invariantes

Un store asigna cada id de documento nativo a un URI de origen con hash bajo un prefijo específico del store. Una etiqueta coincidente delimita el listado y la recuperación. La serialización nativa completa del documento, incluidos los metadatos, los embeddings y los campos binarios, se guarda en los bytes del archivo original del documento. Los metadatos del fragmento llevan únicamente un pequeño marcador de formato; el texto se indexa como contenido del documento, o el id cuando no hay texto. Las rutas normales de Loom para escritura de documentos, procedencia, permisos del espacio de nombres, versionado y borrado se aplican a cada escritura y eliminación. Los documentos se comparten en el espacio de nombres y no son privados de un usuario.

El conteo y el filtrado cargan todo el store mediante páginas de cursor. Los helpers de filtro nativos aplican comparaciones y lógica booleana. La recuperación busca hasta 100 fragmentos con límites explícitos de espacio de nombres, etiqueta y URI, carga los documentos completos y elimina duplicados por URI de origen, conservando el resultado mejor clasificado de cada documento. Estas operaciones no tienen una instantánea entre solicitudes. Las escrituras concurrentes pueden provocar un conflicto devuelto por Loom. Las comprobaciones de duplicados y las operaciones sobre varios documentos no son transacciones.

Las conexiones serializadas llevan referencias Secret de variables de entorno de Haystack; los tokens Secret sin procesar rechazan la serialización. Las URL de endpoint explícitas rechazan secretos incrustados. El adaptador conserva la política de confianza de deserialización de pipelines de Haystack y nunca registra módulos de confianza ni selecciona la deserialización insegura.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/haystack.py`: `LoomDocumentStore` y `LoomRetriever`.
- `sdk/python/tests/test_haystack.py`: comportamiento nativo del document store y del pipeline.
- `tests/integration/test_sdk_haystack.py`: persistencia y recuperación de documentos en vivo.
