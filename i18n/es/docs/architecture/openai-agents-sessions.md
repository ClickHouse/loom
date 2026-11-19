<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=d96f44f5824749f9ff8d8d20e70fb23370c26d8b model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# Sesiones de conversación de OpenAI Agents

`LoomSession` implementa las interfaces de sesión nativas de Python y TypeScript sobre
las memorias episódicas de Loom. Las herramientas existentes pueden conservar por separado hechos más allá de una
conversación. Los paquetes de los frameworks siguen siendo dependencias opcionales.

Cada elemento es una llamada a `memory.set` con su JSON nativo, una clave formada por marca de tiempo y valor aleatorio,
el usuario final y una sesión derivada del usuario y del id de sesión nativo.
Las escrituras puntuales omiten la coalescencia del SDK, de modo que los elementos repetidos concurrentes permanecen separados.
Las filas episódicas conservan los elementos repetidos sin extracción semántica ni fusión de duplicados. Python y TypeScript usan prefijos de sesión derivada distintos porque
sus formatos de elementos nativos difieren.

Las lecturas paginan mediante `memory.list`, eliminan duplicados de claves superpuestas y completan los valores ausentes
o truncados con `memory.get`. Los elementos se devuelven en orden de clave, y un límite
solicitado conserva los elementos más recientes. Las lecturas con límite positivo también recorren todo
el historial porque los tiempos de llegada al servidor no pueden acotar las claves generadas por el escritor. El adaptador de TypeScript
toma una instantánea del usuario validado en la construcción. Sus cargas de bytes nativas de imagen y archivo usan arreglos JSON en el almacenamiento y se devuelven como `Uint8Array`; el JSON ordinario y la
información de medios no cambian. La eliminación incluye el usuario y la sesión derivada: `pop_item` elimina el último elemento, y `clear_session` elimina el
historial listado. Las adiciones o limpiezas concurrentes son operaciones individuales del servidor, no una transacción sobre la conversación.
Si un elemento desaparece antes de completarse, la lectura falla en lugar de devolver un
elemento nativo incompleto. Las claves usan relojes de los escritores; los escritores concurrentes no comparten una
secuencia del servidor.

Ambas implementaciones conservan la familia de protección acotada ya existente de la aplicación
desde la construcción y en cada callback. Python también conserva los contribuyentes activos en
el contexto del callback. Volver a enlazar el cliente no puede quitar un contribuyente anterior.
Los ids nativos de usuario y de sesión seleccionan el historial almacenado; no establecen una familia de
protección. Usa una instancia de sesión por cada familia lógica de conversación y
reabre la misma cápsula de protección propiedad de la aplicación al restaurar esa
conversación.

Las lecturas y adiciones del historial nativo se ejecutan como trabajo automático bajo todos los contribuyentes
conservados. Una respuesta marcada protege a todos los contribuyentes antes de devolver su valor.
Una primera respuesta de historial marcada y completa aún puede devolver sus elementos; la paginación,
la hidratación o las escrituras de elementos posteriores se suprimen. La escritura de un elemento marcado detiene un lote antes del siguiente elemento. Las operaciones explícitas de pop y clear siguen siendo utilizables tras la protección y continúan observando las respuestas bajo los contribuyentes conservados.
Las herramientas seleccionadas por el modelo mantienen su semántica de operación explícita.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

<a id="invariants"></a>

## Invariantes

La organización, el espacio de nombres y el nombre del agente delimitan cada historial. Un usuario omitido
escribe filas en el espacio de nombres compartido; un usuario explícito selecciona el historial de ese usuario. El
servidor proporciona la procedencia del actor y de la fuente. Se aplican las reglas ordinarias de eliminación de memoria, borrado
y actualización progresiva. No se añade ninguna base de datos ni esquema de historial local.
