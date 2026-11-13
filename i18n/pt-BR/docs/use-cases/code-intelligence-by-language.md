<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=414b69b208ddf6610590f99678d2dc8e9bff8780 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# Inteligência de código para C++, Python, TypeScript, Go, Rust, Java e código de infraestrutura

O índice de código do Loom responde às perguntas de estrutura de um agente de programação nas linguagens que um repositório contém: onde um símbolo é definido, quem o chama e o que uma mudança afeta.
Exige um repositório adicionado e `LOOM_CODE_INGEST_ENABLED=true`. Toda linguagem tem a camada rápida, analisada com gramáticas tree-sitter ou com scanners escritos à mão; C/C++, Python, TypeScript e Go podem adicionar uma camada do compilador que resolve nomes da mesma forma que o compilador. A lista completa de extensões e arestas está na
[tabela de suporte a linguagens](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support), e a configuração das camadas do compilador está em
[operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).

Uma resposta indica a camada que a originou (`tier: "scip"` para a camada do compilador, `"treesitter"`
para a camada rápida), e `code.coverage` informa o que um namespace contém por linguagem e camada. Consulte-o antes de confiar nas respostas de `code.callers` ou `code.impact` em uma linguagem com a qual você ainda não usou o Loom.

<a id="c-and-c"></a>

## C e C++

Símbolos, chamadas, includes, herança e sobrescritas vêm da camada rápida; a camada do compilador (`scip-clang`) resolve sobrecargas, templates e namespaces a partir do build do projeto, de modo que `code.symbol` com `uses_brief: true` em um nome qualificado como `DB::IMergeTreeDataPart::getBytesOnDisk` retorna a única definição, a contagem de pontos de chamada e os principais pontos de chamada com suas funções envolventes, cada um marcado com `tier: "scip"`. A camada do compilador exige a configuração de build CMake do projeto.

<a id="python"></a>

## Python

Chamadas, imports, herança e sobrescritas vêm da camada rápida, incluindo as células de código de notebooks; a camada do compilador (`scip-python`) resolve nomes entre módulos. No repositório do próprio Loom, `code.symbol` em `writer_actor` retorna sua definição em
`server/memory/app/provenance.py` com a assinatura e a docstring na camada do compilador, e `uses_brief: true` acrescenta seus pontos de chamada.

<a id="typescript-and-javascript"></a>

## TypeScript e JavaScript

Chamadas (incluindo `new`), imports, herança e sobrescritas vêm da camada rápida, assim como os blocos de script de componentes Vue, Svelte e Astro; um template `.component.html` do Angular é indexado como uma única unidade com seu componente. A camada do compilador (`scip-typescript`) resolve nomes por meio do `tsconfig.json`, `jsconfig.json` ou `package.json` do projeto.

<a id="go"></a>

## Go

Chamadas, embedding de interfaces e implementação estrutural vêm da camada rápida, e o SQL em strings Go se torna referências a tabelas; a camada do compilador (`scip-go`) resolve nomes entre pacotes, incluindo monorepos.

<a id="rust-and-java"></a>

## Rust e Java

Rust recebe chamadas, imports `use` e implementações de traits; Java recebe chamadas, com imports vinculando nomes, e SQL em strings Java. Ambas têm apenas a camada rápida: uma chamada vinculada no mesmo arquivo tem confiança 1.0, pela classe envolvente 0.8 e por correspondência de nome em todo o repositório 0.6, uma chamada que não consegue ser vinculada é recusada em vez de adivinhada, e `code.impact` acrescenta testes mapeados e parceiros de co-alteração no nível de arquivo.

<a id="sql-and-migrations"></a>

## SQL e migrações

Os arquivos `.sql` são indexados por padrão para busca e grep. Com
`LOOM_CODE_SQL_STATEMENT_RECORDS` ativado, cada instrução se torna um registro com suas referências a tabelas, de modo que um agente pode perguntar quais migrações e consultas leem ou escrevem em uma tabela. Ativá-lo em um namespace já indexado exige um `loom code backfill --paths-file` com os arquivos SQL
([chaves de configuração](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## Infraestrutura como código

Terraform e HCL, manifestos Kubernetes, charts Helm e templates Go, Jsonnet, CMake, Protocol Buffers e scripts shell são indexados com suas referências, includes e chamadas, de modo que um agente acompanha um `include` do Helm até seu `define`, ou uma referência do Terraform até o que ela nomeia. Dockerfile, Makefile, Jenkinsfile e jobs de YAML de CI se tornam símbolos com `LOOM_CODE_HOST_ISLANDS` ativado, após um backfill desses arquivos quando o namespace já está indexado.

<a id="start"></a>

## Primeiros passos

1. [Indexe um repositório](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md). As camadas do compilador que rodam
   para ele estão em [operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).
2. [Conecte um agente de programação](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) no toolset
   `coding`.
3. Faça uma pergunta de estrutura; consulte `code.coverage` para as linguagens em que você confia.

<a id="related"></a>

## Relacionados

- [Um índice de código para agentes de programação](code-intelligence.md)
- [Operações de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Adicionando suporte a linguagens](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
