<!-- i18n: source=docs/benchmarks/skill-eval.md blob=c855282f25bb802cf3b249aa01819ec8e21227b7 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md), which is authoritative.

<a id="skill-eval"></a>

# Evaluación de la skill

La evaluación de la skill califica la skill `loom-memory` según el espacio de nombres que un agente deja tras de sí. La skill indica qué va dónde: una afirmación es un único `memory.set` de texto plano bajo una clave legible, un documento va a knowledge con una fila breve que actúa como puntero, una tabla de elementos con identidad va a records, un lease va a la memoria de trabajo, y una revisión reescribe la misma clave. La evaluación ejecuta una tarea, califica el espacio de nombres con `namespace.audit` y considera fallo cualquier hallazgo crítico.

<a id="the-pieces"></a>

## Las piezas

| Pieza             | Dónde                                                | Qué                                                                                                                                                                                                                                                                                                                                                                  |
| ----------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Briefs de tareas  | `bench/skill_eval/tasks/*.md`                        | Tres briefs que tientan a caer en los antipatrones: una investigación de alertas con un log largo, fragmentos de código, una tabla, un lease y un modelo grande que conservar (`sre_alert_investigation`); un lote de PRs con campos estructurados (`pr_review_batch`); una nota revisada dos veces (`research_note_revisions`). Cada uno indica su espacio de nombres y avisa de que será auditado. |
| Runner            | `bench/skill_eval.py`                                | Ejecuta la tarea (`--runner claude-code`, `codex`, `manual`, `replay`), luego `namespace.audit` (ventana de una hora, 5 muestras), después los contadores de forma; escribe el JSON de resultados.                                                                                                                                                                    |
| Agentes grabados  | `bench/skill_eval/replays/{bad,good}_agent.jsonl`    | Flujos de `{op, body}`. El agente malo codifica los valores dos veces, usa claves hash y hace polling de un lease almacenado como memoria; el bueno sigue la guía de [memoria del agente de investigación](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md).                                                                    |
| Suite de evaluación del plugin | `integrations/claude-code-plugin/evals/loom-memory/` | Los mismos briefs como casos de `claude plugin eval`, calificados a partir de la traza de herramientas contra un servidor Loom simulado (`evals/mocks/loom/`).                                                                                                                                                                                                       |
| Prueba unitaria   | `tests/unit/test_skill_eval.py`                      | Ejecuta ambas repeticiones a través del runner contra un Loom falso cuya auditoría usa los propios clasificadores del handler: el malo falla con los códigos esperados y el bueno pasa. También fija la lista de tareas y el contenido de los prompts de evaluación.                                                                                                  |

El front matter de cada brief incluye `task`, `namespace`, `agent` y `traps` (los
antipatrones que tienta). El runner sustituye `$ORG`, `$NAMESPACE` y `$AGENT` en un
flujo de repetición para que una misma grabación se ejecute en cualquier organización.

<a id="the-score"></a>

## La puntuación

`pass` significa que la auditoría devolvió **0 críticos** y **como máximo 1 advertencia**
(`MAX_WARNINGS_TO_PASS`). El informe lista cada hallazgo con su recuento, su proporción de las filas
revisadas, los bytes y la corrección del lado del escritor, y después los contadores de forma leídos de las propias
filas, de modo que la calificación no depende solo de la auditoría:

| Contador                 | Significado                                                                                                                       |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| `rows`                   | Filas de memoria activas                                                                                                          |
| `readable_key_share`     | Claves cuyo último segmento no es una secuencia hexadecimal larga y que no tienen ningún segmento con longitud de sha (la regla `hash_keys` de la auditoría) |
| `plain_text_value_share` | Valores que son una cadena y no JSON serializado                                                                                  |
| `documents`              | Documentos de conocimiento (`knowledge.list`)                                                                                     |
| `records`                | Registros tipados (`memory.record.query` por cada esquema registrado)                                                             |
| `working_memory_blocks`  | Bloques de memoria de trabajo (`working_memory.render`)                                                                           |

