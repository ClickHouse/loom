<!-- i18n: source=docs/use-cases/agent-memory.md blob=e9322c4d7b4b3395943f3caead94d6436cd0954b model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/agent-memory.md), which is authoritative.

<a id="long-term-and-shared-memory-for-ai-agents"></a>

# Memória de longo prazo e compartilhada para agentes de IA

O Loom é um servidor MCP e uma API HTTP auto-hospedados que dão aos agentes de IA uma memória de longo prazo compartilhada: um fato que um agente salva em uma sessão pode ser encontrado pela sessão seguinte, por outro agente ou por uma pessoa no painel. Esta página mostra como isso funciona e por onde começar.

<a id="what-you-get"></a>

## O que você recebe

- **Uma afirmação por chave, em um namespace.** `memory.set` salva um fato; `memory.get` o lê
  de volta pela chave; `memory.search` e `loom_search` o encontram pelo significado. Um namespace é a
  unidade de compartilhamento: por padrão, todo membro lê todas as linhas dele.
  [Memória](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Memória que pode ser corrigida.** Uma alteração cria uma nova versão com seu histórico. O
  mesmo fato, depois da canonicalização, é mesclado na linha armazenada; o mesmo fato com outras
  palavras é armazenado como duplicata; uma contradição é sinalizada para revisão; e uma escrita
  contra um fato fixado é recusada.
  [Conflitos e recusa](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md) ·
  [Histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Busca que se abstém.** Quando nada ultrapassa o limite mínimo de similaridade, `memory.search`
  responde `abstain=true` com um código de motivo e marca cada resultado fraco como `below_threshold`;
  `loom_search` os descarta. [Busca e perguntas](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)
- **Memória de longo prazo para o seu agente de código, compartilhada entre agentes.** Os plugins do Claude Code e
  do Codex carregam o contexto relevante no início da sessão e registram a sessão enquanto ela
  acontece. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode, goose, Zed e outros
  clientes MCP se conectam ao mesmo servidor, e um agente em um namespace encontra os fatos
  que outros agentes salvaram ali.
  [Conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) ·
  [Clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)
- **Memórias ligadas ao código a que se referem.** As lições de um agente de código acompanham as
  definições que citam, mesmo após renomeações e edições, e aparecem quando um agente lê esse
  código. [Memória ligada ao código](memory-linked-to-code.md)
- **Memória por usuário para o seu próprio agente.** Os SDKs de Python e TypeScript escrevem e leem
  memória por usuário final. [Seu primeiro agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)

<a id="measured"></a>

## Medições

Em 100 perguntas pareadas do LongMemEval-S com o mesmo leitor `gpt-5`, as respostas baseadas nas memórias recuperadas pelo Loom tiveram 94% de acerto, contra 90% com o histórico completo, com 91% menos tokens no prompt do leitor (sem contar o custo único de construir a memória); a diferença de acurácia não é estatisticamente estabelecida.
[Com e sem o Loom](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#with-and-without-loom)

<a id="a-research-assistant-that-remembers-your-reading-preferences"></a>

## Um assistente de pesquisa que lembra suas preferências de leitura

Depois do [início rápido](../getting-started/quickstart.md), conecte o Claude Desktop ou o
Cursor pela [configuração de cliente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md#how-do-i-give-cursor-or-another-mcp-client-shared-memory) do painel.
As ferramentas funcionam com o LLM padrão: o seu aplicativo de chat fornece o modelo que as chama.

1. Diga ao assistente: “Use `loom_memory_set` para salvar minha preferência de pesquisa em
   `example/research/source-policy`: incluir artigos revisados por pares na minha lista de leitura.”
2. Verifique se a resposta da ferramenta contém um `memory_id`. Abra **Memories** no
   painel e confirme o valor e o autor da escrita. Se a ferramenta recusar a escrita, resolva
   o problema informado antes de continuar.
3. Inicie um chat novo. Diga ao assistente: “Use `loom_memory_get` para ler
   `example/research/source-policy` e depois me diga quais fontes pertencem à minha lista de leitura.”
   Verifique se a ferramenta retorna `found: true` e a preferência que você salvou.
4. Peça ao assistente para excluir essa chave de exemplo ao terminar e confirme que o
   painel não a lista mais como uma memória ativa.

O segundo chat lê uma preferência armazenada sem a transcrição do primeiro chat. Isso
verifica a persistência e a configuração do cliente; não testa se o assistente sabe avaliar
artigos ou responder perguntas de pesquisa. Para buscas nos próprios artigos, use a
[busca de documentos](document-search.md).

<a id="start"></a>

## Comece

O [início rápido](../getting-started/quickstart.md) sobe a stack local e salva uma primeira
memória por HTTP, Python e MCP. [Quando o Loom não é a melhor opção](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)
descreve os casos em que uma biblioteca de memória em processo atende melhor.

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).

Use um [provedor de contexto do Microsoft Agent Framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
para recuperar fatos do usuário antes de uma execução e registrar a conversa depois dela.

Armazene a árvore de arquivos da [ferramenta de memória da API do Claude](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#claude-api-memory-tool)
no Loom para que os agentes de um namespace possam compartilhar o conteúdo dela.

Mantenha o [histórico de sessão do OpenAI Agents](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#openai-agents-sdk)
no Loom entre execuções e transferências de agentes.

Para um fluxo de trabalho visual, os [nós do n8n](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n) conectam um
agente de IA ao histórico de conversas, às memórias duráveis e à busca de conhecimento do Loom.
