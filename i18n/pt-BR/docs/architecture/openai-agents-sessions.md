<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=96269df2f0037fdcb16f6a101e85bf9aafedef73 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# Sessões de conversa do OpenAI Agents

`LoomSession` implementa as interfaces de sessão nativas de Python e TypeScript sobre
memórias episódicas do Loom. Ferramentas existentes podem reter fatos além de uma
conversa de forma separada. Os pacotes de framework continuam sendo dependências opcionais.

Cada item corresponde a uma chamada `memory.set` com seu JSON nativo, uma chave composta por
timestamp e valor aleatório, o usuário final e uma sessão derivada do usuário e do id de sessão
nativo. As gravações pontuais ignoram a coalescência do SDK, de modo que itens repetidos e
concorrentes permanecem separados. As linhas episódicas preservam itens repetidos sem extração
semântica nem mesclagem de duplicatas. Python e TypeScript usam prefixos de sessão derivada
distintos, porque seus formatos de item nativos são diferentes.

As leituras paginam por `memory.list`, eliminam chaves sobrepostas duplicadas e recuperam valores
ausentes ou truncados com `memory.get`. Os itens são retornados na ordem das chaves, e um limite
solicitado mantém os itens mais recentes. Leituras com limite positivo também varrem o histórico,
porque os horários de chegada no servidor não conseguem delimitar chaves geradas pelo escritor. O
adaptador TypeScript captura o usuário validado no momento da construção. Seus payloads nativos de
bytes de imagem e arquivo usam arrays JSON no armazenamento e retornam como `Uint8Array`; o JSON
comum e as informações de mídia permanecem inalterados. A exclusão carrega o usuário e a sessão
derivada: `pop_item` exclui o item mais recente e `clear_session` exclui o histórico listado.
Acréscimos ou limpezas concorrentes são operações individuais no servidor, e não uma transação
sobre a conversa.
Se um item desaparecer antes da recuperação, a leitura falha em vez de retornar um item nativo
incompleto. As chaves usam os relógios dos escritores; escritores concorrentes não compartilham uma
sequência do servidor.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

<a id="invariants"></a>

## Invariantes

A organização, o namespace e o nome do agente delimitam cada histórico. Um usuário omitido grava
linhas compartilhadas do namespace; um usuário explícito seleciona o histórico desse usuário. O
servidor fornece a proveniência de ator e de origem. Aplicam-se as regras comuns de exclusão de
memória, apagamento e atualização gradual (rolling upgrade). Nenhum banco de dados local de
histórico nem esquema é adicionado.
