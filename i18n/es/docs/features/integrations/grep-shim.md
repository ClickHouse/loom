<!-- i18n: source=docs/features/integrations/grep-shim.md blob=1ce5e2990b99fc9c2e3b50a80f6b06f33fa415f0 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/grep-shim.md), which is authoritative.

<a id="the-grep-shim"></a>

# El shim de grep

El shim de grep es un wrapper de `grep`/`rg` que añade a la línea de cada coincidencia la función que la contiene, obtenida del índice de código; requiere Node 18 o posterior, un token de visor y un repositorio en el [índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md). Está pensado para agentes que solo usan grep de shell, o que se ejecutan donde los hooks no funcionan (`claude --bare` omite el desvío de código del [plugin de Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md)). El agente, su prompt y su harness no cambian.

```text
$ grep -rn "getSettingsRef().max_threads" src/Processors
src/Processors/QueryPlan/ReadFromMergeTree.cpp:1466:    ... max_threads ...   ⟵ DB::ReadFromMergeTree::selectRangesToRead
src/Processors/Executors/PipelineExecutor.cpp:298:      ... max_threads ...   ⟵ DB::PipelineExecutor::initializeExecution
loom: 2/2 hits scoped to their enclosing function via code.enclosing, index at c7a9fe7c9f, 180 ms
```

<a id="install"></a>

## Instalación

```bash
export PATH=/path/to/loom/integrations/grep-shim/bin:$PATH
export LOOM_BASE_URL=https://loom.example.com   # or ~/.loom/config.json {url, token}
export LOOM_API_KEY=<viewer token>              # or ~/.loom/token
export LOOM_CODE_NAMESPACE=code-myrepo
export LOOM_AGENT=pr-analyst                    # optional: names the caller in the ledger
```

`which grep` debe apuntar al shim. Ejecuta el agente dentro del checkout que indexa el espacio de nombres: las rutas se hacen relativas a `git rev-parse --show-toplevel`. Una discrepancia de rama se manifiesta como coincidencias sin resolver, no como coincidencias erróneas.

El shim ejecuta el primer `grep` o `rg` de `PATH` que no sea él mismo (`LOOM_GREP_REAL` y `LOOM_RG_REAL` fuerzan una ruta).

<a id="guarantees"></a>

## Garantías

- **Cada byte que imprime la herramienta real se imprime, en orden.** El único cambio es un sufijo opcional `   ⟵ <qualified name>` en las líneas de coincidencia cuya función contenedora conoce el índice, y un resumen de una línea en stderr.
- **Se conserva el código de salida** (grep: 0 con coincidencias, 1 sin coincidencias, 2 error).
- **Se anotan dos formatos:** `path:line:text` (varios operandos, `-r` sobre un directorio, `-H`) y `line:text` de exactamente un archivo como operando (`grep -n pattern file.cpp`, el formato al que se acomoda un agente cuando ya conoce el archivo; la ruta es el operando, así que `-h` no supone problema ahí). Todo lo demás pasa sin tocar: sin `-n`, `-l`, `-L`, `-c`, `-o`, `-z`, `-Z`, `-b`, `-q`, `--color=always` (rg: `-N`, `--json`, `--files`, `-0`), stdin, o varios archivos con `-h`.
- **Un límite estricto de latencia.** `LOOM_GREP_SHIM_TIMEOUT_MS` (por defecto 2500). Cualquier error, un timeout, un token ausente o más de 8 MB de salida (`LOOM_GREP_SHIM_MAX_BYTES`) imprime la salida sin modificar.
- **Carga acotada en el servidor.** Una llamada a `code.enclosing` por cada 200 coincidencias distintas, con un máximo de 5 en paralelo; se anotan 1,000 coincidencias y el resto se imprime sin anotar.

<a id="the-dependency-graph-block"></a>

## El bloque del grafo de dependencias

