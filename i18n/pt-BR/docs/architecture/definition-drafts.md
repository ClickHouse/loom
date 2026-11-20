<!-- i18n: source=docs/architecture/definition-drafts.md blob=6830be6e2eecf1912353fa8f9194619554e027d5 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/definition-drafts.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/definition-drafts.md), which is authoritative.

<a id="definition-drafts"></a>

# Rascunhos de definição

Requer `knowledge_gaps_enabled` para rascunhos de lacunas ou `knowledge_query_history_enabled` para
o histórico de consultas. Os dois caminhos colocam definições escritas pelo modelo na fila de revisão do administrador.
O modelo nunca publica uma definição. Esta página descreve evidências, candidatos privados
e revisão; as [lacunas de conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/knowledge-gaps.md) descrevem o registro e o agrupamento de buscas.

<a id="evidence-and-access"></a>

## Evidências e acesso

A ação **Draft definition** da Library chama `knowledge.gaps.draft`. Ela relê as perguntas
da lacuna dentro da janela atual de sete dias de buscas sem resultado e exige pelo menos
`knowledge_gaps_min_askers` autores distintos para cada pergunta. Ela envia ao modelo somente essas
perguntas normalizadas. O consentimento de captura é lido sem cache, e um consentimento
indisponível faz o rascunho ser recusado. Uma mudança de limite ou a exclusão de um usuário durante o trabalho invalida
a evidência e remove as versões candidatas daquela tentativa.

`knowledge.query_history.add`, `.list` e `.remove` exigem acesso de administrador do namespace. Na
Library, **Query history** abre a lista de fontes e o formulário de conexão. Um registro tem
nome, host, porta, opção de TLS, conta somente leitura, lista de bancos de dados permitidos e cluster opcional.
Sua senha é selada em repouso e nunca é retornada. A conexão reutiliza as verificações de host do tracker, o endereço fixado e a revisão de concessões somente leitura. Um cluster também precisa de `REMOTE`.
Remover um registro interrompe os turnos seguintes sem excluir um rascunho ou uma definição existente. A desconexão continua disponível quando o operador desativa o histórico de consultas.

O worker lê consultas `SELECT` iniciais e concluídas cuja lista de bancos de dados, não vazia,
esteja inteiramente na lista permitida do registro. Um cluster nomeado usa `clusterAllReplicas`;
caso contrário, apenas o nó conectado é lido. Os grupos usam `normalized_query_hash` e
`normalizedQueryHashKeepNames`, ordenados por contas `initial_user` distintas e por recência.
O segundo hash mantém separados nomes de tabela numerados diferentes, sem dividir variantes
de espaços em branco ou de literais. `normalizeQueryKeepNames` remove comentários e substitui literais,
preservando os identificadores. Somente esse SQL normalizado chega ao modelo.

Pelo menos `knowledge_query_history_min_users` contas devem sustentar um grupo, nunca menos
que três. Contas não são pessoas: uma conta compartilhada conta uma vez, enquanto várias contas de uma mesma pessoa
contam separadamente. Esse mínimo não oferece garantia de anonimato. Um aumento do limite em tempo real
ou uma mudança na janela do histórico invalida um resultado em andamento. A remoção da fonte, uma mudança do UUID da fonte, a desativação do recurso e a exclusão do namespace também o invalidam.

Os candidatos usam `source_kind = knowledge_gap` ou `query_history`. O texto escrito pelo modelo
e o SQL do histórico de consultas permanecem privados para os administradores do namespace em `learnings.list`,
nas contagens de qualidade e na linhagem, tanto explícita quanto inferida. A filtragem precede a paginação e a agregação, inclusive nas leituras de qualidade
de toda a organização. Tokens com leitura restrita não podem chamar nenhum dos dois prefixos de operação.

<a id="one-model-attempt"></a>

## Uma tentativa do modelo

