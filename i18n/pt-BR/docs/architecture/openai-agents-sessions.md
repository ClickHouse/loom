<!-- i18n: source=docs/architecture/openai-agents-sessions.md blob=d96f44f5824749f9ff8d8d20e70fb23370c26d8b model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/openai-agents-sessions.md), which is authoritative.

<a id="openai-agents-conversation-sessions"></a>

# Sessões de conversa do OpenAI Agents

`LoomSession` implementa as interfaces nativas de sessão de Python e TypeScript sobre
as memórias episódicas do Loom. As ferramentas existentes podem reter fatos separadamente
além de uma conversa. Os pacotes de framework continuam sendo dependências opcionais.

Cada item é uma chamada `memory.set` com seu JSON nativo, uma chave composta por timestamp e valor aleatório,
o usuário final e uma sessão derivada do usuário e do id nativo da sessão.
As gravações pontuais ignoram a coalescência do SDK, de modo que itens repetidos e concorrentes permaneçam separados.
As linhas episódicas preservam itens repetidos sem extração semântica nem mesclagem de duplicatas. Python e TypeScript usam prefixos de sessão derivada distintos, porque
seus formatos nativos de item são diferentes.

As leituras percorrem páginas de `memory.list`, eliminam duplicatas de chaves sobrepostas e preenchem valores ausentes
ou truncados com `memory.get`. Os itens são retornados na ordem das chaves, e um limite
solicitado mantém os itens mais recentes. Leituras com limite positivo também varrem o
histórico, porque os horários de chegada no servidor não podem delimitar chaves geradas pelo escritor. O adaptador TypeScript
captura o usuário validado na construção. Seus payloads nativos de bytes de imagem e arquivo usam arrays JSON no armazenamento e retornam como `Uint8Array`; o JSON comum e as informações de mídia permanecem inalterados. A exclusão carrega o usuário e a sessão derivada: `pop_item` exclui o item mais recente, e `clear_session` exclui o histórico listado. Acréscimos ou limpezas concorrentes são operações individuais do servidor, não uma transação sobre a conversa.
Se um item desaparecer antes do preenchimento, a leitura falha em vez de retornar um
item nativo incompleto. As chaves usam os relógios dos escritores; escritores concorrentes não compartilham uma
sequência do servidor.

Ambas as implementações mantêm a família de proteção vinculada já existente na aplicação
desde a construção e em cada callback. O Python também mantém os contribuidores ativos no contexto do
callback. Revincular o cliente não pode remover um contribuidor anterior.
Os ids nativos de usuário e de sessão selecionam o histórico armazenado; eles não estabelecem uma família de proteção. Use uma instância de sessão por família lógica de conversa e
reabra a mesma cápsula de proteção pertencente à aplicação ao restaurar essa
conversa.

As leituras e adições ao histórico nativo são executadas como trabalho automático sob todos os contribuidores
retidos. Uma resposta marcada protege todos os contribuidores antes de seu valor ser retornado.
Uma primeira resposta completa de histórico marcada ainda pode retornar seus itens; a paginação,
o preenchimento ou as gravações de itens subsequentes são suprimidos. Uma gravação de item marcada interrompe um
lote antes do próximo item. As operações explícitas de pop e clear continuam utilizáveis após a proteção e continuam observando as respostas sob os contribuidores retidos.
As ferramentas selecionadas pelo modelo mantêm a semântica explícita de suas operações.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `sdk/python/loom/integrations/openai_agents.py`: `LoomSession`.
- `sdk/typescript/src/integrations/openai-agents.ts`: `LoomSession`.

<a id="invariants"></a>

## Invariantes

A organização, o namespace e o nome do agente delimitam cada histórico. Um usuário omitido
grava linhas compartilhadas do namespace; um usuário explícito seleciona o histórico desse usuário. O
servidor fornece a procedência de ator e de origem. Aplicam-se as regras comuns de exclusão de memória, apagamento e atualização gradual. Nenhum banco de dados local de histórico nem esquema é adicionado.
