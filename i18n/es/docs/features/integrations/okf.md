<!-- i18n: source=docs/features/integrations/okf.md blob=7d8ba9d5d921d351c46c4592c82a48277bb4f2cb model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md), which is authoritative.

<a id="okf-bundles"></a>

# Paquetes OKF

Un espacio de nombres se puede renderizar como un paquete OKF (Open Knowledge Format) v0.2: un archivo Markdown por memoria con frontmatter YAML, un `index.md` por directorio que lista cada concepto con una descripción de una línea, y un `log.md` con los cambios. La CLI funciona del lado del cliente, sobre la API HTTP. Un paquete se puede comparar con diff, revisar en un pull request y leer sin conexión; Loom sigue siendo el almacén.

El paquete es una proyección, no un segundo almacén: se regenera a partir del espacio de nombres, y una edición vuelve a través de la ruta de escritura habitual (detección de conflictos, lista de bloqueo, auditoría).

```bash
loom export-okf --namespace <ns> --out ./knowledge        # render
loom okf-lint ./knowledge                                 # offline check: exit 2 critical, 1 warnings, 0 clean
loom import-okf ./knowledge --namespace <ns> --dry-run    # plan the write-back
```

Los tres comandos aceptan `--server-url`, `--token` (por defecto `$LOOM_API_KEY`, y luego `~/.loom/token`) y `--org` (por defecto: la claim `org` del token); `okf-lint` no necesita ninguno de ellos. Todos los flags están en la [referencia de la CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md).

<a id="export"></a>

## Exportación

`loom export-okf` es de solo lectura. Recorre por páginas cada fila activa del espacio de nombres (todos los agentes) mediante `memory.list`, obtiene los valores completos con `memory.bulk_get` y escribe los archivos planificados en `--out`. Los archivos de concepto existentes se sobrescriben; nada más en el directorio se modifica. El mapeo reside en `server/memory/domain/okf_bundle.py` (funciones puras, con pruebas unitarias); `server/cli/okf.py` se encarga del transporte y del sistema de archivos.

| Memoria                            | Concepto                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`                       | nombre de archivo y `loom.memory_key`: el slug de la clave, limitado a 160 bytes, con un `-<hash>` de la clave y del id de fila cuando otra clave ocupa ese nombre o cuando es el de un archivo del bundle (`index`, `log`, `README`, `AGENTS`, en cualquier capitalización) o un nombre de dispositivo de Windows (`nul`, `con`, `com1`)                                                                                       |
| `memory_type`                      | directorio (el slug del tipo en minúsculas, limitado como un nombre de archivo; `type-<slug>` para `evidence`, `digests`, `staging`, `node_modules`, el nombre completo de un archivo del bundle (`index.md`, `log.md`) o un nombre de dispositivo de Windows como `nul`) y `type`: `semantic` → Fact, `procedural` → Procedure, `preference` → Preference, `episodic` → Event, cualquier otro en formato de título                |
| valor                              | cuerpo: el texto tal cual, el JSON como bloque de código delimitado                                                                                                                                                                                                                                                                                                                                                           |
| `tags`                             | `tags`                                                                                                                                                                                                                                                                                                                                                                                                                        |
| agente y `created_at`              | `generated: {by: "agent:<agent>", at}`                                                                                                                                                                                                                                                                                                                                                                                        |
| `verified_by` / `verified_at` vigentes | `verified: [{by, at}]` (solo atestaciones de personas y de procesos)                                                                                                                                                                                                                                                                                                                                                      |
| `status`                           | `active` → `stable`, `conflicted` → `draft`, cualquier otro → `deprecated`                                                                                                                                                                                                                                                                                                                                                    |
| `expires_at`                       | `stale_after`, una fecha límite de revisión                                                                                                                                                                                                                                                                                                                                                                                   |
| `source_resource_id`               | `sources: [{resource}]`                                                                                                                                                                                                                                                                                                                                                                                                       |
| todo lo específico de Loom         | el bloque `loom:`, incluidos `source_class`, `sensitivity`, `superseded_by`, `valid_to` y `content_sha256`                                                                                                                                                                                                                                                                                                                    |

**Enlaces.** Las relaciones creadas por alguien (las relaciones tipadas `memory.link` del espacio de nombres, leídas con `memory.export_evidence kind=edges`, y la cadena `supersedes` que implica una fila reemplazada) se escriben como `relates_to: [{path, kind}]` autoritativo en el frontmatter y se replican en la sección "Related Concepts" del cuerpo. Después se añaden los enlaces de entidades compartidas, limitados y simétricos, de modo que el bundle tenga un grafo que un validador pueda recorrer. Los conceptos que no enlazan con nada siguen siendo huérfanos, y la CLI los informa en lugar de ocultarlos.

**La procedencia viaja con los datos.** El bloque `loom:` incluye el nivel de procedencia de la fila, de modo que un viaje de ida y vuelta no pueda convertir una inferencia en un hecho de primera mano.

| Flag                   | Efecto                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--include-high`       | exporta también las filas con `sensitivity: high`, los fragmentos sin procesar y los resúmenes enlazados a ellas, y el texto sin procesar que contiene una forma de nivel alto (una credencial, un número de tarjeta, un SSN o un PIN declarado); el servidor audita cada elemento liberado; `memory.bulk_get` no entrega una fila `high` pasada su ventana de retención, por lo que se nombra y se deja fuera. Desactivado por defecto: un repositorio espejo tiene una audiencia más amplia que los permisos del espacio de nombres |
| `--no-evidence`        | solo conceptos; omite los fragmentos sin procesar (`evidence/`) y los resúmenes de sesión (`digests/`) que el bundle incluye por defecto                                                                                                                                                                                                                                                                                                                                                                |
| `--no-links`           | omite las relaciones tipadas (los enlaces de entidades compartidas se siguen escribiendo)                                                                                                                                                                                                                                                                                                                                                                                                               |
| `--include-tombstoned` | exporta también las filas eliminadas lógicamente, como `status: deprecated`; la que la lista no trae completa (un valor más allá de la vista previa, o una fila `high` con `--include-high`) se nombra y se deja fuera                                                                                                                                                                                                                                                                                   |
| `--limit N`            | como máximo las N filas más recientes (ejecuciones de prueba)                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `--title`, `--dry-run` | el título del índice raíz; planifica sin escribir                                                                                                                                                                                                                                                                                                                                                                                                                                                       |

