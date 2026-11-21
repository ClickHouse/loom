<!-- i18n: source=docs/architecture/provider-reconfiguration.md blob=71fa716069549df892dd0ea521757421e0539568 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/provider-reconfiguration.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/provider-reconfiguration.md), which is authoritative.

<a id="provider-reconfiguration"></a>

# Reconfiguração de provedores

O Loom armazena a configuração de modelos do painel como linhas criptografadas e versionadas em
`loom.provider_configuration`. Uma configuração tem um único predecessor confirmado;
salvamentos desatualizados são rejeitados. As substituições feitas no painel têm precedência sobre o ambiente
de inicialização do servidor. Redefinir para o ambiente restaura esse snapshot de inicialização,
e não um arquivo dotenv recém-editado.

<a id="activation-and-admission"></a>

## Ativação e admissão

A rotação de chaves e as alterações que envolvem apenas o LLM podem ser ativadas sem reconstruir os vetores.
Alterar o provedor de embeddings, o endpoint, o modelo ou as dimensões cria uma transição
enfileirada. Ela não deve ser ativada com vetores do espaço anterior.

O runtime fecha seu portão local de admissão de trabalho de modelo quando observa uma
transição não concluída, inclusive uma transição com falha. O trabalho já admitido pode
terminar; as novas requisições do plano de dados recebem uma resposta de manutenção que permite nova tentativa, e
as passagens dos workers esperam sem consumir o prazo de execução. As rotas de saúde, de configuração
de modelo e de controle do painel continuam acessíveis. Uma transição correspondente confirmada (committed) permite recalibrar
e reabrir a admissão. Uma revisão posterior que altere apenas a chave pode
satisfazer essa verificação por meio da cadeia de predecessoras confirmadas.

Esse portão conta o trabalho dentro de um único processo. Ele **não é uma barreira distribuída de escrita**
e não prova que as escritas adiadas ou outra réplica foram esgotadas.

O loop de atualização executa o coordenador fora desse portão. Todo processo de servidor e todo
comando da CLI do plano de dados nativo se inscreve em `loom._provider_participants` antes de
carregar as configurações confirmadas ou admitir trabalho. Uma transição exige que cada participante existente
confirme o mesmo ID de configuração depois que as requisições, as passagens dos workers, as tarefas filhas finitas
registradas e as escritas em buffer tiverem sido esgotadas. Um processo recém-criado
carrega o portão fechado pendente ou a substituição confirmada antes de atender requisições.

Cada participante grava sua linha a cada 10 s a partir de uma thread própria, e todo
cliente ClickHouse que ele abre informa seu id no User-Agent. Um participante só é considerado
parado quando se aposentou por conta própria (um desligamento limpo), quando um processo posterior no mesmo
host provou que ele não existe mais, ou quando um operador o aposentou. Um heartbeat expirado não é prova: um
processo pausado pode manter trabalho admitido fora do ClickHouse e gravá-lo com a configuração antiga
quando retomar, então a transição espera que ele envie o heartbeat novamente e
esgote o trabalho. Cada linha traz seu nome de host, o boot do kernel, o id do contêiner (o do Docker, ou o
pod e o nome do contêiner do Kubernetes), o namespace de pid, o pid e a hora de início; a cada 10 s
cada processo aposenta os participantes do próprio host e boot cujo pid não executa mais aquele processo, ou que
rodavam no próprio contêiner antes de ele reiniciar em um novo namespace de pid, desde que nenhuma consulta deles esteja em execução.
Isso cobre um processo encerrado à força cujo contêiner ou worker reinicia no mesmo lugar; dois contêineres que compartilham um nome de host
(rede do host, dois contêineres de um mesmo pod) nunca se aposentam mutuamente. Um operador que confirmou que o
processo não existe mais o aposenta com `loom participant retire --participant <id> --yes`,
que recusa um participante cujo heartbeat seja recente ou cujas consultas ainda estejam em execução. O endpoint `/v1/health`
do líder dos workers lista todo participante pelo qual uma transição esperaria como
`provider_participants_stale` (`heartbeat_expired` ou `queries_running`).
`system.processes` é lido por meio de `clusterAllReplicas(LOOM_CH_CLUSTER)`; um servidor
sem esse cluster responde apenas por si, e qualquer outra falha na leitura
faz o participante ser considerado em execução.

