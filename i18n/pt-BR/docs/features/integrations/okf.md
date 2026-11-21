<!-- i18n: source=docs/features/integrations/okf.md blob=7d8ba9d5d921d351c46c4592c82a48277bb4f2cb model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md), which is authoritative.

<a id="okf-bundles"></a>

# Bundles OKF

Um namespace pode ser renderizado como um bundle OKF (Open Knowledge Format) v0.2: um arquivo Markdown
por memória com frontmatter YAML, um `index.md` por diretório listando cada conceito
com uma descrição de uma linha, e um `log.md` com as alterações. A CLI roda no lado do cliente, sobre
a API HTTP. Um bundle pode ser comparado com diff, revisado em um pull request e lido offline; o Loom continua
sendo o armazenamento.

O bundle é uma projeção, não um segundo armazenamento: ele é regenerado a partir do namespace,
e uma edição volta pelo caminho de escrita comum (detecção de conflitos, blacklist,
auditoria).

```bash
loom export-okf --namespace <ns> --out ./knowledge        # render
loom okf-lint ./knowledge                                 # offline check: exit 2 critical, 1 warnings, 0 clean
loom import-okf ./knowledge --namespace <ns> --dry-run    # plan the write-back
```

Os três comandos aceitam `--server-url`, `--token` (padrão `$LOOM_API_KEY`, depois
`~/.loom/token`) e `--org` (padrão: a claim `org` do token); o `okf-lint` não precisa de nenhum
deles. Todas as flags estão na [referência da CLI](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md).

<a id="export"></a>

## Exportação

`loom export-okf` é somente leitura. Ele pagina todas as linhas ativas do namespace (todos os agentes) por meio de `memory.list`, busca os valores completos com `memory.bulk_get` e grava os arquivos planejados em `--out`. Os arquivos de conceito existentes são sobrescritos; nada mais no diretório é alterado. O mapeamento fica em `server/memory/domain/okf_bundle.py` (funções puras, com testes unitários); `server/cli/okf.py` cuida do transporte e do sistema de arquivos.

| Memória                            | Conceito                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_key`                       | nome do arquivo e `loom.memory_key`: o slug da chave, limitado a 160 bytes, com um `-<hash>` da chave e do id da linha quando outra chave já usa esse nome ou quando ele é o de um arquivo do bundle (`index`, `log`, `README`, `AGENTS`, em qualquer caixa) ou o nome de um dispositivo do Windows (`nul`, `con`, `com1`)                                                                                         |
| `memory_type`                      | diretório (o slug do tipo em minúsculas, limitado como um nome de arquivo; `type-<slug>` para `evidence`, `digests`, `staging`, `node_modules`, o nome completo de um arquivo do bundle (`index.md`, `log.md`) ou o nome de um dispositivo do Windows, como `nul`) e `type`: `semantic` → Fact, `procedural` → Procedure, `preference` → Preference, `episodic` → Event, qualquer outro com a inicial maiúscula |
| valor                              | corpo: texto como está, JSON como um bloco delimitado                                                                                                                                                                                                                                                                                                                                                            |
| `tags`                             | `tags`                                                                                                                                                                                                                                                                                                                                                                                                           |
| agente e `created_at`              | `generated: {by: "agent:<agent>", at}`                                                                                                                                                                                                                                                                                                                                                                           |
| `verified_by` / `verified_at` ativos | `verified: [{by, at}]` (apenas atestações de humanos e de processos)                                                                                                                                                                                                                                                                                                                                           |
| `status`                           | `active` → `stable`, `conflicted` → `draft`, qualquer outro → `deprecated`                                                                                                                                                                                                                                                                                                                                       |
| `expires_at`                       | `stale_after`, uma data limite de revisão                                                                                                                                                                                                                                                                                                                                                                        |
| `source_resource_id`               | `sources: [{resource}]`                                                                                                                                                                                                                                                                                                                                                                                          |
| tudo o que é específico do Loom    | o bloco `loom:`, incluindo `source_class`, `sensitivity`, `superseded_by`, `valid_to` e `content_sha256`                                                                                                                                                                                                                                                                                                         |

**Links.** As arestas criadas por alguém (as arestas tipadas `memory.link` do namespace, lidas com `memory.export_evidence kind=edges`, e a cadeia `supersedes` que uma linha substituída implica) são gravadas como o frontmatter autoritativo `relates_to: [{path, kind}]` e espelhadas na seção "Related Concepts" do corpo. Em seguida vêm os links de entidades compartilhadas, limitados e simétricos, de modo que o bundle tenha um grafo que um validador possa percorrer. Conceitos sem nenhum link continuam órfãos, e a CLI os reporta em vez de escondê-los.

**A proveniência acompanha.** O bloco `loom:` carrega a camada de proveniência da linha, de modo que uma ida e volta não possa transformar uma inferência em um fato de primeira mão.

| Flag                   | Efeito                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--include-high`       | exporta também as linhas `sensitivity: high`, os chunks brutos e os digests ligados a elas, e o texto bruto que contém um formato high (uma credencial, número de cartão, SSN ou PIN informado); o servidor audita cada item liberado; `memory.bulk_get` não entrega uma linha `high` após o fim do período de retenção, então ela é nomeada e deixada de fora. Desativado por padrão: um repositório espelho tem um público mais amplo do que as concessões do namespace |
| `--no-evidence`        | somente conceitos; ignora os chunks brutos (`evidence/`) e os digests de sessão (`digests/`) que o bundle inclui por padrão                                                                                                                                                                                                                                                                                                                                                    |
| `--no-links`           | ignora as arestas tipadas (os links de entidades compartilhadas continuam sendo gravados)                                                                                                                                                                                                                                                                                                                                                                                      |
| `--include-tombstoned` | exporta também as linhas excluídas de forma lógica, como `status: deprecated`; uma que a listagem não traz por completo (um valor além da prévia, ou uma linha `high` com `--include-high`) é nomeada e deixada de fora                                                                                                                                                                                                                                                         |
| `--limit N`            | no máximo as N linhas mais recentes (execuções de teste rápido)                                                                                                                                                                                                                                                                                                                                                                                                                |
| `--title`, `--dry-run` | o título do índice raiz; planeja sem gravar                                                                                                                                                                                                                                                                                                                                                                                                                                    |

