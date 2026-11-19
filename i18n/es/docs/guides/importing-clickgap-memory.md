<!-- i18n: source=docs/guides/importing-clickgap-memory.md blob=5fff9080da2e5f36035250de4a491ae273d3484c model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/importing-clickgap-memory.md), which is authoritative.

<a id="import-clickgap-memory"></a>

# Importar memoria de ClickGap

`loom import-clickgap` carga un único formato de exportación SQLite, un almacén `lessons` de ClickGap, en un espacio de nombres de Loom; omite esta página a menos que tengas una exportación de este tipo. Una lección es una memoria de revisión breve y deduplicada, con entidades tipadas, categorías, un tipo (kind), un TTL por fila y un `recall_count`.

Dos comandos hacen el trabajo:

- `loom import-clickgap`: el importador (`server/cli/import_clickgap.py`), que se ejecuta donde la CLI `loom` alcanza ClickHouse (el contenedor del servidor o un clon con el `.env` del servidor).
- `python -m bench.clickgap_migration_validate`: el validador (`bench/clickgap_migration_validate.py`).

<a id="what-the-importer-preserves"></a>

## Qué conserva el importador

| ClickGap                    | Loom                                                                    | Nota                                                                                                                                                                                                                                                                            |
| --------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lessons.text`              | `loom.memory.value` + `content_excerpt` con embedding                   | se genera el embedding de la aserción; el razonamiento no                                                                                                                                                                                                                       |
| `lessons.reasoning`         | `value_json.reasoning` (con `.text` = aserción)                         | `get` devuelve `value_json`, así que la aserción viaja junto con el razonamiento, sin concatenarse nunca con él                                                                                                                                                                 |
| `lessons.kind`              | `memory_type` (casi siempre 1:1) **+** etiqueta `kind:<kind>`           | `summary → episodic` (no se puede buscar): los resúmenes siguen accesibles mediante `memory.get` / `memory.list` y la etiqueta `kind:summary`, pero no puntúan en `memory.search`. La etiqueta `kind:<kind>` conserva la taxonomía original. Consulta `_clickgap_memory_type` / `_CLICKGAP_TYPE_REMAP`. |
| `lessons.source`            | `source_trace='clickgap://<class>/<source>'` + etiqueta `source:<class>` | clases: pipeline / session / human / reviewer / eval                                                                                                                                                                                                                            |
| `lessons.created_at`        | `created_at`, `valid_at`, `temporal_anchor`                             | la hora de registro original, no la hora de importación                                                                                                                                                                                                                         |
| `lessons.expires_at`        | `expires_at` **literal, por fila**                                      | nunca se recalcula a partir de un TTL global                                                                                                                                                                                                                                    |
| `lessons.recall_count`      | `recall_count` **y** `memory_embeddings.reinforcement_count`            | consulta la [señal de recuperación](#recall-signal)                                                                                                                                                                                                                             |
| `lessons.last_recalled_at`  | `last_recalled_at` **y** `last_reinforced_at`                           | el eje de recencia de recuperación                                                                                                                                                                                                                                              |
| `lessons.evidence_count`    | `evidence_count`                                                        |                                                                                                                                                                                                                                                                                 |
| `lesson_entities`           | `entities` (simple) + etiquetas `entity:<kind>:<text>` + `loom.entity_vectors` | el array `entities` simple pierde el tipo; la etiqueta lo conserva                                                                                                                                                                                                              |
| `lesson_categories` + items | `loom.categories` + `loom.memory_categories`                            | nombres normalizados a minúsculas antes de calcular el `category_id` determinista                                                                                                                                                                                               |
| lecciones `false_positive`  | etiqueta `polarity:negative`                                            | memorias de supresión («NO volver a reportar»)                                                                                                                                                                                                                                  |
| fuentes atribuidas a personas | etiqueta `pii:human-attributed`                                       | candidatas para borrado por etiqueta                                                                                                                                                                                                                                            |

**La importación es idempotente.** `memory_key='lesson_<id>'` y un `memory_id` UUIDv5 (a partir de
`clickgap://<org>/<namespace>/lesson/<id>`) son deterministas, y cada ejecución asigna una única
`version` nueva. Una reimportación sustituye las filas bajo `ReplacingMergeTree(version)` y el número de filas se mantiene
igual.

<a id="1-dry-run"></a>

