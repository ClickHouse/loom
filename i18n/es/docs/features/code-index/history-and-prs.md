<!-- i18n: source=docs/features/code-index/history-and-prs.md blob=6c133daf27b7ef459055a1cc94a7dd9563aa4255 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md), which is authoritative.

<a id="history-prs-and-releases"></a>

# Historial, PR y releases

Estas ops necesitan el historial de PR fusionados; el worker `code_pr_history` está desactivado por defecto (`LOOM_CODE_PR_HISTORY_ENABLED`).

El plano de PR responde a "cómo llegó el código hasta aquí". Vive en el mismo espacio de nombres de código que el índice, de modo que una respuesta sobre el historial une en una sola lectura los archivos de un PR fusionado con los símbolos, los llamadores y las pruebas actuales, sin ninguna llamada a GitHub en el momento de la consulta. Los registros de incidencias e incidentes de un equipo se incorporan mediante un [rastreador](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) vinculado.

<a id="the-pr-plane"></a>

## El plano de PR

| Tabla                     | Contiene                                                                                                                         | La llena                                                                                  |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `loom.code_pr_history`    | una fila por cada PR fusionado de la rama del espacio de nombres: número, título, autor, etiquetas, archivos, issues vinculados, commit de fusión | el worker `code_pr_history`; `loom code backfill-prs` para los primeros meses             |
| `loom.code_pr_reviews`    | comentarios de revisión en línea de los PR fusionados                                                                            | el mismo worker (`LOOM_CODE_PR_REVIEWS_ENABLED`, activado por defecto; necesita un token) |
| `loom.code_gh_items`      | issues y PR en cualquier estado, con los PR abiertos junto a los archivos que toca su head                                       | el mismo worker (`LOOM_CODE_GH_ITEMS_ENABLED`, activado por defecto; se omite sin autenticación) |
| `loom.code_backport_gaps` | el informe nocturno de backports pendientes para todo el conjunto                                                                | el worker `code_backport_gaps`                                                            |

El worker usa `LOOM_CODE_GITHUB_TOKEN` o el `github_token` de la entrada; si no hay ninguno, aplica el límite de tasa anónimo de GitHub. Un espacio de nombres de una rama de release contiene los PR fusionados en esa rama, así que "qué cambió en la 2.4" se consulta en el espacio de nombres 2.4. `loom code backfill-prs` carga `LOOM_CODE_PR_BACKFILL_MONTHS` (12) meses de historial para un repositorio nuevo ([operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)).

<a id="which-op-answers-which-question"></a>

## Qué op responde a cada pregunta

| Pregunta                                                                           | Op                                                                                                   |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Dame un resumen de este PR, o del cambio que estoy a punto de hacer                | `code.context_pack` (`pr_number` o `task`; `sections` conserva solo los bloques sobre los que actúas) |
| ¿Qué PR fusionados tocaron este símbolo o ruta, y qué dijeron los revisores?       | `code.history`, `code.symbol_history`                                                                |
| ¿Qué PR fusionados tocaron por última vez estas líneas, entre la versión A y la B? | `code.blame_range`                                                                                   |
| ¿Qué comprobaron los revisores en este PR fusionado? ¿Qué suelen comprobar?        | `code.review_brief`, `code.conventions`                                                              |
| Antes de abrir un PR: ¿ya existe uno parecido?                                     | `code.pr_check`                                                                                      |
| ¿Qué PR fusionados pudieron introducir este error?                                 | `code.bisect_candidates`                                                                             |
| ¿Qué ramas de versión aún conservan el error que eliminó una corrección?           | `code.backport_gap` (una corrección), `code.backport_gaps` (todas las correcciones etiquetadas)      |
| ¿Qué build de CI incluyó este commit por primera vez?                              | `code.first_build`                                                                                   |

Los argumentos están en la [referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code).

<a id="history-of-a-symbol-a-path-or-a-line-range"></a>

### Historial de un símbolo, una ruta o un rango de líneas

`code.history` lista los PR fusionados que tocaron más recientemente un símbolo (`name` o
`symbol_id`), un `path` o unos `numbers` de PR dados: número, título, autor, etiquetas y hasta
`reviews_per_pr` (por defecto 3, `0` = ninguno) comentarios de revisión humanos sobre ese archivo.
`code.symbol_history` lo acota al historial de regresiones de una función: los PR que
tocaron su rango, sus incidencias vinculadas y reversiones, y los comentarios de revisión dentro de ella.