Las lecturas de forma se ejecutan **después** de la auditoría, por lo que nunca cuentan como lecturas del agente. Un
contador que el token no puede leer aparece en `shape.skipped`, no se informa como cero.

<a id="run-it"></a>

## Ejecutarlo

<a id="replay-no-model-is-called"></a>

### Replay (no se llama a ningún modelo)

Sirve cualquier Loom; basta con un stack local.

```bash
export LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY=<writer token> LOOM_ORG=<org>
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/bad_agent.jsonl            # exits 1: FAIL
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/good_agent.jsonl --baseline   # exits 0: PASS
```

Usa un espacio de nombres nuevo en cada ejecución (`--namespace skill-eval-sre-$(date +%s)`). La ventana de auditoría es de una hora, así que si reutilizas un espacio de nombres, las lecturas de la ejecución anterior entran en la calificación.

Sin conexión, la misma comprobación se ejecuta como prueba unitaria sin servidor:

```bash
.venv/bin/python -m pytest tests/unit/test_skill_eval.py -q
```

<a id="a-live-agent-with-the-skill-loaded"></a>

### Un agente en vivo con la skill cargada

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code --baseline
```

Esto consume tokens del modelo. Ejecuta, desde la raíz del repositorio:

```bash
claude -p --plugin-dir integrations/claude-code-plugin \
  --mcp-config <generated file: loom at $LOOM_BASE_URL/mcp?toolset=all, Bearer $LOOM_API_KEY> \
  --strict-mcp-config --allowedTools "mcp__loom__*" "Skill" "Read" \
  --permission-mode bypassPermissions --output-format json --max-turns 60 \
  "<the task body>"
```

- `--plugin-dir` carga las skills desde el checkout, de modo que la ejecución mide el texto de la skill del árbol de trabajo.
- `toolset=all` lista `memory_schema.register` y `memory.record.write`, que el listado `core` predeterminado omite. `knowledge.upsert` no aparece listado ni siquiera con `all` (ni el roster core ni el atómico lo incluyen); el agente lo llama a través de `loom_op`.
- La configuración MCP generada contiene el token; se crea con permisos `0600` y se elimina tras la ejecución.
- `--max-turns` (60 por defecto) limita la ejecución.

`--runner codex` ejecuta `codex exec` con la misma conexión al servidor; el plugin de Codex, si está instalado, aporta las skills.

<a id="any-other-agent"></a>

### Cualquier otro agente

`--runner manual` imprime el brief. Ejecuta el agente que prefieras sobre el espacio de nombres que indica y luego califica lo que dejó:

```bash
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual --audit-only
```

El mismo `--audit-only` califica un espacio de nombres escrito por una ejecución de `claude plugin eval` con servidores reales.

<a id="compare-with-a-baseline"></a>

### Comparar con una línea base

Edita la skill, ejecuta de nuevo y compara:

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code \
  --compare bench/out/skill_eval_sre_alert_investigation_baseline.json
```

La tabla de diferencias tiene una fila por cada código de hallazgo (gravedad y recuento, antes y después) y una por cada contador de forma. Un agente en vivo no es determinista, así que compara un cambio en la skill a lo largo de varias ejecuciones y no con una sola.

<a id="output"></a>

### Salida

Cada ejecución escribe `bench/out/skill_eval_<task>_<runner>_<ts>.json` (`--out-dir` lo cambia de ubicación). `--baseline` también escribe `bench/out/skill_eval_<task>_baseline.json`, la referencia que lee `--compare`. `bench/out/` está en gitignore.

<a id="the-plugin-eval-suite"></a>

## La suite de evaluación del plugin

```bash
claude plugin eval integrations/claude-code-plugin --trust-plugin --no-publish \
  --case 'loom-memory/*' --runs 3 --threshold 1.0 --json evals-result.json
```

