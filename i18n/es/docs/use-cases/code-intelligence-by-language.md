<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=797ba5c0830f3cf220afabc07d4a9e4709260b92 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# Inteligencia de código para C++, Python, TypeScript, Go, Rust, Java y código de infraestructura

El índice de código de Loom responde a las preguntas de estructura de un agente de programación en los lenguajes que contiene un repositorio: dónde se define un símbolo, quién lo llama y qué afecta un cambio.
Requiere haber añadido un repositorio y `LOOM_CODE_INGEST_ENABLED=true`. Todos los lenguajes cuentan con el nivel rápido, analizado con gramáticas de tree-sitter o con escáneres escritos a mano; C/C++, Python, TypeScript y Go pueden añadir un nivel del compilador que resuelve los nombres como lo hace el compilador. La lista completa de extensiones y relaciones está en la
[tabla de lenguajes admitidos](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support), y la configuración de los niveles del compilador se encuentra en
[operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).

Cada respuesta indica el nivel que la produjo (`tier: "scip"` para el nivel del compilador, `"treesitter"` para el nivel rápido), y `code.coverage` informa de lo que contiene un espacio de nombres por lenguaje y nivel. Consúltalo antes de confiar en los llamadores o en el impacto de un lenguaje con el que no hayas usado Loom.

<a id="c-and-c"></a>

## C y C++

Los símbolos, las llamadas, los includes, la herencia y las sobrescrituras provienen del nivel rápido; el nivel del compilador (`scip-clang`) resuelve sobrecargas, plantillas y espacios de nombres a partir del build del proyecto, de modo que `code.symbol` con `uses_brief: true` sobre un nombre calificado como `DB::IMergeTreeDataPart::getBytesOnDisk` devuelve la única definición, su número de sitios de llamada y sus principales sitios de llamada con las funciones que los contienen, cada uno marcado con `tier: "scip"`. El nivel del compilador necesita la configuración de build de CMake del proyecto.

<a id="python"></a>

## Python

Las llamadas, las importaciones, la herencia y las sobrescrituras provienen del nivel rápido, incluidas las celdas de código de los notebooks; el nivel del compilador (`scip-python`) resuelve nombres entre módulos. En el propio repositorio de Loom, `code.symbol` sobre `writer_actor` devuelve su definición en `server/memory/app/provenance.py` con su firma y su docstring en el nivel del compilador, y `uses_brief: true` añade sus puntos de llamada.

<a id="typescript-and-javascript"></a>

## TypeScript y JavaScript

Las llamadas (incluido `new`), las importaciones, la herencia y las sobrescrituras provienen del nivel rápido, al igual que los bloques de script de los componentes de Vue, Svelte y Astro; una plantilla `.component.html` de Angular se indexa como una sola unidad junto con su componente. El nivel del compilador (`scip-typescript`) resuelve los nombres a través del `tsconfig.json`, `jsconfig.json` o `package.json` del proyecto.

<a id="go"></a>

## Go

Las llamadas, la incrustación de interfaces y la implementación estructural provienen del nivel rápido, y el SQL en cadenas de Go se convierte en referencias a tablas; el nivel del compilador (`scip-go`) resuelve nombres entre paquetes, incluidos los monorepos.

<a id="rust-and-java"></a>

## Rust y Java

Rust obtiene llamadas, imports `use` e implementaciones de traits; Java obtiene llamadas, con imports que enlazan nombres, y SQL en cadenas de Java. Ambos tienen solo el nivel rápido: una llamada enlazada en el mismo archivo tiene una confianza de 1.0, a través de la clase envolvente 0.8 y por una coincidencia de nombre en todo el repositorio 0.6; una llamada que no puede enlazar se descarta en lugar de adivinarse, y `code.impact` añade las pruebas asociadas y los socios de cambio conjunto a nivel de archivo.

<a id="sql-and-migrations"></a>

## SQL y migraciones

Los archivos `.sql` se indexan por defecto para búsqueda y grep. Con
`LOOM_CODE_SQL_STATEMENT_RECORDS` activado, cada sentencia se convierte en un registro con sus
referencias a tablas, de modo que un agente puede preguntar qué migraciones y consultas leen o escriben una tabla. Activarlo
en un espacio de nombres ya indexado requiere ejecutar `loom code backfill --paths-file` con los archivos
SQL ([interruptores](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## Infraestructura como código

Terraform y HCL, los manifiestos de Kubernetes, los charts de Helm y las plantillas de Go, Jsonnet, CMake, Protocol Buffers y los scripts de shell se indexan junto con sus referencias, inclusiones y llamadas, de modo que un agente sigue un `include` de Helm hasta su `define`, o una referencia de Terraform hasta lo que nombra. Los jobs de Dockerfile, Makefile, Jenkinsfile y YAML de CI se convierten en símbolos con `LOOM_CODE_HOST_ISLANDS` activado, después de un backfill de esos archivos cuando el espacio de nombres ya está indexado.

<a id="start"></a>

## Inicio

1. [Indexa un repositorio](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md). Los niveles del compilador que se ejecutan
   para él están en [operaciones del índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).
2. [Conecta un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) en el toolset
   `coding`.
3. Haz una pregunta sobre la estructura; revisa `code.coverage` para los lenguajes que usas.

<a id="related"></a>

## Relacionado

- [Un índice de código para agentes de programación](code-intelligence.md)
- [Operaciones de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Añadir compatibilidad con lenguajes](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