`code.blame_range` recibe un `path`, un rango de líneas (como máximo 400 líneas) y una ventana expresada
como versiones de release (`from_version`, `to_version`) o shas de commit. Combina las listas de archivos
del registro de PR en la ventana (un mínimo) con `git blame -w -M -L` sobre el mirror,
y cada commit señalado se asigna a su PR. En un espacio de nombres de rama de versión también lee el registro
del espacio de nombres del trunk, solo cuando quien llama puede leer el trunk; de lo contrario,
las filas del trunk se omiten y `legs.ledger.skipped` lo indica. Una versión de release
se resuelve al head indexado de su espacio de nombres de rama cuando quien llama puede leer ese
espacio de nombres; si no, mediante la rama del mirror o la etiqueta `-new`.

- `stage: "introduced"` (por defecto) rebaja los backports, las reversiones y los cambios que solo tocan espacios en blanco o
  renombrados, y promueve un PR que nombra la función contenedora, recibió un comentario de
  revisión dentro de ella, o cuya incidencia vinculada nombra las palabras de `mentions`.
- `stage: "last_touched"` mantiene el orden de blame.

La respuesta es exacta para "tocado por última vez", nunca para "introdujo el error"; su `note` lo
indica.

<a id="review-briefs-and-conventions"></a>

### Resúmenes de revisión y convenciones

`code.review_brief` lee el resumen que la ingesta construyó cuando se fusionó el PR: la clase de
cada archivo cambiado, las funciones tocadas con sus llamadores y fan-in, las pruebas que
las cubren y lo que dijeron los revisores.

En los niveles `standard` y `full` el resumen también tiene una sección `rationale`: las decisiones
de diseño y restricciones (filas de tipo `decision`, o etiquetadas `decision` o `constraint`,
activas o en conflicto tal como las muestra el bloque previo a la edición) en el espacio de nombres de código del trunk del repositorio
cuyos `files` o vínculos de código tocan los archivos del PR. Se lee en cada llamada, desde el espacio de nombres del trunk
solo cuando quien llama puede leerlo, y nunca se almacena con el resumen, de modo que
una decisión registrada o cambiada después de la fusión aparece en la siguiente lectura. Lee los
primeros `LOOM_CODE_REVIEW_BRIEF_RATIONALE_PATHS` (10) archivos cambiados, primero los archivos de código fuente, de
los que almacena el resumen, y conserva como máximo `LOOM_CODE_REVIEW_BRIEF_RATIONALE_ROWS` (8)
filas, primero las que tienen algo que comprobar. Sirve filas completas, primero las `check` y
luego las restricciones, hasta `LOOM_CODE_REVIEW_BRIEF_RATIONALE_TOKENS` (250) tokens, y siempre
la primera fila; el presupuesto del nivel aún puede recortar la sección como cualquier otra. Cada fila
lleva la línea del bloque previo a la edición con su valor cortado a la longitud de línea (se conservan la fuente,
`(pinned)` y la nota de cambio de código), `pinned`, `code_state` y un `verdict`. Cada
archivo cambiado que se lee es una lectura de `memory.for_file`: cuatro lecturas de ClickHouse (coincidencia de archivos, vínculos de código,
sus memorias, las filas vinculadas; un archivo con muchos vínculos divide la última), cada una
conservando hasta 50 filas con hasta `LOOM_MEMORY_FOR_FILE_WHOLE_TOKENS` × 48 bytes de valor.

La comprobación busca dos tipos de nombre: los nombres con forma de código en la cláusula `Rejected:` de una fila,
hasta una cláusula `Realized:` (entre comillas invertidas, calificados como `A::b`, o llamadas como `f()`),
y los nombres en snake_case en cualquier parte del valor de una restricción, con o sin comillas invertidas, simples o
calificados (`Setting::distinct_spill_ratio`). Es una coincidencia de palabra completa sobre las líneas que
añade el diff, leyendo hasta 200,000 caracteres de un diff publicado o 200,000 bytes del
del mirror, así que `check` significa "lee esta fila frente al diff", no que se haya encontrado una infracción.

