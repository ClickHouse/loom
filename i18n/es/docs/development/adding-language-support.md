<!-- i18n: source=docs/development/adding-language-support.md blob=ee2dee0c86ab7bfb46f22017d1d71ab8005a2276 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md), which is authoritative.

<a id="add-language-support-to-the-code-index"></a>

# Añadir compatibilidad con un lenguaje al índice de código

El índice de código lee un repositorio en dos niveles:

- El **nivel rápido** (gramáticas de tree-sitter y escáneres escritos a mano) se ejecuta sobre cada archivo en cada commit. Produce chunks, símbolos y aristas de llamadas.
- Un **nivel del compilador** (un indexador SCIP sobre los archivos modificados) añade símbolos y aristas exactos según el compilador, donde existe un indexador.

Un lenguaje cuenta con compatibilidad cuando el nivel rápido tiene una vía para él. [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md) explica los niveles y enumera lo que obtiene cada lenguaje.

<a id="the-one-rule-stay-outside-the-extractor-stamp"></a>

## La regla principal: mantenerse fuera del sello del extractor

Las filas del nivel rápido llevan un sello de versión del código que las produjo (`server/code/domain/extract/extractor_stamp.py`):

- El sello es un sha256 sobre los **tokens de código** (`logic_tokens`: sin comentarios, líneas en blanco ni docstrings; un import cuenta como los nombres que enlaza, no como la ruta del paquete) de cada módulo de `STAGE_MODULES`, más las versiones instaladas de las gramáticas de `STAMPED_GRAMMARS` (`tree-sitter`, `tree-sitter-cpp`, `tree-sitter-python`, `tree-sitter-bash`).
- Cuando el sello cambia, el worker de ingesta vuelve a derivar las etapas afectadas (`chunk`, `symbol`, `edge`, `guard`) en lotes espaciados para cada archivo de cada espacio de nombres indexado en las familias de lenguajes afectadas (`_maybe_rechunk` en `server/code/app/index/ingest_pass.py`).
- Un módulo asignado a una familia en `MODULE_LANGUAGES` (`lang_python.py`, los módulos `ts_*`) solo mueve los archivos de esa familia. Un módulo compartido (`treesitter.py`, `chunker.py`) o la gramática principal `tree-sitter` mueve todos los archivos.
- En un despliegue grande eso son horas de ingesta.

Añade un lenguaje nuevo donde no mueva ningún sello:

| Archivo                                                        | ¿En el sello? | Qué va ahí                                                                                                                    |
| -------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `server/code/domain/extract/lang_<lang>.py`                    | no            | El carril: `parse_<lang>(path, source) -> ParsedFile` con símbolos, includes y sitios de llamada.                             |
| `server/code/domain/extract/lang_detect.py`                    | no            | `LANG_BY_EXTENSION` (y las reglas de shell): qué archivos son de este lenguaje.                                               |
| `server/code/app/index/ingest_parse.py`                        | no            | Un wrapper `_parse_<lang>` en `_PARSERS`; resolución de includes en `ingest_resolve.py` si el carril tiene imports.          |
| `server/code/domain/extract/lane_units.py`                     | no            | Helpers compartidos para un carril **sin** gramática: `span_chunks` y el enlazador de referencias `bind_sites`.               |
| `server/code/domain/extract/treesitter.py`                     | **sí**        | `lang_for_path` solo para los lenguajes principales sellados. Añadir una extensión aquí vuelve a fragmentar todos los espacios de nombres. |
| `chunker.py`, `edges_treesitter.py`, los demás `STAGE_MODULES` | **sí**        | Fragmentación y extracción de aristas compartidas. Cámbialos solo cuando la extracción cambie para todos.                     |

Los lenguajes que `lang_for_path` no nombra se detectan en `lang_detect.py`, así que añadir uno no mueve ningún sello. Los comentarios y docstrings de los módulos sellados se pueden editar libremente. Esos módulos están excluidos del formateo, porque un formateador podría reescribir las comillas de las cadenas, que son tokens de código.

Cada módulo de `server/code/domain/extract/` está en una etapa de `STAGE_MODULES` o aparece, con su motivo, en `NON_EXTRACTOR_MODULES` en `tests/unit/test_code_extractor_stamp.py`. Pon un módulo de carril nuevo en `NON_EXTRACTOR_MODULES`, o esa prueba fallará.

<a id="1-choose-the-lanes-shape"></a>

## 1. Elige la forma del lane

