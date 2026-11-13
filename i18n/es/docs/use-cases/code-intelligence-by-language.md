<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=0ed7353714a116300c522e3520e5b1986b56ca81 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# Inteligencia de código para C++, Python, TypeScript, Go, Rust, Java y código de infraestructura

El índice de código de Loom responde las preguntas de estructura de un agente de programación en los lenguajes que contiene un repositorio: dónde se define un símbolo, quién lo llama y qué toca un cambio.
Requiere un repositorio añadido y `LOOM_CODE_INGEST_ENABLED=true`. Todos los lenguajes cuentan con el nivel rápido, analizado con gramáticas de tree-sitter o con escáneres escritos a mano; C/C++, Python, TypeScript y Go pueden añadir un nivel de compilador que resuelve los nombres igual que lo hace el compilador. La lista completa de extensiones y relaciones está en la [tabla de lenguajes admitidos](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support), y la configuración de los niveles de compilador está en
[operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).

Cada respuesta indica el nivel que la produjo (`tier: "scip"` para el nivel de compilador, `"treesitter"` para el nivel rápido), y `code.coverage` informa de lo que contiene un espacio de nombres por lenguaje y nivel. Consúltalo antes de confiar en los llamadores o en el impacto en un lenguaje con el que no hayas usado Loom.

<a id="c-and-c"></a>

## C y C++

Los símbolos, las llamadas, los includes, la herencia y los overrides provienen del nivel rápido; el
nivel del compilador (`scip-clang`) resuelve sobrecargas, plantillas y espacios de nombres a partir de la compilación
del proyecto, de modo que `code.symbol` con `uses_brief: true` sobre un nombre cualificado como
`DB::IMergeTreeDataPart::getBytesOnDisk` devuelve la única definición, su número de sitios de llamada
y sus principales sitios de llamada con las funciones que los contienen, cada uno marcado con `tier: "scip"`. El nivel del compilador necesita la
configuración de compilación CMake del proyecto.

<a id="python"></a>

## Python

Las llamadas, las importaciones, la herencia y las sobrescrituras provienen del nivel rápido, incluidas las celdas de código de los notebooks; el nivel del compilador (`scip-python`) resuelve nombres entre módulos. En el propio repositorio de Loom, `code.symbol` sobre `writer_actor` devuelve su definición en `server/memory/app/provenance.py` con su firma y su docstring en el nivel del compilador, y `uses_brief: true` añade sus sitios de llamada.

<a id="typescript-and-javascript"></a>

## TypeScript y JavaScript

Las llamadas (incluido `new`), las importaciones, la herencia y las sobrescrituras provienen del nivel rápido, al igual que los bloques de script de los componentes de Vue, Svelte y Astro; una plantilla `.component.html` de Angular se indexa como una sola unidad junto con su componente. El nivel de compilador (`scip-typescript`) resuelve los nombres a través del `tsconfig.json`, el `jsconfig.json` o el `package.json` del proyecto.

<a id="go"></a>

## Go

Las llamadas, la incrustación de interfaces y la implementación estructural provienen del nivel rápido, y el SQL en cadenas de Go se convierte en referencias a tablas; el nivel del compilador (`scip-go`) resuelve nombres entre paquetes, incluidos los monorepos.

<a id="rust-and-java"></a>

## Rust y Java

Rust admite llamadas, importaciones `use` e implementaciones de traits; Java admite llamadas, con importaciones que enlazan nombres, y SQL en cadenas de Java. Ambos tienen solo el nivel rápido: una llamada enlazada en el mismo archivo tiene una confianza de 1.0, a través de la clase envolvente 0.8 y mediante una coincidencia de nombre en todo el repositorio 0.6. Una llamada que no se puede enlazar se rechaza en lugar de adivinarse, y `code.impact` añade las pruebas asociadas y los socios de cambio conjunto a nivel de archivo.

<a id="sql-and-migrations"></a>

## SQL y migraciones

Los archivos `.sql` se indexan por defecto para búsqueda y grep. Con
`LOOM_CODE_SQL_STATEMENT_RECORDS` activado, cada sentencia se convierte en un registro con sus
referencias a tablas, de modo que un agente puede preguntar qué migraciones y consultas leen o escriben una tabla. Activarlo
en un espacio de nombres ya indexado requiere ejecutar `loom code backfill --paths-file` con los archivos
SQL ([opciones](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## Infraestructura como código

Terraform y HCL, los manifiestos de Kubernetes, los charts de Helm y las plantillas de Go, Jsonnet, CMake, Protocol Buffers y los scripts de shell se indexan junto con sus referencias, includes y llamadas, de modo que un agente puede seguir un `include` de Helm hasta su `define`, o una referencia de Terraform hasta lo que nombra. Los archivos Dockerfile, Makefile, Jenkinsfile y los jobs de YAML de CI se convierten en símbolos con `LOOM_CODE_HOST_ISLANDS` activado, después de un backfill de esos archivos cuando el espacio de nombres ya está indexado.

<a id="start"></a>

## Inicio

1. [Indexa un repositorio](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md); los niveles del compilador se activan cuando el repositorio incluye su archivo de compilación.
2. [Conecta un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) en el toolset `coding`.
3. Haz una pregunta sobre la estructura del código; revisa `code.coverage` para ver los lenguajes en los que te apoyas.

<a id="related"></a>

## Relacionado

- [Un índice de código para agentes de programación](code-intelligence.md)
- [Operaciones de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Añadir soporte de lenguajes](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
