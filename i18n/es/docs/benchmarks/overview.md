<!-- i18n: source=docs/benchmarks/overview.md blob=f3e3a30fcb2c5a330a968c35eb380c8c210ad12f model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md), which is authoritative.

<a id="benchmarks-overview"></a>

# Resumen de benchmarks

Las palancas de ranking, los umbrales de similitud, los umbrales de caché y los prompts de extracción cambian un comportamiento que una prueba unitaria no puede juzgar. Cada uno se decide con un harness bajo `bench/` que se ejecuta por HTTP contra un servidor real con proveedores reales. Esta sección cubre la [metodología](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), [cómo ejecutar](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) la suite y de dónde provienen sus datos, la [matriz de cobertura](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md), los [resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) que reproduce el repositorio, cómo [añadir un caso](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md) y la [evaluación de skills](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md).

[`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) enumera todos los harness. El docstring del módulo de cada script es su manual: protocolo, flags y un comando de ejecución.

<a id="what-the-suite-measures"></a>

## Qué mide la suite

Los harnesses son scripts independientes: argparse más un cliente HTTP, sin fixtures compartidos ni
test runner.

| Área                                | Pregunta                                                                                                       | Harnesses principales                                                                                                            |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Recall de recuperación              | ¿La memoria correcta quedó entre las K primeras?                                                               | `quality.py`, `retrieval_recall.py`, `k_curve.py`, `plane_ablation.py`, `hard_distractors.py`                                    |
| Precisión de las respuestas         | ¿Es correcta la respuesta construida a partir de la memoria recuperada, con un lector y un juez fijos?         | `longmemeval.py`, `locomo_qa.py`, `memoryagentbench.py`, `membench.py`, `beam.py`, `musique.py`, `msc_dmr.py`, …                 |
| Calidad del lado de escritura       | ¿La extracción escribió lo que dicen los turnos, y solo eso?                                                   | `extraction_eval.py`, `graph_eval.py`, `memory_integrity.py`, `supersession_audit.py`                                            |
| Tiempo y revisión                   | ¿Se oculta un hecho reemplazado después de que su intervalo se cierra y es visible dentro de él?               | `fama_forgetting.py`, `mutation_sequence.py`, `fact_consolidation.py`, `pi_llm.py`                                               |
| Agentes y operaciones               | ¿Eligió un agente la op correcta y dejó el almacén con la forma correcta?                                      | `op_planning.py`, `skill_eval.py`, `op_trajectories.py`, `procedure_replay.py`                                                   |
| Costo                               | ¿Cuánto cuesta una respuesta o una sesión?                                                                     | `ask_cost_rollup.py`, `session_ab.py`, `route_replay.py`, `ch_cost_by_op.py`                                                     |
| Seguridad y privacidad              | ¿El borrado llega a todos los niveles? ¿Puede un atacante que solo controla el prompt extraer o envenenar la memoria? | `erasure_compliance.py`, `memory_extraction_redteam.py`, `memory_poison.py`, `sensitivity_leakage.py`                    |
| Índice de código                    | ¿El índice de código encuentra definiciones, llamadores y respuestas conceptuales, y se abstiene en el resto?  | `code_intel_qa.py`, `code_plane_ablation.py`, `code_shunt_tokens.py`, `code_ask_cache_threshold/`                                |
| Análisis de documentos              | ¿El extractor lee el texto, los encabezados, las tablas y las páginas de un documento, sin perder nada?        | `docparse/` (sin conexión)                                                                                                       |
| Tracker                             | ¿`tracker.similar` encuentra el duplicado más antiguo contra el que se cerró una incidencia?                   | `tracker_similar_bench.py`                                                                                                       |
| Latencia y salud del índice         | ¿Dónde se va el tiempo de búsqueda? ¿Un índice vectorial devolvería lo mismo que la fuerza bruta?              | `search_latency.py`, `vector_tenant_recall.py`, `ann_final_staleness.py`, `vector_index_vs_exact.py`, `scripts/ops/benchmark.py` |
| Carga de escritura                  | ¿Qué latencia y cuántas partes de ClickHouse cuesta una tasa de escritura sostenida?                           | `write_rate.py`                                                                                                                  |
| Escala                              | ¿Qué límite alcanza primero la memoria a medida que crecen los espacios de nombres, las organizaciones y las claves? | `scale_load.py`                                                                                                            |
| Aceptación por parte del consumidor | ¿Obtiene un tipo de despliegue respuestas precisas a un costo aceptable?                                       | `acceptance/{code_agent,memory_app,plugin,harness}.py`                                                                           |

[`bench/memoryarena/`](https://github.com/ClickHouse/loom/blob/main/bench/memoryarena/README.md) (un gimnasio de agentes que puntúa las sesiones
posteriores de tareas dependientes) y [`bench/memorydata/`](https://github.com/ClickHouse/loom/blob/main/bench/memorydata/README.md)
(un banco de pruebas que ejecuta Loom junto a otros métodos) son harnesses externos con Loom
conectado. Sus README son sus guías de ejecución.

Si estás evaluando Loom en lugar de modificar su código de ranking, empieza por
[Evaluar Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md). Convierte el inventario de harnesses en un
camino de decisión corto para la integración en el producto, tu propia carga de trabajo de memoria, la inteligencia de código y la
reproducción de resultados publicados.

<a id="entry-points"></a>

## Puntos de entrada

| Punto de entrada             | Qué ejecuta                                                                | Requiere                                                          |
| ---------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `make bench-quality-gate`    | `quality.py` sobre el conjunto sintético incluido en el repositorio; termina con código 1 si el recall es inferior a 0.80 | un servidor con un embedder real y un LLM de extracción           |
| `make bench-algorithm-gate`  | `extraction_eval.py` y `graph_eval.py` sobre los fixtures de regresión     | un servidor con un LLM de extracción real                         |
| `make bench-quick`           | `longmemeval.py` sobre 30 elementos mezclados aleatoriamente con el lector y el juez fijados | el archivo de LongMemEval descargado, `LOOM_API_KEY`, `OPENAI_API_KEY` |
| `make bench-matrix`          | `matrix.py`: modo de búsqueda × rerank × ancho del pool en LongMemEval     | igual que `bench-quick`                                           |
| `make bench-code-intel-dry`  | `code_intel_qa.py --dry`: el evaluador sobre una recuperación sintética perfecta | nada (sin conexión)                                               |
| `make bench-code-intel`      | `code_intel_qa.py` con los umbrales de despliegue                          | un servidor con un repositorio de ClickHouse indexado, embedder real |
| `make bench-code-legs`       | `code_intel_qa.py --compare-extra` para tres ablaciones de ramas, cada una emparejada | igual que `bench-code-intel`                                      |
| `make bench-clickgap-replay` | `clickgap_code_replay.py`: se reproduce la secuencia de llamadas de un bot de revisión de PR | un JSONL de reproducción de los PR de ese consumidor              |
| `make bench-clickgap-golden` | `clickgap_golden_replay.py` sobre los fixtures golden de ese consumidor    | `GOLDEN_DIR`, fixtures privados que no están en este repositorio  |
| `make bench`                 | `scripts/ops/benchmark.py`: carga concurrente sobre una op, p50/p95/p99 y RPS | un servidor                                                       |
| `make test-unit`             | también ejecuta `pytest bench -q`: los núcleos de puntuación y los lectores de conjuntos de datos | nada (sin conexión)                                               |

Cualquier otro arnés se ejecuta como `python bench/<name>.py` ([ejecución](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="how-a-bench-differs-from-a-production-caller"></a>

## Cómo se diferencia un bench de un llamador en producción

Un bench llama a las mismas operaciones por HTTP, con tres diferencias:

1. **Lecturas de evaluación.** Una lectura que acredita refuerza las filas contra las que se ordena la siguiente pasada, de modo que un bench alteraría sus propios resultados. Los harnesses envían `auto_touch: false` en `memory.search` y `reinforce: false` en `memory.ask`, a menos que la ejecución use `--credit`.
2. **Espacios de nombres de bench desechables.** Cada espacio de nombres se registra con `traffic_class` y `kind` con el valor `bench`: el bucket de límite de tasa de bench (la mitad del límite base por defecto), la prioridad de bench en ClickHouse, una columna separada en el registro de costos y el borrado cuando termina la ejecución ([espacios de nombres de bench](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#bench-namespaces)).
3. **Instrumento fijo.** Los modelos de lectura y de juicio están fijados por harness y no siguen a `LOOM_LLM_MODEL`. Lo que varía es el modelo de extracción, el embedder y la configuración del servidor. `--answerer loom-ask` (LongMemEval) o `--reader ask` (LoCoMo) evalúa `memory.ask` en sí.

Los valores por defecto del servidor son los que eligieron estos benches. Cambiar el valor por defecto de `LOOM_LLM_MODEL` o `LOOM_EMBEDDING_MODEL` empieza con `make bench-quick`, y un nuevo embedder necesita una [nueva línea base](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#re-baselining-after-an-embedder-change).

<a id="coverage-and-what-it-leaves-out"></a>

## Cobertura y qué deja fuera

- **Sin señal de calidad en CI.** CI ejecuta proveedores simulados, así que prueba los evaluadores, no la recuperación
  ([por qué](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#fake-providers-make-scores-meaningless)). Una regresión
  de ranking puede fusionarse sin que nadie la vea, a menos que alguien ejecute los gates en un despliegue real.
- **Hay pocas mediciones confirmadas en el repositorio.** [Resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) enumera cada medición
  confirmada con su artefacto en `bench/results/`, el instrumento con el que se ejecutó y un
  comando de reproducción. Un número para tus datos y tus proveedores debe producirse en tu
  propio despliegue.
- **La mayoría de los harnesses necesitan datos que el repositorio no contiene.** Los corpus de terceros son
  descargas ([conjuntos de datos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)); las repeticiones, los conjuntos de confianza, el gold del tracker y
  la latencia a partir de trazas leen datos privados de un solo despliegue.
- **Los fixtures de código son específicos del repositorio.** Los carriles de ClickHouse necesitan un índice
  de ClickHouse/ClickHouse, y el carril de Langfuse un índice de ese repositorio. Sus números dicen
  poco sobre otra base de código.
- **Sin tabla comparativa de sistemas con nombre.** Los harnesses con conjuntos de datos públicos informan los números de Loom y
  las columnas necesarias para situarlos junto a las tablas publicadas. El repositorio compara Loom
  solo con sus propias versiones y brazos.
- **Sin registro de hardware ni modelo de capacidad.** Ningún harness registra el host.
  `scripts/ops/benchmark.py` mide una op con una concurrencia; `search_latency.py`
  desglosa un `memory.search` en etapas según el tamaño del espacio de nombres. Ninguno modela una mezcla real de agentes
  ni la contención multiinquilino.
- **La evaluación de skills califica formas, no recall** ([evaluación de skills](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md#what-this-does-not-measure)).

<a id="what-a-pass-does-not-prove"></a>

### Qué no demuestra que se pase

- **Pasar un gate es un mínimo.** La compilación supera un umbral registrado en un conjunto pequeño con
  un solo embedder. Eso no significa que el recall sea bueno con tus datos.
- **El recall no es calidad de respuesta.** Una memoria puede quedar primera y aun así malinterpretarse o estar
  obsoleta. Revisa el eje evaluado por juez y la etapa del fallo.
- **La exactitud de la respuesta no es éxito del agente.** La mayoría de los harnesses puntúan una pregunta a la vez.
  Solo `code_plane_ablation.py` y los gyms de agentes miden lo que un agente hizo con la
  memoria.
- **Los benchmarks de seguridad miden ataques concretos.** `memory_poison.py` y
  `memory_extraction_redteam.py` informan sobre sus propias formas de ataque. `erasure_compliance.py`
  escanea cada columna de tipo String de cada tabla `loom.*` en busca de su canary; los backends
  externos solo son tan completos como su propio borrado.

<a id="related"></a>

## Relacionado

- [Recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md): qué ejercitan los benchmarks de recuperación.
- [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): qué ejercitan los benchmarks de código.
