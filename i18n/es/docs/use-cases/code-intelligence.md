<!-- i18n: source=docs/use-cases/code-intelligence.md blob=fac5260cc9d02070903874ade5298d5ecff95137 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Un índice de código para agentes de programación

Loom ofrece a los agentes de programación un índice compartido de tus repositorios git, servido a través de MCP, de modo que un agente hace una pregunta sobre la estructura y obtiene las líneas que la responden en lugar de leer archivos completos. Requiere haber añadido un repositorio y `LOOM_CODE_INGEST_ENABLED=true`. Cómo se construye el índice y cómo se mantiene actualizado se explica en [la descripción general del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md).

<a id="what-you-get"></a>

## Qué obtienes

- **Definiciones, llamadores e impacto.** `code.symbol`, `code.callers` y `code.impact`
  responden con el archivo y la línea, y cada respuesta indica el commit y el nivel de índice en que se basa.
  [Operaciones de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Búsqueda por significado y por cadena exacta.** `code.search` encuentra dónde vive un mecanismo;
  `code.grep` encuentra un mensaje o identificador exacto.
- **Historial y pull requests.** Historial de PR fusionados con comentarios de revisión, una comprobación de duplicados previa al PR y candidatos de bisect para una regresión. Esto requiere tener activado el historial de PR
  (`LOOM_CODE_PR_HISTORY_ENABLED`); los comentarios de revisión y los PR abiertos también necesitan un token de GitHub.
  [Historial, PR y releases](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) explica qué necesita cada uno.
- **Actualización visible.** El índice va por detrás de la rama según los commits fusionados, y una respuesta desactualizada lo indica. [Actualización](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Plugins.** El plugin de Claude Code se conecta con el toolset `coding` y el plugin de Codex
  con `core,coding,tracker,tasks`; ambos listan las herramientas de código. Cursor y otros clientes MCP usan
  `?toolset=coding`.
  [Conectar un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)

<a id="measured"></a>

## Resultados medidos

En el repositorio de ClickHouse, Claude Code con Loom nombró el archivo correcto entre sus tres propuestas en el
96.4% de 36 preguntas conceptuales (10 ejecuciones pareadas), frente al 91.4% sin Loom, con un costo de
modelo 21% menor (la factura del modelo del agente; se excluyen las llamadas al modelo del propio Loom).
En 200 issues de SWE-bench Verified, `code.search` ubicó un archivo modificado entre sus 10 primeros
resultados en el 82% de los casos (localización de archivos, no resolución de issues), frente al 57% de BM25 sobre todos los archivos
indexados y al 79% de BM25 solo sobre archivos `.py`; la diferencia con `.py` solamente está dentro del ruido.
[Con y sin Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Localización de archivos en SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## Inicio

[Agregar repositorios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) activa el índice para un
repositorio; [la descripción general del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) explica los
niveles.

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