| Veredicto de fila | Significado                                                                                                                                                                                                                          |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `check`           | el diff añade uno de los nombres comprobados de la fila (`names`)                                                                                                                                                                    |
| `clear`           | la fila tiene nombres comprobados y el diff no añade ninguno                                                                                                                                                                         |
| `unknown`         | no se encontró ningún nombre, pero el diff no pudo leerse aquí (git falló, incluso a medias), se cortó, o tiene un hunk que cuenta mal sus líneas, o el valor de la fila se cortó (`diff_unread`, `diff_cut`, `diff_malformed`, `value_cut`) |
| `""`              | la fila no nombra nada que comprobar                                                                                                                                                                                                 |

El veredicto de la sección es `check` cuando alguna fila lo dice, luego `unknown`, luego `clear`, y
`none` cuando ninguna fila tiene algo que comprobar. Dice `clear` o `none` solo cuando se comprobaron todas las
rutas cambiadas y todas las filas coincidentes; de lo contrario dice `unknown`, y las causas indican qué quedó pendiente: no hay
ningún repositorio configurado para el espacio de nombres (`no_repo`), el resumen no almacena ninguna ruta cambiada (`no_paths`),
quien llama no puede leer el espacio de nombres del trunk (`trunk_not_readable`), una lista almacenada de rutas cambiadas que puede estar cortada (`paths_maybe_cut`: el
resumen almacena los primeros 50 archivos cambiados más los archivos de los hunks del diff, hasta 200, de modo que
una lista de 50 o más puede contener menos de los que cambió el PR), rutas cambiadas de esa lista no leídas
(`paths_not_read`), una ruta cuya lectura alcanzó sus 50 filas (`paths_at_row_ceiling`), una
ruta cuya lectura de vínculos de código falló (`links_not_read`), o filas que el límite de filas dejó fuera y que
tenían algo que comprobar (`rows_not_read`).

`code.conventions` expone las comprobaciones recurrentes de los mantenedores del repositorio como hechos sobre el diff de un
PR (`pr_number`, `head_sha`, `files` o `diff`), nunca como opiniones; `rules` acota
qué comprobaciones se ejecutan. El diff que lee está limitado a `LOOM_CODE_CONVENTIONS_DIFF_MAX_BYTES`.

<a id="pre-pr-duplicate-check-codepr_check"></a>

## Verificación de duplicados antes de un PR: `code.pr_check`

Un agente que está a punto de abrir un pull request pasa el borrador y recibe uno de tres veredictos:
`duplicate_of` (ya hay un PR abierto que hace lo mismo, o uno fusionado que ya lo hizo),
`related` o `none`.

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="pr-bot") as bot:
    check = await bot.call(
        "code.pr_check",
        title="Retry the upload when the token expires",
        paths=["src/upload/client.py"],
        symbols=["UploadClient.send"],
        issues=[1432],
    )
    if check["verdict"] == "duplicate_of":
        for item in check["items"]:
            print(item["pr_number"], item["state"], item["evidence"])
