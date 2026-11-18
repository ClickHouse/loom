<!-- i18n: source=docs/benchmarks/adding-a-case.md blob=63bcbeab205c463f07cd6aa007c0f8d4eeabefab model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md), which is authoritative.

<a id="adding-a-benchmark-case-dataset-or-suite"></a>

# Adicionando um caso, conjunto de dados ou suíte de benchmark

Os conjuntos versionados são pequenos ([tamanhos dos conjuntos](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#set-sizes)), então um novo caso é a forma mais barata de melhorar a suíte. Leia primeiro o cabeçalho ou o `_comment` do arquivo: a maioria explica como o gold foi construído e o que uma nova linha precisa satisfazer.

<a id="add-a-case-to-an-existing-set"></a>

## Adicionar um caso a um conjunto existente

<a id="memory-qa-benchdatasetssynthetic_memory_qajson"></a>

### QA de memória (`bench/datasets/synthetic_memory_qa.json`)

Acrescente uma pergunta a uma conversa, ou uma conversa inteira:

```json
{
  "conversation_id": "06-billing",
  "conversation": [{ "role": "user", "content": "Our invoices go out on the 3rd of each month." }],
  "questions": [{ "question": "When do invoices go out?", "gold_keys": ["invoice", "3rd"] }]
}
```

Um acerto é qualquer `memory_key` ou trecho retornado que contenha uma string gold (sem diferenciar maiúsculas de minúsculas, com underscores tratados como espaços). Escolha palavras gold que uma memória correta deve conter e que uma memória errada dificilmente conterá. Mantenha o caso sintético: sem texto de terceiros e sem pessoas reais.

**Isso move a linha de base.** O limite de 0.80 do gate foi registrado com este arquivo. Execute o gate em uma implantação real antes e depois, e atualize `MIN_RECALL` no `Makefile` se o valor mudar. Testes: `tests/unit/test_bench_quality_gate.py`, `bench/test_quality.py`.

<a id="extraction-and-graph-regressions"></a>

### Regressões de extração e de grafo

- As linhas de `bench/fixtures/extraction_regressions.json` têm o formato `{id, messages, expected}`: frases que devem aparecer na chave ou no valor de alguma memória.
- As linhas de `graph_regressions.json` têm o formato
  `{id, messages, expected_triples: [{subject, relation, object}], as_of}`.
- Um campo `_note` nomeia a classe da falha; o harness o ignora.

`tests/unit/test_algorithm_gate_assets.py` verifica se toda linha tem mensagens e uma expectativa não vazia. Execute `make bench-algorithm-gate` com um LLM de extração real para ver o caso passar.

<a id="code-plane-qa-benchfixturescode_intel_qajsonl"></a>

### QA do plano de código (`bench/fixtures/code_intel_qa*.jsonl`)

Um objeto JSON por linha; linhas `//` são comentários:

```json
{"id": "co037", "type": "conceptual", "query": "where are part merges scheduled", "gold": {"paths": ["src/Storages/MergeTree/MergeTreeBackgroundExecutor"]}}
{"id": "sd011", "type": "symbol_def", "query": "IColumn::insert", "gold": {"qualified_name": "DB::IColumn::insert", "path_prefix": "src/Columns/"}}
{"id": "ab011", "type": "abstain_probe", "query": "kubernetes ingress annotation for sticky sessions", "gold": {}}
```

O gold de cada tipo está na docstring de `bench/code_intel_qa.py` (`callers` usa `expected_callers`, `hub` usa `path_prefix` e `symbols`). Os caminhos são prefixos sem a extensão. Os campos opcionais `lane`, `facets` e `namespace` roteiam e fatiam a linha.

- **Verifique o gold em um índice ativo** antes de ele passar a valer no gate: execute uma vez sem limites mínimos, leia as divergências e corrija o gold.
- **Um abstain probe deve tratar de código que não está no repositório.**
- **Mantenha o conjunto de tipos do arquivo principal.** `bench/test_code_intel_qa.py` (`test_fixture_loads_and_validates`) verifica que `code_intel_qa.jsonl` tem pelo menos 30 linhas e exatamente os tipos `symbol_def`, `callers`, `conceptual`, `abstain_probe`. Linhas de hub, histórico e conjunto de testes vão em seus próprios arquivos de lane.
- `make bench-code-intel-dry` ainda deve passar: cada lane atinge seu teto sob uma recuperação sintética perfeita.
- As lanes mineradas são regeneradas, não editadas à mão: `code_intel_mine_prs.py` e `code_intel_ka_slice.py` gravam o arquivo e as contagens do cabeçalho.

<a id="coding-agent-lessons-benchfixturescoding_agent_qajson"></a>

### Lições do agente de código (`bench/fixtures/coding_agent_qa.json`)

Acrescente a `lessons` (uma linha de memória identificada por `memory_key`) e a `questions` (`question`, `question_type` da taxonomia, `gold_keys` e, opcionalmente, `fix_reuse`). Toda gold key deve nomear uma chave de lição real: caso contrário, `bench/test_coding_agent_qa.py` (`test_load_corpus_rejects_dangling_gold_key`) falha. `--dry` ainda deve passar.

<a id="op-planning-tasks-benchfixturesop_planning_tasksjsonl"></a>

### Tarefas de planejamento de ops (`bench/fixtures/op_planning_tasks.jsonl`)

Uma tarefa contém `id`, `class` (`instruction_type`, `structure`), `prompt`, um plano `gold` (ferramenta, argumentos obrigatórios, argumentos literais) e `assertions` sobre o armazenamento. `tests/unit/test_op_planning_bench.py` (`test_every_gold_tool_and_required_arg_exists_on_the_surface`) falha quando uma ferramenta ou um argumento gold deixa de existir na superfície do MCP. Uma tarefa cuja op foi aposentada vai para `op_planning_tasks_retired.jsonl` em vez de ser excluída.

<a id="skill-eval-tasks"></a>

### Tarefas de avaliação de skill

Um briefing em `bench/skill_eval/tasks/` tem front matter (`task`, `namespace`, `agent`, `traps`), nomeia um namespace `skill-eval-…` no corpo e termina dizendo que o namespace será auditado. O mesmo corpo é um caso de `claude plugin eval` em `integrations/claude-code-plugin/evals/loom-memory/<task>/prompt.md`, idêntico byte a byte após o front matter. `tests/unit/test_skill_eval.py` verifica as três regras e fixa a lista de tarefas, então uma nova tarefa também atualiza esse teste. Veja [avaliação de skill](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md).

<a id="wire-in-a-new-dataset"></a>

## Integrar um novo dataset

1. **Não faça commit dele.** Coloque-o em `bench/datasets/` ou receba um caminho por flag. Se ele for baixado, adicione um caminho `--fetch` ou imprima o comando de download quando o arquivo estiver ausente, como faz o `longmemeval.py`.
2. **Registre a procedência na docstring do harness:** origem, nomes dos arquivos, número de itens e a licença declarada pelo dataset. Adicione uma linha em [conjuntos de dados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets).
3. **Mantenha as categorias do próprio dataset** como uma coluna e indique o subconjunto comparável, se as tabelas publicadas usarem um.
4. **Nomes de benchmarks públicos são aceitos em `bench/`**, mas nunca como motivo de uma escolha em texto sob `server/` (`tests/unit/test_public_copy.py` `BENCHMARK_NAMES`).
5. Crie namespaces temporários com um prefixo de `LOOM_NAMESPACE_BENCH_PREFIXES` (`server/settings/catalog.py`; novos harnesses usam `bench-`), para que `loom namespaces-gc` e o GC de namespaces encontrem o que uma execução abortada deixar para trás.

<a id="write-a-new-harness"></a>

## Escrever um novo harness

Todo harness segue este formato:

| Regra                                  | Como                                                                                                                                                                                                                                                                            |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Script independente                    | argparse, `--base-url`, `--token` (padrão `LOOM_API_KEY`), `--out`; executável como `PYTHONPATH=$PWD python bench/<name>.py` e como módulo                                                                                                                                      |
| Política HTTP compartilhada            | Envie as requisições por `bench/_http.py` (`post_json` / `apost_json`): token Bearer, esperas em 429, novas tentativas limitadas em 5xx, erros que carregam o corpo da resposta                                                                                                 |
| A docstring é o manual                 | O que mede, o que não mede, conjunto de dados, custo e um comando de execução                                                                                                                                                                                                  |
| Núcleo de pontuação puro               | Funções de pontuação acima do caminho em execução real, importáveis sem um servidor, com um teste unitário (`bench/test_<name>.py` ou `tests/unit/test_bench_<name>.py`)                                                                                                        |
| Um modo `--dry` ou de autoteste        | Exercita o avaliador com entrada sintética, sem servidor e sem gasto                                                                                                                                                                                                            |
| Leituras de avaliação                  | Primeiro, mescle `measurement_fields("memory.search")` / `("memory.ask")` de `bench/_gate.py` nos corpos das requisições; ofereça `--credit`                                                                                                                                    |
| Namespaces descartáveis e registrados  | `register_bench_namespace` + `remember_bench_namespace` antes da primeira escrita, o trabalho dentro de `erase_on_exit` e uma flag `--keep-namespaces` (`bench/bench_namespaces.py`)                                                                                             |
| Escopo fixo                            | Envie `namespace_scope: "this"` nas leituras                                                                                                                                                                                                                                    |
| Com julgamento? Use o gate compartilhado | `add_gate_arguments(parser)` para `--repeats`, `--gate*`, `--credit`, `--judge-calibration`; `gate_from_args` para o veredito; fixe os modelos de leitor e de juiz e avise sobre substituições. `tests/unit/test_bench_gate_help_renders.py` mostra por que um `%` solto no texto de ajuda quebra o `--help` |
| Registre o instrumento                 | Embedder, LLM, provedor e `build_commit` de `/v1/health` junto aos números (veja `run_metadata` em `quality.py`)                                                                                                                                                                |
| Separe falhas de erros de recuperação  | Conte e imprima os erros de requisição; não os pontue como erros de recuperação                                                                                                                                                                                                 |
| Comparações pareadas                   | Deltas por item com `paired_bootstrap` usando uma chave de cluster; libere com base no limite inferior                                                                                                                                                                          |

Depois:

- adicione um item à seção adequada de [`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) e, se
  ele responder a uma pergunta comum, uma linha na tabela "When to run what";
- adicione um alvo no Makefile apenas para um gate que alguém execute rotineiramente, com seus limites mínimos como variáveis substituíveis;
- execute `make format` (o ruff formata `bench/`), `pytest bench -q` e o lint de texto:

```bash
.venv/bin/python -m pytest tests/unit/test_public_copy.py tests/unit/test_bench_namespaces.py -q
```

O lint de texto verifica `bench/`: sem nomes de produtos, pessoas, hosts internos ou namespaces, citações de artigos ou referências a registros de decisão em código, comentários ou fixtures.

<a id="related"></a>

## Relacionados

- [Metodologia](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md): o que o gate e o bootstrap calculam.
- [Execução](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md): comandos e saídas.
- [Testes](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md): as suítes de testes unitários e de integração.
