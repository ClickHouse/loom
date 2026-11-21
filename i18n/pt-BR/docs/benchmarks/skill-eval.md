<!-- i18n: source=docs/benchmarks/skill-eval.md blob=c855282f25bb802cf3b249aa01819ec8e21227b7 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md), which is authoritative.

<a id="skill-eval"></a>

# Avaliação da skill

A avaliação da skill mede a `loom-memory` pelo namespace que um agente deixa para trás. A skill define o que vai para cada lugar: uma afirmação é um único `memory.set` em texto simples sob uma chave legível, um documento vai para knowledge com uma linha curta de referência, uma tabela de itens com identidade vai para records, um lease é memória de trabalho, e uma revisão reescreve a mesma chave. A avaliação executa uma tarefa, audita o namespace com `namespace.audit` e trata qualquer achado crítico como falha.

<a id="the-pieces"></a>

## As peças

| Peça                         | Onde                                                 | O quê                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Briefs de tarefas            | `bench/skill_eval/tasks/*.md`                        | Três briefs que tentam o agente a cair nos antipadrões: uma investigação de alerta com um log longo, trechos de código, uma tabela, um lease e um modelo grande a manter (`sre_alert_investigation`); um lote de PRs com campos estruturados (`pr_review_batch`); uma nota revisada duas vezes (`research_note_revisions`). Cada um indica seu namespace e avisa que será auditado. |
| Runner                       | `bench/skill_eval.py`                                | Executa a tarefa (`--runner claude-code`, `codex`, `manual`, `replay`), depois `namespace.audit` (janela de uma hora, 5 amostras), depois os contadores de formato; grava o JSON de resultado.                                                                                                                                                                                                                                             |
| Agentes gravados             | `bench/skill_eval/replays/{bad,good}_agent.jsonl`    | Streams de `{op, body}`. O agente ruim codifica valores em dobro, usa chaves hash e faz polling de um lease armazenado como memória; o bom segue o guia de [memória do agente de investigação](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md).                                                                                                                                                      |
| Suíte de avaliação do plugin | `integrations/claude-code-plugin/evals/loom-memory/` | Os mesmos briefs como casos de `claude plugin eval`, avaliados a partir do rastro de chamadas de ferramentas contra um servidor Loom simulado (`evals/mocks/loom/`).                                                                                                                                                                                                                                                                         |
| Teste unitário               | `tests/unit/test_skill_eval.py`                      | Executa os dois replays pelo runner contra um Loom falso cuja auditoria usa os classificadores do próprio handler: o ruim falha com os códigos esperados, o bom passa. Também fixa a lista de tarefas e o conteúdo dos prompts de avaliação.                                                                                                                                                                                                |

O front matter de cada brief traz `task`, `namespace`, `agent` e `traps` (os
antipadrões que ele tenta provocar). O runner substitui `$ORG`, `$NAMESPACE` e `$AGENT` em um
stream de replay, de modo que uma única gravação rode em qualquer org.

<a id="the-score"></a>

## A pontuação

`pass` significa que a auditoria retornou **0 críticos** e **no máximo 1 aviso**
(`MAX_WARNINGS_TO_PASS`). O relatório lista cada achado com sua contagem, a proporção das linhas
verificadas, os bytes e a correção do lado de quem escreve, e depois contadores de formato lidos das próprias
linhas, de modo que a nota não depende apenas da auditoria:

| Contador                 | Significado                                                                                                     |
| ------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `rows`                   | Linhas de memória ativas                                                                                        |
| `readable_key_share`     | Chaves cujo último segmento não é uma sequência hexadecimal longa e sem nenhum segmento do tamanho de um sha (a regra `hash_keys` da auditoria) |
| `plain_text_value_share` | Valores que são uma string e não JSON serializado                                                               |
| `documents`              | Documentos de conhecimento (`knowledge.list`)                                                                   |
| `records`                | Registros tipados (`memory.record.query` por schema registrado)                                                 |
| `working_memory_blocks`  | Blocos de memória de trabalho (`working_memory.render`)                                                         |

