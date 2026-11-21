<!-- i18n: source=docs/benchmarks/overview.md blob=f3e3a30fcb2c5a330a968c35eb380c8c210ad12f model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md), which is authoritative.

<a id="benchmarks-overview"></a>

# Visão geral dos benchmarks

Fatores de ranqueamento, limites de similaridade, limiares de cache e prompts de extração
alteram o comportamento de maneiras que um teste unitário não consegue avaliar. Cada um deles é decidido por um harness em `bench/`
que roda via HTTP contra um servidor ativo, com provedores reais. Esta seção aborda a
[metodologia](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), [como executar](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md) a suíte e de onde vêm os dados, a [matriz de cobertura](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md), os [resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) que o repositório reproduz, como
[adicionar um caso](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/adding-a-case.md) e a [avaliação de skills](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md).

[`bench/README.md`](https://github.com/ClickHouse/loom/blob/main/bench/README.md) lista todos os harnesses. A docstring do módulo de cada script
é o seu manual: protocolo, flags e um comando de execução.

<a id="what-the-suite-measures"></a>

## O que a suíte mede

Os harnesses são scripts independentes: argparse mais um cliente HTTP, sem fixtures compartilhadas e sem
test runner.

| Área                              | Pergunta                                                                                              | Principais harnesses                                                                                                             |
| --------------------------------- | ----------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Recall de recuperação             | A memória certa ficou entre as K primeiras?                                                           | `quality.py`, `retrieval_recall.py`, `k_curve.py`, `plane_ablation.py`, `hard_distractors.py`                                    |
| Precisão das respostas            | A resposta construída a partir da memória recuperada está correta, com um leitor e um juiz fixos?     | `longmemeval.py`, `locomo_qa.py`, `memoryagentbench.py`, `membench.py`, `beam.py`, `musique.py`, `msc_dmr.py`, …                 |
| Qualidade do lado da escrita      | A extração escreveu o que os turnos dizem, e somente isso?                                            | `extraction_eval.py`, `graph_eval.py`, `memory_integrity.py`, `supersession_audit.py`                                            |
| Tempo e revisão                   | Um fato substituído fica oculto depois que seu intervalo se encerra e visível dentro dele?            | `fama_forgetting.py`, `mutation_sequence.py`, `fact_consolidation.py`, `pi_llm.py`                                               |
| Agentes e operações               | Um agente escolheu a op certa e deixou o armazenamento no formato correto?                            | `op_planning.py`, `skill_eval.py`, `op_trajectories.py`, `procedure_replay.py`                                                   |
| Custo                             | Quanto custa uma resposta ou uma sessão?                                                              | `ask_cost_rollup.py`, `session_ab.py`, `route_replay.py`, `ch_cost_by_op.py`                                                     |
| Segurança e privacidade           | A exclusão alcança todas as camadas; um atacante que usa apenas prompts consegue extrair ou envenenar a memória? | `erasure_compliance.py`, `memory_extraction_redteam.py`, `memory_poison.py`, `sensitivity_leakage.py`                            |
| Índice de código                  | O índice de código encontra definições, chamadores e respostas conceituais, e se abstém no restante?  | `code_intel_qa.py`, `code_plane_ablation.py`, `code_shunt_tokens.py`, `code_ask_cache_threshold/`                                |
| Parsing de documentos             | O extrator lê o texto, os títulos, as tabelas e as páginas de um documento, sem perder nada?          | `docparse/` (offline)                                                                                                            |
| Tracker                           | `tracker.similar` encontra a duplicata mais antiga contra a qual uma issue foi fechada?               | `tracker_similar_bench.py`                                                                                                       |
| Latência e saúde do índice        | Para onde vai o tempo da busca; um índice vetorial retornaria o mesmo que a força bruta?              | `search_latency.py`, `vector_tenant_recall.py`, `ann_final_staleness.py`, `vector_index_vs_exact.py`, `scripts/ops/benchmark.py` |
| Carga de escrita                  | Qual latência e quantas partes do ClickHouse uma taxa de escrita sustentada custa?                    | `write_rate.py`                                                                                                                  |
| Escala                            | Qual limite a memória atinge primeiro à medida que namespaces, orgs e chaves crescem?                 | `scale_load.py`                                                                                                                  |
| Aceitação por consumidor          | Um tipo de implantação obtém respostas precisas a um custo aceitável?                                 | `acceptance/{code_agent,memory_app,plugin,harness}.py`                                                                           |

[`bench/memoryarena/`](https://github.com/ClickHouse/loom/blob/main/bench/memoryarena/README.md) (um ambiente de treino para agentes que pontua sessões
posteriores de tarefas dependentes) e [`bench/memorydata/`](https://github.com/ClickHouse/loom/blob/main/bench/memorydata/README.md)
(um ambiente de testes que executa o Loom ao lado de outros métodos) são harnesses externos com o Loom
conectado. Os READMEs deles são seus guias de execução.

Se você está avaliando o Loom em vez de alterar seu código de ranking, comece por
[Avaliar o Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md). Ele transforma o inventário de harnesses em um
caminho de decisão curto para a integração ao produto, sua própria carga de trabalho de memória, inteligência de código e
reprodução dos resultados publicados.

<a id="entry-points"></a>

## Pontos de entrada

| Ponto de entrada             | O que executa                                                                       | Requer                                                                    |
| ---------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `make bench-quality-gate`    | `quality.py` no conjunto sintético versionado; sai com código 1 abaixo de recall 0.80 | um servidor com um embedder real e um LLM de extração                     |
| `make bench-algorithm-gate`  | `extraction_eval.py` e `graph_eval.py` nas fixtures de regressão                    | um servidor com um LLM de extração real                                   |
| `make bench-quick`           | `longmemeval.py` em 30 itens embaralhados com o leitor e o juiz fixados             | o arquivo LongMemEval baixado, `LOOM_API_KEY`, `OPENAI_API_KEY`           |
| `make bench-matrix`          | `matrix.py`: modo de busca × rerank × largura do pool no LongMemEval                | igual a `bench-quick`                                                     |
| `make bench-code-intel-dry`  | `code_intel_qa.py --dry`: o avaliador em uma recuperação sintética perfeita         | nada (offline)                                                            |
| `make bench-code-intel`      | `code_intel_qa.py` com os pisos de rollout                                          | um servidor com um repositório ClickHouse indexado, embedder real         |
| `make bench-code-legs`       | `code_intel_qa.py --compare-extra` para três ablações de leg, cada uma pareada      | igual a `bench-code-intel`                                                |
| `make bench-clickgap-replay` | `clickgap_code_replay.py`: a sequência de chamadas de um bot de revisão de PR reproduzida | um JSONL de replay dos PRs desse consumidor                         |
| `make bench-clickgap-golden` | `clickgap_golden_replay.py` nas fixtures golden desse consumidor                    | `GOLDEN_DIR`, fixtures privadas que não estão neste repositório           |
| `make bench`                 | `scripts/ops/benchmark.py`: carga concorrente em uma op, p50/p95/p99 e RPS          | um servidor                                                               |
| `make test-unit`             | também executa `pytest bench -q`: os núcleos de pontuação e os leitores de dataset  | nada (offline)                                                            |

Todos os outros harnesses são executados como `python bench/<name>.py` ([execução](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).

<a id="how-a-bench-differs-from-a-production-caller"></a>

## Como um bench difere de um chamador em produção

Um bench chama as mesmas operações via HTTP, com três diferenças:

1. **Leituras de avaliação.** Uma leitura que credita reforça as linhas sobre as quais a próxima passada calcula o ranking, então um bench alteraria seus próprios resultados. Os harnesses enviam `auto_touch: false` em `memory.search` e `reinforce: false` em `memory.ask`, a menos que a execução receba `--credit`.
2. **Namespaces de bench descartáveis.** Cada namespace é registrado com `traffic_class` e `kind` `bench`: o bucket de taxa de bench (metade do limite base por padrão), a prioridade de bench no ClickHouse, uma coluna separada no registro de custos e a exclusão quando a execução termina ([namespaces de bench](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#bench-namespaces)).
3. **Instrumento fixo.** Os modelos de leitor e de juiz são fixos por harness e não seguem `LOOM_LLM_MODEL`. O modelo de extração, o embedder e as configurações do servidor são a variável. `--answerer loom-ask` (LongMemEval) ou `--reader ask` (LoCoMo) avalia o próprio `memory.ask`.

Os padrões do servidor são os que esses benches escolheram. Alterar o padrão de `LOOM_LLM_MODEL` ou `LOOM_EMBEDDING_MODEL` começa com `make bench-quick`, e um novo embedder exige um [novo baseline](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#re-baselining-after-an-embedder-change).

<a id="coverage-and-what-it-leaves-out"></a>

## Cobertura e o que ela deixa de fora

- **Nenhum sinal de qualidade no CI.** O CI executa provedores falsos, então testa scorers, não a recuperação
  ([por quê](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md#fake-providers-make-scores-meaningless)). Uma regressão de ranking
  pode ser mesclada sem ser notada, a menos que alguém execute os gates em um deployment real.
- **Poucas medições estão commitadas.** [Resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) lista cada medição
  commitada com seu artefato em `bench/results/`, o instrumento sob o qual ela foi executada e um
  comando de reprodução. Um número para os seus dados e provedores precisa ser produzido no seu
  próprio deployment.
- **A maioria dos harnesses precisa de dados que o repositório não contém.** Corpora de terceiros
  são downloads ([conjuntos de dados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)); replays, conjuntos de relied-on, gold de tracker e
  latency-from-trace leem os dados privados de um deployment.
- **As fixtures de código são específicas do repositório.** As lanes do ClickHouse precisam de um índice
  de ClickHouse/ClickHouse, e a lane do Langfuse, de um índice desse repositório. Seus números dizem
  pouco sobre outra base de código.
- **Nenhum placar de sistemas nomeados.** Os harnesses em datasets públicos reportam os números do Loom e
  as colunas necessárias para colocá-los ao lado de tabelas publicadas. O repositório compara o Loom
  apenas com seus próprios builds e arms.
- **Nenhum registro de hardware nem modelo de capacidade.** Nenhum harness registra o host.
  `scripts/ops/benchmark.py` mede uma op em uma concorrência; `search_latency.py`
  divide um `memory.search` em etapas por tamanho de namespace. Nenhum dos dois modela uma mistura real de agentes
  nem a contenção multi-tenant.
- **A avaliação de skills avalia formatos, não recall** ([avaliação de skills](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/skill-eval.md#what-this-does-not-measure)).

<a id="what-a-pass-does-not-prove"></a>

### O que uma aprovação não prova

- **Passar em um gate é um piso.** O build supera um limite registrado em um conjunto pequeno com
  um único embedder. Isso não significa que o recall seja bom nos seus dados.
- **Recall não é qualidade de resposta.** Uma memória pode ficar em primeiro lugar no ranking e ainda ser mal interpretada ou
  estar desatualizada. Verifique o eixo julgado e a etapa da falha.
- **Acurácia de resposta não é sucesso do agente.** A maioria dos harnesses pontua uma pergunta por vez.
  Apenas `code_plane_ablation.py` e os gyms de agentes medem o que um agente fez com a
  memória.
- **Os benchmarks de segurança medem ataques nomeados.** `memory_poison.py` e
  `memory_extraction_redteam.py` reportam sobre seus próprios formatos de ataque. `erasure_compliance.py`
  varre todas as colunas do tipo String de todas as tabelas `loom.*` em busca do seu canary; os backends
  externos são tão completos quanto a sua própria exclusão.

<a id="related"></a>

## Relacionados

- [Recuperação e ranqueamento](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md): o que os benchmarks de recuperação exercitam.
- [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md): o que os benchmarks de código exercitam.
