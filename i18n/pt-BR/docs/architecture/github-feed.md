<!-- i18n: source=docs/architecture/github-feed.md blob=bcf50268160cb35aa8da8040dcb9b21ab7f6601a model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md), which is authoritative.

<a id="github-tracker-feed"></a>

# Feed do tracker do GitHub

O feed do GitHub mantém o registro do tracker de uma equipe no banco de dados ClickHouse da equipe.
Os processos de serviço do Loom leem esse registro por meio da
[fonte de tracker somente leitura](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md) já existente. O feed não
coloca corpos de issues, comentários ou pull requests nas tabelas de implantação do Loom.

<a id="ownership-and-credentials"></a>

## Propriedade e credenciais

Um operador executa o feed como um job de CLI limitado, agendado pelo executor de jobs da equipe.
Antes de ler o endpoint registrado ou abrir o registro, a CLI verifica a autoridade de armazenamento
da implantação real e se a sua célula detém a organização do feed.
A conexão externa repete essa verificação de implantação; ela nunca trata os metadados
do registro como autoridade da implantação. O job fecha os dois clientes e o pool de Postgres
da implantação ao sair.
A inicialização cria as tabelas do feed e as views do contrato em um banco de dados dedicado e vazio;
ela recusa objetos já existentes. Em seguida, o operador registra esse banco de dados como uma fonte de tracker
com uma credencial separada somente leitura. A configuração e a sincronização exigem esse
endpoint registrado e a sua lista de repositórios permitidos. Uma credencial de escrita fornecida separadamente
pode escrever apenas no banco de dados de feed da equipe; a credencial de leitura nunca é
promovida nem reutilizada para escrita.

A credencial da fonte é um token granular do GitHub somente leitura ou um token de instalação de App,
restrito aos repositórios configurados: Metadata, Issues e Pull requests,
todos somente leitura. As permissões do token são uma configuração atestada pelo operador: um
GET bem-sucedido, o prefixo do token ou um cabeçalho de permissão do endpoint não podem certificar a ausência de permissões de escrita.
Um token de host não identificado não é elegível para uma sonda ativa somente leitura. O token é
selado com o mecanismo de secret-root existente na tabela de configuração
do feed da equipe. Nenhum token aparece em argumentos, saída ou views do contrato.
A CLI recebe a senha de escrita do banco de dados por uma variável de ambiente nomeada;
ela não persiste essa senha. As requisições ao GitHub usam GET na origem fixa
`https://api.github.com`, recusam redirecionamentos e constroem os caminhos a partir de nomes de repositório validados
e ids numéricos de itens. Elas nunca seguem URLs presentes nas respostas.

A equipe é dona do registro copiado, da sua retenção de backup e da sua exclusão. Para apagar o conteúdo copiado,
a equipe interrompe o seu gravador agendado e remove as linhas do feed, os comentários e os checkpoints afetados
antes de reiniciar com uma exclusão ou de remover o repositório de origem. Excluir um namespace do Loom desativa os dados derivados e as concessões do Loom; isso
não exclui o registro da equipe. Essa é a mesma fronteira de qualquer banco de dados de tracker
registrado. A credencial de escrita do registro não consegue acessar o banco de dados do próprio Loom; o conteúdo copiado
nunca é escrito lá. Apenas o lease do armazenamento da implantação existente é local.

<a id="record-and-progress"></a>

## Registro e progresso

Itens brutos e comentários são versionados no banco de dados da equipe com a tupla de identidade,
o ator do processo e a classe de origem importada. As views do contrato expõem a versão mais recente do item,
os UUIDs do contrato e a visibilidade efetiva. Comentários de issues e de pull requests são
mantidos separadamente e incluídos no corpo pesquisável do item correspondente. Campos de triagem
não suportados usam os padrões tipados do contrato; as arestas ficam vazias até que existam evidências.
O `updated_at` do contrato é o carimbo de data/hora de publicação persistido mais recente entre o item
e os seus comentários, de modo que importações tardias e edições de comentários avançam as leituras incrementais do tracker.
As versões de publicação usam milissegundos inteiros para corresponder aos cursores do tracker. Sob o lease
de escrita do registro, cada lote de conteúdo recebe o maior valor entre o relógio do registro e um milissegundo
além de toda versão de conteúdo persistida da sua organização, em todos os repositórios.
Essa ordenação também cobre namespaces que leem vários repositórios por um único cursor.
As escritas de controle de repositório também avançam além da versão persistida para a sua organização
e repositório exatos, mesmo quando o relógio de quem recebe retrocede. Revogação de visibilidade,
rotação de credenciais e checkpoints de cursor, portanto, substituem a linha de controle anterior
em vez de perder para o seu carimbo de data/hora.
Os carimbos de data/hora originais do upstream permanecem separados e orientam os cursores da API. Nenhuma chamada a modelo ou a embedding
é necessária. As identidades de repositório mantêm a grafia da lista de permitidos registrada,
mesmo quando a API retorna uma capitalização diferente.