O ID do candidato deriva da organização, do namespace, do tipo e do recurso de evidência.
Uma lacuna usa seu ID de lacuna; o histórico de consultas usa o UUID do registro e os dois hashes de consulta.
Um lease curto do candidato persiste uma reivindicação pendente antes de liberar o lease para a
chamada ao modelo. Requisições concorrentes e posteriores retornam esse ID de candidato, inclusive após uma
decisão, um erro do modelo, um resultado inválido ou uma falha. Essas falhas mantêm uma reivindicação pendente
cuja justificativa orienta o administrador a rejeitá-la e escrever explicitamente. Uma retenção por política antes da
geração remove apenas as versões da reivindicação não consumida, para que uma nova tentativa possa prosseguir quando permitido.

A chave de chamadas ao modelo da organização e o limite diário do namespace são verificados antes de reivindicar o trabalho;
um limite ilegível o recusa. Cada tentativa permite uma conclusão JSON limitada. O
resultado valida termo, aliases, corpo e tamanhos das listas de tabelas/colunas. A proveniência da lacuna em
`rests_on` contém o ID da lacuna e as listas de tabelas/colunas do modelo. As perguntas elegíveis chegam
somente à entrada do modelo; a aplicação não as copia para o candidato. O modelo
pode repetir uma pergunta no texto que redige. Após a geração, o rascunho e o ID da lacuna podem
sobreviver à exclusão, por um usuário, das perguntas de origem. A proveniência do histórico de consultas mantém seu
SQL normalizado e as listas de tabelas/colunas do modelo. Um lease final do candidato relê o status para que
a conclusão não substitua uma rejeição nem restaure texto removido. As barreiras de evidência são executadas
antes e depois do trabalho do modelo e após a inserção.

O worker limita fontes, grupos, tentativas, bytes de consulta, linhas, memória e tempo de execução.
Ele não inicia outro grupo após metade do orçamento da passagem e salva o UUID da última fonte antes do seu turno, de modo que uma fonte com falha não impeça a rotação. O UUID de tamanho fixo
também mantém os nomes das fontes fora do cursor persistido. Falhas de conexão usam um circuit breaker
limitado por fonte. Os eventos de custo do worker mantêm a atribuição ao namespace.

<a id="review-and-publication"></a>

## Revisão e publicação

`learnings.review` aceita esses candidatos somente para o destino `memory`. Antes de uma
escrita, ela verifica a capacidade da frota, a sensibilidade, o limite de definições compartilhadas e conflitos de nome. Um rascunho revisa a definição compartilhada ativa cujo termo ou alias, após normalização de caixa, corresponda
ao seu termo; correspondências ambíguas são recusadas. Um termo novo usa a chave aleatória `definition/` persistida
com a reivindicação. As linhas compartilhadas são relidas antes de escrever, para que uma renomeação concorrente não
redirecione a escrita para um termo diferente.

A revisão escreve uma definição derivada, válida em todo o namespace, por meio de `memory.set`, limpando
a expiração e os limites de validade. Uma versão atual exata, ativa e não expirada dispensa a escrita, mesmo quando
fixada ou bloqueada. Um valor alterado recusa uma versão atual fixada ou bloqueada. Uma revisão exige que
o texto de origem atual corresponda à cópia compartilhada; uma edição intermediária de outro escritor a recusa.
Versões atuais expiradas e retidas usam seu hash para uma reescrita condicional, e uma versão atual ausente usa
`if_match_sha256="absent"`. Chamadores condicionais são serializados na chave; escritas concorrentes comuns no ClickHouse
são resolvidas por versão. Essas verificações não tornam atômicas a escrita e a promoção.

A revisão chama `memory.promote` com o hash do valor canônico aprovado. O servidor
fornece internamente a proveniência do candidato e registra o ator autenticado. Um cliente
não pode fornecer proveniência confiável nem escolher o ator decisor por meio de `approver`.
A promoção publica a definição inteira, inclusive o SQL normalizado, para os leitores do
namespace, portanto o administrador aprova esse texto para todos eles.

Uma escrita confirmada seguida de falha na publicação ou na promoção deixa uma definição comum
válida em todo o namespace e um candidato pendente. A recuperação comum pode ler essa linha.
Uma nova tentativa promove uma versão atual exata e inalterada sem reescrevê-la. Rejeitar o candidato
deixa a linha de origem intacta. Não há desfazer automático: ele poderia sobrescrever a revisão
de metadados de mesmo texto feita por um escritor posterior. O administrador pode restaurar o texto pretendido por meio do histórico
de memória e das operações de escrita, ou excluir uma versão exata explicitamente.

