<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=96269df2f0037fdcb16f6a101e85bf9aafedef73 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# Sesiones de conversación de OpenAI Agents

`LoomSession` implementa las interfaces de sesión nativas de Python y TypeScript sobre
las memorias episódicas de Loom. Las herramientas existentes pueden conservar por separado hechos más allá de una
conversación. Los paquetes de los frameworks siguen siendo dependencias opcionales.

Cada elemento es una llamada a `memory.set` con su JSON nativo, una clave formada por marca de tiempo y valor aleatorio,
el usuario final y una sesión derivada del usuario y del id de sesión nativo.
Las escrituras puntuales omiten la coalescencia del SDK, de modo que los elementos repetidos y concurrentes permanecen separados.
Las filas episódicas conservan los elementos repetidos sin extracción semántica ni fusión de duplicados. Python y TypeScript usan prefijos de sesión derivada distintos porque
sus formatos de elemento nativos son diferentes.

Las lecturas paginan mediante `memory.list`, eliminan duplicados de claves solapadas y completan los valores ausentes
o truncados con `memory.get`. Los elementos se devuelven en orden de clave, y un límite
solicitado conserva los elementos más recientes. Las lecturas con límite positivo también recorren el
historial, porque los tiempos de llegada al servidor no pueden acotar las claves generadas por el escritor. El adaptador de TypeScript
captura el usuario validado en el momento de la construcción. Sus cargas de bytes nativas de imagen y archivo usan arreglos JSON en el almacenamiento y se devuelven como `Uint8Array`; el JSON ordinario y la
información de medios no cambian. La eliminación incluye el usuario y la sesión derivada: `pop_item` elimina el último elemento y `clear_session` elimina el
historial listado. Las adiciones o limpiezas concurrentes son operaciones individuales del servidor, no una transacción sobre la conversación.
Si un elemento desaparece antes de completarse, la lectura falla en lugar de devolver un
elemento nativo incompleto. Las claves usan los relojes de los escritores; los escritores concurrentes no comparten una
secuencia del servidor.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

<a id="invariants"></a>

## Invariantes

La organización, el espacio de nombres y el nombre del agente delimitan cada historial. Si se omite el usuario, se
escriben filas compartidas del espacio de nombres; un usuario explícito selecciona el historial de ese usuario. El
servidor aporta la procedencia del actor y de la fuente. Se aplican las reglas habituales de eliminación de memoria, borrado
y actualización progresiva. No se añade ninguna base de datos de historial local ni esquema.
