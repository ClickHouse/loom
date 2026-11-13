<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=0ed7353714a116300c522e3520e5b1986b56ca81 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# Inteligência de código para C++, Python, TypeScript, Go, Rust, Java e código de infraestrutura

O índice de código do Loom responde às perguntas de estrutura de um agente de programação nas linguagens que um
repositório contém: onde um símbolo é definido, quem o chama e o que uma alteração afeta.
Requer um repositório adicionado e `LOOM_CODE_INGEST_ENABLED=true`. Toda linguagem recebe a
camada rápida, analisada com gramáticas tree-sitter ou scanners escritos à mão; C/C++, Python, TypeScript
e Go podem adicionar uma camada de compilador que resolve nomes da mesma forma que o compilador. A lista completa de extensões e arestas está na
[tabela de suporte a linguagens](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support), e a configuração das
camadas de compilador está em
[operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).

Uma resposta informa a camada que a originou (`tier: "scip"` para a camada de compilador, `"treesitter"`
para a camada rápida), e `code.coverage` informa o que um namespace contém por linguagem e
camada. Consulte-o antes de confiar em chamadores ou impacto em uma linguagem com a qual você ainda não usou o Loom.

<a id="c-and-c"></a>

## C e C++

Símbolos, chamadas, includes, herança e overrides vêm da camada rápida; a camada do compilador (`scip-clang`) resolve overloads, templates e namespaces a partir do build do projeto. Assim, `code.symbol` com `uses_brief: true` em um nome qualificado como `DB::IMergeTreeDataPart::getBytesOnDisk` retorna a única definição, o número de pontos de chamada e os principais pontos de chamada com as funções que os contêm, cada um marcado com `tier: "scip"`. A camada do compilador precisa da configuração de build CMake do projeto.

<a id="python"></a>

## Python

Chamadas, imports, herança e overrides vêm da camada rápida (fast tier), incluindo as células de código de notebooks; a camada de compilador (`scip-python`) resolve nomes entre módulos. No repositório do próprio Loom, `code.symbol` em `writer_actor` retorna sua definição em `server/memory/app/provenance.py` com a assinatura e a docstring na camada de compilador, e `uses_brief: true` adiciona seus pontos de chamada.

<a id="typescript-and-javascript"></a>

## TypeScript e JavaScript

Chamadas (incluindo `new`), imports, herança e sobrescritas vêm da camada rápida, assim como os blocos de script de componentes Vue, Svelte e Astro; um template `.component.html` do Angular é indexado como uma única unidade junto com seu componente. A camada do compilador (`scip-typescript`) resolve nomes por meio do `tsconfig.json`, do `jsconfig.json` ou do `package.json` do projeto.

<a id="go"></a>

## Go

Chamadas, embedding de interfaces e implementação estrutural vêm da camada rápida, e o
SQL em strings Go vira referências de tabela; a camada de compilador (`scip-go`) resolve nomes
entre pacotes, incluindo monorepos.

<a id="rust-and-java"></a>

## Rust e Java

Em Rust, o Loom indexa chamadas, imports `use` e implementações de trait; em Java, indexa chamadas, com imports vinculando nomes, e SQL em strings Java. Ambas têm apenas a camada rápida: uma chamada vinculada no mesmo arquivo tem confiança 1.0, pela classe envolvente 0.8 e por correspondência de nome em todo o repositório 0.6. Uma chamada que não pode ser vinculada é recusada em vez de adivinhada, e `code.impact` acrescenta testes mapeados e parceiros de co-change no nível de arquivo.

<a id="sql-and-migrations"></a>

## SQL e migrations

Os arquivos `.sql` são indexados por padrão para busca e grep. Com `LOOM_CODE_SQL_STATEMENT_RECORDS` ativado, cada instrução vira um registro com suas referências a tabelas, de modo que um agente pode perguntar quais migrations e queries leem ou escrevem em uma tabela. Ativar essa opção em um namespace já indexado exige um `loom code backfill --paths-file` com os arquivos SQL ([switches](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## Infraestrutura como código

Terraform e HCL, manifestos Kubernetes, charts Helm e templates Go, Jsonnet, CMake, Protocol Buffers e shell scripts são indexados com suas referências, includes e chamadas, de modo que um agente segue um `include` do Helm até o seu `define`, ou uma referência do Terraform até o que ela nomeia. Jobs de Dockerfile, Makefile, Jenkinsfile e YAML de CI viram símbolos com `LOOM_CODE_HOST_ISLANDS` ativado, após um backfill desses arquivos quando o namespace já está indexado.

<a id="start"></a>

## Início

1. [Indexe um repositório](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md); as camadas de compilador começam
   quando o repositório contém o arquivo de build correspondente.
2. [Conecte um agente de programação](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ao toolset
   `coding`.
3. Faça uma pergunta sobre a estrutura do código; verifique `code.coverage` para as linguagens em que você confia.

<a id="related"></a>

## Relacionados

- [Um índice de código para agentes de programação](code-intelligence.md)
- [Operações de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Adicionando suporte a linguagens](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
