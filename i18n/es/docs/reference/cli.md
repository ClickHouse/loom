<!-- i18n: source=docs/reference/cli.md blob=ff91f4468115d6d9b0cb51b887479b6922e69b72 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md), which is authoritative.

<a id="cli-reference"></a>

# Referencia de la CLI

`loom` es el único punto de entrada de línea de comandos para ejecutar, mantener y
conectarse a un despliegue de Loom. Es el script de consola `loom` del paquete del
servidor (`pyproject.toml`, `[project.scripts]` → `server.cli:main`); el árbol del
parser vive en `server/cli/app.py` y hay un módulo por cada familia de comandos en
`server/cli/`. La tabla al final de esta página se genera a partir de ese árbol.

<a id="where-it-runs"></a>

## Dónde se ejecuta

| Dónde                                                       | Cómo                                                                  | Para qué usarlo                                                                                                       |
| ----------------------------------------------------------- | --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Dentro de la imagen del servidor                            | `docker exec loom-server loom <command>` (el contenedor web)          | Comandos de operador sobre la configuración propia del despliegue: `doctor`, `migrate`, `mint-token`, `reembed`.      |
| Dentro del contenedor de workers de un despliegue dividido  | `docker exec loom-workers loom <command>`                             | Mantenimiento de larga duración que quieres fuera del nivel web: `code backfill`, `code stats`.                       |
| Un checkout                                                 | `pip install -e .` desde la raíz del repositorio, luego `loom <command>` | `up`, `install`, `demo` y desarrollo contra un stack local.                                                        |

Dentro de un contenedor, la configuración es el entorno del contenedor. Desde un
checkout, `loom` primero carga el `.env` del directorio de trabajo (o el archivo
que indique `LOOM_ENV_FILE`; vacío para ninguno) en el entorno, tal como lo hace el
`env_file` de compose, sin sobrescribir las variables que tu shell ya exporta. Esto
importa para las claves de los proveedores: un comando de embeddings ejecutado desde un
checkout sin ellas recurriría al embedder falso y escribiría vectores inutilizables.

Los comandos se dividen en dos tipos:

- **Comandos de operador** leen `server/settings/` y hablan directamente con ClickHouse
  (`migrate`, `reembed`, `code backfill`, `revoke-token`, los backfills y
  las reparaciones). Se ejecutan con las credenciales del despliegue, así que ejecútalos
  donde residan esas credenciales.
- **Comandos de cliente** hablan con un servidor en ejecución por HTTP con una URL y un token (`install`,
  `init`, `demo`, `memory-search`, `ingest-transcript`, `export-okf`, `import-okf`). Aceptan
  `--server-url` (`--base-url` en `demo`) y, salvo `init`, `--token`, resueltos en el orden de los
  hooks del plugin: la URL desde `LOOM_BASE_URL`, luego `url` de `~/.loom/config.json`, o si no
  `http://localhost:7777`; el token desde `LOOM_API_KEY`, luego `token` de `~/.loom/config.json`,
  luego `~/.loom/token` (que se cambia con `LOOM_API_KEY_FILE`). `LOOM_CONFIG_FILE` cambia el archivo de configuración.
  Para un agente (`LOOM_CLIENT_ID` con un token de carga de trabajo) usan solo `--token` o
  `LOOM_API_KEY`, nunca un token guardado, y se niegan (código de salida 2) si no hay uno; `loom install` se niega
  diga lo que diga `--token`, `loom init` se niega antes de registrar nada, y `loom call`
  intercambia por sí mismo el token de carga de trabajo.

`loom mint-token` solo necesita `LOOM_JWT_SECRET`: firma un token localmente.

Los **comandos de sesión** (`login`, `logout`, `whoami`, `call`, `token`) son propios del
SDK de Python (`loom.cli`; `python -m loom …` con solo el SDK instalado), así que `loom`
necesita `pip install clickhouse-loom` para ellos. `loom login [--server URL]
[--profile NAME] [--no-browser] [--org ORG [--wait S]]` inicia sesión con un código de un solo uso,
con `--org` en la [celda](../architecture/cells.md) de esa organización, tal como la indica la búsqueda del router;
`loom call <op|/path> [key=text|key:=json …] [--data JSON] [--org] [--namespace]` llega a
cualquier operación o ruta con esa sesión, y `loom token create|list|revoke`
crea, lista y revoca tus propios tokens para CI y agentes. Consulta
[usar Loom sin el dashboard](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md).

<a id="conventions"></a>

## Convenciones

**Simulación por defecto en las reescrituras.** Un comando que reescribe filas almacenadas
informa de lo que escribiría, a menos que se le pase `--apply`; `--dry-run` se
acepta y nombra el comportamiento por defecto. Los dos flags son mutuamente excluyentes
(`server/cli/_apply.py`, `add_apply_flags`). Una simulación correcta termina con
`dry run: nothing was written; re-run with --apply to write` en stderr, de modo que la
salida `--json` de un comando quede sola en stdout. Algunos comandos expresan la
misma idea con flags propios (`loom entity-edges-invalidate-junk` acepta
`--dry-run` (el valor por defecto) o `--apply`; `loom reembed --dry-run` cuenta filas
sin escribir); la tabla siguiente enumera los flags de cada comando.

**Cada escritura nombra a su principal.** Las escrituras de un comando se marcan con el
actor `process:cli:<command>` (`server/memory/domain/provenance.py`, `cli_actor`), de modo que
la procedencia y el registro de auditoría indican qué comando escribió una fila.

