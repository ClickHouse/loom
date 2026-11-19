<!-- i18n: source=docs/features/code-index/history-and-prs.md blob=6c133daf27b7ef459055a1cc94a7dd9563aa4255 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md), which is authoritative.

<a id="history-prs-and-releases"></a>

# Histórico, PRs e releases

Essas ops precisam do histórico de PRs mergeados; o worker `code_pr_history` vem desativado por padrão
(`LOOM_CODE_PR_HISTORY_ENABLED`).

O plano de PRs responde "como o código chegou até aqui". Ele fica no mesmo namespace de código
do índice, então uma resposta de histórico cruza os arquivos de um PR mergeado com os símbolos,
chamadores e testes de hoje em uma única leitura, sem nenhuma chamada ao GitHub no momento da pergunta. Os registros de issues e incidentes de uma equipe entram no cruzamento por meio de um [rastreador](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) vinculado.

<a id="the-pr-plane"></a>

## O plano de PRs

| Tabela                    | Contém                                                                                                                         | Preenchida por                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| `loom.code_pr_history`    | uma linha por PR integrado (merged) do branch do namespace: número, título, autor, labels, arquivos, issues vinculadas, merge commit | o worker `code_pr_history`; `loom code backfill-prs` para os primeiros meses         |
| `loom.code_pr_reviews`    | comentários de revisão inline dos PRs integrados                                                                               | o mesmo worker (`LOOM_CODE_PR_REVIEWS_ENABLED`, ativado por padrão; precisa de um token) |
| `loom.code_gh_items`      | issues e PRs em qualquer estado, com os PRs abertos acompanhados dos arquivos que o head deles altera                          | o mesmo worker (`LOOM_CODE_GH_ITEMS_ENABLED`, ativado por padrão; ignorado sem autenticação) |
| `loom.code_backport_gaps` | o relatório noturno de backports de todo o conjunto                                                                            | o worker `code_backport_gaps`                                                        |

O worker usa `LOOM_CODE_GITHUB_TOKEN` ou o `github_token` da entrada; na falta de ambos, aplica o limite de taxa anônimo do GitHub. Um namespace de release branch contém os PRs integrados nesse branch, então "o que mudou na 2.4" é lido no namespace 2.4. `loom code backfill-prs` carrega `LOOM_CODE_PR_BACKFILL_MONTHS` (12) meses de histórico de um repositório novo ([operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)).

<a id="which-op-answers-which-question"></a>

## Qual op responde a qual pergunta

| Pergunta                                                                  | Op                                                                                       |
| ------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Me dê um resumo deste PR, ou da mudança que estou prestes a fazer         | `code.context_pack` (`pr_number` ou `task`; `sections` mantém apenas os blocos sobre os quais você age) |
| Quais PRs mesclados tocaram este símbolo ou caminho, e o que os revisores disseram? | `code.history`, `code.symbol_history`                                                    |
| Quais PRs mesclados tocaram essas linhas por último, entre a release A e a B? | `code.blame_range`                                                                       |
| O que os revisores verificaram neste PR mesclado? O que costumam verificar? | `code.review_brief`, `code.conventions`                                                  |
| Antes de abrir um PR: já existe um parecido?                              | `code.pr_check`                                                                          |
| Quais PRs mesclados podem ter introduzido este bug?                       | `code.bisect_candidates`                                                                 |
| Quais branches de release ainda carregam o bug que uma correção removeu?  | `code.backport_gap` (uma correção), `code.backport_gaps` (toda correção com label)       |
| Qual build de CI carregou este commit primeiro?                           | `code.first_build`                                                                       |

Os argumentos estão na [referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code).

<a id="history-of-a-symbol-a-path-or-a-line-range"></a>

### Histórico de um símbolo, de um caminho ou de um intervalo de linhas

`code.history` lista os PRs mesclados que tocaram mais recentemente um símbolo (`name` ou
`symbol_id`), um `path`, ou determinados PR `numbers`: número, título, autor, labels e até
`reviews_per_pr` (padrão 3, `0` = nenhum) comentários de revisão humana sobre esse arquivo.
`code.symbol_history` restringe isso ao histórico de regressões de uma função: os PRs que
tocaram seu trecho, as issues vinculadas e os reverts, e os comentários de revisão dentro dele.

