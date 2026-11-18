<!-- i18n: source=docs/reference/index.md blob=fb131cfc15bed6b11b4ba0dd63862e173de7bcf3 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/reference/index.md), which is authoritative.

<a id="reference"></a>

# Referencia

Las páginas de referencia explican qué existe y qué significa cada opción. Las razones detrás de un diseño están en
[Conceptos](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) y [Arquitectura](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md).
Los procedimientos están en las secciones de [guías](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md) y
[operaciones](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md).

<a id="generated-from-the-code"></a>

## Generado a partir del código

Estas páginas, o las tablas que contienen, se generan a partir del código fuente que las define. Cada
generador tiene un modo `--check` que `make lint` ejecuta, de modo que una página que se desvíe del código hace fallar
CI. Edita la fuente y vuelve a generar; nunca edites a mano una tabla generada.

| Página                              | Qué enumera                                                                                                   | Fuente                                                                               | Generador                                      |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------- |
| [Configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md) | Cada ajuste `LOOM_*`, su valor por defecto y su significado, por área.                                        | los comentarios sobre los campos en `server/settings/`                               | `python scripts/codegen/settings_reference.py` |
| [Operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)      | Cada operación, agrupada, con rol, tipo, listado `all` de MCP e indicador de auditoría; además de cómo funciona el registro. | `server.operations.registry.ALL_OPERATIONS`                                          | `python scripts/codegen/docs_reference.py`     |
| [Procesos en segundo plano](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md) | Cada proceso en segundo plano, su interruptor, valor por defecto y cadencia; además de cómo los ejecuta el supervisor. | `server/workers/registry.py` (`_WORKERS`)                                            | `python scripts/codegen/docs_reference.py`     |
| [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)                     | Cada comando y subcomando de `loom` con sus flags; además de dónde se ejecuta la CLI y los comandos habituales. | el árbol del parser de `loom` (`tests/unit/snapshots/cli_tree.json`)                 | `python scripts/codegen/docs_reference.py`     |
| [Errores](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)              | Cada código de error con el que responde el servidor; además del envelope, las reglas de reintento y las soluciones para los códigos habituales. | `server/core/errors.py` y cada literal `"code"` dentro de `server/`                  | `python scripts/codegen/docs_reference.py`     |
| [Estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)               | Cuántas de las operaciones de cada grupo son estables, cada operación experimental y cada ajuste booleano que se carga como falso. | el `maturity` de las operaciones en el registro y los campos booleanos de `server/settings/catalog.py` | `python scripts/codegen/docs_reference.py`     |
| [Esquema](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)              | Cada tabla y vista materializada con motor, claves, TTL e índices; además de las convenciones de columnas.    | `schemas/schema.sql`                                                                 | `python scripts/codegen/docs_reference.py`     |

En las páginas de `docs_reference.py` solo se generan las regiones entre los marcadores
`<!-- BEGIN GENERATED: … -->` y `<!-- END GENERATED: … -->`; la prosa
que las rodea está escrita a mano. Las tablas de operaciones dentro de los paquetes SDK (`sdk/python/loom/ops.py`,
`sdk/typescript/src/generated/ops.ts`) también se generan, mediante
`scripts/codegen/sdk_py_gen.py` y `sdk_ts_gen.py`; consulta
[Generación de código](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md).

<a id="written-by-hand"></a>

## Escritas a mano

| Página                                        | Qué cubre                                                                                                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md)                     | Todas las rutas (ops, descubrimiento, estado, streams, inicio de sesión, OAuth, dashboard, operador), el orden de las compuertas, cabeceras, campos desconocidos, idempotencia, paginación, límites de tasa. |
| [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md)                               | El endpoint `/mcp`, autenticación y OAuth, sesiones e identidad, la lista de herramientas y toolsets, resultados, errores, la exportación del esquema de acciones. |
| [SDK de Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md)              | Instalación, construcción, llamada a ops, formas tipadas, sesiones y herramientas, adaptadores de frameworks, errores, reintentos.                                 |
| [SDK de TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md)      | Instalación, clientes y opciones, ops tipadas, sesiones y herramientas, paginación, el adaptador de Strands, errores, reintentos.                                  |
| [Variables de entorno](https://github.com/ClickHouse/loom/blob/main/docs/reference/environment.md)      | Variables leídas fuera del modelo de configuración: claves de proveedores, la imagen, archivos compose, la CLI, los SDK y los scripts del operador.                |
| [Superficie SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)            | Las vistas `sql_v1_*` de solo lectura sobre los datos de un tenant: columnas, regla de compatibilidad, qué ocultan, actualidad, `loom sql-access`.                 |
| [Contrato del extractor](https://github.com/ClickHouse/loom/blob/main/docs/reference/extractor-contract.md) | Versión 1 del contrato HTTP que sirve un servicio extractor: capacidades, extracción, códigos de error, los campos y las reglas del artefacto.                     |

Las páginas escritas a mano nombran solo cosas que existen en el árbol y enlazan al código fuente que describen.

<a id="which-page-answers"></a>

## Qué página responde…

- "¿Cómo se llama esta op y quién puede llamarla?" → [Operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md).
- "¿Qué hace esta configuración?" → [Configuración](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md).
- "¿Qué significa este código de error y debo reintentar?" → [Errores](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md).
- "¿Esta op es estable o experimental, y esta configuración está activada por defecto?" → [Estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
- "¿Qué header, qué ruta y cómo pagino?" → [API HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).
- "¿Por qué mi cliente MCP no lista una herramienta?" → [MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/mcp.md#toolsets).
- "¿Este proceso de trabajo se está ejecutando y cómo lo activo?" → [Procesos de trabajo](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).
- "¿Mi cliente dejará de funcionar en el próximo despliegue?" → [campos desconocidos y eliminados](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields) y [cómo descubrir qué sirve un despliegue](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves).
- "¿Qué hay en esta tabla?" → [Esquema](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md).
- "¿Qué parámetros recibe `loom <command>`?" → [CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md).
