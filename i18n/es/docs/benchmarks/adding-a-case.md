<!-- i18n: source=docs/benchmarks/adding-a-case.md blob=63bcbeab205c463f07cd6aa007c0f8d4eeabefab model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), which is authoritative.

<a id="adding-a-benchmark-case-dataset-or-suite"></a>

# Añadir un caso, un dataset o una suite de benchmark

Los conjuntos incluidos en el repositorio son pequeños ([tamaños de los conjuntos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#set-sizes)), así que añadir un caso nuevo es la forma más barata de mejorar la suite. Lee primero el encabezado o el `_comment` del archivo: la mayoría explican cómo se construyó su gold y qué debe cumplir una fila nueva.

<a id="add-a-case-to-an-existing-set"></a>

## Agregar un caso a un conjunto existente

<a id="memory-qa-benchdatasetssynthetic_memory_qajson"></a>

### QA de memoria (`bench/datasets/synthetic_memory_qa.json`)

Agrega una pregunta a una conversación, o una conversación:

```json
{
  "conversation_id": "06-billing",
  "conversation": [{ "role": "user", "content": "Our invoices go out on the 3rd of each month." }],
  "questions": [{ "question": "When do invoices go out?", "gold_keys": ["invoice", "3rd"] }]
}
```

Un acierto es cualquier `memory_key` o fragmento devuelto que contenga una cadena gold (sin distinguir mayúsculas y con guiones bajos como espacios). Elige palabras gold que una memoria correcta deba contener y que una incorrecta difícilmente contenga. Mantenlo sintético: sin texto de terceros ni personas reales.

**Mueve la línea base.** El umbral de 0.80 de la compuerta se registró con este archivo. Ejecuta la compuerta en un despliegue real antes y después, y actualiza `MIN_RECALL` en el `Makefile` si el valor cambia. Pruebas: `tests/unit/test_bench_quality_gate.py`, `bench/test_quality.py`.

<a id="extraction-and-graph-regressions"></a>

### Regresiones de extracción y de grafo

- Las filas de `bench/fixtures/extraction_regressions.json` son `{id, messages, expected}`:
  frases que deben aparecer en la clave o el valor de alguna memoria.
- Las filas de `graph_regressions.json` son
  `{id, messages, expected_triples: [{subject, relation, object}], as_of}`.
- Un campo `_note` nombra la clase de fallo; el arnés lo ignora.

`tests/unit/test_algorithm_gate_assets.py` comprueba que cada fila tenga mensajes y una expectativa no vacía. Ejecuta `make bench-algorithm-gate` contra un LLM de extracción real para ver pasar el caso.

<a id="code-plane-qa-benchfixturescode_intel_qajsonl"></a>

### QA del plano de código (`bench/fixtures/code_intel_qa*.jsonl`)

Un objeto JSON por línea; las líneas `//` son comentarios:

```json
{"id": "co037", "type": "conceptual", "query": "where are part merges scheduled", "gold": {"paths": ["src/Storages/MergeTree/MergeTreeBackgroundExecutor"]}}
{"id": "sd011", "type": "symbol_def", "query": "IColumn::insert", "gold": {"qualified_name": "DB::IColumn::insert", "path_prefix": "src/Columns/"}}
{"id": "ab011", "type": "abstain_probe", "query": "kubernetes ingress annotation for sticky sessions", "gold": {}}
```

El gold de cada tipo está en el docstring de `bench/code_intel_qa.py` (`callers` usa `expected_callers`, `hub` usa `path_prefix` y `symbols`). Las rutas son prefijos sin la extensión. Los campos opcionales `lane`, `facets` y `namespace` enrutan y segmentan la fila.

- **Verifica el gold contra un índice en vivo** antes de que sirva como compuerta: ejecuta una vez sin umbrales mínimos, lee las discrepancias y corrige el gold.
- **Una sonda de abstención debe tratar sobre código que no está en el repositorio.**
- **Conserva el conjunto de tipos del archivo principal.** `bench/test_code_intel_qa.py` (`test_fixture_loads_and_validates`) afirma que `code_intel_qa.jsonl` tiene al menos 30 filas y exactamente los tipos `symbol_def`, `callers`, `conceptual`, `abstain_probe`. Las filas de hub, historial y conjunto de pruebas van en sus propios archivos de carril.
- `make bench-code-intel-dry` debe seguir pasando: cada carril alcanza su techo con una recuperación sintética perfecta.
- Los carriles extraídos se regeneran, no se editan a mano: `code_intel_mine_prs.py` y `code_intel_ka_slice.py` escriben el archivo y los conteos de su encabezado.

<a id="coding-agent-lessons-benchfixturescoding_agent_qajson"></a>

### Lecciones del agente de programación (`bench/fixtures/coding_agent_qa.json`)

Agrega a `lessons` (una fila de memoria con clave `memory_key`) y a `questions` (`question`, `question_type` de la taxonomía, `gold_keys`, y `fix_reuse` opcional). Cada clave gold debe nombrar una clave de lección real: de lo contrario falla `bench/test_coding_agent_qa.py` (`test_load_corpus_rejects_dangling_gold_key`). `--dry` debe seguir pasando.

<a id="op-planning-tasks-benchfixturesop_planning_tasksjsonl"></a>

### Tareas de planificación de ops (`bench/fixtures/op_planning_tasks.jsonl`)

Una tarea incluye `id`, `class` (`instruction_type`, `structure`), `prompt`, un plan `gold` (herramienta, argumentos requeridos, argumentos literales) y `assertions` del almacén. `tests/unit/test_op_planning_bench.py` (`test_every_gold_tool_and_required_arg_exists_on_the_surface`) falla cuando una herramienta o un argumento gold ya no existe en la superficie MCP. Una tarea cuya op se retiró pasa a `op_planning_tasks_retired.jsonl` en lugar de eliminarse.

<a id="skill-eval-tasks"></a>

### Tareas de skill-eval

Un brief en `bench/skill_eval/tasks/` tiene front matter (`task`, `namespace`, `agent`, `traps`), nombra un espacio de nombres `skill-eval-…` en su cuerpo y termina indicando que el espacio de nombres será auditado. El mismo cuerpo es un caso de `claude plugin eval` en `integrations/claude-code-plugin/evals/loom-memory/<task>/prompt.md`, idéntico byte a byte después del front matter. `tests/unit/test_skill_eval.py` comprueba las tres reglas y fija la lista de tareas, así que una tarea nueva también actualiza esa prueba. Consulta [evaluación de skills](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md).

<a id="wire-in-a-new-dataset"></a>

## Integrar un nuevo dataset

1. **No lo subas al repositorio.** Colócalo en `bench/datasets/` o acepta una bandera de ruta. Si se descarga, añade una ruta `--fetch` o imprime el comando de descarga cuando falte el archivo, como hace `longmemeval.py`.
2. **Registra la procedencia en el docstring del harness:** origen, nombres de archivo, número de elementos y la licencia que declara el dataset. Añade una fila a [conjuntos de datos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets).
3. **Conserva las categorías propias del dataset** como una columna, y nombra el subconjunto comparable si las tablas publicadas usan uno.
4. **Los nombres de benchmarks públicos están permitidos en `bench/`**, pero nunca como motivo de una decisión en la prosa bajo `server/` (`tests/unit/test_public_copy.py` `BENCHMARK_NAMES`).
5. Crea espacios de nombres temporales con un prefijo de `LOOM_NAMESPACE_BENCH_PREFIXES` (`server/settings/catalog.py`; los harnesses nuevos usan `bench-`), de modo que `loom namespaces-gc` y el GC de espacios de nombres encuentren lo que deja una ejecución abortada.

<a id="write-a-new-harness"></a>

## Escribir un nuevo harness

Todo harness sigue esta estructura:

| Regla                                  | Cómo                                                                                                                                                                                                                                                                            |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Script independiente                   | argparse, `--base-url`, `--token` (por defecto `LOOM_API_KEY`), `--out`; se ejecuta como `PYTHONPATH=$PWD python bench/<name>.py` y como módulo                                                                                                                                  |
| Política HTTP compartida               | Enviar con `bench/_http.py` (`post_json` / `apost_json`): token Bearer, esperas ante 429, reintentos acotados ante 5xx, errores que incluyen el cuerpo de la respuesta                                                                                                           |
| El docstring es el manual              | Qué mide, qué no mide, el dataset, el costo y un comando de ejecución                                                                                                                                                                                                           |
| Núcleo de puntuación puro              | Funciones de puntuación por encima de la ruta en vivo, importables sin un servidor, con una prueba unitaria (`bench/test_<name>.py` o `tests/unit/test_bench_<name>.py`)                                                                                                         |
| Un modo `--dry` o de autoprueba        | Ejercita el evaluador con entrada sintética, sin servidor y sin gasto                                                                                                                                                                                                           |
| Lecturas de evaluación                 | Combinar primero `measurement_fields("memory.search")` / `("memory.ask")` de `bench/_gate.py` en los cuerpos; ofrecer `--credit`                                                                                                                                                |
| Espacios de nombres desechables y registrados | `register_bench_namespace` + `remember_bench_namespace` antes de la primera escritura, el trabajo dentro de `erase_on_exit` y una opción `--keep-namespaces` (`bench/bench_namespaces.py`)                                                                                |
| Alcance fijo                           | Enviar `namespace_scope: "this"` en las lecturas                                                                                                                                                                                                                                |
| ¿Con juez? Usar la compuerta compartida | `add_gate_arguments(parser)` para `--repeats`, `--gate*`, `--credit`, `--judge-calibration`; `gate_from_args` para el veredicto; fijar los modelos lector y juez y avisar cuando se sobrescriban. `tests/unit/test_bench_gate_help_renders.py` muestra por qué un `%` suelto en el texto de ayuda rompe `--help` |
| Registrar el instrumento               | Embedder, LLM, proveedor y `build_commit` de `/v1/health` junto a los números (véase `run_metadata` en `quality.py`)                                                                                                                                                            |
| Informar los fallos aparte de los aciertos perdidos | Contar e imprimir los errores de solicitud; no puntuarlos como fallos de recuperación                                                                                                                                                                         |
| Comparaciones pareadas                 | Deltas por elemento mediante `paired_bootstrap` con una clave de clúster; publicar según el límite inferior                                                                                                                                                                     |

Después:

- agrega una viñeta en la sección correspondiente de [`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) y, si
  responde una pregunta frecuente, una fila en su tabla "When to run what";
- agrega un target de Makefile solo para una compuerta que alguien ejecute de forma rutinaria, con sus umbrales como variables sobrescribibles;
- ejecuta `make format` (ruff formatea `bench/`), `pytest bench -q` y el lint de textos:

```bash
.venv/bin/python -m pytest tests/unit/test_public_copy.py tests/unit/test_bench_namespaces.py -q
```

El lint de textos analiza `bench/`: no debe haber nombres de producto, personas, hosts internos ni espacios de nombres, citas de artículos ni referencias a registros de decisiones en el código, los comentarios o los fixtures.

<a id="related"></a>

## Relacionado

- [Metodología](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md): qué calculan el gate y el bootstrap.
- [Ejecución](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md): comandos y salidas.
- [Pruebas](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md): las suites de pruebas unitarias y de integración.
