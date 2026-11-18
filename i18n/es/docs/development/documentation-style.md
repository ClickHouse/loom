<!-- i18n: source=docs/development/documentation-style.md blob=f2be8a4de2f96370b18771d4ed344c1034723910 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/documentation-style.md), which is authoritative.

<a id="documentation-style-guide"></a>

# Guía de estilo de la documentación

Cómo ubicar una página nueva en la sección correcta, escribirla con el estilo de la casa y superar las comprobaciones de la documentación en el primer push.

<a id="where-a-page-goes"></a>

## Dónde va una página

Una página hace un solo tipo de escritura. Cuando empieza a hacer un segundo tipo, divídela y enlaza.

| Tipo                    | Responde                                     | Vive en                            | Voz                                                                                                                                                         |
| ----------------------- | -------------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Caso de uso             | "¿Hace Loom X por mí?"                       | `docs/use-cases/`                  | La necesidad se responde en la primera frase; cita un resultado publicado de `docs/benchmarks/results.md`, con un enlace, cuando existe uno                 |
| Tutorial                | "Muéstrame, de principio a fin."             | `docs/getting-started/`            | Pasos numerados que siempre funcionan en el stack por defecto                                                                                               |
| Guía práctica           | "¿Cómo logro X?"                             | `docs/guides/`, `docs/operations/` | Pasos centrados en el objetivo; asume que se conocen los conceptos                                                                                          |
| Concepto / funcionalidad | "¿Qué es y por qué funciona así?"           | `docs/concepts/`, `docs/features/` | Explicación, compromisos de diseño, enlaces a la referencia                                                                                                 |
| Arquitectura            | "¿Cómo está construido por dentro?"          | `docs/architecture/`               | Componentes, flujo de datos, invariantes, rutas de código                                                                                                   |
| Referencia              | "¿Cuáles son exactamente las opciones?"      | `docs/reference/`                  | Exhaustiva, concisa, preferiblemente generada                                                                                                               |
| Benchmark               | "¿Qué tan bien funciona y cómo lo sabemos?"  | `docs/benchmarks/`                 | Primero la metodología; números solo con un comando de reproducción                                                                                         |

- `docs/concepts/` explica el modelo que el usuario tiene en la cabeza (identidad, memoria, ranking).
- `docs/features/` explica una capacidad desde el lado de quien llama: qué hace, las operaciones, un ejemplo, la configuración, los límites.
- `docs/architecture/` explica la misma capacidad desde dentro. Una página de funcionalidad enlaza a ella en lugar de repetirla.
- `docs/development/` es solo para contribuidores.

<a id="page-template"></a>

## Plantilla de página

Toda página empieza con un título y un párrafo introductorio sencillo: qué cubre la página y qué puede hacer el lector después de leerla. Sin etiquetas como **Audience:**.

```markdown
# Retrieval and ranking

`memory.search` combines several retrieval legs into one ranked answer, or abstains when
nothing is close enough. This page explains each leg, how the rank is computed, and
which settings move it.
```

`llms.txt` lista la primera oración de ese párrafo para la página, y el sitio de documentación la usa como descripción de búsqueda de la página. Después, donde correspondan: el comportamiento visible para el usuario, las interfaces (operaciones, CLI, ajustes), cómo funciona, límites y modos de fallo, y enlaces a "Related". Las páginas de arquitectura añaden **Source entry points** (rutas y símbolos) e **Invariants**.

<a id="prerequisites"></a>

## Requisitos previos

Cuando una funcionalidad necesita un interruptor del operador o un sistema externo, indícalo en la primera frase de la página: "Needs `LOOM_X=true`." Una página de casos de uso es la excepción: su primera frase responde a la necesidad y el interruptor va después. No añadas avisos de madurez ni de estado: enlaza la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md) generada, que indica si una op es estable y si un interruptor está activado por defecto, y la [referencia de operaciones](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md), que recoge de qué depende el listado de MCP.

<a id="writing-rules"></a>

## Reglas de redacción