`code.blame_range` recebe um `path`, um intervalo de linhas (no máximo 400 linhas) e uma janela
dada como versões de release (`from_version`, `to_version`) ou shas de commit. Ele combina as
listas de arquivos do ledger de PRs na janela (um piso) com `git blame -w -M -L` no mirror,
com cada commit atribuído mapeado ao seu PR. Em um namespace de branch de release, ele também
lê o ledger do namespace do trunk, apenas quando quem chama pode ler o trunk; caso contrário,
as linhas do trunk ficam de fora e `legs.ledger.skipped` o indica. Uma versão de release é
resolvida para o head indexado do namespace da sua branch quando quem chama pode ler esse
namespace; senão, pela branch do mirror ou pela tag `-new`.

- `stage: "introduced"` (padrão) rebaixa backports, reverts e alterações apenas de espaço em
  branco ou de renomeação, e promove um PR que cita a função envolvente, recebeu um comentário
  de revisão dentro dela, ou cuja issue vinculada cita as palavras em `mentions`.
- `stage: "last_touched"` mantém a ordem do blame.

A resposta é exata para "tocou por último", nunca para "introduziu o bug"; seu `note` diz
isso.

<a id="review-briefs-and-conventions"></a>

### Review briefs e convenções

`code.review_brief` lê o brief que a ingestão montou quando o PR foi mesclado: a classe de
cada arquivo alterado, as funções tocadas com seus chamadores e fan-in, os testes que as
cobrem e o que os revisores disseram.

Nas camadas `standard` e `full`, o brief também tem uma seção `rationale`: as decisões de
design e restrições (linhas do tipo `decision`, ou marcadas com `decision` ou `constraint`,
ativas ou em conflito, como o bloco de pré-edição as mostra) no namespace de código do trunk
do repositório cujos `files` ou vínculos de código tocam os arquivos do PR. Ela é lida a cada
chamada, do namespace do trunk apenas quando quem chama pode lê-lo, e nunca é armazenada com
o brief; assim, uma decisão registrada ou alterada após o merge aparece na próxima leitura.
Ela lê os primeiros `LOOM_CODE_REVIEW_BRIEF_RATIONALE_PATHS` (10) arquivos alterados, com os
arquivos-fonte primeiro, entre os que o brief armazena, e mantém no máximo
`LOOM_CODE_REVIEW_BRIEF_RATIONALE_ROWS` (8) linhas, aquelas com algo a verificar primeiro.
Ela serve linhas inteiras, as linhas `check` primeiro e depois as restrições, até
`LOOM_CODE_REVIEW_BRIEF_RATIONALE_TOKENS` (250) tokens, e sempre a primeira linha; o
orçamento da camada ainda pode cortar a seção como qualquer outra. Cada linha traz a linha do
bloco de pré-edição correspondente, com seu valor cortado no tamanho da linha (fonte,
`(pinned)` e a nota de mudança de código mantidos), `pinned`, `code_state` e um `verdict`.
Cada arquivo alterado lido é uma leitura de `memory.for_file`: quatro leituras no ClickHouse
(correspondência de arquivos, vínculos de código, suas memórias, as linhas vinculadas; um
arquivo com muitos vínculos fatia a última), cada uma mantendo até 50 linhas com até
`LOOM_MEMORY_FOR_FILE_WHOLE_TOKENS` × 48 bytes de valor.

A verificação procura dois tipos de nome: os nomes em formato de código na cláusula
`Rejected:` de uma linha, até uma cláusula `Realized:` (entre crases, qualificados como
`A::b`, ou chamadas como `f()`), e os nomes em snake_case em qualquer parte do valor de uma
restrição, com ou sem crases, simples ou qualificados (`Setting::distinct_spill_ratio`). É uma
correspondência de palavra inteira nas linhas que o diff adiciona, lendo até 200,000
caracteres de um diff enviado ou 200,000 bytes do diff do mirror, de modo que `check`
significa "leia esta linha contra o diff", não que uma violação foi encontrada.

