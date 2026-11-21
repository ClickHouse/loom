<!-- i18n: source=docs/guides/optimize-namespace.md blob=bcf139a5e94cdfb8e192ed26d7bf748075392110 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md), which is authoritative.

<a id="optimize-a-namespace-from-its-audit"></a>

# Otimizar um namespace a partir da sua auditoria

Um escritor que usa `memory.set` como um object store (blobs JSON sob chaves de hash, leases como memórias, documentos como claims) deixa todas as tabelas consistentes e a recuperação inútil. Este procedimento reorganiza esse namespace no layout descrito no
[guia do agente de investigação](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md). Cada movimentação deixa um redirecionamento, de modo que um escritor em execução que ainda leia as chaves antigas continue recebendo respostas.

A skill `loom-optimize` nos plugins do [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md) e do
[Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md) executa os mesmos passos.

<a id="the-two-operations"></a>

## As duas operações

**`namespace.audit`** (`POST /v1/namespace.audit`, MCP `loom_namespace_audit`, CLI
`loom audit-namespace`). Somente leitura.

| Entrada            | Padrão          |
| ------------------ | --------------- |
| `org`, `namespace` | obrigatório     |
| `window_seconds`   | 604800 (7 dias) |
| `sample_limit`     | 5               |

Ela retorna `summary` (`live_rows`, `live_bytes`, `excerpt_bytes`, `version_rows`,
`version_bytes`, `unembedded_rows`, `gets`, `searches`, `sets`, `get_search_ratio` e
`inspector_calls`: os gets e searches do painel e do conjunto de ferramentas do assistente, deixados
fora das contagens; uma escrita feita pelo painel é contada em `sets`),
`findings[]` (`code`, `severity`, `count`, `share`, `bytes`, `samples[]`, `detail`,
`remedy`), `skipped` (regras que não puderam ser executadas: verificações, leituras do resumo, a leitura
de evidências; as demais ainda retornam), `coverage`, `healthy` ("nenhum achado crítico") e as
contagens `criticals` / `warnings` / `infos`. As amostras trazem chaves, ids de linhas, colunas de identidade
e contagens, nunca um valor. Cada achado também traz os [campos de
evidência](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md#reading-a-namespaces-health); um plano lê
`kind` (uma heurística de uso não é prova de uso indevido), `samples_complete` e `evidence[]`,
a `version` e o `content_sha256` atuais de cada linha amostrada.

**`memory.move`** (`POST /v1/memory.move`, MCP `loom_memory_move`).

| Entrada                                                                      | Padrão                         |
| ---------------------------------------------------------------------------- | ------------------------------ |
| `org`, `namespace`, `agent`, `from_key`, `to_key`                            | obrigatório                    |
| `value`, `memory_type`, `tags`, `entities`, `valid_at`, `source_resource_id` | herdado da linha de origem     |
| `keep_redirect`                                                              | `true`                         |
| `if_match_sha256`                                                            | nenhum                         |
| `dry_run`                                                                    | `false`                        |

Ela retorna `{from_key, to_key, redirected, memory_id, version, content_sha256, advisories,
dry_run}`; um dry run adiciona `memory_type` e `value_preview`. Depois de um move:

- `memory.get(from_key)` ainda responde com o valor antigo mais `canonical_key`;
- `memory.get(from_key, follow=true)` serve a nova linha;
- `memory.search` e `memory.list` mostram apenas a nova linha.

<a id="1-audit"></a>

## 1. Auditoria

```bash
loom audit-namespace --org <org> --namespace <ns> --window-days 7 --samples 5
```

A CLI é executada no servidor (ela lê o ClickHouse diretamente). Ela imprime uma tabela (code, severity,
kind, count, share, primeira amostra), ou o modelo de saída com `--json`, e termina com código 1 quando o
namespace não está saudável. De qualquer outro lugar, chame `namespace.audit` por HTTP ou MCP.

Leia `summary` primeiro:

- um `get_search_ratio` na casa das centenas significa que o namespace está sendo usado como um armazenamento chave-valor
  (leituras de procedimentos, que são lidas de volta por chave de propósito, ficam de fora);
- `unembedded_rows` acima de zero significa linhas que a busca nunca consegue encontrar.

Depois leia os achados. Pare quando `healthy` for true e nada estiver em `warning` ou
`critical`: um achado `info` é uma observação para quem escreve, não um retrabalho. Um achado
`system_state` (um backlog de indexação, telemetria ausente) diz respeito à
implantação, nunca a quem escreve; enquanto `telemetry_unavailable` for um warning,
`exact_key_store` e `intentional_exact_key_reads` ficam em `skipped`, e não como limpos, e
`coordination_as_memory` executa apenas o ramo de segmento de chave (`samples_complete: false`).

O painel de auditoria de namespace do painel mostra a mesma auditoria. `context.pack` traz um
bloco `namespace_health` quando o namespace não está saudável (um achado crítico, ou dois
warnings entre `exact_key_store`, `double_encoded_values`, `unembedded_rows` e
`coordination_as_memory`), de modo que um agente o vê no início da sessão.

<a id="2-plan"></a>

## 2. Plano

Escreva uma seção por achado. Só os códigos que a auditoria retornou ganham uma seção.

| Achado                                                                  | O que muda                                                               | Como                                                                                                                                                   |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `double_encoded_values`, `json_prefix_excerpts`, `hash_keys`            | a linha passa para uma chave legível, com um valor que começa pela afirmação | decodifique uma vez; frase da afirmação, linha da fonte, o resto em linhas curtas; `memory.move` com `value`, `tags`, `entities`, `valid_at`, `if_match_sha256` |
| `oversized_values`, `episodic_blobs`                                    | o corpo vira um documento ou um blob; uma linha de um parágrafo mantém a chave | `knowledge.upsert` (Markdown, `source_uri=loom://<org>/<ns>/<key>`) ou `context.offload`, depois `memory.move` da chave antiga com o parágrafo como `value` |
| `unembedded_rows`                                                       | o valor é dividido ou encurtado para que seja gerado o embedding         | como em `oversized_values`; `namespace.validate` as lista entre suas linhas `unembedded`                                                               |
| `coordination_as_memory`                                                | o bloco vai para a memória de trabalho; a chave antiga continua respondendo | `working_memory.set`, depois `memory.move` com `keep_redirect=true`                                                                                    |
| `hand_rolled_index_rows`                                                | as linhas continuam legíveis e ganham `tags`; o escritor deixa de produzi-las | apenas tags (`memory.update_by_id`); sem move                                                                                                          |
| `exact_key_store`, `version_churn`                                      | o escritor muda                                                          | liste as chaves para o responsável                                                                                                                     |
| `telemetry_unavailable`, `index_backlog`, `intentional_exact_key_reads` | nada: é o estado do deployment ou um fluxo de trabalho                   | sem retrabalho; para `telemetry_unavailable`, execute de novo quando o ledger cobrir a janela; para um `index_backlog` parado, verifique o worker de embedding |

O mentor (`mentor.brief`) nomeia mais três formatos que são mudanças de dados. Eles são
retrabalhados sob as mesmas regras, com a `evidence` da dica como contagem:

| Dica do mentor           | O que muda                                                                                      | Como                                                                                                |
| ------------------------ | ----------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `documents_as_memories`  | como em `oversized_values`                                                                      | `knowledge.upsert`, depois `memory.move` com o parágrafo como `value`                               |
| `checkpoints_as_memory`  | um dump de execução vira um blob de `context.offload`; uma linha episódica curta, chaveada pelo id da execução, o substitui | `context.offload`, depois `memory.move` com um `value` de uma linha de ledger, `memory_type=episodic` |
| `records_for_identities` | linhas com chave `<prefix>-<id>` e campos escalares viram um schema e um registro por linha     | `memory_schema.register` uma vez, `memory.record.write` por linha, depois `memory.move` para uma linha de ponteiro |

Cada seção informa: a contagem e os bytes, três chaves de exemplo antes → depois (a partir dos
`samples` do achado), as chamadas exatas com seus argumentos e o que ela **não** fará:

- nenhum `memory.delete`;
- nenhuma alteração de valor no local: um valor reescrito só é gravado sob uma chave nova;
- nenhuma linha gravada na última hora: o escritor pode estar no meio da operação.

Armazene o plano como um documento de conhecimento em `loom://<org>/<ns>/_optimize/plan-<YYYYMMDD>`
(`knowledge.upsert` com esse `source_uri`), para que a aprovação do responsável e os números
de antes/depois fiquem em um só lugar.

<a id="3-get-approval"></a>

## 3. Obter aprovação

O proprietário do namespace aprova por escrito. Um plano apresentado não é um plano aprovado. Uma aprovação parcial se aplica apenas às seções nomeadas. Se o writer do proprietário estiver em execução (o `sets` da auditoria na janela indica isso), avise-o; a última hora de linhas está fora do escopo de qualquer forma.

<a id="4-apply"></a>

## 4. Aplicar

Faça um backup antes, se o namespace for importante: `memory.list … fields=["value"]` para um arquivo JSONL, `namespace.snapshot` (HTTP ou SDK; não é servido via MCP) ou, no servidor, `CREATE TABLE … AS loom.memory` mais `INSERT SELECT`
([backup e recuperação](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)).

1. Execute as três primeiras movimentações de cada seção com `dry_run=true`; leia de volta `to_key`,
   `redirected` e `advisories`.
2. Depois mova em lotes de no máximo 50, em ordem de `created_at`, para que as revisões entrem como versões
   da nova chave.
3. Passe `if_match_sha256` a partir da amostra da auditoria ou de um `memory.get` recente. Um 409 significa que a linha
   mudou enquanto você trabalhava: pause a seção, leia novamente e pergunte.
4. Uma resposta sem `memory_id`, `version` e `content_sha256` é uma movimentação que não
   aconteceu. Não a conte.

```bash
curl -s "$LOOM_BASE_URL/v1/memory.move" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "org": "acme", "namespace": "agent-sre", "agent": "sre-bot",
    "from_key": "3f9a0c1e5b7d2a4c6e8f0a1b3c5d7e9f",
    "to_key": "sre/case/c-42/evidence/e-7",
    "value": "Disk on ingest-3 filled at 02:10 UTC.\nSource: alert A-981.",
    "tags": ["case:c-42", "kind:evidence"],
    "dry_run": true}'
```

<a id="5-measure"></a>

## 5. Medir

Execute a auditoria novamente sobre a mesma janela e acrescente uma tabela de antes/depois ao documento do plano (`knowledge.upsert` no mesmo `source_uri`). Entregue ao responsável as [regras do writer](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md#writer-rules).

<a id="6-retire-nothing"></a>

## 6. Não aposente nada

As chaves antigas continuam legíveis por meio do redirecionamento pelo tempo que o proprietário quiser. Depois que o escritor passar a usar as novas chaves, o proprietário pode marcar as antigas com tombstone usando `memory.delete` (exclusão lógica: `memory.history` as preserva). Este procedimento nunca faz isso.

> **Aviso:** aposentar chaves que um escritor ativo ainda lê divide a visão dele: o `memory.get` responde `found: false`, um cliente pode recorrer ao estado local, e os dois armazenamentos passam a divergir. O redirecionamento existe para evitar isso.

<a id="related"></a>

## Relacionados

- [Memória de agente de investigação](https://github.com/ClickHouse/loom/blob/main/docs/guides/investigation-agent-memory.md): o layout de destino
- [Integração de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md#mistakes-to-avoid)
- [Histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