<a id="lint"></a>

## Verificação (Lint)

`loom okf-lint DIR` lê o diretório do bundle e nada mais: sem servidor, sem ClickHouse, sem rede. Cada achado traz um id, um caminho, uma mensagem e uma severidade (`server/memory/domain/okf_lint.py`).

| id                              | Severidade | Significado                                                                                                 |
| ------------------------------- | ---------- | ----------------------------------------------------------------------------------------------------------- |
| `frontmatter`                   | crítico    | um conceito sem frontmatter, com YAML que não pode ser interpretado, com `type` vazio ou com um valor de `tags` que não é uma lista |
| `required_keys`                 | crítico    | falta `title`, `description`, `loom.memory_key` ou `loom.memory_type`                                       |
| `relates_to`                    | crítico    | uma aresta está malformada ou o destino `.md` dela não existe no bundle                                     |
| `index_missing` / `okf_version` | crítico    | um diretório com conceitos não tem `index.md`; o índice raiz não declara `okf_version`                      |
| `staging`                       | crítico    | existe um diretório `staging/`; o staging nunca responde                                                    |
| `type_directory_mismatch`       | aviso      | `loom.memory_type` discorda da pasta em que o arquivo está                                                  |
| `pointer_unmounted`             | aviso      | um caminho de `relates_to` é um ponteiro `loom://org/namespace/key` para um conceito fora do bundle        |
| `unknown_relation`              | aviso      | um tipo de `relates_to` fora dos verbos canônicos do Loom                                                   |
| `edited_since_export`           | aviso      | o corpo não gera mais o hash de `loom.content_sha256` (um conceito ou chunk bruto editado à mão)            |
| `derived_needs_confidence`      | aviso      | `source_class: derived` com confiança 1.0: uma inferência escrita como fato                                 |
| `high_sensitivity_present`      | aviso      | uma linha de sensibilidade alta (`loom.sensitivity: high` ou a tag `sensitivity:high`) foi exportada        |
| `index_listing`                 | aviso      | um conceito que o índice do seu diretório ou o índice raiz não lista                                        |
| `drift`                         | aviso      | o `loom.corpus_digest` do índice raiz não corresponde mais aos arquivos de conceito                         |
| `orphans`                       | informativo | conceitos que não apontam para nada e para os quais nada aponta (uma propriedade dos dados, não um defeito) |