- **El código es la fuente de verdad.** Cada configuración, op, campo, make target, ruta y flag de CLI que aparece en una página existe en el árbol. Verifica cada uno.
- **Breve.** Oraciones cortas, párrafos cortos, tablas para las opciones. Sin metáforas ni marketing.
- **Presente, comportamiento actual.** Escribe lo que Loom hace. La historia, las fechas y los motivos de un cambio van en el pull request.
- **Nombra técnicas, no productos.** El texto del repositorio no nombra ningún producto cercano, ninguna persona, ningún host ni espacio de nombres internos, y no cita ningún artículo ni registro de decisión de diseño. `tests/unit/test_public_copy.py` aplica las listas. Compara arquitecturas ("un almacén vectorial separado junto al almacén relacional"), nunca marcas.
- **Sin adjetivos sin medir.** "Rápido", "escalable", "preciso" necesitan un número y el comando que lo reproduce, o un mecanismo.
- **Explica el porqué.** Una decisión de diseño va acompañada del problema que resuelve y de lo que cuesta.
- **Enlaza, no repitas.** Una página es dueña de cada explicación y de cada tabla; las demás enlazan a su encabezado.
- **Sin cifras ni versiones obsoletas.** No escribas cuántas ops, configuraciones o tablas existen, ni la versión de un paquete. Enlaza a la referencia generada.
- **Los ejemplos funcionan.** Los ejemplos de código usan nombres reales del SDK, de la CLI y del protocolo, y funcionan con el stack estándar de la [guía de inicio rápido](../getting-started/quickstart.md), salvo que indiquen el proveedor o la configuración que necesitan.

<a id="terminology"></a>

## Terminología

