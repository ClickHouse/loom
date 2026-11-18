<!-- i18n: source=docs/architecture/n8n-adapter.md blob=f2efbe08cacbe3e9b1bb2cb39161b71c20e2be32 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/n8n-adapter.md), which is authoritative.

<a id="n8n-adapter"></a>

# Adaptador do n8n

O pacote da comunidade n8n armazena o histórico de conversas e fatos duráveis em uma
implantação do Loom por meio do SDK TypeScript. Os nós não adicionam nenhum serviço de armazenamento nem cache local.

<a id="interfaces-and-storage"></a>

## Interfaces e armazenamento

`LoomMemory.supplyData` retorna o `BufferWindowMemory` do framework, apoiado por uma
implementação `LoomChatHistory` de `BaseListChatMessageHistory`. A serialização das mensagens
usa o formato de mensagem armazenada do framework. A janela altera o contexto enviado a
um modelo, enquanto as mensagens mais antigas continuam disponíveis para leituras posteriores e para limpeza explícita.

Cada mensagem é uma memória `episodic`, com um timestamp e um sufixo aleatório em sua chave.
As gravações episódicas preservam mensagens repetidas sem mesclagem semântica. A organização,
o namespace e o nome do agente Loom delimitam o escopo do histórico. O usuário e um resumo (digest) dos
identificadores de workflow/sessão fornecem os demais campos de identidade. As leituras percorrem
as páginas de `memory.list`, eliminam duplicatas de páginas sobrepostas quando as linhas projetadas são filtradas,
ordenam por chave e usam `memory.get` para valores truncados. `clear` exclui apenas esses itens.
Gravadores concorrentes não têm transação que abranja um turno inteiro.

O nó de ação do Loom chama `memory.set`, `memory.search` e `knowledge.search`.
Suas entradas fornecem a organização, o namespace, o nome do agente e, opcionalmente, o usuário. O
servidor deriva a proveniência a partir do token e da operação e aplica as concessões de namespace.

<a id="packaging"></a>

## Empacotamento

O pacote inclui o SDK e as bibliotecas do framework para evitar conflitos de dependências em tempo de execução
com outros nós da comunidade. Seu build inclui os arquivos de licença dessas bibliotecas.
`n8n-workflow` é a dependência peer fornecida pelo host. As dependências de desenvolvimento fixam as interfaces
usadas pelos testes; elas não são adicionadas às dependências do servidor nem do SDK do Loom.

O pacote tem como alvo o n8n auto-hospedado, com a configuração da CLI oficial de nós para
esse alvo. Ele não afirma ter sido verificado no Cloud. O workflow de tag de release faz o build
e verifica o pacote antes de publicar com um npm trusted publisher e provenance;
nenhum token de registro é armazenado no repositório.

<a id="invariants"></a>

## Invariantes

- O adaptador envia dados somente para a URL do Loom presente em sua credencial do n8n.
- As concessões do token autorizam o acesso; um parâmetro de usuário é um filtro, não uma autorização.
- A limpeza da conversa não exclui os fatos duráveis criados pelo nó de ação.
- Recusas do SDK ou do servidor fazem o nó falhar, ou viram itens de erro com continue-on-fail.
- O adaptador não cria tabelas e usa os caminhos de exclusão já existentes do Loom.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `integrations/n8n/nodes/LoomMemory/LoomChatHistory.ts`: persistência de mensagens.
- `integrations/n8n/nodes/LoomMemory/LoomMemory.node.ts`: adaptador de memória do framework.
- `integrations/n8n/nodes/Loom/Loom.node.ts`: ações de memória durável e de documentos.
- `.github/workflows/n8n-publish.yml`: verificações e publicação confiável.

[Configuração do framework](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md#n8n)