Os códigos de saída seguem o que uma etapa de compilação precisa: **2** em qualquer achado crítico, **1** apenas com avisos, **0** quando está limpo. `--strict` faz os avisos também saírem com 2 (órfãos continuam apenas informativos); `--json` imprime o relatório legível por máquina.

**O digest do corpus.** Quando o `index.md` raiz traz `loom.corpus_digest`, o lint o recalcula: pega apenas os arquivos de conceito, ordena-os pelo caminho relativo ao bundle, concatena o digest hexadecimal SHA-256 de cada arquivo sem separador e aplica SHA-256 a essa string. Uma divergência gera o aviso `drift`, informando quantos arquivos de conceito a árvore contém em comparação com o que o índice diz.

<a id="import"></a>

## Importação

`loom import-okf DIR --namespace NS` executa o lint primeiro e recusa um bundle com
achados críticos, a menos que se use `--force-import` (`--no-lint` pula essa verificação). Em seguida, grava cada conceito
por meio de `memory.set`, com a identidade vinda do token ou das flags `--agent`, `--user-id` e
`--session-id`, sujeito à detecção de conflitos, à blacklist e ao log de auditoria.
Depois, recria as arestas `relates_to` com `memory.link` (`--no-links` pula essa etapa).

Antes de gravar, a importação lê as linhas ativas sob a sua identidade e ignora todo conceito
cuja linha já contém o que a gravação enviaria: o valor (`content_sha256`), tipo,
tags, confiança, `valid_at`, `valid_to`, `source_trace` e `source_class` (a classe
declarada, ou então `imported`), além das entidades quando o bundle as traz. Ela ignora as arestas
que já estão ativas. Conceitos que caem na mesma linha (uma chave exportada para vários
escritores) são gravados uma só vez: vence o atualizado mais recentemente. Reenviar uma linha idêntica
contaria como um reforço, então importar o mesmo bundle duas vezes não grava nenhum conceito nem aresta
na segunda vez, exceto um conceito com `sources:`, que é gravado a cada execução porque
as leituras feitas pela importação não retornam a sua origem, e um conceito que o destino
mescla em outra chave, que é reenviado a cada execução e reforça essa linha (contado
como mesclado). A última linha conta as linhas do destino gravadas, inalteradas, mescladas e com falha.

`source_trace` é `okf:<path inside the bundle>`, então o local do bundle no disco
não o altera: cada execução pode exportar para um diretório novo.

