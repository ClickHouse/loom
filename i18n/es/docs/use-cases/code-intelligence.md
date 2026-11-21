<!-- i18n: source=docs/use-cases/code-intelligence.md blob=9f10f6e9bc93b39e97d3aee93cedeeb7260a7297 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Un índice de código para agentes de programación

Loom ofrece a los agentes de programación un índice compartido de tus repositorios git, servido por MCP, de modo que un
agente hace una pregunta sobre la estructura y recibe las líneas que la responden en lugar de leer archivos
completos. Requiere un repositorio añadido y `LOOM_CODE_INGEST_ENABLED=true`. Cómo se construye el índice y se mantiene actualizado se explica en [la descripción general del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md);
lo que ofrece en cada lenguaje está en [índice de código por lenguaje](code-intelligence-by-language.md).

<a id="what-you-get"></a>

## Qué obtienes

- **Definiciones, llamadores e impacto.** `code.symbol`, `code.callers` y `code.impact`
  localizan el código con referencias de archivo y línea. `code.index_status` informa del commit indexado de cada nivel. [Operaciones de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Búsqueda por significado y por cadena exacta.** `code.search` encuentra dónde reside un mecanismo;
  `code.grep` encuentra un mensaje o identificador exacto.
- **Historial y pull requests.** Historial de PR fusionadas con comentarios de revisión, una comprobación de duplicados previa a la PR y candidatos de bisect para una regresión. Requieren activar el historial de PR
  (`LOOM_CODE_PR_HISTORY_ENABLED`); los comentarios de revisión y las PR abiertas también necesitan un token de GitHub.
  [Historial, PR y versiones](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) indica qué requiere cada
  uno.
- **Lecciones sobre el código.** `code.symbol` devuelve las memorias vinculadas a una definición, y
  `code.search`, `code.callers`, `code.callees`, `code.impact`, `code.outline` y el
  `code.context_pack` de una tarea adjuntan las lecciones de los archivos que devuelven, cada una indicando si su
  código se movió, cambió o ya no existe. [Memoria vinculada al código](memory-linked-to-code.md)
- **Actualización verificable.** Cada nivel puede ir por detrás de la rama. Revisa `as_of` cuando
  se devuelva, o usa `code.index_status` para ver la marca de agua del nivel correspondiente. La ausencia de
  metadatos de actualización significa desconocido. [Actualización](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Plugins y clientes MCP.** El plugin de Claude Code se conecta con el toolset `coding` y
  el plugin de Codex con `core,coding,tracker,tasks`; ambos listan las herramientas de código. Cursor, GitHub
  Copilot, Gemini CLI, Cline, Kilo Code, OpenCode y otros clientes MCP las listan con
  `?toolset=coding` en su URL (`loom install` lo añade para Cursor; `--toolset coding` lo añade
  para los demás que escribe).
  [Conectar un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## Medición

En el repositorio de ClickHouse, se pidió a Claude Code que terminara cada respuesta con hasta tres
archivos. Con Loom nombró el archivo correcto en el 96.4% de 36 preguntas conceptuales (10 ejecuciones emparejadas),
frente al 91.4% sin Loom, y las ejecuciones con Loom costaron un 21% menos (la factura del modelo del agente;
sin contar las llamadas al modelo del propio Loom).
En 200 incidencias de SWE-bench Verified, `code.search` clasificó un archivo modificado entre sus 10 primeros
resultados en el 82% de los casos (localización de archivos, no resolución de incidencias), frente al 57% de BM25 sobre todos los archivos
indexados y el 79% de BM25 solo sobre archivos `.py`; la diferencia con `.py` está dentro del ruido.
[Con y sin Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Localización de archivos en SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="a-coding-agent-that-reads-the-definition-it-needs"></a>

## Un agente de programación que lee la definición que necesita

Empieza con un repositorio de confianza. En la pila publicada, sigue los
[pasos del índice configurado por el operador](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack);
el flujo de añadir repositorio del dashboard requiere un sandbox que esa pila no habilita. Conecta
Claude Code o Codex con su plugin e inicia una sesión dentro del checkout de ese repositorio
([coincidencia de repositorio](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#when-the-code-index-is-used)).

1. Pide al agente que llame a `loom_op` con `op: "code.index_status"` y
   `arguments: {"namespace": "<indexed namespace>", "brief": true}`. Anota `git_head`,
   el commit indexado del nivel rápido; un valor vacío significa que ese nivel no tiene marca de agua.
2. Elige una función cuya definición conozcas. Pide al agente que llame a `loom_code_symbol`
   en ese espacio de nombres con el nombre de la función e `include_body: true`. Comprueba
   `path`, `start_line`, `end_line`, `body` y `tier` devueltos.
3. Continúa con esta comprobación solo para una definición inequívoca con `tier: "treesitter"`
   y un cuerpo completo, sin elisiones. Compara su ruta, rango y cuerpo con el código fuente en el
   `git_head` anotado antes de fiarte de ella. Usa una lectura local si falta la marca de agua o
   el cuerpo, si la definición difiere o si se devuelve otro nivel. Los niveles del compilador
   avanzan de forma independiente; sus marcas de agua requieren una comprobación de revisión aparte
   ([actualización](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md#the-mirrors-own-age)).
4. Inicia una sesión nueva en el mismo checkout y repite ambas llamadas. El índice suministra
   la definición de nuevo; el agente no necesita cargar el chat anterior ni el archivo completo.

Esto comprueba una definición del nivel rápido. Las ediciones sin confirmar siguen requiriendo lecturas locales, y
una búsqueda exitosa no es una medición de la precisión de la tarea ni del ahorro de costes. Consulta
[operaciones de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md) para búsqueda, llamadores e impacto.

<a id="start"></a>

## Empezar

[Añadir repositorios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) activa el índice para un
repositorio; [la descripción general del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) explica los
niveles.

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
