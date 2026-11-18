<!-- i18n: source=docs/architecture/haystack-adapter.md blob=c2cfa8a3d7cfb7c046a16c39b584aa93d1ea0420 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/haystack-adapter.md), which is authoritative.

<a id="haystack-document-store-and-retriever"></a>

# Document store e retriever do Haystack

O adaptador opcional do Haystack 3.5 implementa o protocolo de document store do framework e um componente nativo de recuperação por meio do SDK síncrono do Loom. O SDK principal e o servidor não dependem do framework. Cada chamada HTTP tem um ciclo de vida de cliente gerenciado pelo SDK; o adaptador não deixa servidor, banco de dados local, cache ou processo persistente em segundo plano.

<a id="invariants"></a>

## Invariantes

Um store mapeia cada id de documento nativo para uma URI de origem com hash, sob um prefixo específico do store. Uma tag correspondente delimita a listagem e a recuperação. A serialização nativa completa do documento, incluindo metadados, embeddings e campos binários, fica nos bytes do arquivo original do documento. Os metadados dos chunks carregam apenas um pequeno marcador de formato; o texto é indexado como conteúdo do documento, ou como o id quando não há texto. Os caminhos normais do Loom para escrita de documentos, proveniência, concessões de namespace, versionamento e apagamento se aplicam a toda escrita e exclusão. Os documentos são compartilhados no namespace, e não privados por usuário.

A contagem e a filtragem carregam o store inteiro por meio de páginas com cursor. Os helpers de filtro nativos aplicam comparações e lógica booleana. A recuperação busca até 100 chunks com limites explícitos de namespace, tag e URI, carrega os documentos completos e remove duplicatas por URI de origem, preservando o resultado mais bem classificado de cada documento. Essas operações não têm snapshot entre requisições. Escritas concorrentes podem receber um conflito do Loom. As verificações de duplicidade e as operações com vários documentos não são transações.

As conexões serializadas carregam referências Secret de variáveis de ambiente do Haystack; tokens Secret brutos recusam a serialização. URLs de endpoint explícitas rejeitam segredos embutidos. O adaptador preserva a política de confiança do Haystack para desserialização de pipelines e nunca registra módulos confiáveis nem seleciona a desserialização insegura.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `sdk/python/loom/integrations/haystack.py`: `LoomDocumentStore` e `LoomRetriever`.
- `sdk/python/tests/test_haystack.py`: comportamento nativo do document store e do pipeline.
- `tests/integration/test_sdk_haystack.py`: persistência e recuperação de documentos em ambiente real.