**Cada consulta está atribuida.** Las consultas de ClickHouse de un comando llevan la
etiqueta `log_comment` `cli.<command>[.<subcommand>]`, y sus lecturas se ejecutan en la
clase de tráfico `background` a menos que el comando acepte su propio `--traffic-class`.
`loom serve` es la excepción: es el servidor, y sus solicitudes y pasadas de workers
se etiquetan por sí mismas.

**`--json`** en los comandos que lo ofrecen imprime un único resultado legible por máquina
en stdout y mantiene el informe para personas en stderr.

<a id="common-commands"></a>

## Comandos comunes

<a id="run-and-check-a-deployment"></a>

### Ejecutar y comprobar un despliegue

```bash
loom up                          # from a checkout: create .env, boot Loom, its extractor and ClickHouse, print how to connect
docker exec loom-server loom doctor          # pre-boot checks + the /v1/health problem list, each with its fix
docker exec loom-server loom doctor --json   # the same, machine-readable
loom doctor --client             # also examine this machine as a coding-agent client
docker exec loom-server loom migrate --status              # applied and pending migrations
docker exec loom-server loom migrate --dry-run --strict    # what a boot would apply; exit 1 on ledger drift
```

`loom up` espera a `/v1/health` (hasta `--timeout`, por defecto 300 s), genera un
token de escritor cuando la autenticación está activada e imprime la URL del
dashboard y los fragmentos para las primeras llamadas; `--no-build` reutiliza la
imagen en caché, y un stack que ya responde a la comprobación de salud se deja
como está salvo que pases `--rebuild`. `loom doctor` termina con código 1 si
falla alguna comprobación. `loom serve` ejecuta el plano de control bajo uvicorn
para uso local (la imagen lo inicia mediante `scripts/serve.sh`). Las migraciones
y las actualizaciones se tratan en
[Actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md); las comprobaciones de salud, en
[Monitorización](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md).

<a id="tokens"></a>

### Tokens (tokens)

```bash
# A writer token for one namespace, valid 30 days, printed on stdout
docker exec loom-server loom mint-token --subject my-bot --org dev \
  --namespaces local --roles writer --ttl-days 30

# A scoped token: per-namespace roles, in read-priority order
loom mint-token --subject triage --org acme \
  --namespace-role priv=admin --namespace-role pub=viewer --json

# Retire one token, or every token of a subject issued before an instant
docker exec loom-server loom revoke-token --jti <jti> --reason "leaked"
docker exec loom-server loom revoke-token --subject my-bot --issued-before 2030-01-01T00:00:00Z
```

A una revocación permanente no se le puede asignar una caducidad ni se puede
restringir para restaurar los tokens retirados antes de ella. Elige una fecha de
corte igual o posterior a la existente, o bien el momento de la revocación
permanente más el margen de tolerancia de desfase de reloj del token.