Cada etapa limitada de reconstrução mantém a chave compartilhada de configuração no Keeper, renovada a cada
20 s (TTL de 60 s). Um concorrente só assume uma chave expirada quando o
participante de seu detentor é considerado parado; um TTL decorrido, por si só, não é prova. `loom
provider-lock clear --holder <holder>` libera uma chave cujo detentor não tem
linha de participante, e recusa enquanto o detentor renova, envia heartbeat ou tem uma consulta
em execução. As migrações de boot nunca rodam sob essa chave: um boot a mantém apenas para verificar
que nenhuma reconstrução está inacabada, após se inscrever, de modo que uma reconfiguração iniciada enquanto
ele migra espera por ele. Um participante congelado por mais de 60 s pode ser considerado parado;
ele se inscreve novamente com o próximo heartbeat e fecha seu portão na próxima atualização.

O primeiro checkpoint do coordenador congela a identidade de endpoint/modelo e as larguras de prefixo
configuradas válidas. Reiniciar não adota configurações de prefixo alteradas.
Os clientes de reconstrução usam requisições brutas ao provedor com credenciais explícitas, contornando
tanto o cache do processo quanto o cache compartilhado de atendimento. As requisições irmãs concluem o cancelamento
antes que um lote com falha libere a propriedade. A ativação limpa os vetores do cache do processo
e a largura sondada antiga antes de reconstruir a calibração de atendimento.

<a id="rebuild-staging"></a>

## Staging da reconstrução

O inventário de reconstrução contabiliza todas as colunas de vetor ativas conhecidas. Os vetores de histórico são preservados; caches e prefixos de código derivados têm ações separadas. Superfícies de vetor desconhecidas fazem o preflight falhar, em vez de serem omitidas silenciosamente.

As primitivas de staging implementadas exigem um lease exclusivo do coordenador e escritores drenados:

- O preflight da fase de snapshot reconcilia cópias parciais por recibo, schema de origem e UUID antes de qualquer escrita. Cópias de origem inacabadas podem ser contabilizadas no inventário, mas são reconstruídas, nunca aceitas como completas. Destinos sem origens correspondentes concluídas e tabelas privadas inesperadas interrompem a recuperação.
- As cópias de origem mantêm todas as colunas graváveis e atribuem ordinais de paginação estáveis. Um recibo de conclusão vincula uma cópia bem-sucedida ao UUID da origem e à impressão digital do schema. Uma cópia não confirmada é reconstruída, não aceita com base na contagem de linhas.
- As tabelas replacing indexadas por modelo selecionam a revisão mais alta de cada chave lógica entre os rótulos de embedding antigos. Em revisões iguais, prefere-se um tombstone; depois, o rótulo do modelo antigo fornece um desempate determinístico. Uma revisão ativa posterior mantém uma restauração explícita. A seleção mantém a linha escolhida completa, em vez de misturar campos agregados de forma independente. A versão da política de origem faz parte da impressão digital do snapshot, de modo que recibos antigos não podem adotar silenciosamente uma receita alterada.
- A limpeza de snapshots verifica propriedade, UUID e status local da consulta. A inspeção de consultas deve usar o mesmo nó ClickHouse fixado usado na criação; ela não é uma confirmação em todo o cluster. Coordenadores antigos devem ser isolados (fenced) antes da recuperação.
- As tabelas de destino são tabelas `ReplacingMergeTree` privadas, indexadas pelo ordinal de origem. Lotes repetidos resultam em uma única linha lógica sob `FINAL`. O marcador de propriedade as vincula a um snapshot de origem e à impressão digital do espaço de embedding.
- A geração de vetores usa um embedder de destino fornecido explicitamente. Ela valida o lote completo antes de gravar e não altera os provedores globais do processo.
- A ação de cache invalida apenas o destino de cache privado próprio e verifica que ele está vazio, sem chamar um provedor de embedding. Isso também invalida entradas antigas específicas de endpoint quando o rótulo do modelo não muda. O cache em uso permanece intocado até que seu substituto seja publicado.
- As páginas do tracker hidratam o texto de título/corpo por meio do source gate já existente do namespace e da receita normal de indexação. A correspondência inclui tenant, tipo, número do item, ID da origem do repositório e digest do texto armazenado. Tombstones não precisam de leitura da origem. Uma origem presente cujo texto mudou atualiza o digest de texto em staging com o texto autorizado exato enviado ao provedor de destino. A propriedade e todos os demais metadados permanecem congelados; as linhas do snapshot não mudam. Origens ausentes ou inacessíveis interrompem a página antes de chamadas ao provedor ou gravações em staging. Tombstones do snapshot nunca disparam leituras da origem nem chamadas ao provedor.
- A derivação de prefixos usa vetores de código completo reconstruídos e verificados, mantendo seus metadados dentro do ClickHouse e aplicando a regra normal de normalização de prefixos. Ordinais estáveis separam as projeções configuradas. A verificação compara multiconjuntos completos de linhas para cada rótulo, não apenas contagens de linhas. Uma política de prefixos congelada vazia remove gêmeos obsoletos. O manifesto persiste a política; requisições que divergem dela falham antes de alterar o staging. A ativação em runtime reconstrói os rótulos de busca de código e as rotas de tabela a partir da receita concluída, inclusive após rotações de chave posteriores. Uma impressão digital de provedor/endpoint divergente mantém a admissão fechada. Um prefixo padrão habilitado mantém sua largura quando essa largura foi reconstruída; larguras ausentes ficam desabilitadas. Os caches de capacidade de vetor são atualizados antes de a admissão ser reaberta.
- Inserções em staging selecionam metadados inalterados diretamente da cópia de origem dentro do ClickHouse. O Python transporta apenas atualizações relacionadas a embedding, evitando reinterpretação de fuso horário e perda de precisão de timestamp nas idas e voltas de metadados.
- A verificação checa cobertura de linhas, igualdade de metadados com tratamento de nulos, identidade do modelo, dimensões dos vetores, vetores finitos e não nulos, e vetores limpos em tombstones. Um resultado de verificação ausente ou malformado não é sucesso.
- A verificação também checa a chave de ordenação ativa final. Ordinais de staging distintos não devem colapsar silenciosamente quando linhas de modelos antigos são reescritas para um único modelo e publicadas em um engine replacing ou aggregating. Colisões remanescentes fazem a verificação falhar após a seleção canônica da entrada.