<a id="lint"></a>

## Validación (lint)

`loom okf-lint DIR` lee el directorio del bundle y nada más: sin servidor, sin ClickHouse, sin red. Cada hallazgo incluye un id, una ruta, un mensaje y una severidad
(`server/memory/domain/okf_lint.py`).

| id                              | Severidad | Significado                                                                                                |
| ------------------------------- | --------- | ---------------------------------------------------------------------------------------------------------- |
| `frontmatter`                   | critical  | un concepto no tiene frontmatter, tiene YAML que no se puede analizar, un `type` vacío o un valor de `tags` que no es una lista |
| `required_keys`                 | critical  | falta `title`, `description`, `loom.memory_key` o `loom.memory_type`                                       |
| `relates_to`                    | critical  | una arista está mal formada o su destino `.md` no existe en el bundle                                      |
| `index_missing` / `okf_version` | critical  | un directorio con conceptos no tiene `index.md`; el índice raíz no declara `okf_version`                   |
| `staging`                       | critical  | existe un directorio `staging/`; staging nunca responde                                                    |
| `type_directory_mismatch`       | warning   | `loom.memory_type` no coincide con la carpeta en la que está el archivo                                    |
| `pointer_unmounted`             | warning   | una ruta de `relates_to` es un puntero `loom://org/namespace/key` a un concepto fuera del bundle           |
| `unknown_relation`              | warning   | un tipo de `relates_to` fuera de los verbos canónicos de Loom                                              |
| `edited_since_export`           | warning   | el cuerpo ya no produce el hash de `loom.content_sha256` (un concepto o chunk sin procesar editado a mano) |
| `derived_needs_confidence`      | warning   | `source_class: derived` con confianza 1.0: una inferencia escrita como un hecho                            |
| `high_sensitivity_present`      | warning   | viajó una fila de sensibilidad alta (`loom.sensitivity: high` o la etiqueta `sensitivity:high`)            |
| `index_listing`                 | warning   | un concepto que el índice de su directorio o el índice raíz no lista                                       |
| `drift`                         | warning   | el `loom.corpus_digest` del índice raíz ya no coincide con los archivos de conceptos                       |
| `orphans`                       | info      | conceptos que no enlazan a nada y a los que nada enlaza (una propiedad de los datos, no un defecto)       |

Los códigos de salida siguen lo que necesita un paso de compilación: **2** ante cualquier hallazgo critical, **1** si solo hay warnings, **0** si todo está limpio. `--strict` hace que los warnings también salgan con 2 (los orphans siguen siendo informativos); `--json` imprime el informe legible por máquina.

