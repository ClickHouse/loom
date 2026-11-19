<!-- i18n: source=docs/features/entity-graph.md blob=f2344ba078b13ea0d97951af2212b5da3b9af8bb model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md), which is authoritative.

<a id="entity-graph"></a>

# Grafo de entidades

O Loom mantém um grafo de entidades ao lado das linhas de memória, no mesmo banco de dados ClickHouse. As entidades são os nós. Duas entidades citadas na mesma memória compartilham uma aresta de coocorrência; a extração adiciona triplas tipadas `(subject, relation, object)` como um segundo conjunto de arestas. O grafo é uma projeção da memória: cada aresta pode ser rastreada até as linhas que a produziram. Use-o para perguntas que a busca vetorial não consegue responder, como "o que está conectado ao serviço de cobrança".

As comunidades de entidades vêm desativadas por padrão (`LOOM_ENTITY_COMMUNITIES_ENABLED`).

<a id="what-the-graph-holds"></a>

## O que o grafo armazena

| Tabela                                                                   | Armazena                                                                                                                                                      |
| ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.entity_edges` (+ `entity_edge_memories`, `entity_edge_rows`)       | Uma linha por observação de coocorrência (`co_occur = 1`); os leitores somam por par. Chave de origem por par, e a linha e o valor que o nomearam.            |
| `loom.entity_triples` (+ `entity_triple_memories`, `entity_triple_rows`) | Triplas tipadas com `confidence` e o modelo extrator como `adapter_name`. Chave de origem por tripla, e a linha e o valor que a declarou.                     |
| `loom.entity_degree`                                                     | O grau total de coocorrência de cada entidade (`SummingMergeTree`); as regras de hub o leem.                                                                  |
| `loom.entity_canonical_map`                                              | Texto da menção → entidade canônica.                                                                                                                          |
| `loom.entity_communities`                                                | ID da comunidade por entidade, por nível.                                                                                                                     |
| `loom.entity_vectors`, `loom.entity_summaries`                           | Embeddings lidos pelas etapas de entidade da busca.                                                                                                           |
| `loom.derived_facts`                                                     | Fatos numéricos com unidade e data, para respostas calculadas na busca.                                                                                       |
| arestas de `memory.link`                                                 | Arestas que um agente ou uma pessoa afirma (`supports`, `depends_on`, `caused`, …); lidas pela busca em modo grafo.                                           |

Arestas e triplas carregam `valid_from` / `valid_to` (a partir de `valid_at` / `valid_to` da memória de origem) e um
carimbo `invalid_at` que as aposenta. Uma leitura com `as_of` soma apenas as observações válidas naquele instante.

O grafo fica no ClickHouse para que as arestas sejam gravadas na mesma requisição que a sua linha, apagadas pela mesma saga
e unidas à memória sem uma ida à rede. O custo: uma travessia é uma série de consultas agregadas limitadas, então toda
travessia tem um limite.

<a id="how-the-graph-is-built"></a>

## Como o grafo é construído

A linha de memória é gravada primeiro; as linhas do grafo são projeções dela. Uma projeção que falha é registrada em
`loom.memory_projection_failures` e repetida, apenas enquanto a linha ainda tiver o valor que falhou na projeção. Ela
nunca faz a escrita falhar. Uma entrada em lote com sensibilidade `high` não projeta nada; um `memory.set` com
sensibilidade `high` projeta apenas as arestas das entidades que o seu autor declarou, nunca o seu texto.

1. **Entidades.** O autor da escrita passa `entities` em `memory.set`, ou `memory.set_from_messages` as extrai. O
   worker `entity_extractor` (`LOOM_ENTITY_EXTRACT_ENABLED`, ativado) marca as linhas gravadas sem elas, fora do caminho
   de escrita; ele precisa de um provedor de LLM real. O texto da entidade é canonicalizado da mesma forma na escrita e
   na leitura (artigos e possessivos removidos, substantivo núcleo no singular, primeira pessoa mapeada para `user`),
   então "The Billing Service's" e "billing service" são um único nó. Um nome `commit:<sha>` ou `error:<name>` é um
   identificador, canonicalizado da mesma forma, mas nunca singularizado: `error:Timeouts`, ou o mesmo nome entre crases
   ou aspas, é o nó `error:timeouts`. Um espaço em branco ou marcação logo após os dois-pontos (`Error: the build failed`,
   ``error:`Timeouts` ``) faz dele texto comum. Pronomes, interjeições, verbos leves e tokens de dois caracteres são
   descartados, assim como texto com mais de 256 caracteres, tal como escrito ou depois de normalizado (NFKC transforma
   `ﬁ` em `fi`), na escrita e em um `entity_filter`; uma escrita pontual recusa um sujeito ou objeto de fato assim, em vez
   de descartá-lo.
2. **Arestas.** Cada par de entidades de uma memória se torna uma linha de observação, no máximo 100 pares por memória.
3. **Triplas.** As relações de um fato extraído se tornam triplas, no máximo 8 por memória. Os verbos são normalizados
   para `lower_snake`. `namespace.register_ontology` (admin) registra tipos de entidade, verbos e atributos preferidos
   com uma cardinalidade. Nomes fora do conjunto produzem `ontology_warnings`; nada é rejeitado.
4. **A substituição fecha triplas.** Quando uma linha substitui outra, as triplas abertas da linha antiga que a nova não
   atesta recebem `invalid_at`. Para uma relação `one` (empregador, localização), o valor mais novo continua vigente e o
   mais antigo é fechado quando o mais novo começa. Uma escrita que afirma o valor mais antigo de novo enquanto esse
   fechamento está em andamento o mantém aberto.
   - Os valores são ordenados pelo dia em que começam (um valor sem data começa quando chega) e depois pela chegada, em
     microssegundos, de modo que escritores concorrentes concordem sobre um único valor vigente. Dentro de uma mesma
     escrita, o valor declarado depois é o mais novo.
   - Dentro de uma mesma escrita, um valor sem data no pretérito (`lived_in`, `worked_at`) é mais antigo que um valor
     com data, em qualquer ordem: "user lived_in london" ao lado de Paris a partir de 1º de outubro deixa Paris vigente.
   - Um valor é julgado pela sua linha mais recente: "London from Oct 15, Paris from Oct 14, London from Oct 13" deixa
     London vigente.
   - Um valor declarado com seu próprio fim ("London until December") nunca é fechado antes do tempo. Ele continua vigente
     ao lado do valor mais novo até esse fim.
   - Um Undo (`memory.restore`) torna o valor restaurado vigente a partir da restauração, ou a partir do início posterior
     do próprio fato ("moving to Berlin on Dec 1"). O outro valor é fechado e sua linha continua ativa. Um valor datado em
     um dia posterior ainda prevalece, assim como um valor declarado depois do Undo. Desfazer de um lado para o outro não
     acrescenta peso.
   - Um fechamento que falhou é reexecutado como de quando ocorreu a escrita ou o Undo que o causou, de modo que um valor
     declarado desde então continua vigente.

   Como um Undo reafirma seus fatos e arestas, os pondera e é reexecutado está em
   [arquitetura de memória](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md#undo).

Uma escrita pontual (`memory.set`, `memory.update`, `memory.update_by_id`) grava as triplas que o chamador passa em
`relations`, no máximo 8, via HTTP e nas operações tipadas dos SDKs (não na listagem do MCP); ela não extrai nenhuma.
A extração automática roda no caminho em lote: `memory.set_from_messages`, ou um escritor em lote que fornece relações.
Com os provedores falsos padrão, apenas as entidades que você passa formam arestas.

<a id="operations"></a>

## Operações

| Operação                     | Papel  | Responde                                                                                                      |
| ---------------------------- | ------ | ------------------------------------------------------------------------------------------------------------- |
| `facts.list`                 | viewer | Triplas filtradas por `subject`, `object`, `relation` (AND) ou `entity` (qualquer uma das pontas).            |
| `facts.related`              | viewer | Entidades que coocorrem com uma entidade, das mais fortes às mais fracas, e de que lado fica a semente.       |
| `facts.traverse`             | viewer | A vizinhança a N saltos de uma semente.                                                                       |
| `facts.path`                 | viewer | A cadeia mais curta de arestas entre duas entidades dentro de `max_hops`.                                     |
| `facts.expire_by_adapter`    | admin  | Marca como removidas (tombstone) as triplas que um extrator escreveu. As memórias permanecem.                 |
| `memory.search` (modo grafo) | viewer | `search_mode="graph"`: resultados a um salto das entidades da consulta ([busca e ask](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)). |
| `memory.related`             | viewer | Memórias que compartilham entidades com uma determinada memória.                                              |
| `memory.timeline`            | viewer | As memórias de uma entidade em ordem cronológica (`entity=…`).                                                |
| `memory.backlinks`           | viewer | Memórias que compartilham as entidades de uma linha.                                                          |
| `namespace.taxonomy`         | viewer | As principais entidades de um namespace e, quando calculadas, as comunidades.                                 |

Listas de campos: [fatos](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/facts.md), [memória](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/memory.md). Fora do modo
grafo, `entity_filter` dá mais peso às linhas sobre as entidades indicadas, e as etapas de entidade da busca encontram
linhas sobre uma entidade que a consulta nomeia com outras palavras ([recuperação e ranqueamento](../concepts/retrieval-and-ranking.md)).

<a id="factslist"></a>

### `facts.list`

Sem filtros, retorna as principais triplas por `co_occur`; `sort_by="recency"` ordena por `last_seen`.

| Campo                   | Efeito                                                                                               |
| ----------------------- | ---------------------------------------------------------------------------------------------------- |
| `include_sources`       | Adiciona até cerca de dez chaves de memórias de origem por tripla.                                   |
| `include_source_status` | Lê cada linha de origem; marca uma tripla como `stale` quando está aberta, mas todas as linhas que a sustentam foram substituídas. |
| `provenance`            | `extracted` (as duas pontas no texto de origem), `inferred` ou `facet` (uma aresta de navegação criada pelo Loom). |
| `as_of`                 | Mantém as triplas válidas naquele instante.                                                          |

<a id="factstraverse"></a>

### `facts.traverse`

O percurso poda antes de expandir. Cada salto envia a fronteira como uma única consulta, soma as observações por par
dentro da janela de validade, descarta as entidades já visitadas e mantém os `top_k_per_hop` pares mais fortes de cada
pai. Uma fronteira no salto h tem no máximo K^h entidades. Cada entidade é reportada uma só vez, no primeiro salto que
a alcança, por meio do seu pai mais forte, de modo que o resultado se desenha como uma árvore.

Uma única consulta recursiva que expande tudo e poda depois materializaria a vizinhança inteira; em um namespace grande,
um percurso de três saltos pode exceder o limite de memória do ClickHouse. Podar a cada salto limita o custo ao tamanho
da resposta.

| Campo                   | Padrão                                   | Efeito                                                                                                         |
| ----------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `max_hops`              | 2 (1–4)                                  | Profundidade do percurso.                                                                                      |
| `top_k_per_hop`         | 20 (até 100)                             | Filhos mantidos por pai a cada salto.                                                                          |
| `min_co_occur`          | 1                                        | Mínimo de observações por par.                                                                                 |
| `max_nodes`             | 400 (até 5,000)                          | Limite rígido; `truncated=true` quando atingido.                                                               |
| `as_of`                 | não definido                             | Mantém as observações válidas naquele instante.                                                                |
| `labeled` / `relations` | false / não definido                     | Percorre triplas tipadas (todas, ou apenas estes verbos); os nós trazem `via_relation`, `via_direction`, `via_provenance`. |
| `hub_degree`            | 0 (desligado)                            | Um nó com grau total igual ou maior que este valor é um hub.                                                   |
| `hub_share`             | `LOOM_FACTS_TRAVERSE_HUB_SHARE` (0.01)   | Um nó é um hub quando a aresta que chegou a ele representa no máximo esta fração do seu grau.                  |
| `rank_by`               | `LOOM_FACTS_TRAVERSE_RANK_BY` (`cosine`) | Poda por salto: `co_occur`, `specificity` (`via_co_occur / degree`) ou `cosine` (`via_co_occur / sqrt(degree)`). |

Cada nó informa `hop`, `cumulative_weight`, `via_co_occur`, `path` (a cadeia a partir da semente), `valid_from` /
`valid_to`, `degree` e `community_id` quando existem comunidades. Os nós são ordenados por salto e depois por peso.

**Hubs.** Entidades genéricas ("user", "pr") coocorrem com tudo. Um hub é reportado (`hub: true`, listado em `hubs`),
mas não é expandido. A semente sempre é expandida, então parta de um hub para ver a vizinhança dele. `hub_share` é a
regra padrão porque não depende da escala de graus do namespace. `hub_degree` é específico de cada namespace; leia a
cauda da distribuição de graus antes de defini-lo.

<a id="factspath"></a>

### `facts.path`

Uma busca em largura bidirecional limitada sobre as mesmas arestas, encontrando-se no meio. `max_hops` (1–4, padrão 3)
limita o comprimento total. A resposta é `found`, `path` e `edges`, com o `co_occur` e a validade de cada par.
`truncated=true` significa que a resposta pode estar incompleta. `found=false` com `truncated=false` significa que não
há conexão dentro de `max_hops`.

<a id="example"></a>

## Exemplo

```python
from loom import Agent

