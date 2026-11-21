<!-- i18n: source=docs/architecture/agent-framework-adapter.md blob=6a5670ddd5050886dcda1b0bf590a83063c1e4aa model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agent-framework-adapter.md), which is authoritative.

<a id="microsoft-agent-framework-context-provider"></a>

# Provedor de contexto do Microsoft Agent Framework

`LoomContextProvider` implementa a interface de provedor de contexto do framework sobre
o SDK Python assíncrono. O extra opcional admite apenas a série de versões menores testada do framework;
o SDK principal e o servidor não importam o framework.

Antes de uma execução, o provedor busca o último texto do usuário e adiciona os fatos correspondentes
às instruções do framework. Depois da execução, ele envia o novo texto do usuário e do assistente
para `memory.set_from_messages` em lotes limitados. Ele executa esse hook uma vez por
turno, inclusive em um loop de middleware do framework. O framework mantém seu próprio histórico;
este provedor armazena fatos duráveis em vez de substituir um provedor de histórico.

<a id="invariants"></a>

## Invariantes

O id de usuário e o id de sessão do framework geram uma sessão do Loom, o que impede que sessões
com o mesmo nome de dois usuários compartilhem chunks extraídos. Um usuário omitido lê todas as memórias do namespace que o token permite e grava
linhas compartilhadas. Ids de usuário explícitos não podem ter espaços em branco nas extremidades. Os grants de namespace autorizam o acesso; o parâmetro de usuário é um filtro.
O servidor fornece a proveniência de ator e de origem e aplica as regras comuns de conflito,
de exclusão e de esquema progressivo. Nenhum banco de dados, cache ou histórico local é adicionado.
Os erros se propagam pelos hooks de provedor do framework, inclusive os erros que ocorrem depois
de uma resposta do modelo. As gravações concorrentes seguem as regras normais de conflito do servidor.

<a id="source-entry-points"></a>

## Pontos de entrada do código-fonte

- `sdk/python/loom/integrations/agent_framework.py`: `LoomContextProvider`.
- `sdk/python/tests/test_agent_framework.py`: comportamento do provedor nativo e do agente.
- `tests/integration/test_sdk_agent_framework_memory.py`: extração e recuperação em ambiente real.

[Configuração do framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#microsoft-agent-framework)
