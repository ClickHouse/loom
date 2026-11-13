<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# Busca em documentos e RAG para agentes

O Loom indexa seus documentos (Markdown e texto, além de arquivos PDF, `.docx` e `.pptx` que o serviço extrator converte) e permite que agentes os pesquisem com citações via MCP, HTTP ou pelos SDKs. Ele é a metade de recuperação da geração aumentada por recuperação (RAG): seu agente ou modelo escreve a resposta a partir dos trechos que o Loom retorna.

<a id="what-you-get"></a>

## O que você recebe

- **Chunks que seguem a estrutura do documento.** `knowledge.add` armazena o texto e,
  opcionalmente, o arquivo original; o Loom o divide pelos títulos, aplica janelas a cada seção e
  gera o embedding de cada chunk. [Documentos de knowledge](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **Citações.** Um resultado informa o `source_uri` do documento e o trecho, para que um agente possa citar
  e linkar a fonte.
- **Uma única busca em memória, documentos e conversas.** `loom_search` executa as buscas de memória,
  knowledge, perfil e conversa em uma só chamada, em todos os namespaces de memória do chamador. [Busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **Fontes de documentos.** Os admins de um namespace podem registrar fontes das quais o Loom extrai documentos;
  o worker de sincronização precisa de `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`.
  [Fontes de documentos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **Um limite mínimo de similaridade.** `loom_search` descarta resultados de knowledge abaixo de um limite mínimo de similaridade
  em vez de entregar ao modelo um trecho fraco. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **Controle de acesso em cada trecho.** Os documentos ficam em namespaces com papéis de viewer, writer e
  admin. [Namespaces e compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## Medições

Em um corpus versionado de arquivos PDF, DOCX e PPTX (27 arquivos reservados para avaliação), o extrator do Loom, com OCR ativado (seu padrão), obteve 0.999 de recall de texto e 0.991 de F1 por célula de tabela na execução medida ([document parsing](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)).
A qualidade da recuperação sobre documentos é medida apenas em parte;
[coverage and known gaps](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) descreve o que ainda falta.

<a id="start"></a>

## Início

O [quickstart](../getting-started/quickstart.md) executa a stack com o serviço extractor. Adicione um documento pela página Library do dashboard, com `knowledge.add` para texto ou, para um arquivo que o extractor converte, com `knowledge.upload` e depois `knowledge.ingest` ([ingestão de arquivos](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