As leituras de proprietário, de revisão do repositório e de marca-d'água de publicação solicitam
`select_sequential_consistency = 1`; o lease de escrita sozinho não torna uma réplica
atual. Os gravadores do feed exigem armazenamento SharedMergeTree ou um único servidor self-hosted,
seguindo as [garantias de read-your-writes](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#read-your-writes).
A configuração sozinha não cobre escritas confirmadas em tabelas replicadas self-hosted.
A validação do gravador aceita apenas `ReplacingMergeTree` e `SharedReplacingMergeTree`
para as cinco tabelas do feed no banco de dados do registro conectado. Engines de tabela replicadas, distribuídas
e não reconhecidas são recusadas antes que o job possa publicar.

Cada repositório tem cursores independentes de itens, de comentários de issues e de comentários de revisão. O
[endpoint de issues](https://docs.github.com/en/rest/issues/issues#list-repository-issues)
lista issues e pull requests juntos; os pull requests são hidratados pelo seu endpoint
numérico. O
[endpoint de comentários de issues](https://docs.github.com/en/rest/issues/comments#list-issue-comments-for-a-repository)
e o [endpoint de comentários de revisão](https://docs.github.com/en/rest/pulls/comments#list-review-comments-in-a-repository)
têm varreduras separadas por hora de atualização. Cada um usa `updated_at` ascendente, com limites de páginas,
de bytes de resposta e de tempo de requisição. O avanço do cursor segue apenas escritas confirmadas,
por meio de uma fronteira de timestamp completa. As requisições reduzem em um segundo o timestamp salvo
porque `since` é exclusivo. Um orçamento de páginas esgotado salva o timestamp mais antigo da sua
última página; o turno seguinte reexecuta essa fronteira. O orçamento é de 2 a 100 páginas: uma página anterior
permite progresso enquanto a última página retém uma fronteira de timestamp incompleta.
Se um único timestamp preencher todo o orçamento de páginas configurado,
o job informa um erro de capacidade e mantém o checkpoint, em vez de pular linhas ou
alegar progresso. O operador pode aumentar o orçamento limitado de páginas para esse repositório.
As linhas reexecutadas substituem a mesma identidade. A API é uma coleção em mudança, não um snapshot;
varreduras sobrepostas reduzem as corridas de paginação sem alegar leituras transacionais do upstream.

Cada turno verifica a identidade e a visibilidade do repositório antes de copiar conteúdo. Um
repositório indisponível, movido ou divergente primeiro oculta as linhas de contrato desse repositório
e depois recusa o turno. As views do contrato também exigem uma observação de visibilidade bem-sucedida recente,
que expira após um intervalo limitado se o agendador parar. Cada publicação de item e de
comentário armazena a classificação de privacidade da observação na mesma linha do seu
conteúdo. As views usam a classificação mais restritiva entre o repositório, o item
e os comentários armazenados, tanto para `is_private` quanto para o UUID do contrato. Uma réplica que conhece
conteúdo novo antes dos metadados do repositório, portanto, não pode rotular uma publicação privada
como pública. O predicado existente do tracker padrão exclui essas linhas. Namespaces restritos
mantêm a sua verificação de concessão direta ao vivo. Visibilidade é uma observação por polling,
não um espelho instantâneo de permissões.
Uma divergência de identidade validada ou uma observação privada pode ocultar o registro atual mesmo
quando outro turno alterou a sua revisão; isso preserva as credenciais e os cursores atuais.
Falhas genéricas de uma credencial mais antiga mantêm a verificação de revisão. Observações públicas
obsoletas não podem ampliar o acesso.

Quando um repositório se torna público, a configuração ou a sincronização redefine os cursores de stream existentes
antes de publicar os metadados públicos. As varreduras limitadas reexecutam os itens e os comentários
e marcam cada um com a observação pública. Um item permanece restrito até que a sua própria
linha e os seus comentários armazenados tenham sido reexecutados. Turnos agendados repetidos continuam um
backlog sob os mesmos limites de página e de fronteira de timestamp. Linhas excluídas no upstream não podem ser
reexecutadas; a equipe deve apagar essas cópias retidas para eliminar a sua classificação
privada.

As listas incrementais da API não informam issues ou comentários excluídos. A equipe trata essas
exclusões explicitamente; o feed não alega detectar exclusões. A configuração, as credenciais seladas e os cursores
permanecem com o registro da equipe, de modo que reinicializações da CLI não dependem de
arquivos locais. A inicialização vincula o registro ao UUID do banco de dados de uma única implantação do Loom;
a configuração e a sincronização recusam qualquer outra implantação. Jobs concorrentes nessa implantação
usam um único lease de publicação com chave no UUID do banco de dados do registro, independente de aliases
de fonte e de renomeações de repositório. As buscas ao provedor ocorrem fora desse lease; a inicialização,
a configuração e a publicação o compartilham, de modo que observações de identidade inéditas e concorrentes
não possam contornar a serialização. Uma escrita incerta no banco de dados mantém a sua reivindicação durante a
janela limitada de envio do servidor. A exclusão do registro exige que a equipe interrompa antes todos os gravadores.

<a id="validation-and-rollout"></a>

## Validação e rollout

Fakes cobrem empates de paginação, respostas parciais, limites de taxa, novas tentativas, mudanças de visibilidade,
redirecionamentos e redação de credenciais. Um ClickHouse privado verifica o contrato, as views atuais,
a reexecução e a publicação de cursor interrompida. Um token somente leitura, quando disponível,
verifica uma fatia limitada de repositório público; um token de CLI com permissão de escrita nunca é usado como substituto.
O feed não adiciona nenhuma operação de wire do Loom nem worker automático. Implantações existentes e
trackers registrados permanecem inalterados até que um operador inicialize e agende um feed.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- [CLI](https://github.com/ClickHouse/loom/blob/main/server/cli/tracker_feed.py): `register_tracker_feed` e `_run` selecionam o
  registro próprio e a sua credencial de escrita separada; `_validate_writer` verifica as concessões
  e as engines de tabela suportadas.
- [Aplicação do feed](https://github.com/ClickHouse/loom/blob/main/server/tracker/app/github_feed.py): `initialize`, `configure`,
  `sync` e `_publish` impõem a propriedade, marcam a privacidade da publicação e protegem as escritas.
- [Armazenamento do registro](https://github.com/ClickHouse/loom/blob/main/server/tracker/store/github_feed.py): `statements` define as
  views do contrato; `repository`, `put_repository` e `put_items` leem o estado de publicação
  de forma consistente.
- [Cliente da API](https://github.com/ClickHouse/loom/blob/main/server/clients/github_feed.py): `pages` limita as varreduras do upstream;
  as [regras de domínio](https://github.com/ClickHouse/loom/blob/main/server/tracker/domain/github_feed.py) validam identidades e
  escolhem as fronteiras de reexecução por meio de `next_cursor`.

<a id="invariants"></a>

## Invariantes

- O conteúdo copiado permanece no registro registrado da equipe, com exclusão sob responsabilidade da equipe.
- As verificações de identidade, de visibilidade e de revisão do repositório precedem a publicação de conteúdo;
  as leituras de controle incluem as escritas confirmadas do registro na topologia de armazenamento suportada.
- O conteúdo e a sua classificação de privacidade compartilham uma única versão de linha bruta. A privacidade do contrato
  e os UUIDs incluem a classificação de cada componente armazenado, mesmo quando as tabelas ficam defasadas
  de forma independente.
- Uma transição de privado para público redefine os cursores antes da publicação pública, de modo que as novas tentativas
  reexecutam o conteúdo retido sem descartar prematuramente a sua classificação privada.
- O lease de escrita de todo o registro cobre a publicação e as escritas de checkpoint. Um envio
  incerto mantém a sua reivindicação até ser resolvido; um cursor só avança após escritas confirmadas.
- As versões de conteúdo em milissegundos inteiros aumentam em todos os repositórios do registro,
  de modo que os corpos combinados de item e comentários permaneçam visíveis para os cursores existentes do tracker.
