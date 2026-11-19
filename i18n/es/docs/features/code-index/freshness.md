<!-- i18n: source=docs/features/code-index/freshness.md blob=9dcf44236c7be8f6d338e452c10b5a0be2e98f93 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/freshness.md), which is authoritative.

<a id="code-index-freshness"></a>

# Actualidad del índice de código

El índice va por detrás del repositorio: indexa los commits fusionados de una rama configurada, pasada a pasada, y cada nivel avanza su propia marca de agua. Revisa los metadatos de actualidad de la respuesta cuando estén presentes y el estado del nivel correspondiente antes de confiar en el código indexado.

<a id="the-as_of-block"></a>

## El bloque `as_of`

Las operaciones que devuelven `as_of` exponen los siguientes campos de actualización. Las definiciones ordinarias de `code.symbol` no incluyen este bloque ni un commit; en su lugar, inspecciona `code.index_status` y verifica la definición devuelta contra la revisión del código fuente correspondiente.

| Campo           | Significado                                                                                                                                          |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `commit`        | el commit del nivel rápido para este espacio de nombres (`git_head`), no la revisión de cada nivel del compilador                                    |
| `branch`        | la rama que sigue el espacio de nombres                                                                                                              |
| `indexed_at`    | cuándo se registró ese commit                                                                                                                        |
| `lag_commits`   | commits de primer padre entre `commit` y la punta obtenida del mirror; `-1` desconocido                                                              |
| `stale`         | `lag_commits` > `LOOM_CODE_STALE_AFTER_COMMITS` (por defecto 5), o la obtención del mirror está atrasada                                             |
| `status`        | `fresh` (en la punta), `behind` (por debajo del umbral de obsolescencia), `stale`, `unknown` (lag `-1`)                                              |
| `stale_reason`  | `mirror_not_fetched` cuando el retraso se cuenta contra un mirror que dejó de obtener cambios                                                        |
| `fetched_at`    | junto con `stale_reason`: cuándo obtuvo cambios el mirror por última vez                                                                             |
| `failing_tiers` | presente mientras un nivel falla en la entrada: `tier`, `kind`, `failing_since`, `failed_passes`, `fix` y, para un nivel con watermark, `lag_commits` |

El bloque se almacena en caché durante 30 segundos por espacio de nombres; una pasada que mueve `git_head` lo invalida. Es fail-open: cuando no se puede leer la actualización, el bloque queda vacío y la respuesta se devuelve igualmente. Trata un `as_of` vacío o ausente como desconocido.

Los datos de un nivel con fallos pueden ir por detrás del head incluso cuando `stale` es false: si `failing_tiers` nombra un nivel del compilador, considera que las respuestas de nivel del compilador (grafos de llamadas exactos, referencias) pueden estar desactualizadas y lee el código en el que te apoyas.

**Comprueba la actualización, no solo el estado de la compilación.** Un espacio de nombres cuya primera compilación terminó puede seguir yendo varios commits por detrás. Cuando se devuelve `as_of`, inspecciona `stale`; si no se devuelve, inspecciona el watermark correspondiente mediante `code.index_status`. Cuando la actualización es obsoleta o desconocida, lee desde tu checkout los archivos que vas a editar.

<a id="the-mirrors-own-age"></a>

## La antigüedad del mirror

El retraso se calcula respecto al mirror local de Loom, por lo que marca 0 mientras el mirror ha dejado de hacer fetch. Por eso `stale` también pasa a verdadero cuando el fetch está vencido, y las operaciones de estado informan de la antigüedad del mirror:

- `code.index_status` (un espacio de nombres) y `code.repos` (todas las entradas legibles) devuelven
  `clone_head`, `clone_fetched_at` y `fetch_overdue` (más antiguo que
  `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`, por defecto 3600). Junto a ellos: `git_head`,
  `fast_tier_lag`, `scip_lag` / `scip_py_lag`, la lista `compiler_tiers`,
  `last_refresh_at` y `chunks_total` / `chunks_embedded` / `embedding_coverage`.
- Ambas devuelven `tier_health`: el último resultado registrado de cada nivel (fetch, ingest, los
  niveles del compilador y la ejecución completa semanal de C++), con el tipo de error, `failing_since`,
  `failed_passes` y la solución mientras falla. Un nivel de Go que omitió módulos cuyas
  dependencias no puede obtener en este host muestra `state: "unavailable"` (tipo
  `dependency_unavailable`, `failing_since` con la fecha en que se detectó por primera vez, sin contador de pasadas) junto con su solución:
  no está fallando y no figura en `as_of.failing_tiers`.
- Las filas de `code.repos` incluyen `searchable`: `embedding_coverage` ha alcanzado
  `LOOM_CODE_READY_EMBED_COVERAGE`.

`compiler_tiers` tiene una ficha por cada nivel que ejecuta la entrada (`producer`, `tool`, `label`,
`commit`, `lag`, `workers`). `code.index_status` responde de forma compacta por MCP y siempre que
`commit` esté definido; `brief: false` devuelve la respuesta completa.

<a id="is-one-commit-in-every-tier"></a>

## ¿Un commit está en todos los niveles?

Quien revisa un PR fusionado necesita que todos los niveles contengan ese merge. `code.index_status` con
`commit=<sha>` responde en `commit_coverage`:

| Campo                   | Significado                                                                                          |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `ready`                 | true cuando el nivel rápido y todos los niveles del compilador con una marca de agua han alcanzado el commit |
| `waiting_on`            | los niveles que no lo han alcanzado                                                                  |
| `in_mirror`             | si el mirror contiene el commit                                                                      |
| `fast`, `tiers[]`       | por nivel: `covered` (null cuando el mirror no tiene el commit) y `commits_since`                    |
| `commits_since`         | cuánto ha avanzado el nivel rápido más allá del commit                                               |
| `paths_changed_overlap` | con `paths=[…]`: cuáles de esas rutas cambió un merge posterior                                      |

