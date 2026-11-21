<!-- i18n: source=docs/development/code-generation.md blob=3f6e27200eccce850139d9114282249d0644e35f model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md), which is authoritative.

<a id="code-generation"></a>

# Generación de código

Varios archivos del repositorio se derivan del código: las tablas de operaciones y los tipos de wire de los SDK, las páginas de referencia, las copias del plugin de Codex, el manifiesto de alcance de las operaciones (op reach manifest) y el volcado del esquema. Cada uno está commiteado, de modo que un lector lo ve sin ejecutar nada, y los generadores marcados a continuación tienen un modo `--check` que termina con código 1 cuando el archivo commiteado difiere de lo que escribiría el generador. `make lint` ejecuta cada una de esas comprobaciones, y CI las ejecuta en los jobs `lint` y `sdk-typescript` y en la suite de pruebas unitarias, de modo que un archivo desactualizado falla indicando en el mensaje el comando que hay que ejecutar.

**Por qué commitear los archivos generados.** Un archivo generado que solo se construye bajo demanda es invisible en la revisión: un cambio en el modelo de entrada de una operación cambiaría los tipos del SDK sin un diff que nadie lea. Commitearlos pone el efecto de un cambio junto al cambio, y la comprobación `--check` impide que ambos se desincronicen. El costo es un paso de regeneración en el cambio que modifica la fuente.

<a id="the-generators"></a>

## Los generadores