**El digest del corpus.** Cuando el `index.md` raíz incluye `loom.corpus_digest`, el lint lo recalcula: toma solo los archivos de conceptos, los ordena por ruta relativa al bundle, concatena el digest hexadecimal SHA-256 de cada archivo sin separador y calcula el SHA-256 de esa cadena. Una discrepancia produce el warning `drift`, que indica cuántos archivos de conceptos contiene el árbol frente a lo que dice el índice.

<a id="import"></a>

## Importación

`loom import-okf DIR --namespace NS` ejecuta primero el lint y rechaza un bundle con hallazgos críticos, salvo que se use `--force-import` (`--no-lint` omite esa comprobación). Después escribe cada concepto mediante `memory.set`, con la identidad tomada del token o de los flags `--agent`, `--user-id` y `--session-id`, sujeto a la detección de conflictos, la lista negra y el registro de auditoría. Al terminar, vuelve a crear las aristas `relates_to` con `memory.link` (`--no-links` omite este paso).

Antes de escribir, la importación lee las filas activas bajo su identidad y omite todo concepto cuya fila ya contiene lo que enviaría la escritura: el valor (`content_sha256`), el tipo, las etiquetas, la confianza, `valid_at`, `valid_to`, `source_trace` y `source_class` (la clase declarada o, en su defecto, `imported`), y las entidades cuando el bundle las incluye. También omite las aristas que ya están activas. Los conceptos que caen en la misma fila (una clave exportada para varios escritores) se escriben una sola vez: gana el actualizado más recientemente. Reenviar una fila idéntica contaría como un refuerzo, por lo que importar el mismo bundle dos veces no escribe ningún concepto ni arista la segunda vez, con dos excepciones: un concepto con `sources:`, que se escribe en cada ejecución porque las lecturas que hace la importación no devuelven su fuente, y un concepto que el destino fusiona con otra clave, que se envía de nuevo en cada ejecución y refuerza esa fila (se cuenta como fusionado). La última línea cuenta las filas del destino escritas, sin cambios, fusionadas y fallidas.

`source_trace` es `okf:<path inside the bundle>`, de modo que la ubicación del bundle en disco no lo cambia: cada ejecución puede exportar a un directorio nuevo.