| Forma             | Ejemplos                                                                                                   | Lo que aportas                                                                                                                                                                                  |
| ----------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lane de gramática | `lang_typescript.py`, `lang_rust.py`, `lang_go.py`, `lang_java.py`                                         | Una dependencia de gramática tree-sitter en `pyproject.toml`; símbolos y sitios de llamada obtenidos del árbol; los chunks y las aristas provienen del chunker compartido y del recorredor de aristas. |
| Lane sin árbol    | `lang_proto.py`, `lang_gotemplate.py`, `lang_hcl.py`, `lang_k8s.py`, `lang_jsonnet.py` (`LANE_UNIT_LANGS`) | Un escáner escrito a mano; `ParsedFile.tree` queda en `None`; chunks de `lane_units.span_chunks`, aristas de `reference_sites` + `SITE_RULES` enlazados mediante `lane_units.bind_sites`.         |
| Lane solo de includes | `tla`, `flatbuffers`, `cmake`, `doc` (`_LANE_INCLUDE_LANGS`)                                           | Símbolos e includes resueltos por el resolvedor de lane simple.                                                                                                                                 |
| Lenguaje embebido | SQL dentro de Go y Java (`go_sql_units`, `java_sql_units`), islas de shell en Dockerfiles y Makefiles      | Unidades incorporadas al análisis del archivo anfitrión (`_join_island_symbols`), normalmente tras un ajuste de interruptor de desactivación.                                                   |
| Solo chunks       | `.css`, archivos de texto (`lang_text.py`)                                                                 | Solo la detección: el archivo se divide en chunks y se puede buscar, sin símbolos.                                                                                                              |

La gramática de un lane de gramática **no** está en `STAMPED_GRAMMARS`, por lo que actualizarla no cambia ningún sello. El lane es responsable de volver a derivar sus filas.

<a id="2-write-the-parser"></a>

## 2. Escribir el parser

`parse_<lang>(path: str, source: bytes) -> ParsedFile` devuelve la etiqueta del lenguaje, los símbolos (`treesitter.Symbol`: nombre calificado, tipo, rango, firma), los includes y lo que necesita la etapa de aristas. Sigue el lane existente más cercano:

- **Los nombres calificados** siguen la ruta de módulos del lenguaje (Rust: la ruta del crate a partir de la ruta del archivo; `mod.rs` / `lib.rs` dan nombre a su directorio). El id de un símbolo es un hash del nombre calificado y de la firma normalizada, así que un nombre inestable regenera filas en cada reindexado.
- **Los includes** se resuelven contra las rutas que contiene el espacio de nombres (`wanted_paths`, y después `resolve_imports` / `resolve_uses` en el lane, llamados desde `ingest_resolve.py`). Los paquetes externos no se resuelven a nada, no a una suposición.
- **Las aristas de llamada** enlazan primero los nombres únicos dentro del mismo archivo, después los únicos en el repositorio con menor confianza, y no enlazan nada cuando un nombre es ambiguo (`lane_units.bind_sites` hace esto para los lanes sin árbol).
- **Los tipos de contenedor** (una clase, un bloque impl) pertenecen al lane. Registrar uno en la tabla por lenguaje del chunker modifica `chunker.py` y cambia el stamp.

Registra el lane:

```python
# server/code/app/index/ingest_parse.py
def _parse_rust(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust(path, source)


_PARSERS = {
    ...
    "rust": _parse_rust,
    ...
}
```

y añade la extensión a `LANG_BY_EXTENSION` (`".rs": "rust"`). El wrapper importa el lane de forma diferida, de modo que una gramática opcional sigue siendo opcional. Una nueva gramática de tree-sitter también se añade a `bounded_parse._LOADERS`, por donde pasa primero cada parseo.

<a id="3-test-the-lane"></a>

## 3. Prueba el lane

- **Una prueba del lane**, `tests/unit/test_code_lang_<lang>.py`, analiza pequeñas fuentes en línea y verifica nombres, tipos, rangos, includes y edges. Se omite cuando la gramática no está presente: `pytest.importorskip("tree_sitter_rust")`. `test_code_lang_rust.py` y `test_code_lang_typescript.py` son los modelos.
- **El corpus de lanes.** Agrega fixtures en `tests/fixtures/code_lanes/<lang>/`: sintéticos, o extractos de código abierto bajo su propia licencia, listados en `tests/fixtures/code_lanes/NOTICE.md`. `tests/unit/test_code_lanes_corpus.py` deriva lenguajes, símbolos, facetas, includes y edges de cada fixture y los compara con `tests/fixtures/code_lanes/expected.json`. Regenera y revisa el diff:

  ```bash
  python -m tests.code_lanes.corpus --write
  git diff tests/fixtures/code_lanes/expected.json
  ```

  Un número que cambia ahí modifica lo que contiene cada espacio de nombres. Revísalo.

