<!-- i18n: source=docs/architecture/documentation-translation.md blob=f3c39bd59c115e29a52e9ce3c79a91bd7e85666a model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/documentation-translation.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/documentation-translation.md), which is authoritative.

<a id="documentation-translation-requests"></a>

# Solicitações de tradução da documentação

O gerador de tradução divide o texto em inglês preparado em solicitações limitadas e verifica
a tradução montada antes de gravá-la. O inglês continua sendo a fonte de verdade.

<a id="request-planning"></a>

## Planejamento das solicitações

`english_requests` atribui âncoras de título com um único contador para a página inteira. Ele prefere cortes seguros
em títulos de segundo nível e agrupa seções grandes demais em fatias de origem de no máximo
16 KiB. As fatias reconstroem exatamente o inglês com âncoras quando unidas com quebras de linha.

O planejador usa os intervalos de origem dos tokens do CommonMark para itens de lista, blocos de código e linhas de tabela.
Código com cercas e indentado, código inline, elementos HTML completos e comentários permanecem juntos.
O cabeçalho de uma tabela fica com seu delimitador. Uma unidade indivisível grande demais é recusada antes da
primeira chamada ao modelo da página. A estimativa de custo usa a mesma lista de solicitações da tradução.

A extensão de listas do site aceita continuações com dois espaços, inclusive em itens ordenados.
Seu analisador não tem mapas de origem, então uma varredura de indentação protege esses itens junto com
os intervalos do CommonMark. Os itens de ambos os dialetos permanecem indivisíveis.

Algumas tabelas da extensão do site são código indentado no CommonMark. O intervalo de código permanece
indivisível, então um bloco grande demais com essa ambiguidade é recusado antes da admissão pelo provedor.
Mover a tabela para fora do contêiner da extensão resolve a ambiguidade no inglês.

<a id="original-unit-reconciliation"></a>

## Reconciliação de unidades originais

`reconcile_completed_units` é uma API de planejamento pura para reter solicitações concluídas entre
edições do inglês. Ela aceita contêineres `PreparedSource` histórico e atual e um manifesto completo de
`OriginalUnit` com seu digest. Ambos os contêineres incluem links preparados e
âncoras da página inteira. Cada intervalo original e cada hash de origem devem corresponder ao contêiner
histórico; as lacunas entre intervalos contêm exatamente uma string vazia ou LF. O digest do manifesto
vincula esses metadados, inclusive cada referência de resposta selecionada.

O chamador autentica o plano original completo e os status de conclusão em relação ao seu
registro de solicitações, ao cache e aos recibos das respostas selecionadas. A ausência de um arquivo de cache não prova nada.
Uma unidade original com `reply_ref=None` atesta explicitamente que não há resposta concluída
depois que as tentativas incertas são resolvidas. Uma resposta concluída com falha confirmada mantém sua referência e um
`UnitFault`; ela não pode virar trabalho inacabado por omissão dessa referência. O chamador também
fornece as recusas para todo o par locale/origem entre versões da origem.

O plano retém apenas unidades concluídas inteiras, únicas, monotônicas e idênticas byte a byte, em
limites atuais seguros para o analisador. Um payload pode ficar com sua quebra de linha final ou deixar um LF para a montagem;
payloads contíguos permanecem contíguos. Cada disposição carrega seu `SourceContext` atual,
a proveniência original, os identificadores de falha e os separadores exatos. Unidades concluídas podem exceder
o limite da solicitação quando reutilizadas sem chamada ao provedor. Lacunas inacabadas e inglês inserido
usam o planejador limitado fora dos intervalos retidos. `chunks.split` aceita um `source_context`
para que uma fatia de tabela sem cabeçalho use os limites de linha da tabela atual completa.

As disposições distinguem reutilização, correção com a mesma entrada, entrada alterada e trabalho inacabado.
Edições da origem e falhas que afetam uma unidade original compartilham uma única disposição de entrada alterada.
Candidatos repetidos, intervalos sobrepostos ou fora de ordem, limites inseguros, falhas desconhecidas
e recusas do par retornam conflitos. Uma unidade concluída alterada entre várias unidades originais
sem correspondência precisa de cortes de contrapartida comprovados; o planejador não adivinha uma partição.
A substituição de uma unidade concluída de página inteira não é suportada. A reutilização exata de página inteira continua
possível, mas a API não resolve todo mapeamento de página alterada.

Tanto `chunks.split` quanto `reconcile_completed_units` aceitam `bounded_new_request_bytes`,
um inteiro de 1 a 16384, excluindo booleanos. O padrão preserva os cortes de 16 KiB.
O limite se aplica a toda solicitação prospectiva, inclusive correções de entrada alterada e de mesma entrada.
Unidades concluídas reutilizadas permanecem inteiras, mesmo acima desse limite. Uma unidade concluída alterada ou com falha
que não couber retorna um conflito em vez de ser dividida.

Um limite menor pode refinar o trabalho inacabado após uma resposta truncada. O chamador autentica
a solicitação encerrada, o intervalo de origem, a resposta e o uso, todos os status de conclusão e recusa
e as entradas posteriores do registro. Seu pacote de execução vincula o limite selecionado e o plano atual exato,
inclusive os intervalos de contrapartida e as referências de unidades concluídas retidas. Um limite
sozinho não estabelece essa evidência, não libera uma retenção nem admite uma chamada. Um prefixo de resposta truncada
não é uma unidade concluída, e uma entrada menor não garante uma resposta completa.