Todos viven en [`scripts/codegen/`](https://github.com/ClickHouse/loom/blob/main/scripts/codegen) y comparten
`scripts/codegen/common.py` (la raíz del repositorio, los datos que una tabla del SDK incluye por op,
el formato de ruff, la escritura atómica y el protocolo de escribir o comprobar).

| Script                      | Escribe                                                                                                                                                                                                                | A partir de                                                                                                                                                                                           | `--check` en `make lint`   |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `sdk_py_gen.py`             | `sdk/python/loom/ops.py`: `OpName`, la tabla `OPS` (idempotencia, rol, madurez, presupuesto de latencia) y un `TypedDict` por cada modelo de entrada y de salida                                                         | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | sí                         |
| `sdk_py_client_gen.py`      | `sdk/python/loom/client.py`: `OpsClient`, un método por op (`agent.client.tracker.source_add`), con los tipos de los nombres de `ops.py`                                                                                | `server.operations.registry.ALL_OPERATIONS`                                                                                                                                                           | sí                         |
| `sdk_py_admin_gen.py`       | `sdk/python/loom/admin_routes.py`: `AdminRoutes`, un método por ruta `/v1/admin/*` (`agent.deployment.routes.settings_set`)                                                                                             | los routers `/v1/admin/*`: el método, la ruta, la puerta de acceso y el docstring de cada ruta                                                                                                        | sí                         |
| `sdk_ts_gen.py`             | `sdk/typescript/src/generated/ops.ts`: una interfaz por modelo, la tabla `OPS` y el tipo `OpMap`                                                                                                                        | `server.operations.registry.ALL_OPERATIONS` (los JSON Schemas de los modelos)                                                                                                                         | sí                         |
| `agent_plugin_sync.py`      | las copias del plugin de Codex de los scripts de hooks (`hooks/*.mjs`), la biblioteca compartida (`lib/*.mjs`), las skills y el icono del listado (`assets/`), idénticos byte a byte y con el bit de ejecución           | `integrations/claude-code-plugin/`                                                                                                                                                                    | sí                         |
| `settings_reference.py`     | la [referencia de configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) generada                                                                                                                                               | `server/settings/catalog.py`: la variable de entorno, el tipo, el valor por defecto y el comentario que precede a cada campo                                                                          | sí                         |
| `settings_schema.py`        | `schemas/loom-settings.schema.json`, el JSON Schema del archivo de settings en YAML                                                                                                                                     | los tipos, límites, valores por defecto y comentarios de los campos de settings, sin los secretos                                                                                                     | sí                         |
| `settings_example.py`       | `loom.example.yaml`, la plantilla del archivo de settings: todos los settings salvo los secretos, comentados en su valor por defecto bajo su descripción, por área                                                      | los campos, valores por defecto, comentarios y marcas de live del catálogo de settings                                                                                                                | sí                         |
| `docs_reference.py`         | las regiones generadas de `docs/reference/`: `operations`, `workers`, `cli`, `errors`, `schema`, `status_ops`, `status_switches`, `admin_routes` (la [tabla de rutas de operador](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#operator-routes)) | el registro, `server/workers/registry.py` (`_WORKERS`), `tests/unit/snapshots/cli_tree.json`, `server/core/errors.py`, `schemas/schema.sql`, `server/settings/catalog.py`, los routers `/v1/admin/*` | sí                         |
| `op_reach_census.py`        | `server/operations/reach_manifest.py` (`--write-manifest`); en otro caso, una tabla de censo                                                                                                                            | el registro y los toolsets de MCP                                                                                                                                                                     | sí                         |
| `wire_changelog.py`         | nada; imprime lo que cambió para los clientes entre dos revisiones                                                                                                                                                      | el catálogo de capacidades construido en cada revisión                                                                                                                                                | no                         |
| `gen_py_library_methods.py` | `server/code/domain/extract/py_library_methods.py`                                                                                                                                                                      | la biblioteca estándar del intérprete en ejecución                                                                                                                                                    | no (depende del intérprete) |
| `push_langfuse_prompts.py`  | `server/observability/langfuse_prompts.lock.json` (`--write`)                                                                                                                                                           | las constantes de prompts del servidor; las sube a Langfuse                                                                                                                                           | no                         |

La referencia de configuración se genera a partir de comentarios, así que el comentario que
precede a un setting es su documentación: edita el comentario, no la página. Las páginas de
`docs/reference/` son prosa escrita a mano alrededor de regiones
`<!-- BEGIN GENERATED: <name> -->` … `<!-- END GENERATED: <name> -->`;
solo se reescribe el texto entre un par de marcadores, de modo que una página puede explicar
la tabla que contiene.

<a id="sdk_py_gen-and-sdk_ts_gen"></a>

### sdk_py_gen y sdk_ts_gen

El SDK de Python sigue siendo de diccionarios de entrada y de salida; el archivo generado añade los nombres, de modo que un editor
autocompleta `agent.call("memory.search", …)` y un verificador de tipos ve los nombres de los campos sin
una capa de modelos mantenida a mano. Los diccionarios son `total=False`, con `Required[...]` en los campos que el
servidor exige. El generador de TypeScript convierte el JSON Schema de cada modelo en interfaces
(los enums y consts pasan a uniones literales, `anyOf` a uniones, y los `$defs` compartidos se elevan una sola vez), de modo que
`client.call("memory.search", input)` queda tipado de extremo a extremo. Ejecútalos tras cualquier cambio en
el registro de una operación o en sus modelos:

```bash
python scripts/codegen/sdk_py_gen.py
python scripts/codegen/sdk_py_client_gen.py
python scripts/codegen/sdk_ts_gen.py
```

Ambos SDK exponen cada op como un método en el mismo lugar: el grupo es el primer segmento
del nombre de la op y el método es el resto unido con `.`, en snake_case en Python
(`agent.client.tracker.source_add`) y en camelCase en TypeScript (`loom.tracker.sourceAdd`).
`tests/unit/test_sdk_parity.py` falla cuando las dos tablas de ops, o los dos nombres de método de
cualquier op, difieren.

<a id="agent_plugin_sync"></a>

### Sincronización de plugins de agentes

Los dos plugins de agentes de programación difieren solo en sus manifiestos y en la conexión de hooks
(`hooks/hooks.json`). El plugin de Codex lleva copias en lugar de referencias `../`, para que cada
plugin sea un directorio autocontenido que un marketplace pueda descargar por sí solo. Tras añadir un
script de hook o una skill al plugin de Claude Code, ejecuta la sincronización y haz commit de las copias; conecta
un hook nuevo en el `hooks.json` de Codex a mano si Codex debe ejecutarlo.

<a id="op_reach_census"></a>

### Censo de alcance de las operaciones

Cada operación declara una madurez y debe tener una vía de acceso distinta de un `POST` HTTP
directo, o estar declarada `experimental`. Los tipos de acceso (`server/operations/reach.py`,
`REACH_KINDS`) son los listados de MCP (`mcp_core`, `mcp_atomic`, `mcp_all`, derivados en vivo
de `server/operations/toolsets.py`), un método del SDK (`sdk_method`), un ejemplo incluido o
un script de operador (`sdk_example`), un hook o integración de plugin (`plugin_hook`) y un
punto de llamada del dashboard (`dashboard`). Los cuatro últimos salen de escanear el árbol, que un
servidor desplegado no incluye, así que se confirman en el repositorio como `reach_manifest.STATIC_REACH`.
`--check` hace cumplir la regla, que toda op sin punto de llamada del dashboard esté en
`server/operations/dashboard.py` (con el motivo por el que no lo tiene), y que el manifiesto
confirmado coincida con el árbol; `--write-manifest` lo actualiza. `--call-counts --days 30` añade
el número de llamadas de cada op desde `loom.agent_events` (el único modo que se conecta a ClickHouse),
y `--owners` indica el commit que introdujo la declaración de cada op experimental.

<a id="wire-changelog"></a>

### Registro de cambios del protocolo

```bash
python scripts/codegen/wire_changelog.py <rev_a> <rev_b>           # markdown
python scripts/codegen/wire_changelog.py --since-hours 24 [rev_b]   # rev_a = rev_b's ancestor N hours back
python scripts/codegen/wire_changelog.py <rev_a> <rev_b> --json
python scripts/codegen/wire_changelog.py --snapshot <rev> > snap.json
python scripts/codegen/wire_changelog.py --diff a.json b.json
```

Materializa cada revisión con `git archive`, importa su registro en un intérprete hijo e
imprime las operaciones y los campos de solicitud y respuesta añadidos o eliminados, además de
los cambios de rol, presupuesto y nivel de MCP. Su salida va a las notas de la versión
([proceso de publicación](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)); la sección
"what changed for clients" se genera, no se escribe.

<a id="gen_py_library_methods"></a>

### Métodos de la biblioteca estándar de Python

El conjunto de nombres de métodos de la biblioteca estándar que usa el extractor de Python está fijado en el árbol
para que una actualización del intérprete no pueda mover las aristas de llamadas. Regenerarlo cambia un módulo
del extractor, lo que mueve el sello de versión del índice de código y vuelve a fragmentar cada espacio de nombres indexado;
revisa el diff y regenéralo solo cuando sea lo que quieres.

<a id="push_langfuse_prompts"></a>

### Publicación de prompts en Langfuse

El servidor enlaza una generación de LLM con una versión de prompt de Langfuse buscando el hash de contenido del
prompt en el lockfile confirmado, sin ninguna llamada en tiempo de ejecución. Sin `--write`
el script planifica contra el lockfile sin conexión; `--write` sube los prompts cambiados (necesita
`LOOM_LANGFUSE_URL` y `LOOM_LANGFUSE_ADMIN_TOKEN`) y reescribe el lockfile. Una prueba
unitaria falla mientras al lockfile le falte un prompt registrado.

<a id="the-schema-dump"></a>

## El volcado del esquema

`schemas/schema.sql` lo genera `scripts/ops/dump_schema.py` a partir de un ClickHouse con
todas las migraciones aplicadas:

```bash
make schema-dump    # python -m scripts.ops.dump_schema
make schema-check   # --check: exit 1 with the head of the diff; writes nothing
```

Se describe en [migraciones](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md#schema-dump-and-check), ya que sigue
cada cambio de migraciones y debe tomarse con la versión de ClickHouse que fija CI.

<a id="snapshots-that-are-tests-not-generators"></a>

## Snapshots que son pruebas, no generadores

La superficie de la CLI (`tests/unit/snapshots/cli_tree.json`, `cli_help.json`) y el registro de workers (`tests/unit/snapshots/worker_registry.json`) están fijados por pruebas unitarias, y se regeneran mediante una variable de entorno en la ejecución de las pruebas ([pruebas](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md#ci-gates)). La referencia de la CLI se genera a partir de `cli_tree.json`, por lo que, tras regenerar el snapshot de la CLI, ejecuta también `docs_reference.py`.

<a id="before-you-push"></a>

## Antes de hacer push

```bash
make lint            # every --check above, plus ruff, the gates and Biome's dashboard accessibility rules
```

Un fallo indica el script que hay que ejecutar; ejecútalo, revisa el diff y haz commit del archivo regenerado en el mismo cambio que su fuente.

<a id="related"></a>

## Relacionado

- [Añadir una operación](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-an-operation.md): la op, el handler, las tablas
  del SDK
- [Estilo de la documentación](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md#generated-pages): qué páginas están
  generadas
- [Pruebas](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md)
