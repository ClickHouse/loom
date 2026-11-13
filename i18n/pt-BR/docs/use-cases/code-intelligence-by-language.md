<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=ecd38c904b18ebd717798fb62bc55f139071429b model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# Inteligência de código para C++, Python, TypeScript, Go, Rust, Java e código de infraestrutura

O índice de código do Loom responde às perguntas de estrutura de um agente de programação nas linguagens que um
repositório contém: onde um símbolo é definido, quem o chama e o que uma mudança afeta.
Exige um repositório adicionado e `LOOM_CODE_INGEST_ENABLED=true`. Toda linguagem tem a camada
rápida, analisada com gramáticas tree-sitter ou scanners escritos à mão; C/C++, Python, TypeScript
e Go podem adicionar uma camada do compilador que resolve nomes como o compilador faz. A lista completa
de extensões e arestas está na
[tabela de suporte a linguagens](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support), e a configuração
das camadas do compilador está em
[operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).

Uma resposta indica a camada que a gerou (`tier: "scip"` para a camada do compilador, `"treesitter"`
para a camada rápida), e `code.coverage` informa o que um namespace contém por linguagem e
camada. Consulte-o antes de confiar em callers ou impact em uma linguagem com a qual você ainda não usou o Loom.

<a id="c-and-c"></a>

## C e C++

Símbolos, chamadas, includes, herança e sobrescritas vêm da camada rápida; a
camada do compilador (`scip-clang`) resolve sobrecargas, templates e namespaces a partir do
build do projeto, de modo que `code.symbol` com `uses_brief: true` em um nome qualificado como
`DB::IMergeTreeDataPart::getBytesOnDisk` retorna a única definição, sua contagem de pontos de chamada
e seus principais pontos de chamada com as funções que os contêm, cada um marcado com `tier: "scip"`. A
camada do compilador exige a configuração de build CMake do projeto.

<a id="python"></a>

## Python

Chamadas, imports, herança e sobrescritas vêm da camada rápida, incluindo as células de código
de notebooks; a camada do compilador (`scip-python`) resolve nomes entre módulos. No repositório
do próprio Loom, `code.symbol` em `writer_actor` retorna sua definição em
`server/memory/app/provenance.py` com a assinatura e a docstring na camada do compilador,
e `uses_brief: true` adiciona seus pontos de chamada.

<a id="typescript-and-javascript"></a>

## TypeScript e JavaScript

Chamadas (incluindo `new`), imports, herança e sobrescritas vêm da camada rápida, assim como
os blocos de script de componentes Vue, Svelte e Astro; um template `.component.html` do Angular
é indexado como uma unidade junto com seu componente. A camada do compilador (`scip-typescript`)
resolve nomes por meio de `tsconfig.json`, `jsconfig.json` ou `package.json` do projeto.

<a id="go"></a>

## Go

Chamadas, embedding de interfaces e implementação estrutural vêm da camada rápida, e
SQL em strings Go vira referências de tabela; a camada do compilador (`scip-go`) resolve nomes
entre pacotes, incluindo monorepos.

<a id="rust-and-java"></a>

## Rust e Java

Rust recebe chamadas, imports `use` e implementações de trait; Java recebe chamadas, com imports
associando nomes, e SQL em strings Java. Ambas têm apenas a camada rápida: uma chamada associada no
mesmo arquivo tem confiança 1.0, pela classe envolvente 0.8 e por uma correspondência de nome em todo o repositório
0.6; uma chamada que não consegue ser associada é recusada em vez de adivinhada, e `code.impact` adiciona
testes mapeados e parceiros de co-change no nível de arquivo.

<a id="sql-and-migrations"></a>

## SQL e migrations

Arquivos `.sql` são indexados por padrão para busca e grep. Com
`LOOM_CODE_SQL_STATEMENT_RECORDS` ativado, cada instrução vira um registro com suas referências
de tabela, de modo que um agente pode perguntar quais migrations e consultas leem ou escrevem uma tabela. Ativá-lo
para um namespace já indexado exige um `loom code backfill --paths-file` dos arquivos
SQL ([chaves de ativação](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## Infraestrutura como código

Terraform e HCL, manifestos Kubernetes, charts Helm e templates Go, Jsonnet, CMake,
Protocol Buffers e shell scripts são indexados com suas referências, includes e chamadas,
de modo que um agente segue um `include` do Helm até seu `define`, ou uma referência do Terraform até o que ela
nomeia. Dockerfile, Makefile, Jenkinsfile e jobs de YAML de CI viram símbolos com
`LOOM_CODE_HOST_ISLANDS` ativado, após um backfill desses arquivos quando o namespace já está
indexado.

<a id="start"></a>

## Primeiros passos

1. [Indexe um repositório](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md). As camadas do compilador que rodam
   para ele estão em [operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).
2. [Conecte um agente de programação](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) no toolset
   `coding`.
3. Faça uma pergunta de estrutura; verifique `code.coverage` para as linguagens em que você confia.

<a id="related"></a>

## Relacionado

- [Um índice de código para agentes de programação](code-intelligence.md)
- [Operações de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Adicionando suporte a linguagens](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
