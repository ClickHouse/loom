<!-- i18n: source=docs/architecture/server-layout.md blob=a488f9e3096ee9ff0f09303b9a382a9ce950bac7 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md), which is authoritative.

<a id="server-layout"></a>

# Estructura del servidor

`server/` está organizado por dominio. Cada plano del producto tiene un único paquete que contiene sus declaraciones de protocolo, handlers, lógica pura, acceso a ClickHouse y bucles en segundo plano, y una prueba fija en qué dirección pueden apuntar las importaciones. Esta página es el mapa para ubicar un módulo nuevo y para interpretar un error de importación de la prueba de contrato.

<a id="packages"></a>

## Paquetes

| Paquete                       | Responsabilidad                                                                                                                                                                                             | Puede importar                                                                                              |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/main.py`              | Conexión de la aplicación: el lifespan, los routers, los workers, los transportes. Junto con `server/boot.py`, `server/health.py`, `server/wiring.py`, `server/worker_main.py` y `server/__init__.py`, los únicos módulos en la raíz | cualquiera                                                                                                  |
| `server/boot.py`              | Los pasos de arranque y apagado del lifespan: la migración de arranque serializada, las filas de espacio de nombres del tracker del líder, la instantánea de revocaciones y el keyring, el bucle de lease del supervisor, el drenaje de escrituras | cualquiera                                                                                                  |
| `server/health.py`            | `GET /v1/health`: la respuesta de salud de varios niveles (ClickHouse, proveedores, la instantánea de arranque, la calibración, el limitador de tasa y, bajo petición, los workers y la observabilidad)      | cualquiera                                                                                                  |
| `server/wiring.py`            | `wire()`: entrega a cada punto de unión de la plataforma su pieza de dominio (el directorio de espacios de nombres de la puerta de autenticación, los datos de los indicadores de estado); lo ejecutan `main.py`, la CLI y las suites de pruebas | cualquiera                                                                                                  |
| `server/worker_main.py`       | La raíz de composición del proceso hijo de workers (`python -m server.worker_main`): el supervisor y lo que usan sus workers, sin el lifespan de servicio                                                   | cualquiera                                                                                                  |
| `server/core/`                | Infraestructura transversal: el cliente de ClickHouse (`ch`), `errors`, presupuestos, fechas y horas, hashing, nombres, cursores, idempotencia, límites de tasa, política de almacenamiento de vectores, `schema/` (migraciones y herramientas de DDL) | `core`, `settings`                                                                                          |
| `server/settings/`            | Cada configuración declarada en `catalog.py`, una sección por área, compuestas en `Settings`                                                                                                                | `core`, `settings`                                                                                          |
| `server/operations/`          | El núcleo del registro: `Operation` y `Role` (`base.py`), el registro aplanado (`registry.py`: `ALL_OPERATIONS`, `OPERATIONS_BY_NAME`), `toolsets.py`, la proyección de lectura, el censo de alcance         | `core`, paquetes de plataforma, el `api` de cada dominio                                                    |
| `server/transport/`           | HTTP y MCP: admisión, enlace de peticiones, listados, el primer                                                                                                                                             | `core`, paquetes de plataforma; el `api`, `app` y `domain` de un dominio (nunca su `store` ni `workers`); no `cli` |
| `server/cli/`                 | El comando `loom`, un adaptador externo                                                                                                                                                                     | cualquiera                                                                                                  |
| `server/workers/`             | El registro de workers (`registry.py`), el supervisor, el contexto de pasada, el estado y su almacén, la planificación de la primera pasada, los eventos de coste. Los bucles de los workers viven en su dominio | `core`, paquetes de plataforma; el `workers` de un dominio (las cadenas de módulo del registro)             |
| `server/observability/`       | El ledger, los eventos, las filas de auditoría, las métricas, la exportación de trazas                                                                                                                      | `core`, paquetes de plataforma                                                                              |
| `server/auth/`                | Emisión y verificación de tokens, la puerta de roles y espacios de nombres, la identidad por petición                                                                                                       | `core`, paquetes de plataforma                                                                              |
| `server/embedding/`, `llm/`   | Proveedores intercambiables de embedder y de LLM                                                                                                                                                            | `core`, paquetes de plataforma                                                                              |
| `server/clients/`, `storage/` | HTTP saliente (el pool compartido, GitHub); cláusulas de retención                                                                                                                                          | `core`, paquetes de plataforma                                                                              |
| `server/memory/`              | El plano de memoria: `memory.*`, búsqueda, ask, extracción, hechos, procedimientos, perfil, memoria de trabajo, paquetes de contexto, conflictos, lista negra, `loom_search`                                | ver [capas](#layers)                                                                                        |
| `server/code/`                | El plano de código: `code.*`, el índice (`app/index/`, `domain/index/`, `store/index/`), sus parsers (`domain/extract/`), los workers de ingesta y del nivel del compilador                                  | ver [capas](#layers)                                                                                        |
| `server/tracker/`             | Espacios de nombres del tracker: `tracker.*`, comprobaciones de duplicados, los lectores de fuentes en vivo y su cliente, las tablas propias del tracker de loom                                             | ver [capas](#layers)                                                                                        |
| `server/knowledge/`           | Documentos y fragmentos de conocimiento, conversaciones, documentos de repositorio                                                                                                                          | ver [capas](#layers)                                                                                        |
| `server/erasure/`             | La saga de borrado, su censo, los backends federados, el worker de recuperación                                                                                                                             | ver [capas](#layers)                                                                                        |
| `server/tasks/`               | La bandeja de tareas, el ledger de ejecuciones, sus bucles de reactivación y de webhooks                                                                                                                    | ver [capas](#layers)                                                                                        |
| `server/identity/`            | Espacios de nombres, permisos, política de historial, alcance de lectura, acceso restringido, la página del dashboard y la API del llamante (`/v1/me/*`)                                                    | ver [capas](#layers)                                                                                        |
| `server/insights/`            | Superficies de operador: `ops.*`, `cost.*`, `agents.*`, `audit.list`, `ui.vitals`, `mentor.brief`, las rutas `/v1/admin/*`                                                                                  | ver [capas](#layers)                                                                                        |
| `server/assistant/`           | El asistente: su usuario de servicio de LibreChat y la definición del agente, configurados al arrancar                                                                                                      | ver [capas](#layers)                                                                                        |

Los paquetes de plataforma son `core` y los demás paquetes que no son de dominio de la tabla anterior: `settings`,
`operations`, `transport`, `cli`, `workers`, `observability`, `auth`, `embedding`, `llm`,
`clients` y `storage`. Aparte de `core` y `settings`, cada uno puede importar cualquier otro paquete
de plataforma, y ninguno importa un paquete de dominio salvo lo que indica la tabla.

<a id="layers"></a>

## Capas

Un paquete de dominio tiene hasta cinco subpaquetes. Un módulo va donde está su
responsabilidad principal; un dominio pequeño tiene menos.

| Capa       | Contiene                                                                                                                                       | Puede importar                                                                                                                                                                                |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api/`     | El contrato de red: los modelos Pydantic de entrada y salida (`models*.py`) y las declaraciones `Operation` de los grupos de ops del dominio (`ops*.py`) | `core`, `operations`, `settings`; el `domain` y el `api` de cualquier dominio; su propio `app` para el handler que enlaza una `Operation`                                                      |
| `app/`     | Handlers y orquestación: lo que hace una op o una pasada de un worker, en orden, con I/O                                                       | su propio `api`, `app`, `domain`, `store`; el `app`, el `domain` y los modelos de red (`api/models*.py`) de otros dominios; `core` y todos los paquetes de plataforma excepto `transport` y `cli` |
| `domain/`  | Lógica pura, valores tipados y políticas: sin ClickHouse, sin red                                                                              | `core` (no sus módulos de I/O: `ch`, `schema`, `idempotency`, `ratelimit_shared`, `namespace_write_marks`), `settings`, el `domain` de cualquier dominio                                       |
| `store/`   | Repositorios de ClickHouse y constructores de filas: cada sentencia que el dominio ejecuta contra sus tablas                                   | `core`, `settings`, `observability`, `storage`, su propio `store`, el `domain` de cualquier dominio                                                                                           |
| `workers/` | Bucles en segundo plano, un módulo por fila del registro                                                                                       | igual que `app`, con `workers` en lugar de `api`; el `app`, el `domain` y los modelos de red de otros dominios                                                                                |

Un módulo de `domain/` inicia un proceso: `server/code/domain/extract/bounded_parse.py`, que
llaman todos los parsers de tree-sitter de `domain/`, analiza cada fuente primero en un
proceso hijo local con límites de recursos.

Dos dependencias están ausentes a propósito. `store/` nunca llama a `app/`: un repositorio recibe valores
y devuelve filas, de modo que un app se puede probar con un store falso. Y ningún dominio lee
el `store/` de otro dominio: llama al `app/` de ese dominio o lee sus tipos de `domain/`, de
modo que el dueño de una tabla es el único que escribe sus sentencias. Para llamar al handler de otro
dominio, `app/` y `workers/` pueden importar los modelos de red de ese dominio
(`api/models*.py`), nunca sus `api/ops*.py`. `app/` importa los modelos
de red, nunca los módulos `ops`, para que las declaraciones `Operation` puedan enlazar handlers sin
un ciclo de importación.

<a id="the-import-contract"></a>

## El contrato de importaciones

`tests/unit/test_package_contracts.py` analiza con `ast` cada módulo bajo `server/` y clasifica cada importación (de nivel superior, anidada bajo `if`/`try`, dentro de una función o bajo `TYPE_CHECKING`) según el paquete y la capa de ambos extremos. Una importación que las reglas anteriores no permiten hace fallar la prueba e indica el módulo importador y el módulo importado.

Las importaciones que hoy incumplen el contrato figuran en `tests/unit/package_contract_allowlist.txt`, con la forma `importer -> imported` según la ruta del módulo. La lista solo se reduce: un par permitido que ya no se produce también hace fallar la prueba, así que corregir una importación implica borrar su línea.

Junto a esta se ejecuta una segunda comprobación: solo `server/transport/`, `server/cli/` y `server/main.py` importan `server.transport` (`tests/unit/test_transport_layering.py`). Ningún módulo de dominio importa `server.cli`.

<a id="adding-a-module"></a>

## Agregar un módulo

1. Elige el dominio cuya tabla, grupo de operaciones o plano sirve el módulo. Un módulo que
   sirve a todos los planos (un helper de ClickHouse, una regla de hashing) va en `core/`; uno que
   sirve a dos dominios va en aquel cuyas filas lee, y el otro lo llama.
2. Elige la capa por lo que hace el módulo, no por quién lo llama: la E/S o un `async def`
   que espera algo va en `app/` o `store/`; una función de sus argumentos va en `domain/`.
3. Nómbralo por lo que contiene dentro de su paquete, sin repetir la ruta:
   `server/erasure/app/saga.py`, no `server/erasure/app/erasure_saga.py`.
4. El `__init__.py` de un paquete contiene un docstring de una línea que indica qué posee el paquete. Solo
   reexporta nombres cuando es la superficie pública del paquete
   (`server/memory/store/`, `server/code/store/index/`).
5. Un bucle en segundo plano es un módulo en `<domain>/workers/` más una fila en
   `server/workers/registry.py` que indica su ruta de módulo completa.
6. Un objetivo de mock o monkeypatch, una fila del registro y un umbral mínimo de cobertura nombran los módulos por
   ruta; escríbelos con la ruta real del módulo, nunca a través de una reexportación.
7. Encuentra los archivos que el servidor distribuye (migraciones, `schema.sql`, el build del frontend) mediante
   `server.core.paths`, nunca contando los directorios padre de `__file__`
   (`tests/unit/test_repo_paths.py`).

<a id="extractor-modules"></a>

## Módulos del extractor

Los archivos listados en `extractor_stamp.STAGE_MODULES` son el sello de versión del índice de código. El sello calcula un hash del nombre de cada archivo y de sus tokens de código, y cuenta una importación como los nombres que enlaza, no como el paquete del que proceden. Si el sello cambia, se vuelve a derivar el índice de cada espacio de nombres de código. Estos archivos viven en `server/code/domain/extract/` junto con `extractor_stamp.py`, que los lee desde su propio directorio, y conservan sus nombres de archivo. Los módulos de los carriles de lenguaje están a su lado. Mover uno de ellos entre paquetes no altera el sello; renombrarlo, reformatearlo (`ruff format` los excluye) o cambiar su código sí lo altera.

<a id="related"></a>

## Relacionado

- [Visión general del sistema](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) — los componentes del plano de control y el flujo de las solicitudes.
- [Arquitectura del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md) — la marca del extractor y la reindexación.
- [Ruta de la solicitud](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md#request-path) — el camino que sigue una solicitud a través del transporte, el registro y un handler.
