<!-- i18n: source=docs/guides/extraction-pipeline.md blob=a8d7e423185b09c1fa986972aa01b8c1b70d77d8 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/extraction-pipeline.md), which is authoritative.

<a id="customize-the-extraction-pipeline"></a>

# Personalizar o pipeline de extração

`memory.set_from_messages` executa sua transcrição por uma lista ordenada de etapas que compartilham
um único estado. Uma requisição pode substituir essa lista por meio de `pipeline_steps`: reordenar ou pular etapas, ou
chamar uma etapa em processo que um operador registrou. Esta página trata apenas da lista de etapas;
[Extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) trata do que a extração faz.

`pipeline_steps` é um campo do corpo HTTP. Ele fica oculto do schema da ferramenta MCP e não é um
parâmetro do helper `memory.set_from_messages` do SDK Python; envie-o por HTTP ou por
`call(...)`. Os tipos de entrada de op gerados (`sdk/python/loom/ops.py`,
`sdk/typescript/src/generated/ops.ts`) o incluem.

<a id="the-default-pipeline"></a>

## O pipeline padrão

`DEFAULT_PIPELINE` em `server/memory/app/pipeline.py`:

| Etapa                             | O que faz                                                                                                          |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `loom.fetch_namespace_categories` | Carrega a taxonomia de tags do namespace para o extrator.                                                          |
| `loom.fetch_existing_memories`    | Memórias existentes mais próximas (até 50), mostradas ao extrator.                                                 |
| `loom.llm_extract`                | A chamada de extração.                                                                                             |
| `loom.drop_recall_echoes`         | Descarta fatos que repetem memórias fornecidas à sessão.                                                           |
| `loom.drop_session_echoes`        | Descarta fatos que repetem o que as próprias chamadas do Loom da sessão trouxeram.                                 |
| `loom.drop_activity_narration`    | Descarta narração de atividade de ferramentas sem resultado duradouro.                                             |
| `loom.drop_non_durable`           | Sessões de código: descarta retratos de status, tarefas de manutenção e planos.                                    |
| `loom.verify_durable`             | Sessões de código: passada de verificação sobre os fatos restantes.                                                |
| `loom.hash_dedup`                 | Descarta fatos cujo hash canônico já está armazenado.                                                              |
| `loom.bulk_write`                 | Envia regras do bot (`about="agent"`) para a fila de learnings e grava o restante via `memory.bulk_set`.           |

Uma sessão de código é uma transcrição cujas `extra_tags` carregam uma tag de origem de agente de código
(`source:claude-code`, `source:codex`, `source:cursor`), ou um replay de trajetória. As duas etapas
de código deixam passar inalteradas as outras transcrições.

**Um `pipeline_steps` vazio executa esta lista. Um não vazio executa exatamente as etapas que
ele nomeia, em ordem**, sobre um único estado compartilhado. Deixar de fora `loom.bulk_write` significa que nada é
gravado: a resposta ainda informa o que foi extraído.

<a id="run-a-shorter-pipeline"></a>

## Executar um pipeline mais curto

Extrair e gravar sem contexto de deduplicação, para uma importação única de dados sabidamente novos:

```bash
curl -s "$LOOM_BASE_URL/v1/memory.set_from_messages" \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' -d '{
  "org": "acme",
  "namespace": "support",
  "agent": "importer",
  "messages": [{ "role": "user", "content": "..." }],
  "pipeline_steps": [{ "step_id": "loom.llm_extract" }, { "step_id": "loom.bulk_write" }]
}'
```

Isso exige um provedor de LLM real (`LOOM_LLM_PROVIDER` e sua chave); o provedor falso da
stack padrão não extrai fatos significativos.

<a id="step-behaviour"></a>

## Comportamento das etapas

Cada descritor é `{"step_id": ..., ...}`; o objeto inteiro é passado à etapa como sua
configuração.

- **No máximo 16 etapas** por requisição.
- **Etapas `hook.*` são recusadas** com 422 `VALIDATION_FAILED`: o servidor não faz nenhuma chamada
  de saída em nome do chamador.
- **IDs de etapa desconhecidos** são ignorados e informados em `pipeline_errors` na resposta como
  `unknown_step:<id>`.
- **Etapas fail-open.** Uma etapa de busca que falha registra o erro em `pipeline_errors`
  (`fetch_existing_failed:<error>`, por exemplo) e o pipeline continua.
- **`loom.llm_extract` falha de forma fechada**: um erro de extração aborta a requisição com 502, de modo que uma
  transcrição nunca receba silenciosamente `written=[]` como resposta. Ela aceita `{"judge": true}` em
  seu descritor para ativar o filtro de julgamento; `judge_extraction=true` na requisição faz o mesmo
  para a lista padrão.
- **Tempo.** O `stage_ms` da resposta traz o tempo de relógio de cada etapa (`step.<step_id>`).
  Cada etapa também é executada dentro de um span do OpenTelemetry chamado `pipeline.step`, com o ID da etapa
  como atributo.

<a id="register-an-in-process-step"></a>

## Registrar uma etapa em processo

Um operador registra uma etapa em código que o processo do servidor importa na inicialização:

```python
from server.memory.app.pipeline import WorkflowState, register_step


async def spam_filter(state: WorkflowState, config: dict) -> WorkflowState:
    state.extracted_facts = [
        f for f in state.extracted_facts if "spam" not in str(f.get("value", "")).lower()
    ]
    return state


register_step("operator.spam_filter", spam_filter)
```

As requisições podem então nomear `{"step_id": "operator.spam_filter"}`; quaisquer outras chaves do
descritor chegam como `config`. A etapa é executada em processo, com acesso de leitura e escrita a todo o
`WorkflowState`: identidade, mensagens, memórias existentes, fatos extraídos, contadores.
`register_step` substitui um registro existente com o mesmo ID, então uma etapa de operador
também pode substituir uma etapa embutida.

Uma etapa que substitui `loom.bulk_write` deve deixar de fora do que grava os fatos com `about="agent"`:
essas são as regras do bot, que a etapa embutida envia para a fila de learnings.

> **Aviso:** uma etapa que descarta ou reescreve `extracted_facts` muda o que todo chamador
> que a nomeia armazena. Uma etapa posicionada depois de `loom.bulk_write` vê as linhas gravadas em
> `state.written`, mas não consegue desfazer a gravação.

Para adicionar uma etapa embutida ao próprio Loom, adicione a função e sua chamada a `register_step` em
`server/memory/app/pipeline.py`, coloque-a em `DEFAULT_PIPELINE` e execute
`make bench-quick` antes e depois: a lista padrão molda toda extração.

<a id="limits"></a>

## Limites

- As etapas são executadas em sequência; não há ramificação nem paralelismo.
- A lista padrão é global; não há pipeline por namespace.
- Não há retry no nível da etapa.

<a id="related"></a>

## Relacionados

- [Extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)
- [Estendendo o Loom](../development/extending-loom.md)
- [Gravando memória](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)