`--save` escribe el token en `~/.loom/token` (o en una ruta que indiques) en
lugar de imprimirlo. Sin `--ttl-days`, el token no caduca y solo se puede retirar
con `loom revoke-token`. `--read-scope own_user` limita las lecturas al
`user_id` que indica cada solicitud; `--consumer NAME` asocia la
[etiqueta de consumidor](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#the-consumer-tag) a la credencial. Consulta
[Tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="connect-a-client"></a>

### Conectar un cliente

```bash
loom install --client cursor --token "$LOOM_API_KEY" --server-url https://loom.example.com
loom install --client claude-code --toolset coding+tracker --dry-run
loom init --agent triage-bot --server-url https://loom.example.com   # self-signup: a namespace + token
```

`loom install` escribe la entrada del servidor MCP en la configuración de un
cliente (o ejecuta `claude mcp add` / `codex mcp add`); sin `--client` configura
todos los clientes compatibles que encuentre. Consulta [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md).
`loom init` guarda el token en `~/.loom/token` y el servidor, la organización y el
espacio de nombres en `~/.loom/config.json`, el archivo que leen los comandos del
cliente y los hooks del plugin.

<a id="re-embed-after-changing-the-embedder"></a>

### Regenerar embeddings tras cambiar el embedder

```bash
docker exec loom-server loom reembed --dry-run          # count rows whose embedding_model is not the configured one
docker exec loom-server loom reembed --namespace support
docker exec loom-server loom reembed --delete-old        # re-embed, then delete the off-model rows
```

Loom nunca regenera embeddings por sí mismo: tras cambiar `LOOM_EMBEDDING_MODEL`,
ejecuta `loom reembed`. Regenera los vectores a partir del contenido almacenado y
conserva el resto de las columnas. `--in-database <named collection>` calcula los
embeddings dentro de ClickHouse con `aiEmbed` (ClickHouse 26.7 o posterior) como
un único `INSERT … SELECT` por tabla; se rechaza mientras haya definida una
cabecera de cliente o de identidad, o cuando se exija la pasarela de inferencia;
`--resume` continúa después de la fila que indica la línea `resume:` de una
ejecución anterior. Consulta
[Mantenimiento](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md).

<a id="index-a-repository"></a>

### Indexar un repositorio

```bash
docker exec loom-workers loom code init --org acme --namespace code-myrepo
docker exec loom-workers loom code backfill --org acme --namespace code-myrepo --embed
docker exec loom-workers loom code stats --org acme --namespace code-myrepo
```

`code backfill` se puede reanudar. `--phase index` analiza, divide en fragmentos y
deriva símbolos y aristas; `--phase embed` rellena los vectores pendientes;
`--embed` hace ambas cosas en una sola ejecución. Se niega a generar embeddings
con el embedder falso salvo que pases `--allow-fake-embedder` (solo para
desarrollo). `--org` y `--namespace` toman por defecto `LOOM_CODE_ORG` y
`LOOM_CODE_NAMESPACE`. El procedimiento completo, el nivel del compilador y los
backfills del historial de PR están en
[Operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).

<a id="export-and-import"></a>

### Exportar e importar

```bash
loom export-okf --namespace support --out ./support-okf            # a namespace as a Markdown bundle
loom okf-lint ./support-okf --strict                               # offline lint; exit 2 on a critical finding or, with --strict, a warning
loom import-okf ./support-okf --namespace support-copy
loom export-actions --format cedar -o loom.cedarschema             # the registry as a policy schema
```

`export-okf` deja fuera las filas con `sensitivity=high`, o sin sensibilidad
conocida, salvo que pases `--include-high`, porque un repositorio espejo tiene una
audiencia más amplia que los permisos del espacio de nombres. Los fragmentos sin
procesar y los resúmenes vinculados a una fila así, y el texto sin procesar que
contenga una forma de sensibilidad alta (una credencial, un número de tarjeta, un
SSN o un PIN indicado), se quedan con ella; `--include-high` los libera, y el
servidor audita cada elemento liberado. Consulta [OKF](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md) y
[Exportar el esquema de acciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#exporting-the-action-schema).

<a id="diagnose-and-repair"></a>

### Diagnosticar y reparar

`loom report` imprime el campo Environment del formulario de errores con la salida
de `loom doctor`; `--open` abre el formulario con las versiones ya rellenadas.
`loom drops` imprime el registro de descartes de ingesta de memoria de un espacio
de nombres. `loom erasure-re-verify` vuelve a comprobar las sagas de borrado que
fallaron. Los comandos de backfill y reparación
(`integrity-repair`, `reconcile-status`, `repair-value-bytes`,
`entity-edges-invalidate-junk`, `rechunk`, `lexical-stats-rebuild`, …) y cuándo
ejecutar cada uno están en [Mantenimiento](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md) y
[Solución de problemas](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md).

<a id="reading-the-table"></a>

## Cómo leer la tabla

Cada fila es un comando o subcomando: su invocación completa, la primera frase
de su texto de ayuda y sus argumentos. Los argumentos posicionales se muestran como
`<name>`; las opciones, con su forma larga. `loom <command> --help` imprime el texto
completo, los valores por defecto y las opciones disponibles. El generador
(`scripts/codegen/docs_reference.py`, `render_cli`) lee la instantánea del parser
registrada en el repositorio, `tests/unit/snapshots/cli_tree.json`.
`tests/unit/test_cli_surface_snapshot.py` mantiene esa instantánea y
`tests/unit/snapshots/cli_help.json` iguales al parser real, de modo que un flag nuevo
hace fallar la CI hasta que se regeneren ambas instantáneas y esta página:

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
python scripts/codegen/docs_reference.py
```

<a id="commands"></a>

## Comandos

<!-- BEGIN GENERATED: cli -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

| Comando | Resumen | Argumentos y flags |
| --- | --- | --- |
| `loom code` | Operaciones del plano de inteligencia de código. |  |
| `loom code init` | Crea el clon bare y el estado inicial. | `--org`, `--namespace` |
| `loom code backfill` | Indexación del árbol completo (reanudable). | `--org`, `--namespace`, `--phase`, `--embed`, `--include-poisoned`, `--allow-fake-embedder`, `--no-fetch`, `--limit`, `--commit`, `--paths-file`, `--concurrency`, `--edges-second-pass`, `--no-edges-second-pass`, `--dialect-folder-pass`, `--embedding-model-suffix`, `--context-callees`, `--callers-from` |
| `loom code reconcile` | Comprobación de obsolescencia basada en el contenido: rutas cuyos chunks son anteriores a su último cambio en el head de la rama; --heal las vuelve a ingerir (la marca de agua no cambia). | `--org`, `--namespace`, `--heal`, `--limit`, `--max-commits`, `--no-fetch`, `--show` |
| `loom code compact` | Ejecuta OPTIMIZE ... FINAL sobre una tabla de vectores de código cuando sus copias físicas por clave alcanzan --min-ratio, y espera a que termine la fusión. | `--table`, `--min-ratio`, `--apply`, `--dry-run`, `--timeout-s` |
| `loom code reembed` | Escribe los chunks vivos del espacio de nombres bajo la etiqueta de un segundo modelo de embeddings: dentro de la base de datos, reanudable, sin checkout. | `--org`, `--namespace`, `--embedding-model`, `--source-model`, `--prefix`, `--batch` |
| `loom code vector-twin` | Rellena dentro de ClickHouse el gemelo de prefijo Matryoshka de los vectores de chunks de un espacio de nombres: sin re-embedding, reanudable. | `--org`, `--namespace`, `--embedding-model`, `--all-namespaces` |
| `loom code crossref` | Pasada de referencias de pruebas SQL/shell -> C++ que las implementa (escribe code_test_map method=content_reference; idempotente). | `--org`, `--namespace` |
| `loom code coverage-import` | Importa la ejecución más reciente de cobertura de líneas por prueba de la fuente configurada (escribe code_test_map method=coverage y code_coverage_files). | `--org`, `--namespace`, `--dry-run` |
| `loom code export-pairs` | Exporta pares de recuperación (consulta -> unidad usada, negativos difíciles) a partir de tráfico real (join de trace x outcome) para el ajuste fino de embedders y rerankers. | `--org`, `--namespace`, `--days`, `--out` |
| `loom code backfill-prs` | Historial de PRs fusionados desde GitHub. | `--org`, `--namespace`, `--months`, `--max-prs` |
| `loom code repack` | Regenera los paquetes de contexto de los PRs fusionados recientemente con la forma de paquete actual. | `--org`, `--namespace`, `--days`, `--limit`, `--dry-run` |
| `loom code describe` | Descripciones contextuales de una línea generadas por LLM para los chunks con mayor PageRank. | `--org`, `--namespace`, `--top-pct`, `--limit`, `--suffix`, `--concurrency` |
| `loom code answers-purge` | Marca como eliminadas las respuestas de code.ask en caché (tras actualizar el modelo de síntesis, las respuestas obsoletas de un modelo más débil se seguirían sirviendo indefinidamente: la caché solo se invalida cuando cambia un archivo citado). | `--llm-model`, `--all`, `--apply`, `--dry-run`, `--org`, `--namespace` |
| `loom code scip-full` | Reindexación completa del nivel del compilador a mano (el worker code_scip_full la ejecuta semanalmente por defecto: corrige las degradaciones por el límite de TU de los deltas por merge y cualquier deriva). | `--checkout`, `--jobs`, `--keep-delta`, `--apply-delta`, `--org`, `--namespace` |
| `loom code scip-lock-path` | Imprime el archivo de bloqueo del nivel del compilador de la entrada en este host. | `--org`, `--namespace` |
| `loom code entry-lock` | Ejecuta un comando bajo el bloqueo del nivel del compilador de la entrada (el archivo de este host y el lease del despliegue): termina con 75 cuando otra pasada lo tiene, y con 3 si el lease se pierde durante la ejecución (el comando se detiene). | `--org`, `--namespace`, `--tier`, `<command>` |
| `loom code scip-delta` | Una pasada delta del nivel del compilador (el mismo código que el worker code_scip_delta), a mano: git_head frente a scip_commit por cada repositorio configurado, scip-clang sobre las TU modificadas y aplicación. |  |
| `loom code entries` | Una línea por repositorio configurado: org namespace checkout archivo de bloqueo (la ruta del bloqueo resuelta; una ejecución manual lo toma mediante `loom code entry-lock`). |  |
| `loom code warm` | Precarga la caché de respuestas de code.ask a partir de una lista de preguntas (una pregunta por línea, o JSONL con un campo 'question'). | `<file>`, `--org`, `--namespace` |
| `loom code scip-retry` | Devuelve a pendiente un lote SCIP fallido. | `--org`, `--namespace`, `<batch_id>` |
| `loom code backfill-reviews` | Comentarios de revisión en línea de los PRs fusionados que ya están en code_pr_history y aún no tienen ninguno almacenado (primero los más recientes, una petición a GitHub por PR). | `--org`, `--namespace`, `--prs` |
| `loom code backfill-assignees` | Rellena code_pr_history.assignees (a quién asignó el repositorio la revisión) para los PRs fusionados ya almacenados, recorriendo los PRs cerrados más recientes de 100 en 100 por petición. | `--org`, `--namespace`, `--pages` |
| `loom code backfill-items` | Rellena code_gh_items (issues y PRs, en cualquier estado) actualizados desde una fecha, 100 por petición, empezando por la actualización más antigua; avanza gh_items_cursor. | `--org`, `--namespace`, `--since`, `--pages` |
| `loom code stats` | Marcas de agua y número de filas vivas. | `--org`, `--namespace` |
| `loom code export-matrix` | Matriz de mantenimiento a partir del co-cambio en PRs fusionados: «cuando cambia este archivo, normalmente cambian también estos», con evidencia, en markdown para un archivo de instrucciones (AGENTS.md / copilot-instructions.md). | `--org`, `--namespace`, `--prs`, `--min-together`, `--min-conf`, `--max-partners`, `--max-rows`, `--out`, `--json` |
| `loom code lint-instructions` | Archivos de instrucciones para agentes (CLAUDE.md, AGENTS.md, copilot-instructions, .claude/.md, skills) cuyas referencias a rutas o símbolos ya no se resuelven en el head del clon. | `--org`, `--namespace`, `--file`, `--glob`, `--symbols`, `--all`, `--json`, `--fail-on-stale` |
| `loom decisions` | Registros de decisiones extraídos de documentos. |  |
| `loom decisions extract` | Extrae registros de decisiones de un documento de conocimiento: el modelo lee el documento completo (o en ventanas de secciones completas a partir de LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS; un encabezado solo sobre una imagen y… | `--org`, `--namespace`, `--source-uri`, `--repo`, `--into`, `--max-per-section`, `--window-chars`, `--apply`, `--dry-run` |
| `loom knowledge` | Documentos de conocimiento y sus fuentes. |  |
| `loom knowledge sync` | Ejecuta ahora una pasada de sincronización sobre las fuentes de documentos de un espacio de nombres de repositorio: lista los archivos modificados de cada fuente (todos con --full), escribe los modificados como documentos y retira aquellos que un listado completo no… | `--org`, `--namespace`, `--source`, `--full` |
| `loom knowledge source` | Las fuentes de documentos de un espacio de nombres, sobre un servidor en ejecución. |  |
| `loom knowledge source list` | Lista las fuentes del espacio de nombres, sus elementos y su estado de sincronización. | `--namespace`, `--org`, `--server-url`, `--token` |
| `loom knowledge source add` | Registra una fuente a partir de enlaces o elementos, probada con la credencial antes de almacenar nada (knowledge.source.add). | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--kind`, `--link`, `--item-json`, `--include`, `--credential-file`, `--dry-run` |
| `loom knowledge source update` | Añade y elimina elementos de una fuente o cambia sus tipos de archivo; los elementos añadidos se prueban con la credencial almacenada (knowledge.source.update). | `--namespace`, `--org`, `--server-url`, `--token`, `--name`, `--link`, `--item-json`, `--include`, `--remove-link`, `--remove-item-json` |
| `loom knowledge migrate-originals` | Mueve a LOOM_DOCUMENT_S3_BUCKET los originales de documentos que aún están en ClickHouse y que un documento, una carga o una ingesta necesita: cada uno se copia a su propio objeto, se verifica con hash y se conmuta, protegido por… | `--org`, `--namespace`, `--dry-run` |
| `loom export-okf` | Renderiza un espacio de nombres como un bundle OKF v0.2 (Markdown + frontmatter): la proyección de la memoria de Loom revisable en git. | `--server-url`, `--token`, `--org`, `--namespace`, `--out`, `--title`, `--include-tombstoned`, `--limit`, `--dry-run`, `--no-evidence`, `--no-links`, `--include-high` |
| `loom import-okf` | Escribe los conceptos de un bundle OKF v0.2 en un espacio de nombres mediante memory.set. | `<path>`, `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--session-id`, `--keep-sessions`, `--verify`, `--dry-run`, `--no-evidence`, `--no-lint`, `--force-import`, `--no-links`, `--prune` |
| `loom okf-lint` | Valida sin conexión un directorio de bundle OKF: la puerta de compilación que ejecuta el CI de un repositorio espejo. | `<path>`, `--json`, `--strict` |
| `loom up` | Arranca el stack local (Loom, su extractor de documentos y ClickHouse) e imprime cómo conectarse: crea .env y la configuración de ClickHouse a partir de sus plantillas, ejecuta docker compose up --build, espera a… | `--root`, `--file`, `--env-file`, `--no-build`, `--rebuild`, `--port`, `--timeout`, `--subject`, `--org`, `--namespace`, `--open` |
| `loom down` | Detiene el stack local conservando los datos; --delete-data realiza un reinicio confirmado. | `--root`, `--file`, `--env-file`, `--delete-data`, `-y` |
| `loom ch-setup` | Prepara un ClickHouse autogestionado o un servicio de ClickHouse Cloud para Loom: crea la base de datos loom y el usuario lector de solo lectura, o imprime el SQL para un administrador. | `--print-sql`, `--print-analyst-sql` |
| `loom participant` | Los procesos de Loom que espera un cambio de proveedor. |  |
| `loom participant retire` | Retira el participante de proveedor de un proceso que murió sin apagarse (su id es `participant_id` en el /v1/health de ese proceso). | `--participant`, `--yes` |
| `loom deployment` | La configuración con la que se comprueba cada réplica. |  |
| `loom deployment publish` | Publica la configuración de este env como la del despliegue, reemplazando a propósito su secreto raíz o LOOM_EMBEDDING_MODEL/DIM. | `--yes`, `--check` |
| `loom settings` | El valor vigente de cada ajuste y las sobrescrituras de los ajustes en vivo. |  |
| `loom settings list` | Cada ajuste: valor, de dónde viene y si es en vivo. | `--prefix`, `--changed`, `--json` |
| `loom settings set` | Sobrescribe un ajuste en vivo para todo el despliegue; sobrevive a un reinicio. | `<name>`, `<value>`, `--expect` |
| `loom settings reset` | Elimina la sobrescritura: vuelve al env o al valor por defecto. | `<name>`, `--expect` |
| `loom settings history` | Cambios registrados, del más reciente al más antiguo: quién, el valor anterior y el posterior. | `<name>`, `--limit`, `--json` |
| `loom secrets` | Credenciales y claves de firma almacenadas selladas en ClickHouse. |  |
| `loom secrets reseal` | Vuelve a sellar con LOOM_JWT_SECRET lo que solo abre LOOM_JWT_SECRET_PREVIOUS. | `--dry-run`, `--check` |
| `loom signing-key` | Las claves de firma de tokens junto a LOOM_JWT_SECRET, guardadas en ClickHouse. |  |
| `loom signing-key rotate` | Añade una clave; firma en cuanto todos los procesos la hayan cargado. | `--algorithm`, `--activate-in` |
| `loom signing-key retire` | Hace que una clave (o 'default') deje de verificar tokens. | `<kid>` |
| `loom signing-key record-root` | Registra este LOOM_JWT_SECRET como el secreto raíz del despliegue, tras reemplazarlo a propósito (las claves selladas con el anterior quedan inutilizables). |  |
| `loom signing-key list` | Lista las claves. |  |
| `loom provider-lock` | El bloqueo de configuración del proveedor. |  |
| `loom provider-lock clear` | Libera el bloqueo de configuración del proveedor de un titular cuyo proceso se detuvo (se rechaza mientras renueve, envíe heartbeats o tenga una consulta en ejecución). | `--holder` |
| `loom export-analytics` | Escribe en S3 como Parquet un día UTC de uso y una instantánea de espacios de nombres, con los nombres hasheados bajo LOOM_ANALYTICS_EXPORT_NAME_KEY. | `--bucket`, `--prefix`, `--day`, `--role-arn`, `--region`, `--expected-bucket-owner` |
| `loom sql-access` | Crea, reajusta el alcance de o elimina un usuario de ClickHouse que lee la memoria, las conversaciones y el registro de peticiones de un tenant a través de las vistas sql_v1_*. |  |
| `loom sql-access grant` | Hace que el usuario lea exactamente este alcance, creándolo (e imprimiendo su contraseña una sola vez) si no existe. | `--user`, `--org`, `--namespaces`, `--all`, `--rotate` |
| `loom sql-access revoke` | Quita el alcance del usuario y luego elimina el usuario. | `--user` |
| `loom sql-access list` | Cada fila de alcance concedido, por usuario SQL. |  |
| `loom reembed` | Vuelve a generar los embeddings de los vectores cuyo embedding_model != el embedder configurado (ejecútalo tras cambiar LOOM_EMBEDDING_MODEL: Loom no vuelve a generar embeddings automáticamente). | `--org`, `--namespace`, `--batch-size`, `--in-database`, `--dry-run`, `--delete-old`, `--key-only`, `--resume` |
| `loom reextract` | Reproduce la evidencia en bruto de un espacio de nombres (turnos de memory.set_from_messages) a través de la ruta de escritura de un servidor en ejecución hacia OTRO espacio de nombres: el extractor actual sobre las transcripciones almacenadas; la fuente es… | `--org`, `--namespace`, `--into`, `--base-url`, `--token`, `--dates`, `--concurrency`, `--limit-sessions`, `--dry-run` |
| `loom deploy-delta` | Qué cambió un intercambio de colores: las filas del registro del despliegue (volumen, errores, p50/p95, gasto upstream por operación) y las formas de sentencias de ClickHouse (ejecuciones, bytes, segundos) para una ventana a cada lado… | `--swap-at`, `--window-min`, `--ch-os-user`, `--cluster`, `--top`, `--json` |
| `loom entity-export` | Exporta el grafo de entidades de un espacio de nombres (aristas de co-ocurrencia y tripletas) a GraphML o JSON-LD para una herramienta externa de grafos o RDF. | `--org`, `--namespace`, `--format`, `--output`, `--all` |
| `loom audit-namespace` | Cómo se está usando un espacio de nombres (namespace.audit): lecturas por clave exacta frente a búsquedas, claves hash, valores con doble codificación o demasiado grandes, filas de índice hechas a mano, leases guardados como memorias, rotación de versiones. | `--org`, `--namespace`, `--window-days`, `--samples`, `--json` |
| `loom mentor` | El consejo del almacén a un agente (mentor.brief): qué llamó, los planos que ofrece su espacio de nombres, las sugerencias con su evidencia y con qué frecuencia se siguieron las sugerencias pasadas. | `--org`, `--namespace`, `--agent`, `--window-days`, `--json` |
| `loom serve` | Arranca el plano de control (uvicorn). | `--host`, `--port`, `--log-level`, `--reload` |
| `loom migrate` | Aplica las migraciones de esquema sobre el CH configurado. | `--status`, `--dry-run`, `--strict`, `--target`, `--migrations-dir` |
| `loom reconcile-status` | Vuelve a emitir las filas de memory_embeddings cuyo estado divergió de loom.memory (marcas de superseded / conflicted / duplicate que el pool nunca vio), comparadas y reemitidas por fila de identidad (org, namespace, agent… | `--org`, `--namespace`, `--all-namespaces`, `--since-days`, `--exclude`, `--limit`, `--apply`, `--dry-run` |
| `loom period-digest` | Escribe resúmenes de período: una memoria semántica derivada a partir de K resúmenes de sesión consecutivos de cada identidad en un espacio de nombres. | `--org`, `--namespace`, `--sessions`, `--max-identities`, `--max-periods`, `--apply`, `--dry-run`, `--rewrite` |
| `loom demote-activity-narration` | Encuentra los hechos extraídos de un espacio de nombres que narran actividad de herramientas ('User read lines 1-1000 of X', 'Claude ran a command on …') sin un resultado duradero y los degrada (confidence x0.25) o los etiqueta mediante… | `--org`, `--namespace`, `--action`, `--classifier`, `--days`, `--limit`, `--apply`, `--dry-run` |
| `loom recalibrate` | Vuelve a sondear el embedder y ejecuta de inmediato la calibración por NS (de lo contrario se ejecuta semanalmente mediante el worker de recalibración). |  |
| `loom import-clickgap` | Importa a Loom una exportación SQLite de ClickGap (Detector→Validator→Reviewer) (lecciones, entidades tipadas y categorías). | `--sqlite`, `--org`, `--namespace`, `--agent`, `--dry-run`, `--batch-size`, `--seed-recall-events` |
| `loom import-recall` | Importa a Loom una exportación SQLite de memoria de agente con recuperación vectorial externa. | `--sqlite`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom import-brain` | Importa a Loom un directorio markdown de «brain» de agente: MEMORY.md (capa HOT siempre inyectada) → secciones de profile.set; memory/contexts/*.md (capa WARM de lectura bajo demanda) → documentos de knowledge.add. | `--dir`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom claim-release` | Libera un claim de autoregistro: revoca sus permisos, borra el propietario y lo desactiva. | `--org`, `--namespace`, `-y` |
| `loom mint-token` | Emite un JWT localmente (requiere LOOM_JWT_SECRET). | `--subject`, `--org`, `--namespaces`, `--roles`, `--platform-admin`, `--namespace-role`, `--ttl-days`, `--save`, `--read-scope`, `--read-user-id`, `--traffic-class`, `--consumer`, `--service-account`, `--user-token`, `--json` |
| `loom revoke-token` | Revoca un token por jti o por subject (escribe loom.revoked_tokens a través del módulo de revocación; necesita el ClickHouse configurado). | `--jti`, `--subject`, `--list`, `--issued-before`, `--scim`, `--reason`, `--expires-at`, `--json` |
| `loom sso` | Traslada las cuentas con contraseña al inicio de sesión único. |  |
| `loom sso adopt` | Permite que los inicios de sesión SSO lleguen a las cuentas con contraseña cuyo correo está en --domain: cada persona inicia sesión con el proveedor de identidad en su propia cuenta, sus espacios de nombres y sus datos. | `--domain`, `--org`, `--skip`, `--keep-tokens`, `--apply`, `--dry-run` |
| `loom sso link` | Permite que los inicios de sesión SSO de una persona lleguen a su cuenta con contraseña registrada bajo otra dirección (una personal, un +tag). | `--email`, `--org`, `--namespace`, `--apply`, `--dry-run` |
| `loom sso unlink` | Deshace los vínculos de un correo: sus concesiones dejan de admitir las cuentas. | `--email`, `--apply`, `--dry-run` |
| `loom sso password-link` | Imprime un enlace de un solo uso (válido durante LOOM_PASSWORD_LINK_TTL_SECONDS, un día por defecto) que establece la contraseña de las cuentas SSO de un correo, para cuando se desactiva el inicio de sesión único. | `--email`, `--org` |
| `loom bot` | Bots y trabajos de CI que inician sesión con una clave o con la identidad de su CI. |  |
| `loom bot create` | Registra un bot: sus espacios de nombres y una clave (--generate-key o --public-key) y/o una confianza de CI (--issuer, --audience, --claim). | `--name`, `--org`, `--namespace-role`, `--generate-key`, `--public-key`, `--issuer`, `--audience`, `--claim`, `--jwks-uri`, `--max-token-age`, `--max-access-token-ttl`, `--max-agents`, `--agent-claim`, `--platform-admin`, `--replace` |
| `loom bot list` | Todos los bots, activos o deshabilitados. |  |
| `loom bot disable` | Rechaza sus pruebas y revoca los tokens que posee. | `--name` |
| `loom doctor` | Diagnostica configuraciones erróneas previas al arranque (versión de Python, ajustes, JWT, accesibilidad de CH) e imprime la lista de problemas que lleva /v1/health, cada uno con su solución. | `--json`, `--client`, `--server-url`, `--token` |
| `loom report` | Imprime el campo Environment del formulario de errores: versiones y las comprobaciones de `loom doctor`. | `--open` |
| `loom drops` | Imprime el registro de descartes de ingesta de memoria (loom.memory_drops) de un espacio de nombres, por etapa/motivo: lo que el pipeline descartó, truncó o abandonó. | `--namespace`, `--org`, `--hours` |
| `loom namespaces-gc` | Lista (por defecto) o borra espacios de nombres desechables —ejecuciones de bench terminadas bajo los prefijos de bench que nadie registró y en los que nadie ha escrito durante --idle-hours— en una saga de borrado por lista por… | `--org`, `--prefix`, `--namespace`, `--exclude-prefix`, `--idle-hours`, `--include-registered`, `--erase`, `--no-purge`, `--skip-external`, `--batch`, `--show`, `--requester`, `--worker-pass` |
| `loom erasure-re-verify` | Vuelve a verificar las sagas de borrado fallidas: imprime el último motivo, con el nombre del paso, de cada una y un nuevo censo de residuos (por defecto, solo lectura); --resume las vuelve a ejecutar desde su punto de control más allá del worker de recuperación… | `--org`, `--id`, `--resume`, `--skip-external`, `--completed-since`, `--orphan-graph`, `--erase`, `--batch`, `--show`, `--requester` |
| `loom reconcile` | Tras una restauración de ClickHouse o Postgres, vuelve a ejecutar en el almacén restaurado los borrados y las redacciones que los demás registros tienen después del punto de restauración y (tras una restauración de ClickHouse) vuelve a publicar cada… | `--restored`, `--point`, `--allow-live`, `--apply`, `--dry-run` |
| `loom router` | El router frente a las celdas de un despliegue. |  |
| `loom router serve` | Sirve las API de ubicación, consulta y operador; enruta las demás solicitudes a las celdas. | `--host`, `--port`, `--log-level` |
| `loom router migrate` | Aplica las migraciones pendientes del directorio. |  |
| `loom cell` | Opera las celdas de un despliegue a través del directorio del router. |  |
| `loom cell credential` | Crea una credencial de router o rota la de una celda. | `<action>`, `--cell`, `--next`, `--finish`, `--revert`, `--operator`, `--router` |
| `loom cell limit` | Cierra, reabre o limita una celda para nuevas organizaciones. | `--cell`, `--closed`, `--open`, `--placements-max`, `--router` |
| `loom cell which` | Imprime la celda y el host que devuelve la consulta para una organización (la primera celda si no está ubicada). | `--org`, `--router` |
| `loom cell remove` | Elimina la fila del directorio de una celda que se conecta sin ubicación. | `--cell`, `--router` |
| `loom cell prepare` | Registra esta celda en el directorio del router y escribe sus filas. | `--cell`, `--host`, `--credential-sha256`, `--probe-url`, `--wait`, `--router` |
| `loom tracker-probe` | Primer contacto de solo lectura con la fuente del tracker: recuentos y marcas de tiempo más recientes por tabla de origen y por credencial, estados de los interruptores y los espacios de nombres del tracker configurados. |  |
| `loom tracker-contract-check` | Solo lectura: ¿el esquema de cada fuente del tracker cumple el contrato de registros (docs/features/tracker/record-contract.md)? | `--source`, `--namespace`, `--json` |
| `loom tracker-dupcheck-fit` | Reajusta ahora la confianza calibrada de tracker.duplicate_check para un espacio de nombres del tracker: reproduce el patrón oro de duplicados en el momento de la creación, ajusta y almacena el ajuste. | `--namespace`, `--org`, `--negatives`, `--dry-run`, `--out` |
| `loom tracker-embed-reset` | Borra los cursores de pasada de tracker_embed de un espacio de nombres del tracker para que la siguiente pasada recorra de nuevo la ventana y vuelva a generar embeddings de cada elemento cuya receta de texto cambió. | `--namespace`, `--org`, `--kind`, `--apply`, `--dry-run` |
| `loom namespaces-adopt` | Asigna una fila de propietario a los espacios de nombres que contienen memoria activa y no tienen fila en el registro: kind=bench bajo los prefijos de bench, si no memory; retention_days 0, sin TTL. | `--org`, `--owner`, `--prefix`, `--namespace`, `--kind`, `--apply`, `--dry-run`, `--show` |
| `loom sensitivity-sweep` | Etiqueta las filas activas que el detector de sensibilidad habría etiquetado al escribir pero que una ruta omitió. | `--org`, `--namespace`, `--since-days`, `--limit`, `--show`, `--only-shape`, `--apply`, `--dry-run` |
| `loom integrity-repair` | Lee los indicadores de integridad a nivel de fila (filas dim-0 con un vector, desajuste de tombstoned_at/status, filas activas pasada su valid_to) y, con --apply, vuelve a emitir las filas dim-0 con su dimensión real para que la búsqueda… | `--org`, `--namespace`, `--limit`, `--show`, `--apply`, `--dry-run` |
| `loom rechunk` | Vuelve a dividir en fragmentos y a generar embeddings de los documentos de conocimiento de un espacio de nombres a partir del contenido almacenado, con el chunker actual, de modo que cada documento lleve metadatos de sección, contexto del fragmento y frecuencia de documento. | `--org`, `--namespace`, `--source-uri`, `--apply`, `--dry-run` |
| `loom export-actions` | Exporta el esquema de acciones derivado del registro de operaciones: un manifiesto MCP tools/list con extras de loom (json) o el esquema Cedar (cedar) con el que un gateway de políticas gobierna la superficie. | `--format`, `--output` |
| `loom lexical-stats-rebuild` | Recalcula la frecuencia de documento BM25 de un espacio de nombres a partir de sus filas activas (las vistas de frecuencia de términos solo ven inserciones; las sustituciones y las lápidas dejan vocabulario obsoleto). | `--org`, `--namespace`, `--no-knowledge`, `--no-memory` |
| `loom version` | Imprime la versión instalada de Loom. |  |
| `loom install` | Conecta Loom a la configuración de un cliente de IA local. | `--client`, `--server-url`, `--token`, `--name`, `--toolset`, `--dry-run`, `--force` |
| `loom demo` | Los primeros cinco minutos: ingiere el AGENTS.md de este repositorio (o --file) en el plano de conocimiento como lo hace `make ingest`, le hace una pregunta e imprime la respuesta con sus filas de evidencia. | `--file`, `--question`, `--tags`, `--base-url`, `--token`, `--org`, `--namespace`, `--root`, `--search-only` |
| `loom init` | Crea una identidad en un servidor Loom. | `--agent`, `--server-url`, `--org`, `--purpose`, `--ttl-days`, `--token-path`, `--config-path`, `--no-files`, `--json` |
| `loom ingest-transcript` | Envía un archivo de historial de chat (markdown / JSON) a la tabla de conversaciones de Loom como un solo hilo. | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--thread-id`, `--file`, `--dry-run` |
| `loom memory-search` | Ejecuta un memory.search desde la shell. | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--query`, `--top-k`, `--search-mode`, `--json-agent` |
| `loom set-host` | Designa los host de Loom de este despliegue. | `<emails>`, `--env-file`, `--append`, `--dry-run` |
| `loom login` | Inicia sesión en un servidor Loom desde cualquier navegador con un código de un solo uso. |  |
| `loom logout` | Termina la sesión almacenada en el servidor y la olvida aquí. |  |
| `loom whoami` | Muestra como quién te inicia sesión la sesión almacenada. |  |
| `loom call` | Llama a cualquier operación o ruta de la API con la sesión almacenada (`loom call --help`). |  |
| `loom token` | Crea, lista y revoca tus propios tokens para CI y agentes (`loom token --help`). |  |

<!-- prettier-ignore-end -->
<!-- END GENERATED: cli -->

<a id="related"></a>

## Relacionado

- [Referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md): las ops que llaman los comandos del cliente.
- [Mantenimiento](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md): re-embed, backfills y reparaciones.
- [Operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).
- [Actualizaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): `loom migrate` en el procedimiento de actualización.
- [Desarrollo local](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md).