async with Agent("dev", "local", "sre-bot", base_url="http://localhost:7777") as agent:
    await agent.memory.set(
        "billing/depends_on",
        "The billing service reads invoices from the ledger database.",
        entities=["billing service", "ledger database"],
    )
    walk = await agent.facts.traverse("billing service", max_hops=2, top_k_per_hop=10)
    for node in walk["nodes"]:
        print(node["hop"], " > ".join(node["path"]), node["via_co_occur"])

    route = await agent.facts.path("billing service", "ledger outage")
    print(route["found"], route["path"], route["truncated"])
```

Via HTTP, uma travessia do grafo como ele estava em um instante passado:

```bash
curl -s localhost:7777/v1/facts.traverse -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"org":"dev","namespace":"local","seed_entity":"billing service","max_hops":2,"as_of":"'"$AS_OF"'"}'
```

Via MCP, chame `loom_op` com `op="facts.traverse"`.

<a id="maintaining-the-graph"></a>

## Manutenção do grafo

| Tarefa                                    | Como                                                                                                           |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| Reverter as triplas de um extrator ruim   | `facts.expire_by_adapter(adapter_name=…, max_confidence=…)`. As memórias permanecem.                           |
| Exportar o grafo                          | `loom entity-export --org O --namespace N --format graphml` (ou `jsonld`; `--all` inclui arestas invalidadas). |
| Agrupar entidades em comunidades          | Defina `LOOM_ENTITY_COMMUNITIES_ENABLED=true` (abaixo).                                                        |
| Visualizar                                | A visão Graph do painel ([painel](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md)).   |

O worker `entity_communities` executa propagação de rótulos (label propagation) por namespace em `loom.entity_communities`, lida por
`namespace.taxonomy`, pela expansão em modo grafo e por `community_id`. Ele cobre os namespaces com consolidação ativada, além daqueles
percorridos nos últimos `LOOM_ENTITY_COMMUNITIES_WALKED_DAYS` (14) dias. Ele descarta primeiro as entidades de maior grau
(`LOOM_ENTITY_COMMUNITIES_HUB_DROP_FRACTION`) e dobra essa fração enquanto uma comunidade concentrar mais que
`LOOM_ENTITY_COMMUNITIES_GIANT_SHARE` das entidades, de modo que tokens genéricos não consigam fundir o grafo em uma única comunidade.

<a id="settings"></a>

## Configurações

| Configuração                                 | Seção                                                                                                                        |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_FACTS_TRAVERSE_HUB_SHARE`, `…_RANK_BY` | [Saúde do namespace, grafo de conhecimento, comunidades](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#namespace-health-knowledge-graph-communities) |
| `LOOM_ENTITY_COMMUNITIES_*`                  | Mesma seção.                                                                                                                 |
| `LOOM_ENTITY_EXTRACT_*`                      | [Consolidação](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)                                                                 |
| `LOOM_MEMORY_SEARCH_GRAPH_SEED`              | [Busca de memória](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#memorysearch)                                                               |

<a id="limits"></a>

## Limites

- **Coocorrência não é uma relação.** Duas entidades na mesma memória são ligadas, independentemente do que a frase diga. Use triplas tipadas (`labeled`, `relations`) ou `memory.link` quando a relação importar.
- **O grafo é tão bom quanto as entidades.** Linhas sem entidades não adicionam arestas até que o worker `entity_extractor` chegue a elas.
- **Os percursos são limitados.** Leia `truncated` antes de concluir que duas entidades não estão conectadas.
- **As arestas podem ficar defasadas em relação a uma linha substituída.** `facts.list(include_source_status=true)` mostra as triplas ainda abertas.
- **A validade é o tempo de validade.** `as_of` lê quando um fato era verdadeiro, não quando o Loom o aprendeu.

<a id="related"></a>

## Relacionados

- [Busca e perguntas](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md): modo de grafo.
- [Escrita de memórias](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md): como passar `entities`.
- [Extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md): de onde vêm as entidades e os triplos extraídos.
- [Arquitetura de memória](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md): os escritores de projeção.
- [Referência de workers](https://github.com/ClickHouse/loom/blob/main/docs/reference/workers.md).
