<!-- i18n: source=docs/getting-started/evaluate.md blob=882e7d370aa195573ce2e4029995fa1f32beadc9 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md), which is authoritative.

<a id="evaluate-loom"></a>

# Avaliar o Loom

Avalie a decisão que você precisa tomar, não cada harness do repositório. Este guia
começa com um teste rápido do produto, sem custo, e depois passa para um teste de qualidade
específico para a sua carga de trabalho. A suíte pública de benchmarks é a etapa final,
para quando você precisa auditar ou reproduzir um resultado publicado.

<a id="choose-an-evaluation"></a>

## Escolha uma avaliação

| Decisão                                         | Dados                                                          | Provedores                                             | Saída típica                                                                          |
| ----------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| O Loom é instalado e conecta corretamente?      | Quatro fatos de teste e um documento                           | Os provedores falsos padrão são suficientes            | Um fato sobrevive a uma nova sessão; linhas de origem, histórico e auditoria aparecem no painel |
| A memória é útil para o nosso agente?           | 25–50 perguntas reais com chaves de memória esperadas          | Embedder real; LLM real para extração ou `memory.ask`  | recall@k, rank@1, abstenção, precisão das respostas, latência e custo                 |
| A inteligência de código é útil para o nosso repositório? | Símbolos representativos, conjuntos de chamadores e perguntas no formato de issues | Embedder real; repositório indexado                    | definições exatas, F1 de chamadores, recall de arquivos, abstenção, tokens e latência |
| Conseguimos reproduzir um resultado publicado?  | O dataset público indicado e o artefato de resultado commitado | Os modelos fixados ou substitutos documentados         | Uma execução comparável com a mesma fatia, o mesmo leitor, o mesmo juiz e o mesmo modo de busca |

Não use o embedder falso (o `.env` de um checkout) nem o LLM falso para avaliar qualidade: eles existem para que a stack suba sem credenciais e para que os testes de integração sejam determinísticos. O embedder local da stack lançada recupera de verdade, então uma primeira olhada na busca é significativa com ele; avalie respostas e extração com um LLM real.

<a id="before-you-run"></a>

## Antes de executar

Anote estas cinco coisas primeiro:

1. **Carga de trabalho.** Os usuários, repositórios e tipos de pergunta que a decisão abrange.
2. **Linha de base.** O que o mesmo usuário ou agente faz sem o Loom: contexto completo, grep, BM25,
   outro sistema de memória ou nenhuma recuperação.
3. **Limites de precisão.** O recall, a acurácia das respostas ou o sucesso nas tarefas mínimos que você
   aceitará. A qualidade vem antes do custo.
4. **Limites de economia.** Latência p95 máxima, tokens, custo de modelo, tempo de indexação e armazenamento.
5. **Instrumento.** Commit do Loom, implantação do ClickHouse, modelos dos provedores, parâmetros dos modelos,
   hardware, versão do dataset e semente aleatória.

Registrar os limites com antecedência evita que uma métrica ou um recorte conveniente se torne o resultado
depois da execução. Mantenha visíveis as falhas e as abstenções; relate os erros de requisição separadamente,
em vez de contá-los ou descartá-los silenciosamente como falhas de recuperação.

<a id="1-product-smoke-test"></a>

## 1. Teste de fumaça do produto

Leva cerca de 15 minutos e comprova a conexão entre os componentes, a persistência e a visibilidade, não a qualidade semântica.

