<!-- i18n: source=docs/use-cases/document-search.md blob=0848e72f516323271d09e930d51b4314d82ce625 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# Busca de documentos e RAG para agentes

O Loom indexa seus documentos (Markdown e texto, além de arquivos PDF, `.docx` e `.pptx` que o
serviço extrator converte) e permite que agentes os pesquisem com citações via MCP, HTTP ou
pelos SDKs. É a metade de recuperação da geração aumentada por recuperação (RAG): seu agente ou
modelo escreve a resposta a partir das passagens que o Loom retorna.

<a id="what-you-get"></a>

## O que você recebe

- **Chunks que seguem a estrutura do documento.** `knowledge.add` armazena o texto e,
  opcionalmente, o arquivo original; o Loom o divide pelos títulos, aplica janelas a cada seção e
  gera o embedding de cada chunk. [Documentos de conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **Citações.** Um resultado indica o `source_uri` e a passagem do seu documento, para que um agente possa citar
  e linkar a fonte.
- **Uma única busca em memória, documentos e conversas.** `loom_search` executa as buscas de memória,
  conhecimento, perfil e conversa em uma só chamada, em todos os namespaces de memória do chamador.
  [Busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Fontes de documentos.** Os admins de um namespace podem registrar fontes das quais o Loom extrai documentos;
  o worker de sincronização exige `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`.
  [Fontes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **Documentos junto ao código.** Documentos de uma fonte no namespace de código do trunk de um repositório
  carregam a tag `repo:<id>` dele, de modo que as buscas desse repositório os leem junto com o código. [Fontes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources) ·
  [Memória vinculada ao código](memory-linked-to-code.md)
- **Um limite mínimo de similaridade.** `loom_search` descarta resultados de conhecimento abaixo de um limite mínimo de similaridade
  em vez de entregar ao modelo uma passagem fraca. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **Controle de acesso em cada passagem.** Os documentos ficam em namespaces com papéis de visualizador, escritor e
  admin. [Namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## Medições

Em um corpus versionado de arquivos PDF, DOCX e PPTX (27 arquivos reservados para teste), o extrator do Loom,
com OCR ativado (seu padrão), obteve 0.999 de recall de texto e 0.991 de F1 de células de tabela na
execução medida ([análise de documentos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)).
A qualidade da recuperação sobre documentos é medida apenas em parte;
[cobertura e lacunas conhecidas](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) indica o que ainda falta.

<a id="start"></a>

## Primeiros passos

O [guia de início rápido](../getting-started/quickstart.md) executa a stack com o serviço
extrator. Adicione um documento pela página Library do painel, com `knowledge.add` para texto
ou, para um arquivo que o extrator converte, com `knowledge.upload` e depois `knowledge.ingest`
([ingestão de arquivos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