## 1. Ejecución de prueba

No se escribe nada. La ejecución de prueba comprueba que cada marca de tiempo se pueda interpretar, imprime la tabla cruzada de kind → type y TTL junto con el recuento de categorías, y sondea el embedder para que ninguna fila quede sin embedding.

```bash
loom import-clickgap \
  --sqlite ~/Downloads/clickgap-memory-YYYYMMDD/memory.db \
  --org <org> --namespace clickgap --dry-run
```

Confirma que la tabla cruzada coincide con el origen y que el embedder es accesible.

<a id="2-import"></a>

## 2. Importar

Usa un **espacio de nombres dedicado** (`clickgap`) para que el rollback sea un único `namespace.delete`. La exportación no incluye vectores, así que cada fila se genera como embedding al importar con el `LOOM_EMBEDDING_MODEL` activo.

```bash
loom import-clickgap \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap
```

`--agent` tiene como valor predeterminado `clickgap`; `--batch-size` define las filas por inserción en ClickHouse (valor predeterminado 1000). El bloque de verificación del final imprime los siguientes datos y hace fallar la ejecución (exit 1) en estos casos:

- filas de memoria activas frente al número de lecciones de la fuente;
- filas de embedding con un modelo incorrecto (deben ser 0);
- el número de filas permanentes (sin TTL);
- el valor más alto de `recall_count`, leído de nuevo.

`lesson_recalls` (el registro de eventos por cada recuperación) **no** se importa de forma predeterminada: las columnas desnormalizadas `recall_count` ya aportan la señal de ranking. Pasa `--seed-recall-events` solo si necesitas el registro por evento en `loom.memory_usage_events` para análisis.

> **Advertencia:** `--seed-recall-events` es aditivo y no idempotente: ejecútalo una sola vez, en un espacio de nombres nuevo. **Descarta `pr_number`**, porque los eventos de uso de Loom no tienen una columna de PR.

<a id="recall-signal"></a>

## Señal de recuperación

El importador replica `recall_count` en `memory_embeddings.reinforcement_count` y `last_recalled_at` en `last_reinforced_at`. En `memory.search`, ese contador alimenta el término de ranking de refuerzo con decaimiento: `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` define su peso (0 lo desactiva), `LOOM_MEMORY_SEARCH_REINFORCEMENT_HALF_LIFE_DAYS` establece su decaimiento, y una solicitud puede sobrescribir ambos con `reinforcement_weight` / `reinforcement_half_life_days`
([configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)).

El término está limitado por debajo de la relevancia, por lo que Loom **no** reproduce el orden `recall_count DESC` de ClickGap: ordena principalmente por similitud, con términos de entidad, antigüedad y refuerzo ([recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)). Si tu carga de trabajo depende del orden por frecuencia de recuperación, valida que el orden resultante sea aceptable (el validador informa el τ del orden de recuperación) antes del cambio definitivo.

<a id="3-validate-before-cutover"></a>

## 3. Validar antes del cambio definitivo

El validador genera su propio token de administrador con `server.auth.jwt.mint_token`, por lo que necesita `LOOM_JWT_SECRET` y un `LOOM_BASE_URL` explícito: no hay un destino por defecto. Sus sondeos son lecturas de evaluación (`auto_touch: false`, los campos de medición de `bench/_gate.py`), de modo que una ejecución no registra uso de recuperación en las lecciones importadas y las ejecuciones repetidas no alimentan la señal de refuerzo que mide la compuerta τ.

> **Advertencia:** apúntalo a un endpoint de confianza. Las consultas de los sondeos son las aserciones de las lecciones; los datos de importación no deben salir de tu infraestructura a través de un embedder de terceros. `--show-queries` imprime el texto truncado de los sondeos y está desactivado por defecto por la misma razón.

```bash
LOOM_BASE_URL=http://localhost:7777 LOOM_JWT_SECRET=... \
python -m bench.clickgap_migration_validate \
  --sqlite .../memory.db --org <org> --namespace clickgap --agent clickgap \
  --probes 200 --top-k 10 \
  --min-recall 0.90 --min-recallw 0.90 --min-tau 0.2
```

Cada compuerta hace fallar la ejecución con un código de salida distinto de cero cuando se fija por encima de 0 (0 = solo informar):