1. [Suba a stack local](quickstart.md#1-boot-the-stack) e confirme que `/v1/health` retorna
   `status: ok`.
2. [Escreva e busque um fato](quickstart.md#4-first-memory-over-http).
3. [Conecte um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), diga a ele uma preferência duradoura, abra
   uma sessão nova e peça essa preferência.
4. Corrija o fato e depois confirme que o painel mostra a origem e o histórico de versões dele.
5. Execute `loom demo` com um documento e confirme que os chunks retornados citam esse documento
   ([teste de fumaça com documento](quickstart.md#optional-ask-a-question-about-a-document)).

Considere o teste aprovado somente quando a nova sessão lembrar o fato, a correção substituir a resposta atual, uma pergunta não relacionada resultar em abstenção e o painel mostrar quem escreveu e o histórico.
O resultado não diz nada sobre a recuperação em produção enquanto o health indicar `fake_embedding` ou
`fake_llm`.

<a id="2-evaluate-your-workload"></a>

## 2. Avalie sua carga de trabalho

Configure [provedores reais](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers), redefina ou refaça o embedding dos dados
se a largura do vetor mudou, e registre os modelos resolvidos a partir de `/v1/health`. Use um
namespace isolado e mantenha os mesmos dados indexados para cada braço pareado.

<a id="memory-and-rag"></a>

### Memória e RAG

Monte um conjunto de pelo menos 25–50 perguntas a partir de tarefas reais. Inclua:

- fatos diretos, paráfrases, perguntas de vários saltos e atualizações sensíveis ao tempo;
- perguntas cuja resposta deveria ter sido substituída;
- perguntas plausíveis, mas sem resposta, para medir respostas falsas e abstenção;
- distratores quase duplicados que diferem no valor que importa.

Para cada pergunta de memória com resposta, rotule o `memory_key` esperado antes de executar. Para
RAG de documentos, rotule o documento e o chunk esperados, consulte `knowledge.search` e avalie
esses identificadores com as mesmas métricas de recuperação. Meça a recuperação e a síntese
separadamente:

- **Recuperação:** recall@k, rank@1, MRR, taxa de falsos positivos e precisão/recall de abstenção.
- **Resposta:** correção sob um único leitor e juiz fixos, mais a correção das citações.
- **Economia:** latência p50/p95, tokens de entrada/saída, custo do modelo, custo de construção e
  linhas armazenadas.

Para um corpus de memória existente, salve um dataset JSON com este formato (o harness fornecido
tem como alvo `memory.search`; use um avaliador equivalente para `knowledge.search`):

```json
{
  "org": "acme",
  "namespace": "support-eval",
  "questions": [{ "query": "When can we deploy?", "gold_keys": ["deploy_window"] }]
}
```

Em seguida, execute a recuperação sem alterar o corpus:

```bash
export PYTHONPATH=$PWD LOOM_API_KEY=<token>
.venv/bin/python bench/retrieval_recall.py \
  --dataset /path/to/retrieval_qa.json --base-url http://localhost:7777 \
  --top-k 10 --out /tmp/loom-retrieval.csv
```

Defina `--min-recall` e `--min-mrr` somente a partir dos seus critérios pré-registrados. Use
`memory.ask` ou o seu leitor de produção nas mesmas perguntas para medir a acurácia das respostas; o recall
e a acurácia das respostas são medições diferentes.

<a id="code-intelligence"></a>

### Inteligência de código

Indexe um repositório parecido com os seus repositórios de produção. Não comece apenas com
o repositório no qual o Loom foi ajustado. Rotule:

- definições com o arquivo esperado e o símbolo qualificado;
- chamadores com o conjunto completo esperado de chamadores;
- perguntas conceituais e no formato de issues com arquivos aceitáveis no top 5 e no top 10;
- perguntas fora do repositório que devem resultar em abstenção;
- os testes ou dependentes esperados de uma consulta de impacto.

Execute o relatório de aceitação do agente de código em um repositório indexado:

```bash
export PYTHONPATH=$PWD
.venv/bin/python bench/acceptance/code_agent.py \
  --base-url http://localhost:7777 --token-file /path/to/token \
  --namespace <code-namespace>
```

O fixture embutido tem como alvo o ClickHouse e é útil para verificar uma implantação. Para
uma decisão de produto, substitua-o ou amplie-o com suas perguntas rotuladas e compare as mesmas
tarefas do agente usando apenas as ferramentas comuns do repositório. Informe primeiro o sucesso da tarefa, depois turnos,
tokens, custo e tempo total. A sobrecarga do schema de ferramentas depende do cliente, então registre se
ele adia as definições das ferramentas MCP.

<a id="keep-the-comparison-fair"></a>

### Mantenha a comparação justa

- Use o mesmo modelo, prompt, snapshot de dados e ordem das perguntas nos braços pareados.
- Mantenha o custo de extração/construção do índice separado do custo por pergunta e, em seguida, informe o
  número de perguntas necessárias para amortizar a construção.
- Desative o aprendizado a partir das leituras de avaliação (`auto_touch: false`, `reinforce: false`) para que uma
  consulta anterior não melhore uma posterior.
- Informe o tamanho da amostra, os resultados por categoria e a incerteza. Não promova uma pequena
  amostra de teste rápido a uma afirmação geral.
- Salve as saídas por pergunta, incluindo erros e falhas, e não apenas as pontuações agregadas.

<a id="3-audit-or-reproduce-published-results"></a>

## 3. Auditar ou reproduzir os resultados publicados

O README é um placar, não o registro de evidências. Para cada número publicado:

1. Abra a seção correspondente em [Resultados dos benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md).
2. Inspecione o artefato commitado em [`bench/results/`](https://github.com/ClickHouse/loom/blob/main/bench/results).
3. Confirme o recorte do dataset, o commit do Loom, os modelos, o leitor, o juiz, o modo de busca e a seed.
4. Execute o comando de reprodução exato da seção do resultado.
5. Interprete a diferença usando a [metodologia](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), incluindo
   a calibração do juiz, os intervalos de bootstrap e os vieses conhecidos.

Nem todos os conjuntos de dados de terceiros são redistribuídos por este repositório. A origem e a
licença de cada um estão listadas em [conjuntos de dados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets). As execuções públicas
completas podem levar horas e gerar cobranças dos provedores; comece pelo recorte de teste rápido documentado e depois execute
o protocolo completo antes de publicar uma comparação.

<a id="what-to-publish"></a>

## O que publicar

Publique um resultado no README somente quando ele for representativo, compreensível em uma linha e respaldado por um artefato commitado e por um comando de reprodução. Coloque a tabela completa de categorias, os modelos, os prompts, a incerteza, os custos, a análise de falhas e as limitações no
[registro de resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md). Coloque as regras estáveis de avaliação na
[metodologia](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md), e os comandos e a obtenção dos datasets em
[execução](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md).

Toda afirmação pública deve informar:

- a tarefa e o tamanho da amostra;
- a métrica e se ela mede recuperação, qualidade da resposta ou sucesso de ponta a ponta;
- o baseline e o delta pareado, quando existir;
- os modelos e o juiz, ou que nenhum juiz LLM foi usado;
- a incerteza ou a granularidade da mudança de uma única pergunta;
- o custo de construção e o custo de consulta, separadamente;
- o artefato commitado e o comando exato de reprodução;
- os achados negativos relevantes e onde o resultado não se generaliza.

Não publique todos os números disponíveis no README. Um placar longo esconde a decisão de produto, incentiva comparações inválidas entre benchmarks e dificulta a identificação de afirmações desatualizadas. O README deve levar os leitores até as evidências; a documentação deve conter as evidências.

<a id="next"></a>

## Próximos passos

- [Cobertura dos benchmarks](../benchmarks/coverage.md): o que tem medição pública, o que
  tem apenas um harness e o que ainda não tem evidência pública.
- [Visão geral dos benchmarks](../benchmarks/overview.md): escolha um harness pelo sinal que ele mede.
- [Executando benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md): provedores, tokens, comandos e datasets.
- [Metodologia dos benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md): regras de comparação e limitações.
- [Resultados dos benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md): medições e artefatos commitados.