O step runner persiste o progresso somente depois que um efeito é concluído. Os efeitos devem ser idempotentes, porque um processo pode parar após uma gravação no banco de dados, mas antes de seu checkpoint ser salvo. As falhas mantêm o último cursor e expõem um código de erro fixo; a ação de nova tentativa do administrador reenfileira essa mesma transição.

O checkpoint de progresso também carrega um manifesto de recuperação versionado, com o ID da configuração, a impressão digital do espaço de embedding, as dimensões de prefixo congeladas e o inventário completo e ordenado de tabelas. Uma política de prefixos ausente não é interpretada como uma política vazia; versões mais antigas do manifesto falham de forma fechada, em vez de adotar os padrões do runtime.
Cada entrada registra o schema e o UUID originais, além dos UUIDs do snapshot e do destino. A verificação adiciona o UUID preparado e a contagem de linhas verificada, sem alterar essas identidades congeladas. O runner persiste esse manifesto antes da publicação e o repassa à publicação e à limpeza. Manifestos ausentes, versões desconhecidas, inventários parciais e UUIDs reutilizados falham de forma fechada; a recuperação não infere a identidade da tabela a partir de um nome. O manifesto contém apenas metadados, sem credenciais, vetores ou conteúdo de origem.

O inventário de recuperação exclui uma cópia privada somente quando seu nome exato e seu UUID físico correspondem ao manifesto. Tabelas de vetor desconhecidas, cópias de outro job e substituições com o mesmo nome continuam falhando no preflight. Cópias não registradas em journal precisam de reconciliação por recibo antes dessa verificação; um prefixo de staging sozinho nunca contorna o inventário. Essa verificação de inventário não prova que todas as cópias registradas em journal ainda existem.

Os patches de vetor trafegam como dados de arquivo multipart JSONEachRow tipados, não como um único campo de parâmetro HTTP. Páginas de tamanho máximo podem exceder o limite de campo de formulário do ClickHouse mesmo quando o SQL em si é pequeno. Apenas o ordinal e os campos relacionados a embedding cruzam esse caminho de escrita; os metadados inalterados são unidos a partir do snapshot dentro do ClickHouse, mantendo a precisão de DateTime64. O upload não cria nenhuma tabela de patch persistente, e as gravações síncronas mantêm as configurações normais de nova tentativa e deduplicação.

<a id="publication-primitives"></a>

## Primitivas de publicação

A preparação do candidato clona o schema ativo para uma tabela privada e copia
apenas as colunas graváveis do staging verificado. Ela confere, dentro do ClickHouse,
tanto a contagem lógica de linhas quanto a igualdade de multiconjunto das linhas completas,
incluindo nulos e contagens de duplicatas, antes de registrar a conclusão. Retomar um candidato concluído repete
essas verificações; uma contagem de linhas igual, por si só, não prova que a cópia está íntegra. Atualmente, este
helper aceita apenas engines MergeTree locais: engines replicadas e compartilhadas
exigem um plano separado para o caminho de replicação.