Usa estas grafías en todas partes; el [glosario](https://github.com/ClickHouse/loom/blob/main/docs/glossary.md) las define.

| Escribe                            | No                                           |
| ---------------------------------- | -------------------------------------------- |
| operación, op (`memory.set`)       | endpoint, llamada a la API, herramienta (para la op) |
| herramienta MCP (`loom_memory_set`) | función                                     |
| espacio de nombres                 | proyecto, colección, índice                  |
| tupla de identidad                 | clave de ámbito                              |
| plano (plano de memoria, plano de código) | módulo, servicio (para el área del producto) |
| memoria, hecho (una fila de `loom.memory`) | nota, registro (un registro de un tracker es otra cosa) |
| documento de conocimiento          | archivo, carga                               |
| worker                             | job, cron (salvo que sea un cron)            |
| abstenerse                         | resultado vacío                              |
| tombstone                          | borrado definitivo                           |
| el dashboard                       | UI, consola                                  |
| ClickHouse Cloud o ClickHouse OSS  | CH (en prosa)                                |

<a id="formatting"></a>

## Formato

- Markdown, formateado con Prettier (`make format`; CI ejecuta `make format-check`). Los bloques de código Python se formatean con `ruff format`, que CI también comprueba.
- Encabezados en minúsculas, salvo la primera palabra y los nombres propios. Un solo título `#` por página.
- Código, nombres de op, ajustes, rutas y nombres de tablas entre acentos graves. Escribe los ajustes como su variable de entorno (`LOOM_CONFLICT_THRESHOLD`); las páginas de arquitectura pueden usar el nombre del campo (`settings.conflict_threshold`).
- Las referencias al código fuente son `path/to/file.py` más el símbolo: `server/workers/registry.py` (`_WORKERS`). Sin números de línea.
- Los diagramas son Mermaid, solo cuando un flujo se entiende mejor dibujado que escrito, y con menos de ~20 nodos.
- Tablas para opciones y comparaciones; prosa para el razonamiento.
- Enlaces relativos entre documentos. `tests/unit/test_doc_links.py` comprueba cada enlace relativo y cada ancla bajo `docs/` y en los archivos Markdown de la raíz.

<a id="generated-pages"></a>

## Páginas generadas

Estas páginas se generan a partir del código. Edita la fuente y luego ejecuta el generador. `make lint` ejecuta cada uno con `--check`, de modo que una página desactualizada hace fallar CI.

| Página                                                                                | Fuente                                                                                 | Generador                                      |
| ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ---------------------------------------------- |
| [reference/configuration.md](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)                           | comentarios sobre los campos en `server/settings/`                                     | `python scripts/codegen/settings_reference.py` |
| [reference/operations.md](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                 | `server.operations.registry.ALL_OPERATIONS`                                            | `python scripts/codegen/docs_reference.py`     |
| [`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt) (raíz del repositorio) | el título de cada página y la primera frase de su párrafo inicial                 | `python scripts/codegen/docs_reference.py`     |
| `reference/operations/<group>.md` (uno por grupo)                                     | los modelos de entrada y salida de las ops y los comentarios sobre sus campos          | `python scripts/codegen/docs_reference.py`     |
| [reference/workers.md](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md)                                       | `server/workers/registry.py` (`_WORKERS`)                                              | `python scripts/codegen/docs_reference.py`     |
| [reference/cli.md](../reference/cli.md)                                               | la instantánea del analizador de `loom` `tests/unit/snapshots/cli_tree.json`           | `python scripts/codegen/docs_reference.py`     |
| [reference/errors.md](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)                                         | `server/core/errors.py`                                                                | `python scripts/codegen/docs_reference.py`     |
| [reference/status.md](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)                                         | la `maturity` de las ops y los ajustes booleanos de `server/settings/catalog.py` que se cargan como false | `python scripts/codegen/docs_reference.py`     |
| [reference/schema.md](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)                                         | `schemas/schema.sql`                                                                   | `python scripts/codegen/docs_reference.py`     |

En el sitio de documentación, `scripts/docs_site/hooks.py` sirve la copia en Markdown de cada página junto a la propia página, en su URL más `index.html.md` y, para el índice de una sección, en `<folder>.md`, indicada mediante una etiqueta `<link rel="alternate" type="text/markdown">` en la página; `llms.txt` con sus enlaces apuntando a esas copias; `llms-full.txt` (las páginas que explican qué es Loom y cómo empezar); y el `llms-install.md` del repositorio.

El sitio publica las páginas generadas desde `i18n/<language>/docs/` bajo `/<language>/`. El inglés es el idioma predeterminado y canónico. Cada página conserva su ruta relativa a docs bajo `/<language>/`, de modo que la página de inicio y el índice de casos de uso tienen rutas distintas. La página de inicio en inglés enlaza a cada página de inicio traducida disponible, o a su índice de casos de uso traducido cuando la página de inicio no está disponible. Cada página traducida enlaza a su fuente en inglés y a las traducciones equivalentes disponibles. Un aviso marca las traducciones cuyo blob de origen difiere de la página actual en inglés. La generación de traducciones permanece en `scripts/i18n/translate.py`; la compilación del sitio no llama a ningún proveedor de traducción.

Cuando se elimina una fuente en inglés, sus traducciones marcadas conservan sus rutas hasta que el productor elimine los archivos generados. Estas páginas muestran un aviso de fuente eliminada en lugar de un enlace a la página inexistente en inglés. Los enlaces explícitos de respaldo en inglés conservan su destino en el repositorio porque la página traducida correspondiente puede no contener el ancla solicitada.

Las tablas de ops del SDK (`sdk/python/loom/ops.py`, `sdk/typescript/src/generated/ops.ts`) provienen de `scripts/codegen/sdk_py_gen.py` y `sdk_ts_gen.py` ([generación de código](code-generation.md)).

<a id="before-you-push"></a>

## Antes de hacer push

```bash
make format                                   # Prettier formats the Markdown, ruff its Python blocks
uv run pytest tests/unit/test_doc_links.py tests/unit/test_public_copy.py -q
python scripts/codegen/docs_reference.py --check
pip install --require-hashes --no-deps --only-binary=:all: -r scripts/docs_site/requirements.txt  # once, in a virtualenv
mkdocs build --strict                         # the docs site build CI runs
```
