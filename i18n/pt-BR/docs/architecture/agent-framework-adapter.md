<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=d7c22dbc73bf2a6dd64de2cb8471a3fafc9d76ba model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Provedor de contexto do Microsoft Agent Framework

`LoomContextProvider` implementa a interface de provedor de contexto do framework
sobre o SDK Python assíncrono. O extra opcional admite apenas a série de versões
minor do framework que foi testada; o SDK principal e o servidor não importam o framework.

Antes de uma execução, o provedor busca o texto mais recente do usuário e adiciona os fatos
correspondentes às instruções do framework. Depois da execução, ele envia o novo texto do usuário
e do assistente para `memory.set_from_messages` em lotes limitados. Ele executa esse hook uma vez
por turno, inclusive em um loop de middleware do framework. O framework mantém seu próprio
histórico; este provedor armazena fatos duráveis em vez de substituir um provedor de histórico.

<a id="invariants"></a>

## Invariantes

O id do usuário e o id da sessão do framework geram uma sessão do Loom, o que impede que
sessões com o mesmo nome de dois usuários compartilhem chunks extraídos. Um usuário omitido lê todas as memórias do namespace que o token permite e grava
linhas compartilhadas. Ids de usuário explícitos não podem ter espaços em branco nas extremidades. As concessões de namespace autorizam o acesso; um parâmetro de usuário é um filtro.
O servidor fornece a proveniência de ator e de origem e aplica as regras comuns de conflito,
apagamento e esquema progressivo. Nenhum banco de dados, cache ou histórico local é adicionado.
Os erros se propagam pelos hooks de provedor do framework, inclusive os erros após
uma resposta do modelo. Gravações concorrentes seguem as regras normais de conflito do servidor.

<a id="conversation-protection"></a>

## Proteção da conversa

Um provedor atende a uma família lógica de conversa. Ele retém os handles de proteção
pertencentes à aplicação que estão presentes na construção e na entrada do callback, incluindo
contribuidores ambientes e cada handle de cliente vinculado posteriormente. Os ids nativos de usuário e de sessão roteiam
as requisições; eles não criam nem substituem essa autoridade. A restauração reabre a cápsula
existente antes de construir o provedor.

A recuperação e a extração próprias rodam no contexto de captura automática do SDK. O transporte
reverifica cada família retida após as credenciais e antes do envio, observa as respostas
antes da conversão e marca de forma durável todos os contribuidores antes de liberar uma resposta marcada.
A primeira recuperação marcada pode chegar ao modelo. O trabalho automático posterior é recusado, e cada lote de extração
é verificado antes que o próximo possa começar. Uma recusa após uma resposta do modelo
se propaga pelo ciclo de vida nativo do provedor; ela não pode desfazer a resposta nem os efeitos das ferramentas.
O provedor não adiciona declaração de capacidade nem qualificação de host restaurado.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: comportamento do provedor nativo e do agente.
- `tests/integration/test_sdk_agent_framework_memory.py`: extração e recuperação em ambiente real.

[Configuração do framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
