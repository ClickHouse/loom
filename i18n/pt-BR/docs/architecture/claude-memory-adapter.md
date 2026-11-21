<!-- i18n: source=docs/architecture/claude-memory-adapter.md blob=b72a628092f2f86d148e87a01ed0f9e99469b83f model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md), which is authoritative.

<a id="claude-api-memory-file-tree"></a>

# Árvore de arquivos de memória da Claude API

O adaptador de memória da Claude API implementa as bases da ferramenta de memória do framework em Python e os handlers de memória em TypeScript por meio de documentos de conhecimento do Loom. Os SDKs do framework são dependências opcionais. As duas implementações usam o mesmo URI de documento e a mesma tag de store, de modo que cada linguagem lê os arquivos criados pela outra.

Um arquivo é `loom-memory://<store>/memories/<path>`. Diretórios são prefixos de URI, sem linhas de diretório separadas. Os caminhos são normalizados e recusados, a menos que permaneçam dentro de `/memories`. As leituras de diretório usam `knowledge.list` com uma tag de store, um prefixo de URI e campos de URI projetados; as leituras de arquivo usam `knowledge.get`. As edições usam `knowledge.upsert`, e a exclusão usa chamadas limitadas de `knowledge.bulk_delete`. A renomeação copia cada arquivo antes de excluir o URI antigo. Uma renomeação que falha pode deixar cópias, e edições simultâneas de leitura-modificação-escrita mantêm a gravação mais recente.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `sdk/python/loom/integrations/claude_memory.py`: `AsyncLoomMemoryTool` e
  `LoomMemoryTool`.
- `sdk/typescript/src/integrations/claude-memory.ts`: `loomMemoryTool` e
  `loomMemoryHandlers`.

<a id="invariants"></a>

## Invariantes

Um store agrupa arquivos; a autorização do namespace controla o acesso. Árvores privadas de usuário exigem namespaces separados. O adaptador nunca interpreta o nome de um store como uma concessão de acesso. Ator, proveniência da fonte, revisões de documentos, limpeza de blobs e eliminação seguem as operações de conhecimento existentes. Nenhum estado persistente local, banco de dados ou esquema é adicionado. As operações de diretório não são snapshots atômicos.
