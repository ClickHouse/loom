<!-- i18n: source=docs/architecture/agno-adapter.md blob=3cda341b0d2ad39a80b6546d3002b103a90de53d model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md), which is authoritative.

<a id="agno-user-memory-database"></a>

# Base de datos de memoria de usuario de Agno

El adaptador de Agno implementa los métodos de memoria de usuario de su interfaz `BaseDb` 3.2
a través del SDK síncrono de Loom. La dependencia opcional, fijada a una versión menor, se mantiene fuera
del SDK principal y del servidor. `MemoryManager` acepta esta base de datos de forma independiente a
la base de datos de sesiones de un agente. Las capacidades que no son de memoria se rechazan en lugar de usar un almacén
predeterminado del framework.

<a id="invariants"></a>

## Invariantes

Cada adaptador se vincula a un usuario y a un almacén de aplicación. Cada llamada al SDK lleva ese usuario
y una sesión derivada; las claves incluyen el mismo digest de ámbito más un digest del id nativo.
Se rechaza un usuario explícito distinto. Las memorias de usuario nativas son JSON en filas episódicas
de Loom, que conservan ids, metadatos y revisiones sin fusión semántica de
registros nativos diferentes. La procedencia del actor y de la fuente proviene de la ruta de escritura normal de Loom.
Se aplican las reglas habituales de eliminación de memoria, borrado y esquema progresivo.

El listado sigue páginas limitadas y proyectadas, elimina duplicados por solapamiento e hidrata los valores
truncados antes de convertirlos en objetos nativos. Los filtros de contenido, temas y atribución, la ordenación, la paginación y las estadísticas se ejecutan sobre esta instantánea de usuario/almacén acotada. No hay estado persistente local. Las ediciones concurrentes conservan
la última escritura del servidor; las operaciones masivas son una secuencia de escrituras y pueden terminar
parcialmente si hay un fallo. Las concesiones de espacio de nombres aíslan aplicaciones que no confían entre sí.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/agno.py`: `LoomMemoryDb`.
- `sdk/python/tests/test_agno.py`: comportamiento nativo del gestor de memoria.
- `tests/integration/test_sdk_agno_memory.py`: ámbito de memoria y procedencia en vivo.