Los directorios `evidence/` y `digests/` del bundle se vuelven a escribir mediante `memory.import_evidence` (`--no-evidence` los omite) y el servidor que importa los vuelve a generar como embeddings en cada importación, hayan cambiado o no: una importación programada usa `--no-evidence`.
Un fragmento o resumen cuyo texto se clasifica como alto (una credencial, un número de tarjeta, un SSN o un PIN declarado), o un fragmento que contiene parte de uno cortado en el límite entre fragmentos, se escribe sin vector y no se puede buscar
([sensibilidad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).

| Flag              | Efecto                                                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--verify`        | vuelve a atestar los conceptos cuyo frontmatter incluye una verificación `human:`, mediante `memory.verify`, **como el llamador que importa**                                           |
| `--keep-sessions` | escribe cada concepto bajo la sesión de su `loom.session_id`, de modo que una clave que vivía bajo varias sesiones sigue siendo varias filas; `--session-id` es el valor de respaldo     |
| `--dry-run`       | planifica las escrituras frente a las filas activas del destino (las lee) sin enviar ninguna escritura                                                                                  |
| `--prune`         | elimina, mediante `memory.delete_version`, las filas que escribió una importación anterior con este `--agent` y este token (un `source_trace` `okf:`) cuya clave ya no está en el bundle; requiere `--agent` |

Cada bundle importado en un espacio de nombres con `--prune` necesita su propio `--agent`: prune elimina las filas de ese agente con traza `okf:`, escritas por su token, que el bundle no contiene. `--prune` rechaza un bundle sin conceptos y uno cuyos archivos de conceptos no producen el hash de `loom.corpus_digest` del índice raíz, de modo que un bundle editado a mano nunca provoque eliminaciones. Un bundle cuyo índice raíz no incluye `loom.complete: true` se escribe pero no se poda. La exportación escribe la marca solo cuando leyó todas las filas: todas las filas que `memory.keys` lee en una sola sentencia tras listar, y todas las filas listadas volvieron de `memory.bulk_get`. Nunca la escribe cuando se detuvo en `--limit`, con `--include-tombstoned` (que conserva en el bundle las filas eliminadas) ni desde una versión anterior. Cada eliminación pasa por `memory.delete_version` con la versión que vio el listado de la importación con una traza `okf:`, bajo esa misma identidad: una fila reescrita desde entonces, o bloqueada con `memory.lock`, se conserva, y un destino demasiado antiguo para conocer la op no elimina nada. Para un prune, exporte a un directorio vacío: la exportación sobrescribe los archivos de conceptos pero nunca elimina el archivo de una clave que salió del espacio de nombres, así que un directorio `--out` reutilizado deja de coincidir con su digest. [Copiar un espacio de nombres a otro Loom](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) ejecuta la exportación y la importación de forma programada.

`--verify` nunca falsifica una atestación. El servidor decide el actor a partir del token; el verificador original del bundle se registra en las etiquetas de la memoria como procedencia.

<a id="ci-gate"></a>

## Puerta de CI

`integrations/okf-ci/okf-lint.yml` es un workflow de GitHub Actions para el repositorio
que contiene el bundle:

1. Cópielo en ese repositorio como `.github/workflows/okf-lint.yml`.
2. Defina `LOOM_REF` con un SHA de commit de Loom de 40 caracteres. El job rechaza
   cualquier otro valor (el archivo se distribuye con él vacío): las reglas están
   versionadas con Loom, y una rama o una etiqueta puede moverse sin que el check deje de
   estar en verde.
3. Defina `OKF_ROOT` con el directorio del bundle, el que contiene el `index.md` raíz
   (por defecto `.`): el bundle de un espacio de nombres, o una matriz sobre los espacios
   de nombres que contiene el repositorio.

El job hace checkout de Loom en `LOOM_REF` junto al bundle y ejecuta el lint con
`uv run --frozen`, de modo que los paquetes provienen del `uv.lock` de ese commit: un
`LOOM_REF` equivale a un conjunto de reglas y un conjunto de dependencias. Ejecuta el
lint con `--json`, imprime el resumen y los primeros cuarenta hallazgos (ordenados por
severidad, id y ruta) en el log del job, sube `okf-lint.json` como artefacto y falla con
el código de salida 2 o si el lint no llegó a ejecutarse (cualquier otro código de
salida). Las advertencias pasan, y el informe se conserva para el revisor.

Cada action está fijada a un SHA de commit completo, con su versión en un comentario. El
workflow solicita únicamente `contents: read`, y ninguno de los dos checkouts conserva el
token en la configuración de git (`persist-credentials: false`). El lint en sí no abre
ninguna conexión de red y no necesita ClickHouse. `integrations/okf-ci/README.md`
describe cada comprobación.

<a id="run-the-gate-locally"></a>

### Ejecutar la puerta en local

El mismo comando produce el mismo resultado:

```bash
git clone https://github.com/ClickHouse/loom .okf-lint-loom
git -C .okf-lint-loom checkout <LOOM_REF>
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT>          # human report
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT> --json   # the CI report
```

<a id="limits"></a>

## Límites

- **Un bundle es una instantánea.** Los cambios hechos en Loom después de la exportación no están en él hasta
  la siguiente exportación; vuelve a exportar antes de editar un bundle desactualizado.
- **Las eliminaciones solo viajan con `--prune`,** y únicamente para las filas que escribió la importación. Las aristas
  eliminadas del bundle no se borran.
- **Las filas de sensibilidad alta se quedan en origen** a menos que pases `--include-high`: una fila es alta
  por su campo `sensitivity` o por su etiqueta `sensitivity:high`. Una fila cuya sensibilidad la exportación
  no pudo leer, o leyó como un nivel que no conoce, también se queda en origen. Lo mismo ocurre con los fragmentos sin procesar
  y los resúmenes de sesión vinculados a dicha fila, y con cualquiera cuyo texto contenga un patrón de sensibilidad alta (una
  credencial, un número de tarjeta, un SSN o un PIN declarado; el texto sin procesar no tiene columna de sensibilidad, así que el
  detector que ejecuta `memory.set` decide, diga lo que diga `LOOM_MEMORY_SENSITIVITY_AUTODETECT`).
  El lint avisa cuando viajó una fila de sensibilidad alta.

<a id="related"></a>

## Relacionado

- [Escritura de memoria](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md) y
  [conflictos y rechazo](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md): por qué pasa una importación.
- [Historial y procedencia](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md).
- [Grafo de entidades](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md): las aristas tipadas que transporta `relates_to`.
