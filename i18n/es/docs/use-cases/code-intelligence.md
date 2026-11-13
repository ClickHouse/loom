<!-- i18n: source=docs/use-cases/code-intelligence.md blob=71addec3496d26800b8e03234eb0c0adc578b52c model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence.md), which is authoritative.

<a id="a-code-index-for-coding-agents"></a>

# Un índice de código para agentes de programación

Loom ofrece a los agentes de programación un índice compartido de tus repositorios git, servido por MCP, de modo que un
agente hace una pregunta sobre la estructura y obtiene las líneas que la responden en lugar de leer archivos
completos. Requiere un repositorio añadido y `LOOM_CODE_INGEST_ENABLED=true`. Cómo se construye el índice y cómo se mantiene actualizado se explica en [la descripción general del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md);
lo que ofrece en cada lenguaje está en [índice de código por lenguaje](code-intelligence-by-language.md).

<a id="what-you-get"></a>

## Qué obtienes

- **Definiciones, llamadores e impacto.** `code.symbol`, `code.callers` y `code.impact`
  responden con el archivo y la línea, y cada respuesta indica el commit y el nivel del índice que la respaldan.
  [Operaciones de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- **Búsqueda por significado y por cadena exacta.** `code.search` encuentra dónde reside un mecanismo;
  `code.grep` encuentra un mensaje o identificador exacto.
- **Historial y pull requests.** Historial de PR fusionados con comentarios de revisión, una comprobación de duplicados previa al PR y candidatos de bisect para una regresión. Requieren activar el historial de PR
  (`LOOM_CODE_PR_HISTORY_ENABLED`); los comentarios de revisión y los PR abiertos requieren además un token de GitHub.
  [Historial, PR y versiones](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) indica qué necesita cada uno.
- **Actualización visible.** El índice va por detrás de la rama en commits fusionados, y una respuesta desactualizada lo indica. [Actualización](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md)
- **Plugins y clientes MCP.** El plugin de Claude Code se conecta con el toolset `coding` y
  el plugin de Codex con `core,coding,tracker,tasks`; ambos listan las herramientas de código. Cursor, GitHub
  Copilot, Gemini CLI, Cline, Kilo Code, OpenCode y otros clientes MCP las listan con
  `?toolset=coding` en su URL (`loom install` lo añade para Cursor; `--toolset coding` lo añade
  para los demás que escribe).
  [Conectar un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)

<a id="measured"></a>

## Mediciones

En el repositorio de ClickHouse, se pidió a Claude Code que terminara cada respuesta con hasta tres
archivos. Con Loom nombró el archivo correcto en el 96.4% de 36 preguntas conceptuales (10 ejecuciones emparejadas),
frente al 91.4% sin Loom, y las ejecuciones con Loom costaron un 21% menos (la factura del modelo del agente;
sin contar las llamadas al modelo del propio Loom).
En 200 incidencias de SWE-bench Verified, `code.search` situó un archivo modificado entre sus 10 primeros
resultados en el 82% de los casos (localización de archivos, no resolución de incidencias), frente al 57% de BM25 sobre todos los archivos
indexados y al 79% de BM25 solo sobre archivos `.py`; la diferencia con `.py` está dentro del ruido.
[Con y sin Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom) ·
[Localización de archivos en SWE-bench Verified](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#swe-bench-verified-file-localization)

<a id="start"></a>

## Primeros pasos

[Añadir repositorios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md) activa el índice para un
repositorio; [la descripción general del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) explica los
niveles.

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