```

La misma llamada por HTTP es `POST /v1/code.pr_check`, y por MCP la herramienta es
`loom_code_pr_check` (en los toolsets `coding` y `tracker`). Se requiere un título, las rutas modificadas o
un número de issue.

Los candidatos provienen de los propios registros de Loom a través de cuatro vías, sin ninguna llamada a GitHub:

| Vía                    | Lee                                                                                                                                  |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| PRs abiertos           | todos los PRs abiertos en `code_gh_items`: su título y los archivos que toca su `refs/pull/N/head` (sin cuerpo)                      |
| cerrados sin fusionar  | PRs cerrados sin fusionar en los últimos `LOOM_CODE_PR_CHECK_CLOSED_DAYS` (180): «alguien ya lo intentó»; limitado a `related`       |
| fusionados, por ruta   | PRs fusionados que tocaron las `paths` del borrador                                                                                  |
| fusionados, por texto  | PRs fusionados cuyo texto puntúa frente al título y los símbolos del borrador, y PRs fusionados que cierran los mismos `issues`      |

Cada candidato se puntúa con una única regla fija: la proporción de tokens del título y de los símbolos del borrador que contiene (ponderada por rareza), un pequeño término de cuerpo para los PRs fusionados, la superposición de rutas en ambos sentidos y si cierra un issue que el borrador nombra. No hay un conjunto etiquetado con el que ajustar los pesos, ya que los PRs duplicados se cierran, no se fusionan. La respuesta indica `epistemic: "heuristic"`, y un título por sí solo nunca llega a `duplicate_of`.

| Configuración                            | Valor por defecto | Efecto                                                                                          |
| ---------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------------- |
| `LOOM_CODE_PR_CHECK_DUPLICATE_THRESHOLD` | 0.55              | puntuación a partir de la cual un candidato es `duplicate_of`                                   |
| `LOOM_CODE_PR_CHECK_RELATED_THRESHOLD`   | 0.2               | puntuación a partir de la cual es `related`                                                     |
| `LOOM_CODE_PR_CHECK_MERGED_DAYS`         | 365               | los PRs fusionados más antiguos que esto no votan como duplicados                               |
| `LOOM_CODE_PR_CHECK_MASS_FILES`          | 200               | un candidato que toca más archivos es un cambio masivo; sus archivos no votan                   |

`tracker.duplicate_check` es su equivalente del lado de los issues: ante un veredicto `none` o `related`, incluye como `related_prs` los PRs de `code.pr_check` que ya podrían abordar el borrador
([resumen del tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#duplicate-check)).

<a id="bisect-candidates-codebisect_candidates"></a>

## Candidatos de bisect: `code.bisect_candidates`

Dado un bug reportado, ¿qué PRs fusionados debería investigar primero un ingeniero?
`code.bisect_candidates` prioriza candidatos; no es un `git bisect` automatizado.
No compila el repositorio, no ejecuta pruebas, no reproduce el reporte, no descubre versiones
buenas y malas ni demuestra que un candidato haya causado el bug. La puntuación de un candidato es
un orden de investigación, no una probabilidad de causalidad.

El límite de la regresión proviene de la evidencia registrada antes de la llamada:

| Entrada de la ventana         | De dónde proviene                                                                                                                                      | Qué hace Loom con ella                                                                                                                                                  |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bad_version`, `good_version` | Un usuario, un job de CI, un flujo de soporte o un agente de pruebas ha observado el bug en la versión mala y ha observado la versión buena sin el bug. | Escanea las versiones entre esas observaciones. Loom no vuelve a probar ninguna de las dos versiones.                                                                   |
| `issue_number`                | El issue enlazado del tracker ya tiene observaciones de triaje `reproduces_on` y `clean_on`, rellenadas por el flujo del tracker.                       | Lee esas observaciones cuando no se proporcionaron versiones explícitas. La existencia de un issue o de un valor de triaje no prueba que el reporte sea válido.        |
| `after`, `before`             | El llamador solo conoce un intervalo aproximado de tiempo de fusión, como la última fecha buena conocida y la fecha en que se vio por primera vez.      | Acota el escaneo del historial de PRs a ese intervalo; las fechas son límites, no resultados de reproducción.                                                           |
| `bad_sha`                     | El llamador conoce un commit igual o anterior al primer fallo observado.                                                                                | Resuelve la hora del commit en el espejo del repositorio y la usa como `before` cuando `before` no está presente. No ejecuta el commit.                                 |

Después, el llamador aporta la evidencia del reporte de que disponga: frames del fallo (`symbols`),
`paths` de código fuente, `patterns`, `text` libre, un `issue_number` y, cuando se conoce, un `fix_pr`.
Cuando no hay un rango de versiones preciso, el respaldo por historial del repositorio usa el
historial reciente retenido para este escaneo (`LOOM_CODE_BISECT_HISTORY_DAYS`, 90 por defecto), que es una
ventana de investigación más amplia y menos precisa.

1. Los frames se resuelven a archivos mediante el índice de código.
2. El escaneo de candidatos lee los PRs fusionados y no derivados cuya primera versión cae en
   la ventana y que tocaron esos archivos, ordenados por los frames que nombra su diff. Con
   un tracker enlazado, escanea los diffs de PRs del tracker; las columnas de triaje registradas previamente
   de `issue_number` (`reproduces_on`, `clean_on`) pueden proporcionar la ventana. Sin tracker,
   o con un tracker sin archivos modificados, lee el historial de PRs propio del repositorio
   (`LOOM_CODE_BISECT_HISTORY_DAYS`, 90 por defecto), y la respuesta indica
   `candidates_from: "code_history"`.
