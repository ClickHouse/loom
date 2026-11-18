<!-- i18n: source=docs/operations/runtime-settings.md blob=b519ead8aee373492c371073baaa1fe49501d971 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md), which is authoritative.

<a id="runtime-settings"></a>

# Configurações de runtime

Toda configuração é uma variável de ambiente `LOOM_<FIELD>` lida na inicialização e também pode
ir no arquivo de configurações YAML ([referência de configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)). Uma
configuração que a referência marca como **live** também aceita uma sobrescrita válida para todo o deployment enquanto o Loom
está em execução (entre elas, recuperação e ranqueamento, `memory_search_*`; síntese de respostas,
`memory_ask_*`; ajustes de extração, contexto e conhecimento; e o agrupamento de inserts no ClickHouse,
`ch_batched_insert*`). Toda outra configuração é lida uma vez na inicialização e muda com uma
reinicialização. Os limites de similaridade que a calibração substitui
(`memory_search_min_similarity`, `memory_search_abstain_threshold`,
`memory_search_hyde_fallback_threshold`, `memory_search_query_rewrite_threshold`) não são
live: com `LOOM_ADAPTIVE_THRESHOLDS` ativado, eles só valem quando não há calibração.

<a id="see-what-is-in-force"></a>

## Veja o que está em vigor

```bash
loom settings list --changed              # off its default, or overridden
loom settings list --prefix memory_ask_   # one area
loom settings list --json
```

Cada linha mostra o valor, de onde ele veio e se é live:

```text
memory_ask_recency_weight = 0.35  (override, live)
ch_host = "clickhouse"  (env)
```

| Origem          | Significado                                                                              |
| --------------- | ---------------------------------------------------------------------------------------- |
| `override`      | definida com `loom settings set` ou `/v1/admin/settings.set`                             |
| `dashboard`     | a configuração de modelos salva em Settings → Models & keys                              |
| `env`           | o ambiente do processo                                                                   |
| `env_file`      | o arquivo dotenv (`LOOM_ENV_FILE`, padrão `.env`), também quando copiado para o ambiente |
| `settings_file` | o arquivo de configurações YAML (`LOOM_SETTINGS_FILE`), também quando `scripts/serve.sh` o exporta |
| `inferred`      | preenchida na inicialização a partir de outras configurações (o provedor de LLM, a largura de um modelo local) |
| `default`       | o padrão embutido                                                                        |

Um segredo mostra apenas o seu tamanho.

<a id="override-a-live-setting"></a>

## Sobrescreva uma configuração live

```bash
loom settings set memory_ask_recency_weight 0.35
loom settings reset memory_ask_recency_weight
```

O valor é gravado como a variável de ambiente correspondente seria e validado da mesma forma: um
valor fora do intervalo, ou uma configuração que não é live, é recusado e nada é armazenado. A
sobrescrita é uma linha de `loom.runtime_overrides`. O processo que a armazena a aplica
imediatamente; todos os outros processos, inclusive os workers, a aplicam dentro de
`LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS` (padrão 10). Ela sobrevive a uma reinicialização e prevalece sobre
o ambiente e o arquivo de configurações até ser redefinida; a redefinição restaura o valor próprio de cada
processo ou o padrão. `reset` recusa uma configuração sem sobrescrita armazenada e
remove a sobrescrita de uma configuração que um build posterior não tem mais. Cada alteração grava uma
linha de auditoria `setting.changed` com o valor antes e depois.

`--expect <value>` (`expected` via HTTP) não altera nada, a menos que esse seja o valor em vigor
após uma nova leitura das sobrescritas armazenadas, e responde `409 PRECONDITION_FAILED`
com o valor que encontrou: uma alteração feita desde então por outra réplica ou pela CLI não
é sobrescrita sem ser vista. Duas alterações enviadas no mesmo instante ainda podem passar ambas.

<a id="see-and-undo-past-changes"></a>

## Veja e desfaça alterações passadas

```bash
loom settings history memory_ask_recency_weight
loom settings history --limit 20      # every setting
```

Cada linha é uma alteração registrada, da mais recente para a mais antiga: quando, quem (`cli:<user>` ou o subject
do token) e o valor antes e depois. Um valor anterior que era uma sobrescrita é o do
deployment: desfaça definindo-o de volta com `loom settings set`. Qualquer outro era o valor
do processo que fez a alteração (seu ambiente ou um padrão), marcado
`(undo: reset)`: desfaça com `loom settings reset`, que restaura o valor próprio de cada réplica. Uma
alteração sem valor anterior (a redefinição de uma configuração que este build não tem mais) não tem nada
a desfazer.

Uma sobrescrita é o padrão do deployment, então o que um namespace ou uma requisição define continua prevalecendo
sobre ela: os `retrieval_defaults` de um namespace (`top_k`, `min_similarity`,
`reinforcement_weight` e os demais parâmetros de busca que `namespace.register` aceita) e os
campos da própria requisição.

As mesmas operações via HTTP, com um token de administrador da plataforma. Com a autenticação desativada, uma alteração é
aceita apenas em um endereço de loopback (`localhost`): caso contrário, uma página web poderia alcançá-la
por meio de DNS rebinding.

```bash
curl -s "$LOOM_BASE_URL/v1/admin/settings.list" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"changed": true}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "value": 0.35}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "clear": true}'
```

`POST /v1/admin/settings.history` recebe `{"name": "...", "limit": 50}`. O SDK Python
tem `agent.deployment.settings()`, `set_setting(name, value, expected=...)`,
`reset_setting(name, expected=...)` e `setting_history(name)`.

<a id="during-a-rolling-upgrade"></a>

## Durante um rolling upgrade

Um build que não tem uma configuração, não a lê como live, ou cujo campo recusa o
valor armazenado, registra a sobrescrita armazenada uma vez no log e a ignora; os outros processos continuam
aplicando-a. Um build anterior à existência das configurações de runtime ignora as sobrescritas armazenadas
sem avisar, então, enquanto um deles ainda estiver atendendo, uma sobrescrita se aplica apenas às
réplicas mais novas. As configurações de ranqueamento mudam o que `memory.search` retorna: meça uma alteração com um
benchmark antes de mantê-la ([executando um benchmark](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).