| Verdict da linha | Significado                                                                                                                                                                                                            |
| ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check`          | o diff adiciona um dos nomes verificados da linha (`names`)                                                                                                                                                            |
| `clear`          | a linha tem nomes verificados e o diff não adiciona nenhum deles                                                                                                                                                       |
| `unknown`        | nenhum nome encontrado, mas o diff não pôde ser lido aqui (o git falhou, mesmo parcialmente), foi cortado, ou tem um hunk que conta as linhas errado, ou o valor da linha foi cortado (`diff_unread`, `diff_cut`, `diff_malformed`, `value_cut`) |
| `""`             | a linha não cita nada para verificar                                                                                                                                                                                   |

O verdict da seção é `check` quando qualquer linha o indica, depois `unknown`, depois
`clear`, e `none` quando nenhuma linha tem algo para verificar. Ele diz `clear` ou `none`
apenas quando todo caminho alterado e toda linha correspondente foram verificados; caso
contrário, diz `unknown`, e as causas dizem o que ficou faltando: nenhum repositório está
configurado para o namespace (`no_repo`), o brief não armazena nenhum caminho alterado
(`no_paths`), quem chama não pode ler o namespace do trunk (`trunk_not_readable`), uma lista
armazenada de caminhos alterados que pode estar cortada (`paths_maybe_cut`: o brief armazena
os primeiros 50 arquivos alterados mais os arquivos dos hunks do diff, até 200, de modo que
uma lista de 50 ou mais pode conter menos do que o PR alterou), caminhos alterados dessa
lista não lidos (`paths_not_read`), um caminho cuja leitura atingiu o limite de 50 linhas
(`paths_at_row_ceiling`), um caminho cuja leitura de vínculos de código falhou
(`links_not_read`), ou linhas que o limite de linhas deixou de fora e que tinham algo a
verificar (`rows_not_read`).

`code.conventions` declara as verificações recorrentes dos mantenedores do repositório como
fatos sobre o diff de um PR (`pr_number`, `head_sha`, `files` ou `diff`), nunca como opiniões;
`rules` restringe quais verificações são executadas. O diff que ele lê é limitado a
`LOOM_CODE_CONVENTIONS_DIFF_MAX_BYTES`.

<a id="pre-pr-duplicate-check-codepr_check"></a>

## Verificação de duplicatas antes do PR: `code.pr_check`

Um agente prestes a abrir um pull request envia o rascunho e recebe um de três veredictos:
`duplicate_of` (um PR aberto já está fazendo isso, ou um PR já mesclado fez),
`related` ou `none`.

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="pr-bot") as bot:
    check = await bot.call(
        "code.pr_check",
        title="Retry the upload when the token expires",
        paths=["src/upload/client.py"],
        symbols=["UploadClient.send"],
        issues=[1432],
    )
    if check["verdict"] == "duplicate_of":
        for item in check["items"]:
            print(item["pr_number"], item["state"], item["evidence"])
```

A mesma chamada via HTTP é `POST /v1/code.pr_check`, e via MCP a ferramenta é
`loom_code_pr_check` (nos toolsets `coding` e `tracker`). É obrigatório informar um título, caminhos alterados ou
um número de issue.

Os candidatos vêm dos próprios ledgers do Loom por meio de quatro etapas, sem nenhuma chamada ao GitHub:

| Etapa                    | Lê                                                                                                                                       |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| PRs abertos              | todo PR aberto em `code_gh_items`: o título e os arquivos que o `refs/pull/N/head` altera (sem o corpo)                                  |
| fechados sem merge       | PRs fechados sem merge nos últimos `LOOM_CODE_PR_CHECK_CLOSED_DAYS` (180): "alguém já tentou isso"; limitado a `related`                   |
| mesclados, por caminho   | PRs mesclados que alteraram os `paths` do rascunho                                                                                       |
| mesclados, por texto     | PRs mesclados cujo texto pontua contra o título e os símbolos do rascunho, e PRs mesclados que fecham as mesmas `issues`                 |

Cada candidato recebe uma pontuação por uma regra fixa: a fração dos tokens do título e dos símbolos do rascunho que ele contém (ponderada pela raridade), um pequeno termo de corpo para PRs mesclados, a sobreposição de caminhos nas duas direções e se ele fecha uma issue citada pelo rascunho. Não há um conjunto rotulado para ajustar os pesos, já que PRs duplicados são fechados, não mesclados. A resposta traz `epistemic: "heuristic"`, e um título sozinho nunca chega a `duplicate_of`.