Os diretórios `evidence/` e `digests/` do bundle são gravados de volta por meio de `memory.import_evidence`
(`--no-evidence` os ignora) e reindexados com embeddings pelo servidor de importação a cada importação,
tenham mudado ou não: uma importação agendada usa `--no-evidence`.
Um chunk ou digest cujo texto é classificado como alto (uma credencial, número de cartão, SSN ou
PIN declarado), ou um chunk que contém parte de um desses cortado no limite entre chunks, é gravado sem
vetor e não pode ser pesquisado
([sensibilidade](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).

| Flag              | Efeito                                                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--verify`        | reatesta conceitos cujo frontmatter traz uma verificação `human:`, por meio de `memory.verify`, **como o chamador da importação**                                                       |
| `--keep-sessions` | grava cada conceito sob a sessão em seu `loom.session_id`, de modo que uma chave que viveu em várias sessões continue sendo várias linhas; `--session-id` é o fallback                  |
| `--dry-run`       | planeja as gravações contra as linhas ativas do destino (que ele lê) sem enviar nenhuma gravação                                                                                        |
| `--prune`         | exclui as linhas que uma importação anterior com este `--agent` e token gravou (um `source_trace` `okf:`) cuja chave não está mais no bundle, por meio de `memory.delete_version`; exige `--agent` |

Cada bundle importado em um namespace com `--prune` precisa de um `--agent` próprio: o prune
exclui as linhas desse agente com um trace `okf:`, gravadas pelo seu token, que o bundle
não tem. `--prune` recusa um bundle sem conceitos e um cujos arquivos de conceito não geram
o hash de `loom.corpus_digest` do índice raiz, de modo que um bundle editado à mão nunca provoca exclusões. Um
bundle cujo índice raiz não traz `loom.complete: true` é gravado, mas não sofre prune.
A exportação grava o marcador somente quando leu todas as linhas: todas as linhas que `memory.keys` lê em
uma única instrução depois que a listagem foi feita, e todas as linhas listadas retornaram de
`memory.bulk_get`. Nunca quando parou em `--limit`, com `--include-tombstoned` (que
mantém as linhas excluídas no bundle), ou a partir de uma versão mais antiga. Cada exclusão passa por
`memory.delete_version` com a versão que a listagem da importação viu com um trace `okf:`,
sob essa mesma identidade: uma linha reescrita desde então, ou bloqueada com `memory.lock`, é mantida,
e um destino antigo demais para conhecer a op não exclui nada. Exporte para um diretório vazio para um
prune: a exportação sobrescreve arquivos de conceito, mas nunca remove o arquivo de uma chave que saiu do
namespace, então um diretório `--out` reutilizado deixa de corresponder ao seu digest. [Copiar um
namespace para outro Loom](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md) executa a exportação e a importação em
um agendamento.

`--verify` nunca forja uma atestação. O servidor decide o ator a partir do token; o
verificador original do bundle é registrado nas tags da memória para fins de proveniência.

<a id="ci-gate"></a>

## Gate de CI

`integrations/okf-ci/okf-lint.yml` é um workflow do GitHub Actions para o repositório que
contém o bundle:

1. Copie-o para esse repositório como `.github/workflows/okf-lint.yml`.
2. Defina `LOOM_REF` como um SHA de commit do Loom com 40 caracteres. O job recusa qualquer outro valor
   (o arquivo vem com ele vazio): as regras são versionadas junto com o Loom, e um branch ou uma tag
   pode mudar sem que o check verde reflita isso.
3. Defina `OKF_ROOT` como o diretório do bundle, aquele que contém o `index.md` raiz (padrão
   `.`): o bundle de um namespace, ou uma matriz sobre os namespaces que o repositório contém.

O job faz o checkout do Loom em `LOOM_REF` ao lado do bundle e executa o lint com
`uv run --frozen`, de modo que os pacotes vêm do `uv.lock` daquele commit: um `LOOM_REF`
corresponde a um conjunto de regras e a um conjunto de dependências. Ele executa o lint com `--json`, imprime o
resumo e os primeiros quarenta achados (ordenados por severidade, id e caminho) no log do job,
envia `okf-lint.json` como artefato e falha com o código de saída 2 ou quando o lint não chegou a rodar
(qualquer outro código de saída). Avisos passam, e o relatório é mantido para o revisor.

Cada action é fixada em um SHA de commit completo, com a versão em um comentário. O workflow
pede apenas `contents: read`, e nenhum dos dois checkouts mantém o token na configuração do git
(`persist-credentials: false`). O lint em si não abre nenhuma conexão de rede e não precisa de
ClickHouse. `integrations/okf-ci/README.md` descreve cada verificação.

<a id="run-the-gate-locally"></a>

### Executar o gate localmente

O mesmo comando produz o mesmo resultado:

```bash
git clone https://github.com/ClickHouse/loom .okf-lint-loom
git -C .okf-lint-loom checkout <LOOM_REF>
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT>          # human report
uv run --project .okf-lint-loom --frozen --python 3.12 loom okf-lint <OKF_ROOT> --json   # the CI report
```

<a id="limits"></a>

## Limites

- **Um bundle é um snapshot.** As alterações feitas no Loom depois da exportação não estão nele até
  a próxima exportação; exporte novamente antes de editar um bundle desatualizado.
- **As exclusões só são transportadas com `--prune`,** e apenas para as linhas que a importação gravou. As arestas
  removidas do bundle não são excluídas.
- **As linhas de sensibilidade alta ficam em casa** a menos que você passe `--include-high`: uma linha é alta
  pelo campo `sensitivity` ou pela tag `sensitivity:high`. Uma linha cuja sensibilidade a exportação
  não conseguiu ler, ou leu como um nível que ela não conhece, também fica em casa. O mesmo vale para os chunks brutos
  e os resumos de sessão vinculados a essa linha, e para qualquer um cujo texto contenha um formato de sensibilidade alta (uma
  credencial, número de cartão, SSN ou PIN declarado; o texto bruto não tem coluna de sensibilidade, então o
  detector que `memory.set` executa decide, independentemente do que
  `LOOM_MEMORY_SENSITIVITY_AUTODETECT` diga). O lint avisa quando uma linha de sensibilidade alta foi transportada.

<a id="related"></a>

## Relacionados

- [Escrita de memória](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md) e
  [conflitos e recusa](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md): o que uma importação percorre.
- [Histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md).
- [Grafo de entidades](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md): as arestas tipadas que `relates_to` carrega.