| Compuerta       | Qué mide                                                                                                                                                                                           |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--min-recall`  | Recall@k de elemento conocido: ¿está la propia lección del sondeo entre los k primeros de Loom? Los desgloses por tipo y por tipo de entidad detectan el colapso de un solo segmento (por ejemplo, una reasignación de tipo incorrecta). |
| `--min-recallw` | Recall@k ponderado por recuperación: cada sondeo se pondera por su `recall_count`, de modo que pasar por alto una lección muy consultada cuesta más.                                               |
| `--min-tau`     | τ del orden de recuperación: concordancia de Kendall entre el orden de Loom y `recall_count`. Es un diagnóstico: Loom no ordena por `recall_count`, así que no esperes un τ alto.                  |

Una búsqueda fallida se informa y se excluye de las razones, nunca se puntúa como un fallo, y cualquier búsqueda fallida hace que la ejecución termine con un código distinto de cero, haya compuertas o no. Una ejecución sin τ calculable imprime `n/a` y falla `--min-tau`.

Los resúmenes (`summary → episodic`) se excluyen de los sondeos, ya que no son buscables por diseño. `--seed` (por defecto 42) hace reproducible la muestra de sondeos; `--concurrency` (por defecto 8) limita las búsquedas concurrentes.

<a id="use-suppression-memories"></a>

## Usar memorias de supresión

Las lecciones `false_positive` ("parece un bug, NO volver a reportarlo") son la capa de supresión. Se pueden buscar y llevan la etiqueta `polarity:negative`. Un detector que decide si reporta un hallazgo candidato ejecuta una búsqueda filtrada por etiquetas sobre el texto del hallazgo:

```python
hits = await agent.memory.search(finding_text, tags=["polarity:negative"], top_k=5)
if hits and hits[0].similarity >= SUPPRESS_THRESHOLD:  # calibrate per embedder
    skip_or_downweight(finding)  # a known false positive
```

El filtro `tags` se aplica a cada vía de recuperación (vectorial, léxica y el relleno por recencia/confianza), de modo que el resultado contiene solo supresiones
(`test_lexical_fallback_applies_caller_filters` en `tests/unit/test_memory_search_service.py`).
Un positivo filtrado por error ocultaría un hallazgo real.

Cualquier otro tipo se filtra de la misma manera mediante su etiqueta `kind:<kind>`: `tags=["kind:principle"]`, o `tags=["kind:false_positive", "kind:maintainer_preference"]` para una lista de exclusión de varios tipos (`tags` es un filtro OR). El parámetro `memory_type` de `memory.search` acepta solo los tipos generales (`semantic`, `procedural`, `preference`, `reflection`, `decision`), por lo que la taxonomía completa de tipos reside en las etiquetas.

<a id="roll-back"></a>

## Revertir

- **Reversión completa:** `namespace.delete` sobre `clickgap` (admin; por MCP, llámalo a través de
  `loom_op`). La importación apunta a un espacio de nombres dedicado, por lo que no se elimina nada más.
- **Borrado o limpieza de una cohorte:** `erasure.request` con un alcance de espacio de nombres (con puntos de control y
  reanudado por el worker de recuperación), o borra solo las filas `pii:human-attributed` cuando un
  mantenedor solicite su eliminación ([retención y borrado](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)).
- **Corrección del mapeo:** vuelve a ejecutar el importer. Las claves deterministas y un `version` más alto reemplazan
  las filas incorrectas en su lugar.

<a id="consolidate-optional-after-validation"></a>

## Consolidar (opcional, después de la validación)

Si compactas casi duplicados de conjuntos de entidades, ejecuta `memory.consolidate` **después** de la validación.
Las protecciones de `server/memory/app/consolidate.py` excluyen las filas **fijadas (pinned)** y nunca fusionan
entre distintos `memory_type` ni entre distintas **polaridades**, de modo que una supresión `false_positive` no puede
ser absorbida por una observación positiva, y un principio no puede absorber una observación.

Mantén `consolidation_enabled=false` en el espacio de nombres (`namespace.register`) para que el
worker de consolidación en segundo plano no consolide por segunda vez las lecciones `summary` de ClickGap que ya
fueron agrupadas.

<a id="related"></a>

## Relacionado

- [Escritura de memoria](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
- [Recuperación y ranking](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- [Espacios de nombres y uso compartido](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