As leituras de formato são executadas **depois** da auditoria, portanto nunca contam como leituras do agente. Um
contador que o token não consegue ler aparece em `shape.skipped`, e não é reportado como zero.

<a id="run-it"></a>

## Executar

<a id="replay-no-model-is-called"></a>

### Replay (nenhum modelo é chamado)

Qualquer Loom serve; uma stack local é suficiente.

```bash
export LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY=<writer token> LOOM_ORG=<org>
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/bad_agent.jsonl            # exits 1: FAIL
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner replay \
  --replay bench/skill_eval/replays/good_agent.jsonl --baseline   # exits 0: PASS
```

Use um namespace novo a cada execução (`--namespace skill-eval-sre-$(date +%s)`). A janela de auditoria
é de uma hora, então um namespace reutilizado leva as leituras da execução anterior para a avaliação.

Offline, a mesma verificação roda como teste unitário, sem servidor:

```bash
.venv/bin/python -m pytest tests/unit/test_skill_eval.py -q
```

<a id="a-live-agent-with-the-skill-loaded"></a>

### Um agente real com a skill carregada

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code --baseline
```

Isso consome tokens do modelo. Ele executa, a partir da raiz do repositório:

```bash
claude -p --plugin-dir integrations/claude-code-plugin \
  --mcp-config <generated file: loom at $LOOM_BASE_URL/mcp?toolset=all, Bearer $LOOM_API_KEY> \
  --strict-mcp-config --allowedTools "mcp__loom__*" "Skill" "Read" \
  --permission-mode bypassPermissions --output-format json --max-turns 60 \
  "<the task body>"
```

- `--plugin-dir` carrega as skills a partir do checkout, de modo que a execução mede o texto da skill
  na árvore de trabalho.
- `toolset=all` lista `memory_schema.register` e `memory.record.write`, que a listagem
  `core` padrão omite. `knowledge.upsert` não é listada nem com `all`
  (nem o roster core nem o atomic a listam); o agente a chama por meio de `loom_op`.
- A configuração MCP gerada contém o token; ela é criada com `0600` e apagada após a
  execução.
- `--max-turns` (padrão 60) limita a execução.

`--runner codex` executa `codex exec` com a mesma configuração de servidor; o plugin do Codex, quando
instalado, fornece as skills.

<a id="any-other-agent"></a>

### Qualquer outro agente

`--runner manual` imprime o briefing. Execute qualquer agente que preferir no namespace
indicado e depois avalie o que ele deixou:

```bash
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual
.venv/bin/python bench/skill_eval.py --task pr_review_batch --runner manual --audit-only
```

O mesmo `--audit-only` avalia um namespace escrito por uma execução de `claude plugin eval` com servidores
reais.

<a id="compare-with-a-baseline"></a>

### Comparar com uma baseline

Edite a skill, execute de novo e compare:

```bash
.venv/bin/python bench/skill_eval.py --task sre_alert_investigation --runner claude-code \
  --compare bench/out/skill_eval_sre_alert_investigation_baseline.json
```

A tabela de diferenças tem uma linha por código de achado (severidade e contagem, antes e depois) e
uma por contador de formato. Um agente real não é determinístico, então compare uma edição de skill ao longo de
várias execuções, e não com apenas uma.

<a id="output"></a>

### Saída

Toda execução grava `bench/out/skill_eval_<task>_<runner>_<ts>.json` (`--out-dir` muda o local).
`--baseline` também grava `bench/out/skill_eval_<task>_baseline.json`, a referência que
`--compare` lê. `bench/out/` está no gitignore.

<a id="the-plugin-eval-suite"></a>

## A suíte de avaliação do plugin

```bash
claude plugin eval integrations/claude-code-plugin --trust-plugin --no-publish \
  --case 'loom-memory/*' --runs 3 --threshold 1.0 --json evals-result.json
