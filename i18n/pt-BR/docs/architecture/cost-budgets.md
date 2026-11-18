<!-- i18n: source=docs/architecture/cost-budgets.md blob=932ba3cd2049b60a49294a5dd87fa51b9401b693 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cost-budgets.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cost-budgets.md), which is authoritative.

<a id="cost-budgets"></a>

# Orçamentos de custo

Os limites por namespace e por usuário nomeado usam o registro de custo móvel de 24 horas em `agent_events`.
Uma política de namespace positiva substitui o padrão correspondente do servidor; zero herda o padrão,
e zero em ambas as camadas desativa esse limite. O limite de um usuário nomeado é independente do
teto do namespace. A admissão recusa a síntese paga quando qualquer um dos dois é atingido, e o roteamento
de modelos usa a maior fração de gasto. `memory.ask`, incluindo sua rota de documento inteiro,
`code.ask`, `knowledge.synthesize` e a expansão de consultas de `code.search` fornecem o
usuário lógico registrado no registro de custo. Os workers de todo o namespace mantêm a contabilidade do próprio namespace;
o subject de um token nunca é usado no lugar de um usuário sem atribuição. Esses controles leves
não reservam gasto nem estabelecem uma identidade de cobrança.

`cost.budget_status` compartilha com as proteções a leitura de política e de gasto. O modo de usuário exige
um namespace e informa o limite e o veredito do usuário, além de `namespace_over_cap`. O gasto exato
do usuário e seu percentual são `null`, a menos que o chamador seja um administrador de plataforma verificado
sem restrições de papel no namespace. Isso vale mesmo quando todos os eventos são comuns;
variar a presença de um número revelaria atividade marcada. A ausência de autenticação não concede acesso ao gasto privado.
A contabilidade interna, a admissão e a pressão de orçamento incluem todo o gasto,
inclusive requisições marcadas. Os totais de namespace e de organização continuam numéricos.
A admissão no transporte aplica o usuário fixado de um token. Os modos de namespace e de organização mantêm
seus escopos de leitura existentes. O [guia de custos](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md#daily-spend-caps)
descreve a configuração e o momento dos avisos.

Um `budget_alert_inbox` opcional deve indicar uma inbox existente pertencente ao namespace do orçamento
na mesma organização. O registro a valida e a entrega verifica novamente a propriedade.
As verificações pontuais e as consultas de status agendam avisos limitados em segundo plano pelo fluxo de eventos
de tarefas existente, com no máximo 32 entregas pendentes por processo. As entregas de uma
organização são executadas em série dentro do processo, para que um aviso de namespace e um de usuário não concorram
entre si. Não há worker de descoberta. O ID do evento inclui organização, namespace, inbox, usuário selecionado, limite, fração de aviso
e dia UTC. Uma reivindicação distribuída e uma leitura de eventos com consistência forte serializam
a verificação e a inserção; um token de deduplicação mais a releitura tratam uma nova tentativa incerta.
Falhas de notificação não podem alterar o veredito de um limite.

Os payloads de aviso de usuário carregam o usuário selecionado, o limite e a fração de aviso, com
`spent_24h_usd: null`. Isso vale para a inbox, a consulta periódica, os streams e o webhook, mesmo
quando a verificação de um operador dispara a entrega: a autoridade de quem iniciou não estabelece
a autoridade do destinatário. Os avisos de namespace mantêm o total de gasto do namespace.
O veredito do limite do usuário e o cruzamento do limiar de aviso revelam limites sobre o gasto; um administrador
que altera os limites pode inferir limites mais estreitos. Este é um sinal de política aceito, ao lado da
[exceção dos totais de gasto marcado](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md), e não uma não interferência total.
Ele não concede acesso a trabalho, modelos ou resultados marcados.

As atualizações de tarefas e os streams de watch aplicam aos avisos o escopo de usuário e de sessão do chamador
antes da paginação. Um escopo incompleto não recebe avisos; os eventos de tarefa comuns continuam
compartilhados dentro da inbox. O webhook da inbox registrada recebe o stream do seu namespace.

Toda entrega e toda exclusão de eventos de tarefa tomam o mesmo lease de gravação de avisos em toda a organização.
As inserções usam o prazo de início e o limite de execução do lado do servidor já existentes; o TTL do lease
excede a janela de trabalho mais o limite total de envio. Uma inserção ou um cancelamento incerto
deixa esse lease expirar, de modo que a exclusão não o adquire até que todo envio possível
tenha terminado. A exclusão mantém seu protocolo de mutação marcada, polling e retomada. As verificações do registro
antes e depois da publicação invalidam uma observação alcançada pela exclusão; a limpeza posterior à gravação
nomeia apenas o evento e a sequência exatos daquela entrega. Um registro ilegível
recusa a publicação.

As linhas de aviso carregam a tupla de identidade completa, um ator de processo e uma classe de origem derivada.
A exclusão de um usuário apaga seus eventos de tarefa e o censo de resíduos verifica a exclusão;
a exclusão de um namespace cobre tanto as tarefas quanto os eventos. As colunas adicionadas têm valores padrão para gravadores
mais antigos. A produção de avisos aguarda que todas as réplicas ativas suportem a migração, de modo que
uma implementação de exclusão mais antiga não deixe passar linhas de aviso com dados de usuário durante uma atualização
gradual. Um estado desconhecido da frota adia a publicação até uma verificação posterior.

<a id="activation-and-rollback"></a>

## Ativação e rollback

Os limites por usuário e os avisos de orçamento exigem que toda build em serviço suporte a migração 107.
Mantenha `LOOM_DEFAULT_DAILY_USER_COST_CAP_USD` e o `daily_user_cost_cap_usd` de cada namespace
em zero, e `budget_alert_inbox` vazio, enquanto qualquer implantação
que compartilhe os stores executar uma build mais antiga ou tiver versão desconhecida.
Não dependa dos limites por usuário nesse intervalo. Aplique a migração e atualize todos os processos em serviço antes de
definir essas políticas. A verificação da frota de avisos adia a produção quando a frota é antiga ou desconhecida;
ela não remove avisos já armazenados.

Salve o padrão global do limite por usuário e o limite por usuário e a inbox de alerta de cada namespace antes do
rollback. O rollback exige um intervalo de manutenção:

1. Pare todos os processos de atendimento de requisições e de segundo plano que possam verificar orçamentos ou agendar
   avisos, em todas as implantações que compartilham os stores. Pare também a consulta periódica de status.
   Desligar os workers de segundo plano ou limpar a política de uma inbox isoladamente não interrompe
   as entregas já agendadas com essa inbox.
2. Esgote as entregas agendadas e resolva toda gravação em andamento ou incerta antes da
   limpeza. Aguarde os leases de gravação de avisos serem liberados ou expirarem e as janelas
   limitadas de início tardio e de execução no servidor terminarem. Um esgotamento interrompido ou uma
   lista vazia de consultas em execução não prova que um envio atrasado não possa ainda chegar.
3. Apague apenas as linhas de `loom.task_events` cujo `event` seja `budget.warning`. Aguarde a
   exclusão ser concluída em todas as réplicas de armazenamento e verifique que nenhuma linha de aviso permanece
   em nenhuma delas antes de iniciar o primeiro processo mais antigo. Uma réplica ilegível ou uma
   gravação não resolvida bloqueia o rollback. A exclusão de namespace ou de usuário não é um substituto: ela
   também apaga dados não relacionados.
4. Mantenha os limites por usuário e os avisos desativados enquanto builds mais antigas estiverem em serviço. Depois que todos os processos em serviço
   voltarem a suportar a migração 107, restaure o padrão global salvo e reaplique
   os limites por usuário e as inboxes de alerta dos namespaces com `namespace.register`. Leia as políticas
   dos namespaces e verifique os limites efetivos antes de retomar a produção de avisos.

Isso limita a ativação durante a operação com versões mistas e troca disponibilidade por
privacidade durante o rollback. Os leitores de tarefas mais antigos não filtram avisos privados, e a exclusão
de usuário mais antiga não os apaga. Os registros de namespace mais antigos também omitem as colunas de limite por usuário
e de inbox de alerta; suas linhas de substituição redefinem essas políticas para os padrões.
O registro de namespaces não propaga essas colunas por gravações mais antigas.
Portanto, o operador deve preservar e restaurar a política, em vez de contar com que ela sobreviva ao rollback.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `server/observability/budget.py`: `budget_snapshot` lê a política e o gasto móvel para as proteções e o status.
- `server/tasks/app/budget_notifications.py`: `deliver` controla as reivindicações de avisos e as barreiras de exclusão.
- `server/tasks/store/repo.py`: `events_since` aplica o escopo de avisos antes da paginação por cursor.
- `server/tasks/app/reads.py` e `server/tasks/app/watch_routes.py`: os leitores de polling e de streaming vinculam esse escopo.
- `server/erasure/store/steps_ledgers.py`: os leases de gravação de avisos protegem a exclusão de eventos de tarefa.

<a id="invariants"></a>

## Invariantes

- A cota de um usuário nomeado nunca eleva o teto do namespace, e a atribuição nunca adivinha o subject de um token.
- Um leitor restrito não recebe nenhum aviso fora do seu escopo de usuário e de sessão.
- Uma gravação de aviso incerta mantém a exclusão protegida até que seu possível envio tenha terminado.
- Uma falha de notificação nunca altera a decisão sobre o limite.