<a id="erasure-audit-and-upgrades"></a>

## Exclusão, auditoria e atualizações

A exclusão do namespace apaga `loom.query_history_sources` por meio de
`query_history_sources.delete`. O registro contém configuração e uma credencial selada, por isso é classificado nessa etapa sem uma linha de censo de conteúdo de memória.
Os candidatos seguem a cobertura de exclusão da fila de learnings. Uma redação de definição histórica
também remove o texto dos candidatos privados correspondentes pelo hash do texto canônico ou pelas tags de candidato da versão redigida, em todas as versões de candidato. Ela usa o lease de revisão, recusa a revisão posterior de texto em branco e preserva uma versão atual ativa que ainda contenha o texto redigido.

A auditoria da revisão identifica o ator decisor autenticado e o candidato. Os `memory.set` e `memory.promote`
internos emitem seus próprios eventos de auditoria; o evento de promoção é também
o ID de requisição de auditoria que `memory.lineage` segue. As mutações de fonte auditam o nome do registro
no escopo, nunca sua senha.

A produção e a promoção de tipos privados exigem que todo heartbeat ativo de servidor e de worker
anuncie a migração do registro. Um estado da frota ausente ou ilegível falha de forma fechada.
Aplique a migração e atualize todos os servidores e workers antes de ativar qualquer um dos dois caminhos de rascunho. Mantenha os dois caminhos desativados enquanto houver alguma build em operação mais antiga ou desconhecida. Uma
build mais antiga não filtra candidatos privados nem apaga registros. As verificações da frota sozinhas
não protegem um rollback: uma requisição pode passar na verificação antes que um heartbeat mais antigo apareça.

O rollback exige uma janela de manutenção antes que qualquer leitor mais antigo seja iniciado:

1. Desative `knowledge_gaps_enabled` e `knowledge_query_history_enabled`. Interrompa novas
   requisições de rascunho privado, de revisão e de registro de fontes, e os turnos do worker do histórico de consultas. Esvazie o trabalho enfileirado e toda chamada ao modelo, revisão, registro
   e remoção admitidos. Desativar uma chave não cancela uma requisição ou revisão existente.
2. Deixe essas operações terminarem ou encerre-as, depois resolva seus envios incertos do servidor
   e os leases de escritor retidos dentro de seus limites. Não limpe as tabelas enquanto uma
   operação admitida ainda puder escrever um candidato, uma decisão ou um registro.
3. Com apenas uma build compatível em execução para a manutenção, remova todas as versões dos candidatos
   `knowledge_gap` e `query_history` de `loom.learnings_queue`, e todos os registros de
   `loom.query_history_sources`. Aguarde a conclusão da exclusão e verifique se ambos estão ausentes em toda réplica de armazenamento que um leitor possa usar. Preserve os candidatos
   comuns e as definições publicadas. Só então inicie um leitor mais antigo.

Mantenha os produtores privados desativados durante todo o downgrade. Ative-os novamente somente depois que
todos os processos em operação oferecerem suporte à migração do registro. A memória governada e as linhas compartilhadas
mantêm suas regras de atualização existentes.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `server/memory/app/definition_drafts.py`: reivindicações duráveis e geração limitada.
- `server/memory/app/definition_review.py`: barreira da frota, promoção e redação de candidatos.
- `server/knowledge/app/gap_drafts.py`: elegibilidade atual das lacunas e consentimento.
- `server/knowledge/app/query_history_sources.py`: verificações de registro e de credenciais.
- `server/knowledge/store/query_history_remote.py`: leituras agrupadas e somente leitura do log de consultas.
- `server/knowledge/workers/query_history.py`: rotação limitada de fontes.

<a id="invariants"></a>

## Invariantes

- Somente um administrador pode ler um candidato privado ou publicá-lo por meio da revisão.
- Nenhuma conclusão do modelo substitui a decisão sobre um candidato nem ressuscita evidências excluídas.
- Um registro de fonte não pode sobreviver à exclusão do namespace e continuar produzindo rascunhos.
- Uma promoção recusada ou interrompida não desfaz a linha de um escritor.