`translate_text` aceita `initial_feedback` confiável com um contexto de origem atual para
restrições de entrada alterada. `request_system` inclui esse feedback exato no SYSTEM
canônico enviado ao provedor, de modo que o payload real da solicitação vincula as restrições. O feedback
é limitado a 4096 bytes de ASCII imprimível e LF; feedback vazio preserva o SYSTEM padrão.
A primeira mensagem contém apenas o inglês atual. Nenhuma resposta antiga do assistente para a entrada anterior
é reenviada; uma nova tentativa mecânica pode usar apenas a resposta retornada para a entrada atual.
Feedback inválido ou um contexto de origem divergente é recusado antes de qualquer gasto ou chamada ao provedor.

Um plano de reconciliação não resolve respostas, não admite chamadas ao provedor nem grava saída.
Os consumidores autenticam as respostas resolvidas e as validam em seus contextos atuais,
depois validam a página montada completa. Os invariantes de conteúdo e os dois dialetos de tabela continuam
valendo. A publicação também exige revisão semântica e de completude do idioma em relação ao inglês atual;
o sucesso estrutural não elimina falhas confirmadas nem recusas.

<a id="assembled-validation"></a>

## Validação da página montada

A página montada passa pelos invariantes de conteúdo e pelas verificações de tabela existentes antes de seu
diretório de saída ou arquivo ser criado. As verificações de tabela usam tanto o analisador Markdown
do site de documentação quanto o CommonMark com sua extensão de tabelas: as regras de contêiner deles diferem.
Elas retêm grupos de tabela, linhas, células, alinhamento e se uma célula está vazia. As extensões de listas,
admoestações e código do site participam da verificação.
O texto da célula é medido após a serialização e a restauração do HTML bruto, para que marcação vazia não
oculte texto ausente e tags literais escapadas continuem sendo texto.

Os pacotes dos analisadores são fixados nos requisitos de tradução e no extra de desenvolvimento.
A versão fixada do analisador do site corresponde à do gerador da documentação. Um limite de bytes da origem não
garante que uma resposta traduzida caiba no limite de tokens de saída. Respostas truncadas continuam sendo
falhas, e as verificações estruturais não estabelecem a qualidade da tradução nem a completude da prosa.

<a id="source-spelling-contracts"></a>

## Contratos de grafia da origem

A validação de rótulos distingue a grafia da origem das descrições comuns. Declarações de nomes
específicas da origem protegem trechos exatos de produtos e projetos. Um rótulo de nome de arquivo deve corresponder
a um arquivo rastreado do repositório no destino do link da origem; o rótulo convencional `README` pode omitir o sufixo `.md`.
Declarações de campos de operação protegem vocabulários de strings retornados e uma fórmula
de campo documentada. Um campo do tipo string cuja descrição inteira seja uma lista de alternativas de identificadores entre aspas
também declara valores literais. Aspas, capitalização e grafia semelhante a nome de arquivo,
por si sós, não concedem isenção.

Essas declarações exigem evidência na origem: identidades de pacotes ou projetos, terminologia
explícita, destinos de arquivos reais ou o contrato de transmissão da operação. Alterações nessas declarações da origem
exigem verificar a atestação e seus casos de regressão focados. Qualificadores comuns
ao redor de um trecho protegido continuam sujeitos à validação de rótulos.

Os analisadores existentes do site e do CommonMark fornecem as células de tabela, inclusive pipes escapados e
pipes em código inline. Cada solicitação limitada é verificada em seu contêiner completo em inglês,
sem enviar esse texto ao redor ao provedor. Células exclusivas do site, sem mapas de origem, são
verificadas na montagem da página. As novas tentativas, a montagem e as verificações da página armazenada compartilham a mesma regra:
os trechos protegidos devem manter sua grafia, ordem de ocorrência e rótulo ou campo de origem.
Este é um contrato de origem finito, não um classificador de idioma nem uma revisão semântica.

<a id="invariants"></a>

## Invariantes

- Toda fatia de origem enviada a um provedor fica dentro do limite; todos os bytes da origem e
  as âncoras da página sobrevivem à montagem. As unidades concluídas reutilizadas mantêm seus payloads inteiros.
- Uma unidade indivisível é recusada antes da admissão pelo provedor.
- As configurações do provedor, os limites de tokens de saída, as novas tentativas e as verificações financeiras cumulativas governam
  cada solicitação de forma independente de como a página é dividida.
- A saída só é gravada depois que a validação da página montada é bem-sucedida.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `scripts/i18n/translate.py`: `english_requests`, `request_chars`, `translate_page`.
- `scripts/i18n/translate.py`: `request_system`, `translate_text` e feedback inicial.
- `scripts/i18n/reconcile.py`: `reconcile_completed_units`, `OriginalUnit` e vinculação do manifesto.
- `scripts/i18n/chunks.py`: intervalos protegidos, cortes de origem contextuais e células de tabela analisadas.
- `scripts/i18n/context.py`: declarações de grafia da origem e projeção residual de rótulos.
- `scripts/i18n/requirements.in`: dependências de runtime da tradução.