Em tabelas codificadas, a preparação preserva o DDL canônico e o método Quantized e a
cadeia de codecs existentes, substituindo apenas a identidade da tabela, o comentário de propriedade
e a largura do vetor. O candidato é criado conforme o servidor aceita as configurações de codec
([configurações com restrição](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)): é recusado antes
do seu `CREATE` quando o codec não pode ser habilitado (um candidato obsoleto próprio é removido antes),
e é criado sem a cadeia quando `allow_suspicious_codecs` não pode ser habilitado. Os vetores de staging apagados viram
placeholders de zeros de largura fixa nessas colunas físicas; os flags de dimensão, as tombstones e os demais
metadados permanecem inalterados. A verificação de linhas completas compara essa projeção
física explícita, e não apenas uma contagem de linhas igual. As larguras de Rabitq e TurboQuant devem ser
múltiplos de oito. Argumentos de codec desconhecidos e índices vetoriais ou constraints
criados por operadores exigem um plano de dimensão separado; a preparação falha de forma fechada
em vez de remover codecs ou publicar um layout incompatível.

O helper de publicação recebe os UUIDs duráveis da tabela original e da tabela preparada.
Ele move a tabela ativa para um nome de backup próprio e, em seguida, move a tabela preparada para o nome
ativo. Uma nova tentativa reconhece, por UUID, os estados anterior, intermediário e concluído.
Ele não usa `EXCHANGE`: a nova tentativa automática de rede do cliente compartilhado poderia
repetir um exchange após a perda de uma resposta e restaurar a tabela antiga sem querer.

A receita congelada também registra no journal as materialized views com TO explícito que dependem das tabelas
e os UUIDs de suas substitutas. O DDL das views é criptografado porque definições escritas por operadores
podem conter segredos. Depois que todas as tabelas substitutas estão ativas, a publicação recria
essas views para que futuros inserts se vinculem à origem substituta, e não ao UUID
do backup. Respostas perdidas de DROP/CREATE são retomadas com base nas identidades registradas no journal;
identidades desconhecidas interrompem todo o preflight. Views com dados internos implícitos
não são suportadas: removê-las descartaria dados. Os destinos das views com TO explícito
são preservados, e o rebinding das views termina antes da limpeza dos backups.
A descoberta segue os arrays `dependencies_database` e
`dependencies_table` das tabelas de origem, e não suas dependências assíncronas de carregamento.
Metadados de view ausentes e dependências entre bancos de dados falham de forma fechada.

Essas renomeações sem volta deixam uma lacuna temporária no nome ativo. Todos os leitores e
escritores devem permanecer drenados até que todas as tabelas sejam publicadas e verificadas. A
tabela preparada já deve ter o schema, os índices e as constraints ativos pretendidos;
o alvo de staging indexado por ordinal não é, por si só, uma substituta publicável. A limpeza de backups
e a ativação do provider são etapas separadas do coordenador.

A limpeza valida cada tabela ativa contra o UUID da substituta registrado no journal antes de
remover qualquer cópia privada. Ela recusa cópias desconhecidas ou substituídas e verifica as consultas locais
e as mutations não concluídas. Os snapshots, alvos e backups exatos registrados no journal
são removidos com drops seguros para nova tentativa; cópias ausentes permitem retomar uma limpeza
interrompida. Um censo final de identidades não deve encontrar nenhuma cópia restante deste job. O
chamador continua sendo responsável pela barreira distribuída e mantém a admissão fechada durante todo o processo.

<a id="recovery-and-erasure"></a>

## Recuperação e exclusão

A ativação do runtime local aguarda a conclusão das requisições admitidas e do trabalho registrado em `TaskSet` antes de trocar os clientes de embedding e reabrir a admissão. A verificação não cancela escritas adiadas. Ela não é uma confirmação distribuída: tarefas não registradas, escritas em buffer e consultas no lado do servidor exigem verificações de drenagem separadas, e a conclusão por si só não prova que uma escrita foi bem-sucedida.

O backend de armazenamento concreto conecta manifestos de snapshot, reconstrução por tabela, verificação, preparação da substituição, publicação e limpeza ao executor de etapas durável. Ele recebe um embedder explícito, uma política congelada e um callback de barreira; ele não cria o lease do coordenador nem comprova por conta própria a quiescência distribuída.

As migrações de inicialização compartilham a barreira de configuração do provedor assim que a tabela dela existe. Reinicializações com o mesmo schema ignoram o DDL de migração enquanto uma transição está pendente; atualizações com migrações pendentes devem aguardar o término dessa transição. O DDL de retenção é admitido pelo gate de trabalho de modelo e é ignorado em uma reinicialização de manutenção. Aplique uma variável de ambiente de retenção alterada após a reconstrução, reiniciando novamente. As varreduras de execuções ociosas do MCP não iniciam novo trabalho de sumarização durante a manutenção.