Las rutas en `paths_changed_overlap` difieren en el índice respecto al commit; léelas desde
tu checkout.

```python
st = await agent.code.index_status(namespace="code-myrepo", commit=merge_sha, paths=pr_files)
cov = st["commit_coverage"]
if not cov["ready"]:
    print("waiting on", cov["waiting_on"])
```

<a id="waiting-for-coverage-without-polling"></a>

## Esperar la cobertura sin polling

`code.coverage.watch` publica un evento en la bandeja de entrada de una tarea cuando un commit queda cubierto:

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.coverage.watch" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","commit":"'"$MERGE_SHA"'","inbox":"<your-inbox>"}'
```

- `commit` es un SHA completo o una abreviatura que el mirror resuelve. `inbox` es una bandeja de entrada de tareas (`inbox.register`). El llamador necesita el rol de viewer en el espacio de nombres de código y el de writer en el espacio de nombres propietario de la bandeja de entrada.
- El worker `code_coverage_notify` (activado por defecto, se ejecuta cada `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS`, 60) publica un evento `code.coverage.ready` con `patch` `{namespace, commit, tiers: [{producer, label, watermark}]}`. Llega a través de `/v1/task/watch?inbox=…`, `task.updates` y el webhook firmado de la bandeja de entrada.
- Un commit que ya está cubierto responde `ready=true`, `emitted=true` y publica el evento en la misma llamada.
- Hay un evento por (namespace, commit, inbox): su `task_id` es el `event_id` de la respuesta, de modo que un reintento nunca publica dos veces. Las entregas de un mismo evento se ejecutan de una en una bajo un lease sobre ese id, así que, de dos entregas en competencia, solo la que almacenó el evento responde `emitted=true`. Un `code.coverage.watch` que entrega en la misma llamada y espera más de 10 s a otra entrega responde 409 `PRECONDITION_FAILED`, y 503 cuando el almacén de leases (Keeper) no responde; reintenta en ambos casos.
- `code.coverage.unwatch` (con los mismos campos) elimina una vigilancia pendiente; `removed: false` significa que no había ninguna activa.

El SDK de Python tiene `agent.code.coverage_watch(commit, inbox, namespace=…)` y `agent.code.coverage_unwatch(...)`. La bandeja de entrada se describe en [tareas y decisiones](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox).

<a id="pushing-freshness-from-ci"></a>

## Enviar actualizaciones desde CI

El poller (`LOOM_CODE_INGEST_INTERVAL_SECONDS`, 120) detecta cada merge. Para indexar un
merge de inmediato, llama a `code.refresh` desde el job posterior al merge con un token de escritor limitado al
espacio de nombres de código:

```bash
curl -fsS -X POST "$LOOM_BASE_URL/v1/code.refresh" \
  -H "Authorization: Bearer $LOOM_CI_TOKEN" -H 'Content-Type: application/json' \
  -d '{"org":"<org>","namespace":"code-myrepo","expected_head":"'"$GITHUB_SHA"'"}'
```

- La pasada cubre solo el espacio de nombres indicado, que debe ser una entrada configurada (de lo contrario devuelve 404), e indexa su rama configurada hasta la punta obtenida.
- `expected_head` solo se registra en el log. `wait=true` bloquea hasta que termina la pasada (hasta
  `wait_timeout_seconds`, 300 por defecto); la respuesta incluye `triggered`,
  `already_running`, `head_before` y `head_after`.
- La llamada es de mejor esfuerzo; el poller corrige un disparo perdido.

Los operadores vigilan `loom.code.tier_lag_seconds{tier}`; el runbook para niveles con retraso está en
[operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).

<a id="settings"></a>

## Configuración

| Configuración                                | Valor por defecto | Efecto                                                                                      |
| -------------------------------------------- | ----------------- | ------------------------------------------------------------------------------------------- |
| `LOOM_CODE_STALE_AFTER_COMMITS`              | 5                 | commits de retraso antes de que `as_of.stale` sea true                                      |
| `LOOM_CODE_FETCH_STALE_AFTER_SECONDS`        | 3600              | antigüedad del mirror antes de `fetch_overdue` y `stale` (0 desactiva la comprobación)      |
| `LOOM_CODE_INGEST_INTERVAL_SECONDS`          | 120               | con qué frecuencia `code_ingest` consulta el mirror                                         |
| `LOOM_CODE_READY_EMBED_COVERAGE`             | 0.95              | proporción de embeddings requerida antes de que `code.search` use su rama vectorial         |
| `LOOM_CODE_COVERAGE_NOTIFY_INTERVAL_SECONDS` | 60                | con qué frecuencia se comprueban las vigilancias de cobertura                               |

<a id="limits"></a>

## Límites

- La actualización es por espacio de nombres de rama. Una corrección fusionada en `main` no está en el espacio de nombres de una rama de lanzamiento hasta que se fusione allí; `code.grep mode=presence namespaces=[…]` comprueba varios.
- Los pull requests abiertos y tu árbol de trabajo nunca están en el índice.
- No hay ninguna notificación push de que "el índice se movió"; `code.coverage.watch` notifica por commit.

<a id="related"></a>

## Relacionado

- [overview.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): cómo leer una respuesta, resolución de problemas
- [operations.md](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md): las ops de estado
- [arquitectura del índice de código](../../architecture/code-index-architecture.md): marcas de agua, reconciliación, el sello del extractor
