<!-- i18n: source=docs/benchmarks/methodology.md blob=0e0708fc624ae94fe8f285a72bac00e00924c567 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), which is authoritative.

<a id="benchmark-methodology"></a>

# Metodología de benchmarks

Esta página describe qué calculan los harnesses, cómo se juzga un delta frente al ruido, cuándo dos números son comparables y dónde están sesgados los números. Las reglas viven en el código: `bench/_gate.py` (gate compartido), `bench/bootstrap.py`, `bench/judge_calibration.py`, `bench/bench_namespaces.py` y `bench/failure_taxonomy.py` ([puntos de entrada del código fuente](#source-entry-points)).

<a id="recall-and-answer-accuracy-are-separate-axes"></a>

## Recall y answer accuracy son ejes distintos

**Recall@K** mide si una memoria relevante quedó entre las K primeras. **Answer accuracy** mide si la respuesta sintetizada es correcta. Una puede saturarse mientras la otra se mantiene baja. `longmemeval.py` imprime recall@K y la precisión evaluada por el juez como métricas separadas; `coding_agent_qa.py` imprime R@5 / R@10 / MRR aparte de la precisión de QA evaluada por el juez. Una cifra destacada de un embedder sin lector ni juez es una cifra de recall. No la compares con una cifra de precisión de respuestas.

<a id="task-categories"></a>

## Categorías de tareas

Cada harness conserva las etiquetas de categoría de su fuente, de modo que un resultado se lee por categoría.

| Harness               | Categorías                                                                                                                                                      |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `locomo_qa.py`        | 1 multi-hop, 2 temporal, 3 open-domain, 4 single-hop, 5 adversarial. `overall_comparable` cubre 1–4 (la columna publicada); `overall_all` puntúa 5 como abstención. |
| `longmemeval.py`      | El `question_type` del conjunto de datos (`--question-type`); `--stale-check` etiqueta las respuestas incorrectas de knowledge-update que devolvieron el valor reemplazado. |
| `memoryagentbench.py` | Recuperación precisa, aprendizaje en tiempo de prueba, comprensión de largo alcance, resolución de conflictos.                                                  |
| `beam.py`             | Diez capacidades de memoria, calificadas con el juez de rúbrica del conjunto de datos; el ordenamiento de eventos añade Kendall τ_b × F1.                       |
| `membench.py`         | Escenarios de participación y observación en niveles factual y reflexivo.                                                                                       |
| `code_intel_qa.py`    | El `type` de cada fila: `symbol_def`, `callers`, `conceptual`, `hub`, `history`, `test_set`, `abstain_probe`; además de `lane` y `facets`.                      |
| `coding_agent_qa.py`  | Aterrizaje de corrección de errores, corrección de infraestructura, ubicación de refactorización, prueba inestable, fuga de rendimiento, migración de esquema, causal multisesión, temporal, convención de equipo. |
| `op_planning.py`      | La `class` de cada tarea: `instruction_type` × `structure`.                                                                                                     |

<a id="ground-truth"></a>

## Verdad de referencia

| Tipo                                  | Dónde                                                                                                | Cómo se construye                                                                                                                                              | Debilidad                                                                                  |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Claves gold por subcadena             | `bench/datasets/synthetic_memory_qa.json`, `extraction_regressions.json`, `graph_regressions.json`   | Escritas a mano; un acierto es cualquier cadena gold presente en el `memory_key` devuelto o en el extracto (sin distinguir mayúsculas, `_` como espacio)       | Indulgente a propósito; un extracto largo puede coincidir por casualidad                   |
| Respuestas gold publicadas            | Conjuntos de datos de terceros ([conjuntos de datos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)) | La respuesta y las etiquetas de evidencia del conjunto de datos                                                                                                | Hereda los errores de etiquetado y la saturación del conjunto de datos                     |
| Pares consulta → clave etiquetados a mano | `retrieval_recall.py`, `plane_ablation.py`, `hard_distractors.py` `--dataset`                    | Un consumidor etiqueta consultas contra un corpus existente                                                                                                    | Conjuntos pequeños; las etiquetas no se guardan en el repositorio                          |
| Etiquetado por uso                    | `relied_on_set.py` → `bench/datasets/relied_on/`                                                     | Una búsqueda cuyas claves devueltas un agente usó después (eventos `used` / `used_cited`) dentro de una ventana corta                                          | Se une por tiempo, no por id; favorece lo que el ranking actual ya mostró                  |
| Extraído de PRs fusionados            | `bench/fixtures/code_intel_mined_*.jsonl`, `code_intel_history.jsonl`, `code_plane_ablation.py mine` | Consulta = título del issue o del PR; gold = archivos de `src/` modificados. Se descartan las filas cuyo texto nombra la raíz de una ruta gold, y se informa la proporción | El índice contiene todas las correcciones: los absolutos son un límite superior, lee los deltas pareados |
| Redactado y verificado por un modelo  | `bench/fixtures/code_intel_oss.jsonl`                                                                | Un corpus de terceros cuyas consultas y rutas escribió un modelo y comprobó un modelo juez                                                                     | Un fallo puede deberse a la etiqueta; las rutas `secondary` nunca puntúan                  |

El gold estructural de código (`symbol_def`, `callers`) es exacto, pero se escribió a partir del conocimiento del
repositorio upstream. Después de cada backfill nuevo, ejecuta una vez sin gates, lee las
discrepancias y corrige el gold: un gold erróneo es un error del corpus.

La calibración del juez construye sus propias sondas (`build_probes`): el gold de un elemento formulado como lo respondería un lector es una sonda correcta conocida; el gold de otro elemento de la misma categoría es una sonda incorrecta conocida. Los elementos adversariales se excluyen.

<a id="judges"></a>

## Jueces

El lector y el juez forman parte del instrumento, no del sistema bajo prueba.

- **Fijados por harness.** `longmemeval.py` y `coding_agent_qa.py` leen con
  `gpt-4o-mini` y juzgan con `gpt-4o-2024-08-06` (la versión fija del juez de LongMemEval).
  `locomo_qa.py` lee con `gpt-4.1-mini` y juzga con `gpt-4.1`.
  `BENCH_READER_MODEL` / `BENCH_JUDGE_MODEL` los sobrescriben e imprimen una advertencia
  de comparabilidad; `OPENAI_BASE_URL` apunta a un gateway compatible.
- **Evaluación de la respuesta de Loom.** `longmemeval.py --answerer loom-ask` y
  `locomo_qa.py --reader ask` evalúan `memory.ask`; `--ask-arms` compara configuraciones
  de `memory.ask` sobre una misma ingesta.
- **Las rúbricas son archivos.** `bench/rubrics/entailment.md` es el prompt de NLI que
  carga `dialogue_consistency.py`. `beam.py` usa el juez de rúbricas del dataset.
- **Concordancia.** `locomo_qa.py --second-judge-model` vuelve a evaluar una muestra e
  informa de la concordancia y del kappa de Cohen.

Las puntuaciones léxicas existen solo para ponerlas junto a las tablas léxicas
publicadas. `longmemeval.py --lexical-scores` añade F1 por token, BLEU-1 y ROUGE-L sobre
una respuesta **simplificada** (`--simplify-model`, por defecto el juez; si no, una
heurística etiquetada como tal). Sin simplificación evalúan la verbosidad: una fecha
incorrecta puede superar en puntuación a una frase correcta
([valores de referencia](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#lexical-scoring-reference-values)). Nunca reemplazan
al juez.

<a id="metrics"></a>

## Métricas

| Métrica                                | Definición                                                                                                                                                  | Calculada en                                     |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| recall@K (exhaustividad@K)             | Proporción de preguntas con un acierto de referencia entre los K primeros                                                                                   | `quality.py` `summarise`, `retrieval_recall.py`  |
| MRR                                    | Media de 1 / posición del primer acierto de referencia (0 si no hay ninguno)                                                                                | igual                                            |
| rank@1                                 | Proporción de preguntas cuyo resultado de referencia quedó en primer lugar                                                                                  | igual                                            |
| precision@K (precisión@K)              | Aciertos de referencia entre los resultados devueltos, promediados                                                                                          | `quality.py`                                     |
| `best_cos_rank`                        | Posición del resultado con mayor coseno; una diferencia respecto a la posición 1 significa que la fusión colocó primero una coincidencia más débil          | `retrieval_recall.py`                            |
| exact@1 (exactitud@1), F1 de llamadores | Exactitud estructural del código                                                                                                                           | `code_intel_qa.py`                               |
| precisión / exhaustividad de abstención | Exhaustividad = sondeos en los que se abstuvo / sondeos; precisión = sondeos en los que se abstuvo / todas las abstenciones                                | `code_intel_qa.py`                               |
| `placebo@k`                            | Por pregunta, la misma cantidad de rutas extraídas (con semilla) del conjunto que el sistema devolvió en todas las preguntas conceptuales. Un recall inferior indica cobertura del conjunto, no ranking | `code_intel_qa.py`                               |
| tokens por respuesta                   | Media / p90 de `tokens_returned` por pregunta respondida                                                                                                    | `code_intel_qa.py`, pruebas de aceptación        |
| exactitud evaluada                     | Etiqueta mayoritaria CORRECT sobre las repeticiones                                                                                                         | `_gate.py`                                       |
| tasa de cambio, discordancia           | Dentro de un mismo brazo, proporción de elementos repetidos cuyas etiquetas no coincidieron; entre brazos, proporción de pares cuyas etiquetas mayoritarias difieren | `_gate.py` `flip_rate`, `GateResult.discordance` |
| etapa de fallo                         | `extraction`, `graph_resolution`, `retrieval_ranking`, `compression_loss`, `reader`, `not_verbatim`                                                         | `failure_taxonomy.py`                            |

La etapa de fallo se calcula por cada elemento fallado, en el orden del pipeline: si no se
recuperó, es `retrieval_ranking`; si la referencia está en un fragmento recuperado pero la
respuesta es incorrecta, es `reader`; si la referencia solo está en la evidencia sin procesar
detrás de una memoria recuperada, es `compression_loss`; si la referencia no aparece textualmente
en ningún sitio, es `not_verbatim`. `lme_diagnose.py` vuelca los fallos de LongMemEval con
estas etiquetas. Una regresión de `reader` con la recuperación sin cambios es atribuible al reader.

Los fallos de petición se excluyen de las proporciones y nunca se cuentan como fallos de
recuperación (`code_intel_qa.py` imprime `errors(excluded)`). `quality.py` se aborta ante
cualquier fallo HTTP.

<a id="baselines"></a>

## Líneas base

Loom se compara con otras técnicas y con sus propios brazos, bajo el mismo instrumento y sobre la misma ingesta.

| Línea base                                                        | Dónde                                                                              |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Recuperación solo léxica, léxica más expansión de consulta, solo vectorial | `make bench-code-legs`: cada una es un brazo B pareado frente al valor predeterminado híbrido |
| Memoria solo de resúmenes                                         | brazo `digest` de `msc_dmr.py` junto a `ask`, `search`, `search_recall`, `search_reader` |
| Almacenar textual frente a extraer hechos                         | modos `verbatim` / `extract` de `memoryarena/` y `memorydata/`                     |
| Contexto largo, BM25, recuperación densa simple                   | brazos de `memoryarena/memory_server.py`                                           |
| Agente de código con el plugin desactivado                        | brazo A de `code_plane_ablation.py`                                                |
| Lecturas masivas de archivos                                      | brazo "cold" de `code_shunt_tokens.py`                                             |
| Vecinos más cercanos por fuerza bruta                             | `vector_tenant_recall.py`                                                          |
| Un modelo de síntesis potente o uno económico                     | brazos strong, cheap y cascade de `route_replay.py` sobre recuperaciones congeladas |
| El ranking nativo del sistema de origen                           | `clickgap_migration_validate.py`: el orden de `recall_count` de la exportación     |
| Ablación por plano                                                | máscaras `execution_planes` de `plane_ablation.py`                                 |

<a id="statistics"></a>

## Estadísticas

<a id="paired-clustered-bootstrap"></a>

### Bootstrap pareado y por clústeres

Un cambio de ranking se publica según el **límite inferior** del delta pareado, no según la media.
`paired_bootstrap` toma deltas por ítem (B menos A), remuestrea clústeres completos con
reemplazo (10,000 remuestreos, `seed=7`) y lee los percentiles 2.5 y 97.5. Una
mejora cuenta solo cuando el límite inferior está por encima de cero. Los ítems sobre un mismo mecanismo se mueven
juntos, por lo que los harnesses pasan una clave de clúster: una conversación, un tipo de pregunta o, para código,
los dos primeros segmentos de la ruta (`cluster_of_path`). Con muy pocos clústeres el intervalo resulta demasiado
estrecho; lee `clusters` junto al límite. `code_plane_ablation.py` añade una prueba de rangos con signo
de Wilcoxon pareada sobre las medias por tarea entre semillas.

<a id="repeats-and-the-majority-label"></a>

### Repeticiones y la etiqueta mayoritaria

El lector vuelve a variar incluso con temperatura 0, y la recuperación dentro de la síntesis vuelve a descomponer
la consulta. `--repeats N` pregunta y juzga cada ítem N veces sobre la misma recuperación y conserva la etiqueta
**mayoritaria**. Un empate es WRONG; un ítem es ERROR solo cuando todas las ejecuciones fallaron.
Las ejecuciones de la compuerta usan tres repeticiones (`GATE_REPEATS`). `identical_share` sobre los `top_keys`
de las repeticiones muestra si un cambio de resultado se debió al lector o a la recuperación. Las repeticiones reducen el ruido de las etiquetas
([simulación](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#majority-voting-on-simulated-reader-noise)); solo más
ítems reducen el ruido de muestreo.

<a id="the-verdict"></a>

### El veredicto

`paired_gate` ejecuta el bootstrap sobre las etiquetas mayoritarias de los brazos:

| Veredicto    | Condición                                                                                                                                                                              |
| ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pass`       | el límite inferior del 95% supera `--gate-min-delta` (0 por defecto; un valor negativo es una compuerta de no inferioridad)                                                            |
| `fail`       | el límite superior del 95% está por debajo del umbral                                                                                                                                  |
| `unresolved` | el intervalo abarca el umbral. Dentro de la banda nula (`--gate-band`, por defecto `DEFAULT_BAND_PP` = 4.0 pp) eso es el piso de ruido; fuera de ella la ejecución no tiene potencia suficiente |

Los ítems inestables dentro de las repeticiones de cualquiera de los brazos también se informan como un par solo de ítems estables.

**Mide tu propia banda.** El valor por defecto provino de un harness y un lector. Ejecuta la misma
configuración como brazos A y B con `--repeats 3 --gate`, y pasa el lado más ancho del resultado
(`band_from_null`) como `--gate-band` en la comparación real.

<a id="how-wide-the-interval-is"></a>

### Qué tan ancho es el intervalo

El ancho depende de en cuántos ítems pareados discrepan los brazos, no solo de n. Estas filas son
`paired_bootstrap` sobre deltas fijos (+1 = B acierta y A falla, −1 al revés, 0 = coinciden):

| n                    | Victorias / empates / derrotas   | Delta medio | Intervalo del 95% | Lectura                                                  |
| -------------------- | -------------------------------- | ----------- | ----------------- | -------------------------------------------------------- |
| 60                   | 1 / 59 / 0                       | +1.7 pp     | +0.0 .. +5.0 pp   | Una pregunta; el límite no supera cero                   |
| 30                   | 2 / 26 / 2                       | 0           | −13.3 .. +13.3 pp | Un par de igual configuración: una variación de 13 puntos cabe dentro |
| 60                   | 5 / 50 / 5                       | 0           | −10.0 .. +10.0 pp |                                                          |
| 164                  | 13 / 138 / 13                    | 0           | −6.1 .. +6.1 pp   |                                                          |
| 500                  | 40 / 420 / 40                    | 0           | −3.6 .. +3.6 pp   |                                                          |
| 60                   | 6 / 52 / 2                       | +6.7 pp     | −1.7 .. +16.7 pp  | Una "mejora de 7 puntos" sin resolver                    |
| 60                   | 8 / 50 / 2                       | +10.0 pp    | +0.0 .. +20.0 pp  | El límite solo llega a cero: no es un pass               |
| 500                  | 40 / 440 / 20                    | +4.0 pp     | +1.0 .. +7.0 pp   | Una mejora menor que pasa porque n es mayor              |
| 60, 6 clústeres de 10 | 8 / 50 / 2, victorias en dos clústeres | +10.0 pp    | +0.0 .. +30.0 pp  | Los ítems correlacionados ensanchan el intervalo         |

```bash
.venv/bin/python - <<'EOF'
from bench.bootstrap import paired_bootstrap
def row(w, t, l, clusters=None):
    d = [1.0] * w + [-1.0] * l + [0.0] * t
    print(paired_bootstrap(d, clusters).line(unit="pp", scale=100))
row(1, 59, 0); row(2, 26, 2); row(5, 50, 5); row(13, 138, 13); row(40, 420, 40)
row(6, 52, 2); row(8, 50, 2); row(40, 440, 20)
row(8, 50, 2, [f"c{i // 10}" for i in range(60)])
EOF
```

<a id="set-sizes"></a>

### Tamaños de los conjuntos

Los conjuntos pequeños no pueden ordenar brazos cercanos.

| Conjunto                               | Tamaño                       | Un ítem equivale a                |
| -------------------------------------- | ---------------------------- | --------------------------------- |
| `synthetic_memory_qa.json`             | 15 preguntas                 | 6.7 puntos: solo detecta un colapso |
| `code_intel_qa.jsonl` porción conceptual | 36 de 60 preguntas         | 2.8 puntos                        |
| Fixtures de la compuerta de algoritmos | 19 de extracción, 2 casos de grafo | una prueba de humo          |
| Pares de caché de `code.ask`           | 42 paráfrasis, 43 distintos  | más de 2 puntos                   |

<a id="judge-calibration"></a>

### Calibración del juez

`bench/judge_calibration.py` mide las tasas de verdaderos y falsos positivos del juez sobre las
sondas. La precisión observada es `TPR·p + FPR·(1−p)`, por lo que un delta pareado se reduce por el
`J = TPR − FPR` de Youden. Con `--judge-calibration <json>` la compuerta informa `Δ/J` y ensancha el
intervalo con la varianza propia del conjunto de calibración (método delta). Un juez con `J ≤ 0`
no aporta información; las entradas pasan sin cambios.

<a id="when-two-numbers-compare"></a>

## Cuándo se pueden comparar dos números

Todos estos elementos deben coincidir. Si uno difiere, etiqueta la comparación como no comparable.

| Debe coincidir                       | Por qué                                                                                                         |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| Eje                                  | Recall@K y la precisión evaluada por un juez son métricas distintas                                             |
| Lector y juez                        | Un lector más potente mueve las categorías que requieren mucho razonamiento; un juez distinto mueve las etiquetas |
| División y subconjunto del dataset   | LongMemEval oracle vs S, categorías 1–4 vs 1–5 de LoCoMo, un carril vs el archivo completo                      |
| Modo de búsqueda                     | El valor predeterminado sin fijar es `LOOM_MEMORY_SEARCH_DEFAULT_MODE`; otro modo es una ablación               |
| Embedder y build del servidor        | Cambiar de embedder desplaza toda la escala de similitud; el `.meta.json` de `quality.py` registra ambos        |
| Revisión del índice (código)         | `code_intel_qa.py` indica el `as_of.commit` que sirvió las respuestas y el número de resultados obsoletos       |
| Acreditación                         | Una ejecución con `--credit` refuerza lo que lee                                                                |
| Denominador de la precisión          | Excluir las abstenciones favorece a un brazo que se abstiene más                                                |

**Abstenciones.** La puerta evaluada por el juez cuenta una abstención como incorrecta. `route_replay.py` informa
ambas: `accuracy` = correct / (correct + wrong), y `accuracy_abstain_wrong` = correct /
(correct + wrong + abstained), el denominador de la puerta. Las consultas con error quedan fuera de ambas. Cita
la que lee el veredicto y nómbrala.

**Cotas superiores y placebos.** Los carriles de código extraídos se ejecutan contra un índice que contiene todas las
correcciones (`fix_in_index` en cada tarea de ablación): cita deltas pareados, no valores absolutos. Un recall
que no supera a `placebo@5` / `placebo@10` es cobertura del conjunto de candidatos. Un conjunto diminuto hace que el
placebo sea trivialmente fuerte. Un conjunto en el que la mayoría de los brazos puntúan cerca del techo no puede separarlos;
lee los resultados por categoría y el par solo de estables.

<a id="fake-providers-make-scores-meaningless"></a>

## Los proveedores falsos hacen que las puntuaciones carezcan de sentido

El `.env` de un checkout ejecuta `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, CI ejecuta `fake-ngram-1024`, y ambos ejecutan `LOOM_LLM_PROVIDER=fake` (el stack publicado genera embeddings con `local` y mantiene el LLM falso). El embedder falso (`server/embedding/fake.py`) aplica un hash a los trigramas de caracteres para distribuirlos en buckets, de modo que la similitud refleja la ortografía y no el significado. El LLM falso (`server/llm/fake.py`) devuelve JSON con la forma esperada a partir de una frase ancla, así que los pipelines se ejecutan sin extraer nada real. Con ellos:

- el recall mide la coincidencia de cadenas entre la pregunta y el texto almacenado;
- los benchmarks de extracción miden la salida del falso, no el prompt;
- los umbrales de similitud usan sus valores escalares por defecto, porque la calibración adaptativa omite los embedders falsos.

Un gate en verde con embeddings falsos es peor que no tener gate: informa de una seguridad que nunca se midió. Por eso `make bench-quality-gate` y `make bench-code-intel` no están en CI (aplicarlos exige un secreto de un proveedor de pago), y CI ejecuta solo el código de puntuación puro (`pytest bench`) y las pruebas unitarias de las funciones de gate (`quality.gate`: el límite pasa, 0/0 falla). Define `LOOM_REQUIRE_REAL_PROVIDERS=true` en un servidor de benchmarks para que un proveedor falso o sin clave haga que `/v1/health` informe `degraded` antes de gastar una ejecución.

<a id="reproducibility-controls"></a>

## Controles de reproducibilidad

| Control                          | Mecanismo                                                                                                                                                                          |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Remuestreo determinista          | `paired_bootstrap` y `paired_gate` son deterministas bajo `seed`; el gate registra `resamples` y `seed`                                                                            |
| Muestras deterministas           | Los flags de muestreo reciben una semilla (`musique.py --seed`, `judge_calibration.py`, muestras de carriles minados)                                                               |
| Instrumento del servidor registrado | `quality.py` escribe un `.meta.json` con `embedding_model`, `embedding_dim`, `llm_model`, `llm_provider`, `build_commit` obtenidos de `/v1/health`; `code_intel_qa.py` nombra `as_of.commit` |
| Alcance fijado                   | Los arneses de búsqueda envían `namespace_scope: "this"`, de modo que las filas de otros espacios de nombres legibles nunca entran en la ventana. Las sondas ad hoc también deben hacerlo |
| Lecturas de evaluación           | `measurement_fields()` envía `auto_touch: false` / `reinforce: false`; `--credit` lo activa; en un espacio de nombres leído antes, pasa `reinforcement_weight: 0` o usa filas nuevas |
| Espacios de nombres nuevos       | Un espacio de nombres desechable por elemento o conversación, borrado al final                                                                                                     |
| Calentamiento                    | `search_latency.py --warm 1` descarta la primera llamada de cada consulta, de modo que los fallos de la caché de embeddings no cuenten como recuperación                            |
| Duración del token               | Genera un token que dure más que la ejecución ([ejecución](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#2-mint-a-token-that-outlives-the-run))          |

<a id="bench-namespaces"></a>

### Espacios de nombres de benchmark

Los arneses que siembran datos crean espacios de nombres desechables (`lme-…`, `locomo-…`, `mab-…`,
`qabench-…`; los arneses nuevos usan `bench-`). `bench/bench_namespaces.py`:

- registra cada uno antes de su primera escritura con `traffic_class = "bench"`, `kind = "bench"`
  y `capture_generation_io = True` (valor por defecto);
- ejecuta el trabajo dentro de `erase_on_exit`, que borra todos los nombres generados con un único
  borrado con purga cuando la ejecución termina, falla o se interrumpe. El cuerpo establece `external: false`, de modo que
  no se toca ningún backend externo;
- con `--keep-namespaces`, los conserva para una nueva puntuación posterior (`--namespaces-json`) y
  los registra como `kind = "memory"` para que el recolector de inactividad los deje.

El borrado requiere un token de administrador de la organización. Si se rechaza, el arnés registra el comando
`loom namespaces-gc`. El worker `namespace_gc` purga los espacios de nombres `kind = bench` inactivos después de
`LOOM_NAMESPACE_GC_IDLE_HOURS`, y los nombres generados empiezan con un prefijo de
`LOOM_NAMESPACE_BENCH_PREFIXES`, de modo que también se encuentran las ejecuciones abortadas.

<a id="http-policy"></a>

### Política HTTP

Los arneses envían peticiones mediante `bench/_http.py` (`post` / `apost` devuelven la respuesta,
`post_json` / `apost_json` el cuerpo):

| Respuesta                                        | Política                                                                                                                 |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| 429                                              | espera el tiempo de `Retry-After` (1 s si no está, limitado a 0.2–60 s) y reintenta; una espera no consume el presupuesto de 5xx |
| 5xx                                              | se reintenta con un backoff de `0.5 × 2^n` s hasta que se hayan enviado `attempts` peticiones                           |
| cualquier otro estado ≥ 400, o el último 429 / 5xx | lanza `httpx.HTTPStatusError` con el inicio del cuerpo                                                                   |
| error de transporte                              | se propaga; un arnés que resiste un reinicio mantiene su propio bucle                                                    |

<a id="sources-of-bias"></a>

## Fuentes de sesgo

| Fuente                           | Efecto                                                                                          | Mitigación en el código                                                            |
| -------------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Conjuntos escritos por autores   | Quienes ajustan el sistema escribieron el conjunto sintético y eligieron su umbral sobre él     | Mantenerlo como control de humo; decidir los rankings con conjuntos públicos o etiquetados por uso |
| Gold de subcadenas permisivo     | Un fragmento extenso puede contener una palabra gold por casualidad                             | MRR, rank@1 y precision@K junto con recall                                         |
| Carriles de código de cota superior | El índice ya contiene todas las correcciones                                                 | Advertencias en el encabezado, `fix_in_index`; citar solo deltas emparejados       |
| Gold escrito por modelos         | Las etiquetas de búsqueda de código de terceros fueron escritas y revisadas por modelos         | Las rutas `secondary` nunca se acreditan; solo lectura emparejada                  |
| Etiquetas por uso                | Un conjunto en el que se confía contiene solo lo que el ranking actual dejó ver                 | Barrido emparejado sobre la cota bootstrap; una fuente entre varias                |
| Permisividad del juez            | Un juez LLM da por buenas, con cierta frecuencia, respuestas relacionadas con el tema pero erróneas | `judge_calibration.py` tasa de verdaderos positivos y falsos positivos, `--second-judge-model` |
| Deriva del juez y del lector     | Una revisión del proveedor detrás de un nombre fijado cambia las etiquetas                      | Snapshots fijados cuando el dataset indica uno; ejecutar ambos brazos juntos       |
| Autorrefuerzo                    | Acreditar lecturas refuerza lo que el siguiente pase clasifica                                  | Lecturas de evaluación por defecto                                                 |
| Errores excluidos                | Si los fallos se concentran en los elementos difíciles, la proporción sube                      | Se imprimen los recuentos de errores; `quality.py` aborta ante cualquier fallo     |
| Fuga en benchmarks de agentes    | Un agente encuentra la corrección a través de la red o del historial de git                     | El registro de exclusión de la ablación descarta esas celdas, con cierre por fallo |
| Saturación                       | Las categorías que casi todos los sistemas resuelven no permiten ordenar los brazos             | Resultados por categoría; el par de solo estables                                  |
| Paridad del arnés                | Los bancos de pruebas externos envuelven cada consulta en su propia plantilla para todos los métodos | Los adaptadores mantienen esa paridad                                         |

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `bench/_gate.py`: `paired_gate`, `verdict_for`, `majority_label`, `flip_rate`,
  `JudgeCalibration`, `measurement_fields`, `add_gate_arguments`.
- `bench/bootstrap.py`: `paired_bootstrap`, `cluster_of_path`.
- `bench/judge_calibration.py`: `build_probes`.
- `bench/bench_namespaces.py`: `register_body`, `erase_on_exit`, `erase_on_exit_sync`.
- `bench/failure_taxonomy.py`: `classify_answer_failure`.
- `bench/answer_scoring.py`: `token_f1`, `bleu1`, `rouge_l`, `cohen_kappa`.
- Pruebas: `tests/unit/test_bench_gate.py`, `test_bench_bootstrap.py`,
  `test_bench_judge_calibration.py`, `test_bench_namespaces.py`,
  `test_bench_failure_taxonomy.py`, `test_answer_scoring.py`,
  `test_bench_gate_help_renders.py`.

<a id="related"></a>

## Relacionado

- [Ejecución](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) y [resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md).
- [Recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md): qué hace cambiar un ranking.