| Configuração                             | Padrão | Efeito                                                                              |
| ---------------------------------------- | ------ | ----------------------------------------------------------------------------------- |
| `LOOM_CODE_PR_CHECK_DUPLICATE_THRESHOLD` | 0.55   | pontuação a partir da qual um candidato é `duplicate_of`                            |
| `LOOM_CODE_PR_CHECK_RELATED_THRESHOLD`   | 0.2    | pontuação a partir da qual ele é `related`                                          |
| `LOOM_CODE_PR_CHECK_MERGED_DAYS`         | 365    | PRs mesclados mais antigos que isso não votam como duplicatas                       |
| `LOOM_CODE_PR_CHECK_MASS_FILES`          | 200    | um candidato que altera mais arquivos é uma mudança em massa; seus arquivos não votam |

`tracker.duplicate_check` é o equivalente do lado das issues: em um veredicto `none` ou `related`, ele traz os PRs do `code.pr_check` que talvez já tratem o rascunho como `related_prs`
([visão geral do tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md#duplicate-check)).

<a id="bisect-candidates-codebisect_candidates"></a>

## Candidatos de bisect: `code.bisect_candidates`

Dado um bug reportado, quais PRs mergeados um engenheiro deve investigar primeiro?
`code.bisect_candidates` prioriza candidatos; não é um `git bisect` automatizado.
Ele não compila o repositório, não executa testes, não reproduz o relato, não descobre versões boas e ruins
e não prova que um candidato causou o bug. A pontuação de um candidato é um ranking
de investigação, não uma probabilidade de causa.

O limite da regressão vem de evidências registradas antes da chamada:

| Entrada da janela             | De onde vem                                                                                                                       | O que o Loom faz com ela                                                                                                                            |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bad_version`, `good_version` | Um usuário, job de CI, fluxo de suporte ou agente de testes observou o bug na versão ruim e observou a versão boa como limpa.     | Examina as versões entre essas observações. O Loom não retesta nenhuma das versões.                                                                 |
| `issue_number`                | A issue vinculada no tracker já tem observações de triagem `reproduces_on` e `clean_on`, preenchidas pelo fluxo do tracker.       | Lê essas observações quando versões explícitas não foram informadas. A existência de uma issue ou de um valor de triagem não prova que o relato é válido. |
| `after`, `before`             | O chamador conhece apenas um intervalo aproximado de tempo de merge, como a última data boa conhecida e a data da primeira ocorrência. | Restringe a varredura do histórico de PRs a esse intervalo; as datas são limites, não resultados de reprodução.                                     |
| `bad_sha`                     | O chamador conhece um commit igual ou anterior à primeira falha observada.                                                        | Resolve o horário do commit no espelho do repositório e o usa como `before` quando `before` está ausente. Não executa o commit.                     |

O chamador então fornece as evidências do relato que tiver: frames de crash (`symbols`),
`paths` de código-fonte, `patterns`, `text` livre, um `issue_number` e, quando conhecido, um `fix_pr`.
Quando não há um intervalo preciso de versões, o fallback pelo histórico do repositório usa o histórico
recente retido para esta varredura (`LOOM_CODE_BISECT_HISTORY_DAYS`, padrão 90), que é uma
janela de investigação mais ampla e menos precisa.

1. Os frames são resolvidos para arquivos por meio do índice de código.
2. A varredura de candidatos lê os PRs mergeados e não derivados cuja primeira versão está
   na janela e que alteraram esses arquivos, ordenados pelos frames que o diff menciona. Com
   um tracker vinculado, ela varre os diffs de PRs do tracker; as colunas de triagem registradas anteriormente
   de `issue_number` (`reproduces_on`, `clean_on`) podem fornecer a janela. Sem tracker,
   ou com um tracker sem arquivos alterados, ela lê o histórico de PRs do próprio repositório
   (`LOOM_CODE_BISECT_HISTORY_DAYS`, padrão 90), e a resposta traz
   `candidates_from: "code_history"`.
3. Sem frame (um relato de resultado errado) e com um `issue_number`, a varredura é limitada
   pelos identificadores mais raros da issue nos diffs da janela, pelos diretórios do componente
   e pelos arquivos da correção; `wideners` escolhe quais, e `widened_by` informa qual foi aplicado.
4. A fusão no momento do relato combina a varredura de diffs com os rankings vetorial, lexical, de caminho, de subárvore e de
   recência; um PR que o relato aponta como introdutor do bug fica na frente. Quando o topo fundido
   é fraco (margem sobre o #2 abaixo de 0.1 e não é um suspeito citado no relato), uma janela mais ampla
   (12 versões, ou `LOOM_CODE_BISECT_WIDE_DAYS`) é incluída e uma chamada de LLM reordena os 20
   primeiros (`LOOM_CODE_BISECT_LLM_RERANK_MODEL`; um timeout mantém a ordem fundida), ou
   `rerank: true` solicita isso.
5. Um relato que não cita correção nem issue toma emprestada a correção da issue com a qual a verificação de duplicatas
   encontra correspondência, dentro de `LOOM_CODE_BISECT_DUPCHECK_TIMEOUT_S`.

Cada candidato traz marcas (backport, revert, revertido, re-land, apenas whitespace ou
renomeação, `touches_symbol` a partir dos cabeçalhos de hunk do diff); `marks: false` retorna a
lista ordenada sem marcas. Quais caminhos contam como código-fonte é definido por `bisect.code_prefixes` do tracker ([contrato de registro](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#source-entry-options)).
O rerank precisa de um provedor de LLM real; as outras etapas rodam na stack padrão assim que o histórico
de PRs está carregado. As configurações de bisect estão listadas na
[referência de configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#code).

O exemplo abaixo ordena os PRs mergeados que entraram entre uma versão boa e uma ruim
e alteraram os arquivos de uma função que falha. `bad_version` é a primeira versão em que o
bug foi visto e `good_version` a versão mais nova vista como limpa; o Loom não testa nenhuma delas.

A janela de versões precisa de um namespace de tracker vinculado ao repositório cujos registros de PR tragam `minor_version` (o [contrato de registro](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)). Apenas com o histórico de PRs mergeados
(`LOOM_CODE_PR_HISTORY_ENABLED` ou `loom code backfill-prs`; veja [primeira indexação](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#first-index)), as versões
não limitam a varredura: a op lê os últimos `LOOM_CODE_BISECT_HISTORY_DAYS` (90 por padrão).

```python
from loom import Agent

async with Agent(org="acme", namespace="code-widgets", agent="triage-bot") as bot:
    result = await bot.call(
        "code.bisect_candidates",
        bad_version="26.3",
        good_version="26.2",
        symbols=["UploadClient.send"],
    )
    for candidate in result["candidates"]:
        print(candidate["pr_number"], candidate["score"])
```

Os candidatos voltam ordenados. O `score` indica quais PRs investigar primeiro; ele não
mostra que um PR causou o bug. Um engenheiro ou um job de CI ainda precisa reproduzir o
bug e testar os candidatos.

A verificação final permanece externa a esta operação: um engenheiro, fluxo de CI ou
agente de testes deve reproduzir a issue e testar os candidatos ordenados.

<a id="backport-gaps"></a>

## Lacunas de backport

`code.backport_gap` nomeia uma correção já mesclada (`pr_number`) ou a issue que ela fecha
(`issue_number`, resolvida pelas arestas `fixes` do rastreador ou por um vínculo recuperado) e
responde por namespace de branch de release do mesmo repositório, do mais novo para o mais antigo, ou para os
`branches` que você indicar (namespaces ou versões como `26.4`). Três pontos se encontram: o registro de
backport (`is_backport_pr`, `upstream_pr_number` e `base_branch` do rastreador, ao lado dos backports
mesclados do ledger de branches), a primeira release da correção (um branch criado nessa release ou depois dela
contém a correção) e se as funções que os hunks da correção tocam existem em cada branch.

| Veredito       | Significado                                                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `gap`          | sem backport, o branch é anterior à correção e as funções (ou arquivos) tocados estão presentes; `cherry_pick` nomeia o PR e o sha do merge |
| `pending`      | existe um PR de backport e ele está aberto                                                                                           |
| `covered`      | um backport foi mesclado, o branch foi criado na primeira release da correção ou depois dela, ou os `fix_versions` da issue nomeiam o branch |
| `not_affected` | todas as funções (ou arquivos) tocados estão ausentes no branch                                                                      |
| `unknown`      | o registro estava indisponível enquanto o código está presente, a verificação de presença esgotou o tempo ou o branch não tem versão para comparação |

Um `gap` em nível de símbolo é um limite inferior: o código existe no branch, o que não
prova que o bug também exista.

- `exact: true` verifica o patch por hunk. Linhas removidas ainda presentes e linhas adicionadas
  ausentes tornam o `gap` exato; linhas adicionadas presentes, linhas removidas já ausentes e nenhum backport
  registrado tornam o `not_affected` exato.
- `history: true` (implica `exact`) também lê a árvore e o histórico do branch no
  mirror, para as linhas que a comparação de linhas deixa indecididas.
- `presence.patch` mostra o modo e as sobreposições como evidência.

`code.backport_gaps` é o relatório do conjunto inteiro: toda correção dos últimos 90 dias que
os mantenedores marcaram para backport, por branch de release, com as lacunas primeiro. O worker
`code_backport_gaps` o grava toda noite em `loom.code_backport_gaps`, executando
`code.backport_gap(exact, history)` para cada correção (`LOOM_CODE_BACKPORT_GAPS_ENABLED`, ativado por
padrão, efetivo apenas com `LOOM_CODE_PR_HISTORY_ENABLED`); a op lê essa tabela.
`wanted_only: false` inclui as correções sem marcação. Todo namespace de branch é autorizado
antes de ser lido, falhando de forma fechada.

<a id="first-build"></a>

## Primeiro build

`code.first_build` recebe o `sha` de um commit ou o `pr_number` de um PR já integrado (merged) e responde qual build de CI o incluiu primeiro. Ele precisa da tabela `version_history` do tracker vinculado: uma execução que cita o sha dá a resposta exata; caso contrário, as bases das execuções posteriores ao merge são verificadas no mirror (`git merge-base --is-ancestor`, no máximo dez) para encontrar o descendente mais antigo, e a resposta é um limite inferior.

<a id="pr-and-tracker-planes"></a>

## Planos de PR e de tracker

Um mantenedor vincula trackers a um repositório com `code.repo.set_source`, começando pelo principal
([visão geral do tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)). Depois disso:

- `code.bisect_candidates` examina os diffs de PR do tracker e confere os suspeitos com
  correções conhecidas;
- `code.backport_gap` lê o registro de backport, e `code.first_build` lê as execuções de CI
  do tracker;
- `code.history` e as partes de histórico de `code.search` e `code.ask` trazem o registro
  do tracker junto ao PR quando um vínculo é resolvido e o chamador tem permissão para lê-lo;
- `code.context_pack` pode incluir itens do tracker (`sections=["qa"]`).

Sem um tracker, as ops respondem a partir do plano de PR sempre que possível e indicam como ausentes
as partes que dependem apenas do tracker.

<a id="limits-and-failure-modes"></a>

## Limites e modos de falha

- **Sem histórico, sem resposta.** Com `LOOM_CODE_PR_HISTORY_ENABLED` desativado e sem `backfill-prs`, as tabelas de histórico ficam vazias e as ops respondem sem nada para ranquear.
- **O acesso anônimo ao GitHub** cobre repositórios públicos no limite de taxa público; comentários de revisão e o espelho de issues exigem um token.
- **PRs abertos não são código indexado.** `code.pr_check` enxerga o título e os arquivos de um PR aberto, não o corpo nem os símbolos.
- **Veredictos heurísticos.** `code.pr_check` é não calibrado por design; `code.blame_range` é exato apenas para "última alteração"; `code.bisect_candidates` ranqueia suspeitos e nunca aponta um culpado como fato.
- **Indisponibilidades do tracker degradam, nunca causam falha.** Uma leitura do tracker que expira por timeout ou aciona o circuit breaker define `tracker_unavailable`; as etapas do plano de PRs continuam respondendo.

<a id="related"></a>

## Relacionados

- [Visão geral do tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md) e [contrato de registro](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)
- [Adicionando repositórios](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md)
- [Operações do índice de código por pergunta](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Arquitetura do índice de código](../../architecture/code-index-architecture.md)
- [Referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#code)