Esto califica cada ejecución a partir de la traza de herramientas, sin conexión: el servidor MCP de Loom es un mock con respuestas fijas (`evals/mocks/loom/`), por lo que no se escribe nada. Los graders usan solo tipos de grader documentados:

- `tool_used` con `max: 0` en escrituras de memoria cuya entrada tenga una clave hash, un valor que sea una cadena JSON de JSON, un valor de más de unos 2 KB, una clave de lease o lock, o una clave de revisión versionada: las formas que `namespace.audit` califica como críticas o de advertencia;
- `tool_used: Skill` que coincida con `loom-memory`, el indicador de que el plugin se activó;
- mínimos positivos para la memoria de trabajo y las claims por elemento.

No hay graders `llm`: una lectura de la prosa con una rúbrica no es un criterio de aprobación. Los casos se filtran con un glob de nombre (`--case sre_alert_investigation`). `--ablation with-without`, el valor predeterminado cuando el plugin se resuelve, informa con qué frecuencia el mismo prompt sin el plugin activa los mismos graders.

El framework no tiene un grader de scripts, así que la calificación a nivel de espacio de nombres es un segundo comando. Con `--mocks off --allow-real-servers --allow-tools "mcp__plugin_loom_loom__*"` y un token de escritor en `LOOM_API_KEY`, el agente escribe en el espacio de nombres del caso, y `bench/skill_eval.py --task <case> --runner manual --audit-only` lo califica.

<a id="reading-a-failure"></a>

## Cómo leer un fallo

El remedio de cada hallazgo es la corrección del lado del escritor. La skill `loom-optimize` asigna los mismos
códigos a la reelaboración de filas que ya existen ([optimizar un espacio de nombres](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)).

| Hallazgo                                   | La regla que no se cumplió                                                          |
| ------------------------------------------ | ----------------------------------------------------------------------------------- |
| `double_encoded_values`                    | Envía el valor una sola vez; nunca uses `json.dumps` dos veces sobre una cadena.    |
| `hash_keys`                                | Las claves son legibles, con la forma `<scope>/<kind>/<id>`, no hashes.             |
| `coordination_as_memory`                   | Los leases y los locks pertenecen a la memoria de trabajo.                          |
| `exact_key_store`                          | Mantén el puntero en la memoria de trabajo en lugar de consultar por clave.         |
| `oversized_values`, `json_prefix_excerpts` | Un documento va a knowledge, con una fila de puntero corta.                         |

Una ejecución que pasa la auditoría con `documents: 0` en la tarea SRE dejó el modelo grande
donde la búsqueda no puede alcanzarlo. Lee la línea de forma, no solo el veredicto.

<a id="adding-a-task"></a>

## Añadir una tarea

Un nuevo brief necesita front matter, un espacio de nombres `skill-eval-…` indicado en su cuerpo y la frase final que indica que el espacio de nombres será auditado; un caso de evaluación del plugin correspondiente con un cuerpo idéntico byte por byte; y una actualización de la lista de tareas que fija `tests/unit/test_skill_eval.py`.
Consulta [añadir un caso](adding-a-case.md#skill-eval-tasks).

<a id="what-this-does-not-measure"></a>

## Qué no mide esto

La calidad de recuperación. Determinar si las afirmaciones que escribió un agente responden la pregunta de la semana próxima es tarea de un benchmark de recuperación ([resumen](overview.md)); esta evaluación califica las formas que hacen imposible la recuperación.

<a id="related"></a>

## Relacionado

- [Memoria del agente de investigación](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md): la receta que sigue la
  buena reproducción.
- [Optimizar un espacio de nombres](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md): reelaboración de las filas que señala la auditoría.
- [Plugin de Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) y
  [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md).
- [`integrations/claude-code-plugin/evals/README.md`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/evals/README.md).