- **La protección de costo.** Un parser agregado a `ingest_parse._PARSERS` necesita una entrada `SAMPLES` en `tests/unit/lane_walks.py`; `tests/unit/test_code_regex_cost.py` luego mide el tiempo del lane sobre formas generadas y falla ante un recorrido que supere el tiempo lineal.
- **La prueba del stamp.** `tests/unit/test_code_extractor_stamp.py` con el nuevo módulo en
  `NON_EXTRACTOR_MODULES`.
- **Recuperación.** Para un lane que deba responder bien a `code.search` o `code.symbol`, ejecuta el gate de code-intelligence antes y después. `make bench-code-intel-dry` comprueba el conjunto de preguntas sin conexión; `make bench-code-intel` lo ejecuta contra un servidor con un embedder real, nunca el falso ([ejecución de benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="4-bring-existing-namespaces-up-to-date"></a>

## 4. Actualizar los espacios de nombres existentes

Un carril fuera del stamp no cambia nada de lo que ya está indexado. Los commits nuevos usan el carril; los archivos antiguos conservan sus filas anteriores (a menudo solo con chunks). Vuelva a derivarlos con un backfill dirigido:

```bash
git -C <checkout of the repository> ls-files '*.rs' > rust-paths.txt
loom code backfill --org <org> --namespace <code-namespace> --paths-file rust-paths.txt
```

`--paths-file` vuelve a ingerir solo esas rutas relativas al repositorio en la punta de la rama y no modifica la marca de agua. Ejecútelo donde estén el `.env` del servidor y los espejos de los repositorios, por ejemplo en el contenedor de workers ([operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)).

> **Advertencia:** si un cambio debe tocar un módulo con stamp (una corrección en `chunker.py`, una extensión en `treesitter.py`), indíquelo en el PR. Cada despliegue que lo incorpore vuelve a derivar las etapas afectadas para todos los archivos de las familias correspondientes en todos los espacios de nombres. Un cambio en `chunker.py` solo mueve `chunk`; un cambio en `edges_treesitter.py` mueve `edge`, `chunk` y `guard`; un cambio en el parser mueve todas las etapas.

<a id="5-add-a-compiler-tier-optional"></a>

## 5. Añadir un nivel del compilador (opcional)

Un nivel del compilador compensa cuando las aristas del nivel rápido son ambiguas (sobrecargas, despacho dinámico, reexportaciones) y existe un indexador SCIP. `TIERS` en `server/code/domain/index/compiler_tiers.py` enumera los niveles. Cada `TierSpec` nombra el indexador (`scip-clang`, `scip-typescript`, `scip-go`, `scip-python`), su clave de marca de agua en `code_index_state` (`scip_commit`, `scip_commit_ts`, `scip_commit_go`, `scip_commit_py`) y los workers que la hacen avanzar. Un nivel nuevo necesita:

1. Un arnés que ejecute el indexador sobre los archivos modificados de un rango de commits y convierta
   su salida al delta JSON de Loom: `scripts/scip_harness/` (`run_delta.sh`,
   `run_typescript_delta.sh`, `run_python_delta.sh`, `run_go_monorepo_delta.sh`,
   `scip_to_delta.py`).
2. Un worker de delta (`server/code/workers/scip_ts_delta.py`, `scip_py_delta.py` y
   `scip_go_delta.py` son los modelos): su `run_pass` entrega su `_run_repo` a
   `run_tier` en `server/code/app/index/compiler_tier.py`, que se encarga del bucle de entradas, el aislamiento de fallos y el reinicio de la ejecución completa. Viene con sus ajustes `LOOM_CODE_SCIP_<LANG>_DELTA_ENABLED` /
   `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS`
   ([añadir un worker](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-worker.md)). Un productor externo sube los datos mediante
   `code.scip_ingest`, y el worker `code_scip_apply` aplica los lotes completos.
3. Una clave de marca de agua en `watermark_key_for` de `code_scip_apply` y una fila de `TierSpec`, para que
   `code.index_status` y el dashboard informen del nivel.

Las filas del nivel del compilador llevan `tier='scip'`. Las lecturas las combinan con el nivel rápido por nombre calificado, dando preferencia al nivel del compilador. Los archivos que el indexador omite, y los commits anteriores a la llegada de su delta, se siguen leyendo desde el nivel rápido.

<a id="related"></a>

## Relacionado

- [Descripción general del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- [Arquitectura del índice de código](../architecture/code-index-architecture.md)
- [Operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md)
- [Ampliar Loom](https://github.com/ClickHouse/loom/blob/main/docs/development/extending-loom.md)