<a id="docker-verification"></a>

## Verificação do Docker

`scripts/gates/docker_quickstart_smoke.py` instala o bundle de release gerado
em um projeto Compose novo e descartável. Ele verifica o carregamento do painel, a memória,
as toolchains do compilador incluídas, os resultados persistidos do compilador, as reinicializações e o caminho documentado
de atualização e recriação do ambiente pelo entrypoint de produção padrão.

A fase de provedores usa o Chromium para enviar o formulário real de Models & keys, rotacionar uma
chave, alterar as dimensões de embedding, tentar novamente uma reconstrução que falhou e restaurar a configuração
de ambiente pela interface de confirmação. Ela verifica vetores de
memória e de código não vazios e metadados preservados, a coordenação entre dois web workers,
as respostas de manutenção, a recuperação após falha,
a recriação de contêineres e a remoção das cópias privadas de reconstrução. A fase de provedores
redireciona apenas o endpoint HTTP do LLM upstream em um entrypoint Compose usado somente em testes;
as requisições de embedding usam um fixture de provedor compatível apenas de rede. A validação
no servidor, a autorização, a criptografia, o coordenador e o armazenamento ClickHouse continuam
reais. Credenciais e saídas do fixture não comprovam acesso a provedores pagos, qualidade de modelo
nem comportamento de cluster de produção. O CI executa este gate em amd64 e arm64.

Para uma execução local, primeiro instale as dependências do frontend e o Chromium
(`cd frontend && npm ci && npx playwright install chromium`), construa a imagem de
produção e então execute o gate com `--image <repository:tag>`. Ele remove apenas os
contêineres e volumes do projeto gerado.
Os testes unitários exercitam essa orquestração com efeitos de armazenamento simulados, não um fluxo completo
de mudança de provedor no Docker.

O coordenador avança os jobs enfileirados por snapshot, reconstrução, verificação,
publicação, limpeza e ativação. Jobs com falha permanecem fechados até uma nova tentativa explícita;
ela retoma o cursor durável. O progresso no painel expõe a fase, a tabela
e os contadores, não os manifestos ou receitas privados de recuperação.

As exclusões que chegam durante uma transição recebem a mesma resposta de manutenção
com nova tentativa que as demais gravações do plano de dados. As sagas de exclusão desacopladas
iniciadas antes são tarefas finitas registradas e devem terminar antes do snapshot. O censo de resíduos
também inspeciona cada origem, destino, cópia preparada e backup da reconstrução. Ele
conta linhas físicas, incluindo versões antigas de replacing e tombstones; cópias sem
colunas de escopo utilizáveis são contadas de forma conservadora em todos os escopos. Cópias desconhecidas
ou contagens ilegíveis fazem a verificação falhar, em vez de serem interpretadas como zero.
As cópias são removidas pela limpeza da reconstrução, não editadas por baixo da receita congelada dela.
Assim, uma reconstrução interrompida não pode deixar um snapshot fora da contabilidade de exclusão
nem informar a exclusão enquanto uma cópia ainda puder ressuscitar os dados do titular.

<a id="current-implementation-boundary"></a>

## Limite da implementação atual

Atualmente, a publicação oferece suporte a engines MergeTree locais em um único nó ClickHouse fixado. A preparação de engines replicadas/compartilhadas e a inspeção de consultas residuais em todo o cluster ainda precisam de um plano próprio e com suporte. Por isso, um salvamento ou redefinição que altere o espaço de embeddings é recusado com 409 enquanto qualquer tabela de reconstrução usar outra engine e qualquer tabela de re-embedding contiver linhas, antes de uma transição ser enfileirada ou de a admissão ser fechada. A resposta de status traz o mesmo motivo em `embedding_change_blocked`.
Sem vetores armazenados, não há nada a reconstruir, em qualquer armazenamento: a alteração é confirmada como ativa e cada processo drena e, em seguida, troca de modelo por meio da atualização da sua configuração. As linhas gravadas por um processo antes de ele trocar de modelo mantêm os vetores do modelo anterior. Escritores diretos no banco de dados fora dos caminhos de servidor/CLI registrados do Loom devem ser parados pelo operador; um censo dos processos do Loom não consegue isolar um cliente SQL externo arbitrário. Um participante remoto perdido, sem prova de falecimento, é um limite de recuperação explícito, não uma permissão para ignorá-lo. A validação E2E completa no Docker da troca de provedor e da recuperação após falha continua sendo um pré-requisito para o merge; a orquestração em testes unitários e os testes em nível de armazenamento não substituem esse fluxo.