`LOOM_GREP_SHIM_GRAPH=on` (por defecto off) añade un bloque después de la salida real: `[Related from loom dependency graph]`, y luego una línea por cada función contenedora con su módulo, hasta `LOOM_GREP_SHIM_GRAPH_K` (5) llamadores, llamados y pruebas, y una nota `(lower bound: …)` cuando el grafo está incompleto. Cortar desde la línea de encabezado devuelve los bytes reales. El bloque usa la misma llamada a `code.enclosing`, el mismo timeout y las mismas reglas de fallo abierto; se omite si hay más de `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS` (20) funciones o cuando la salida se transmitió en streaming (más de 8 MB).

<a id="switches"></a>

## Interruptores

| Variable                                | Por defecto                            | Efecto                                                                                  |
| --------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_GREP_SHIM`                        | on                                     | `off` convierte el shim en un simple pass-through                                       |
| `LOOM_GREP_SHIM_QUIET`                  | sin definir                            | defínelo para suprimir la línea de resumen en stderr                                    |
| `LOOM_GREP_SHIM_TIMEOUT_MS`             | 2500                                   | el límite de latencia                                                                   |
| `LOOM_GREP_SHIM_MAX_BYTES`              | 8 MB                                   | la salida que supere esto pasa sin modificar                                            |
| `LOOM_GREP_SHIM_GRAPH`                  | off                                    | `on` añade el bloque del grafo de dependencias                                          |
| `LOOM_GREP_SHIM_GRAPH_K`                | 5                                      | vecinos por función (1–20)                                                              |
| `LOOM_GREP_SHIM_GRAPH_MAX_ANCHORS`      | 20                                     | número de funciones a partir del cual se omite el bloque                                |
| `LOOM_CODE_NAMESPACE`, `LOOM_AGENT`     |                                        | el espacio de nombres de código; el nombre del agente que llama (por defecto `grep-shim`) |
| `LOOM_CONFIG_FILE`, `LOOM_API_KEY_FILE` | `~/.loom/config.json`, `~/.loom/token` | de dónde se leen la URL, el espacio de nombres y el token cuando el entorno no los define |

<a id="server-side-codeenclosing"></a>

## Del lado del servidor: `code.enclosing`

El shim es un cliente ligero sobre una sola operación. `code.enclosing` (`POST /v1/code.enclosing`, visor) recibe `locations: ["src/F.cpp:12", …]` (hasta 200) y devuelve un resultado por ubicación en el orden de entrada, con `symbol == ""` cuando la línea está en el ámbito del archivo o la ruta no está indexada. Cada fila resuelta incluye `symbol_id`.

- `include_body: true` (con `token_budget`, por defecto 4000) coloca en la fila el cuerpo completo de cada función contenedora, obtenido una vez por función distinta y rellenado en orden de primera aparición hasta agotar el presupuesto; el resto incluyen un `next` listo para pegar (`code.symbol(symbol_id=…)`). El shim lo deja desactivado.
- `neighbours: true` (con `neighbour_k`, por defecto 5) devuelve los datos del bloque del grafo: una entrada por cada función contenedora distinta (como máximo 20; `anchors_capped` cuando se resolvieron más) con `callers`, `callees` (se prefiere el nivel del compilador; se descartan los símbolos fuera del repositorio y las suposiciones del parser con confianza inferior a 0.8), `tests`, `module` y el envelope `epistemic` / `causes`.

`code.symbol` también acepta `name: "src/F.cpp:12"` para una sola ubicación, y el SDK de Python expone `agent.code.enclosing([...], include_body=True)`. Consulta las [operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md).

<a id="limits"></a>

## Límites

- **Solo código integrado.** El índice conoce la rama que indexa; una coincidencia en una edición sin commit se resuelve contra la versión indexada del archivo, o no se resuelve en absoluto.
- **Mejor esfuerzo.** Ante cualquier imprevisto se imprime la salida sin modificar; una línea sin anotar no significa que grep haya fallado.
- **Las mismas búsquedas.** El agente sigue usando grep. Un agente que pueda llamar a herramientas MCP debería usar `code.search`, `code.callers` y `code.impact` directamente.

<a id="related"></a>

## Relacionado

- [Resumen del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md).
- [Plugin de Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md): el desvío de código basado en hooks para agentes que sí ejecutan hooks.
- [Resumen de integraciones](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md).