```

Isso avalia cada execução a partir do rastro de chamadas de ferramentas, offline: o servidor MCP do Loom é um mock com respostas fixas (`evals/mocks/loom/`), então nada é gravado. Os avaliadores usam apenas tipos de avaliador documentados:

- `tool_used` com `max: 0` em gravações de memória cuja entrada tenha uma chave de hash, um valor que seja uma string JSON de JSON, um valor acima de cerca de 2 KB, uma chave de lease ou de lock, ou uma chave de revisão versionada: os formatos que `namespace.audit` classifica como crítico ou aviso;
- `tool_used: Skill` correspondendo a `loom-memory`, o indicador de que o plugin foi acionado;
- mínimos positivos para a memória de trabalho e para as claims por item.

Não há avaliadores `llm`: uma leitura da prosa por rubrica não é um critério de aprovação. Os casos podem ser filtrados por glob de nome (`--case sre_alert_investigation`). `--ablation with-without`, o padrão quando o plugin é resolvido, informa com que frequência o mesmo prompt sem o plugin ativa os mesmos avaliadores.

O framework não tem avaliador de script, então a nota no nível do namespace exige um segundo comando. Com `--mocks off --allow-real-servers --allow-tools "mcp__plugin_loom_loom__*"` e um token de escrita em `LOOM_API_KEY`, o agente grava no namespace do caso, e `bench/skill_eval.py --task <case> --runner manual --audit-only` o avalia.

<a id="reading-a-failure"></a>

## Lendo uma falha

O remédio de cada achado é a correção do lado de quem escreve. A skill `loom-optimize` mapeia os mesmos
códigos para o retrabalho de linhas que já existem ([otimizar um namespace](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)).

| Achado                                     | A regra que não foi cumprida                                              |
| ------------------------------------------ | ------------------------------------------------------------------------- |
| `double_encoded_values`                    | Envie o valor uma só vez; nunca aplique `json.dumps` duas vezes a uma string. |
| `hash_keys`                                | As chaves são legíveis, no formato `<scope>/<kind>/<id>`, e não hashes.   |
| `coordination_as_memory`                   | Leases e locks pertencem à memória de trabalho.                           |
| `exact_key_store`                          | Mantenha o ponteiro na memória de trabalho em vez de fazer polling por chave. |
| `oversized_values`, `json_prefix_excerpts` | Um documento vai para o knowledge, com uma linha de ponteiro curta.       |

Uma execução que passa na auditoria com `documents: 0` na tarefa SRE manteve o modelo grande
onde a busca não consegue alcançá-lo. Leia a linha de formato (shape), não apenas o veredito.

<a id="adding-a-task"></a>

## Adicionando uma tarefa

Um novo brief precisa de front matter, de um namespace `skill-eval-…` citado no corpo e da frase final informando que o namespace será auditado; de um caso de eval de plugin correspondente, com corpo idêntico byte a byte; e de uma atualização na lista de tarefas que `tests/unit/test_skill_eval.py` fixa.
Veja [adicionando um caso](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md#skill-eval-tasks).

<a id="what-this-does-not-measure"></a>

## O que isto não mede

A qualidade do recall. Saber se as claims que um agente escreveu respondem à pergunta da semana que vem é trabalho de um benchmark de recall ([visão geral](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md)); aqui são avaliados os formatos que tornam o recall impossível.

<a id="related"></a>

## Relacionados

- [Memória de agente de investigação](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md): a receita que o
  bom replay segue.
- [Otimizar um namespace](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md): retrabalho das linhas sinalizadas pela auditoria.
- [Plugin do Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) e
  [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md).
- [`integrations/claude-code-plugin/evals/README.md`](https://github.com/ClickHouse/loom/blob/main/integrations/claude-code-plugin/evals/README.md).