3. Sin ningún frame (un reporte de resultado incorrecto) y con un `issue_number`, el escaneo se acota
   por los identificadores más raros del issue en los diffs de la ventana, los directorios del componente y
   los archivos del fix; `wideners` elige cuál, `widened_by` indica cuál se aplicó.
4. La fusión en el momento del reporte combina el escaneo de diffs con los rangos vectorial, léxico, de rutas, de subárbol y de
   recencia; un PR que el reporte nombra como introductor del bug va primero. Cuando el primero
   fusionado es débil (margen sobre el n.º 2 inferior a 0.1 y no es un sospechoso nombrado en el reporte),
   se incorpora una ventana más amplia (12 versiones, o `LOOM_CODE_BISECT_WIDE_DAYS`) y una llamada a un LLM reordena los 20 primeros
   (`LOOM_CODE_BISECT_LLM_RERANK_MODEL`; un timeout conserva el orden fusionado), o
   `rerank: true` lo solicita.
5. Un reporte que no nombra ningún fix ni issue toma prestado el fix del issue con el que
   coincide la comprobación de duplicados, dentro de `LOOM_CODE_BISECT_DUPCHECK_TIMEOUT_S`.

Cada candidato lleva marcas (backport, revert, reverted, re-land, solo espacios en blanco o
solo renombrado, `touches_symbol` a partir de las cabeceras de hunk del diff); `marks: false` devuelve la
lista ordenada sin más. Qué rutas cuentan como código fuente lo define
`bisect.code_prefixes` del tracker ([contrato de registros](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#source-entry-options)).
El reordenamiento necesita un proveedor de LLM real; las demás etapas funcionan con la pila estándar una vez que el historial de
PRs está cargado. Los ajustes de bisect se enumeran en la
[referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#code).

El ejemplo siguiente ordena los PRs fusionados que llegaron entre una versión buena y una mala y
tocaron los archivos de una función que falla. `bad_version` es la primera versión en la que se vio
el bug y `good_version` la versión más reciente vista sin el bug; Loom no prueba ninguna de las dos.

La ventana de versiones necesita un espacio de nombres de tracker enlazado al repositorio cuyos registros de PR incluyan `minor_version` (el [contrato de registros](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)). Solo con el historial de PRs fusionados
(`LOOM_CODE_PR_HISTORY_ENABLED` o `loom code backfill-prs`; consulta [primer índice](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)), las versiones
no acotan el escaneo: la operación lee en su lugar los últimos `LOOM_CODE_BISECT_HISTORY_DAYS` (90 por defecto).

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="triage-bot") as bot:
    result = await bot.call(
        "code.bisect_candidates",
        bad_version="26.3",
        good_version="26.2",
        symbols=["UploadClient.send"],
    )
    for candidate in result["candidates"]:
        print(candidate["pr_number"], candidate["score"])
```

Los candidatos se devuelven ordenados. El `score` indica qué PRs investigar primero; no
demuestra que un PR causara el bug. Un ingeniero o un job de CI todavía tiene que reproducir el
bug y probar los candidatos.

La verificación final sigue siendo externa a esta operación: un ingeniero, un flujo de CI o un
agente de pruebas debe reproducir el issue y probar los candidatos ordenados.

<a id="backport-gaps"></a>

## Brechas de backport

`code.backport_gap` nombra una corrección fusionada (`pr_number`) o el issue que cierra
(`issue_number`, resuelto mediante las aristas `fixes` del tracker o un vínculo recuperado) y
responde por cada espacio de nombres de rama de release del mismo repositorio, del más reciente al más antiguo, o por las
`branches` que indiques (espacios de nombres o versiones como `26.4`). Se combinan tres elementos: el registro de
backport (`is_backport_pr`, `upstream_pr_number` y `base_branch` del tracker, junto a los
backports fusionados del libro de ramas), el primer release de la corrección (una rama creada en
esa versión o después la incluye) y si las funciones que tocan los hunks de la corrección existen en cada rama.

| Veredicto      | Significado                                                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `gap`          | no hay backport, la rama es anterior a la corrección y las funciones (o archivos) tocados están presentes; `cherry_pick` nombra el PR y el sha de la fusión |
| `pending`      | existe un PR de backport y está abierto                                                                                              |
| `covered`      | se fusionó un backport, la rama se creó en el primer release de la corrección o después, o los `fix_versions` del issue nombran la rama |
| `not_affected` | todas las funciones (o archivos) tocados están ausentes en la rama                                                                   |
| `unknown`      | el registro no estaba disponible mientras el código está presente, la comprobación de presencia agotó el tiempo, o la rama no tiene una versión con la que comparar |

Un `gap` a nivel de símbolo es un límite inferior: el código existe en la rama, lo que no
demuestra que el bug también exista.

- `exact: true` comprueba el parche hunk por hunk. Si las líneas eliminadas siguen presentes y las líneas añadidas están ausentes, `gap` es exacto; si las líneas añadidas están presentes, las líneas eliminadas ya no están y no hay backport
  registrado, `not_affected` es exacto.
- `history: true` (implica `exact`) también lee el árbol y el historial de la rama en el
  mirror para las filas que las líneas dejan sin decidir.
- `presence.patch` muestra el modo y las coincidencias como evidencia.

`code.backport_gaps` es el informe del conjunto completo: todas las correcciones de los últimos 90 días que
los maintainers etiquetaron para backport, por rama de release, con las brechas primero. El
worker `code_backport_gaps` lo escribe cada noche en `loom.code_backport_gaps` ejecutando
`code.backport_gap(exact, history)` por corrección (`LOOM_CODE_BACKPORT_GAPS_ENABLED`, activado por defecto,
efectivo solo con `LOOM_CODE_PR_HISTORY_ENABLED`); la op lee esa tabla.
`wanted_only: false` incluye las correcciones sin etiqueta. Cada espacio de nombres de rama se autoriza
antes de leerse, con fallo cerrado.

<a id="first-build"></a>

## Primera build

`code.first_build` recibe el `sha` de un commit o el `pr_number` de un PR fusionado y responde qué build de CI lo incluyó por primera vez. Necesita la tabla `version_history` del tracker vinculado: una ejecución que nombra el sha es exacta; en caso contrario, se comprueban en el mirror las bases de las ejecuciones posteriores a la fusión (`git merge-base --is-ancestor`, como máximo diez) para encontrar el descendiente más temprano, y la respuesta es un límite inferior.

<a id="pr-and-tracker-planes"></a>

## Planos de PR y de tracker

Un mantenedor vincula trackers a un repositorio con `code.repo.set_source`, el principal primero
([resumen del tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)). Después:

- `code.bisect_candidates` analiza los diffs de PR del tracker y contrasta los sospechosos con
  correcciones conocidas;
- `code.backport_gap` lee el registro de backports, y `code.first_build` las ejecuciones
  de CI del tracker;
- `code.history` y las partes de historial de `code.search` y `code.ask` incluyen el
  registro del tracker junto al PR cuando se resuelve un vínculo y el solicitante puede leerlo;
- `code.context_pack` puede incluir elementos del tracker (`sections=["qa"]`).

Sin un tracker, las operaciones responden desde el plano de PR cuando pueden, e indican
que faltan las partes que solo provienen del tracker.

<a id="limits-and-failure-modes"></a>

## Límites y modos de fallo

- **Sin historial, sin respuesta.** Con `LOOM_CODE_PR_HISTORY_ENABLED` desactivado y sin `backfill-prs`, las tablas de historial están vacías y las ops responden sin nada que ordenar.
- **El acceso anónimo a GitHub** cubre los repositorios públicos con el límite de tasa público; los comentarios de revisión y el espejo de issues necesitan un token.
- **Los PR abiertos no son código indexado.** `code.pr_check` ve el título y los archivos de un PR abierto, pero no su cuerpo ni sus símbolos.
- **Veredictos heurísticos.** `code.pr_check` no está calibrado por diseño; `code.blame_range` es exacto solo para "last touched"; `code.bisect_candidates` ordena sospechosos y nunca señala a un culpable como un hecho.
- **Las caídas del tracker degradan el servicio, pero nunca lo hacen fallar.** Una lectura del tracker que agota el tiempo de espera o activa su breaker establece `tracker_unavailable`; las ramas del plano de PR siguen respondiendo.

<a id="related"></a>

## Relacionado

- [Descripción general del tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) y [contrato de registros](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)
- [Cómo añadir repositorios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)
- [Operaciones del índice de código por pregunta](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Arquitectura del índice de código](../../architecture/code-index-architecture.md)
- [Referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)
