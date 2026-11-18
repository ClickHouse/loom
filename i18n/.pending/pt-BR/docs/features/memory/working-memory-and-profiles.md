<!-- i18n: source=docs/features/memory/working-memory-and-profiles.md blob=bedd8394f653eb3b5cd589b4bd7aed2749be116b model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md), which is authoritative.

<a id="working-memory-and-profiles"></a>

# Memória de trabalho e perfis

Dois planos guardam texto que o agente recebe sempre, em vez de encontrá-lo por busca:

- **Memória de trabalho**: pequenos blocos nomeados para leases, cursores, checkpoints e "onde eu estava". Os blocos não são embedded, ranqueados nem pesquisados.
- **Perfis**: contexto sempre carregado sobre o usuário ou o agente (persona, preferências, foco), renderizado como um único fragmento de prompt.

Use `memory.set` para tudo o que uma sessão posterior deva encontrar por meio de uma consulta. Use estes planos para o que ela deve receber sem precisar perguntar. Uma linha de rascunho escrita como fato compete com fatos reais em toda busca e em toda verificação de conflitos.

<a id="working-memory"></a>

## Memória de trabalho

Um bloco fica em `loom.working_memory`, indexado pela tupla de identidade mais `run_id` e
`block_key` (padrão `scratchpad`, no máximo 80 caracteres). Os blocos sobrevivem a reinicializações e
não têm TTL, então funcionam como ponto de retomada para uma execução longa
([trace e resume](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)). Um bloco só sai quando seu
escritor o exclui ou quando uma eliminação o abrange.

| Op                       | Papel  | Faz                                                                                |
| ------------------------ | ------ | ---------------------------------------------------------------------------------- |
| `working_memory.set`     | writer | Substitui o conteúdo de um bloco; `char_limit` é aplicado.                         |
| `working_memory.append`  | writer | Acrescenta `addition` com `separator` (padrão: quebra de linha).                   |
| `working_memory.replace` | writer | Substitui toda ocorrência de `old` por `new` dentro de um bloco.                   |
| `working_memory.delete`  | writer | Marca um bloco com tombstone.                                                      |
| `working_memory.get`     | viewer | Lê um bloco, com `fill_ratio`.                                                     |
| `working_memory.render`  | viewer | Renderiza os blocos no escopo (ou até 20 `block_keys` nomeados) como XML ou Markdown. |

Listas de campos: [referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#working_memory).

<a id="size-limits"></a>

### Limites de tamanho

| Limite               | Valor                                                                     |
| -------------------- | ------------------------------------------------------------------------- |
| `char_limit` padrão  | 4,096 caracteres por bloco                                                |
| Maior `char_limit`   | 16,384 caracteres                                                         |
| Acima do limite      | 422 `WORKING_MEMORY_LIMIT_EXCEEDED`, com `value_length` e `char_limit`    |
| Blocos por render    | `limit` 1–100 (padrão 20)                                                 |

Toda escrita retorna `value_length` e `fill_ratio` (`len(content) / char_limit`), para que o
chamador possa reduzir o conteúdo antes que uma escrita seja recusada. Um `replace` cujo texto `old` não existe responde
422 `SUBSTRING_NOT_FOUND`; um em um bloco inexistente responde
`WORKING_MEMORY_BLOCK_NOT_FOUND`. `working_memory.get` em um bloco inexistente responde
`found=false`.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.working_memory.set(
        "cursor: ticket 4812\nlease: holds refunds queue", block_key="state", run_id="run-7"
    )
    await bot.working_memory.append("step 3 done", block_key="state", run_id="run-7")
    print(await bot.working_memory.render(run_id="run-7"))
    # <working-memory block="state">cursor: ticket 4812 ...</working-memory>
```

<a id="profiles"></a>

## Perfis

Uma seção de perfil é uma linha de memória com `memory_type='profile'` cuja chave é o
`label` da seção. `profile.set`, `profile.append` e `profile.replace` são wrappers finos sobre
`memory.set` que fixam esse tipo e um `char_limit`. Ler uma seção é `memory.get`
com o label como chave; o SDK Python encapsula isso como `agent.profile.get(label)`,
`agent.profile.list()` e `agent.profile.delete(label)` (métodos do SDK, não ops de wire).

| Op                 | Papel  | O que faz                                                                                              |
| ------------------ | ------ | ------------------------------------------------------------------------------------------------------ |
| `profile.set`      | writer | Cria ou substitui a seção em `label` (`if_match_sha256` para escritores concorrentes).                 |
| `profile.append`   | writer | Acrescenta a uma seção; tenta de novo com compare-and-swap quando outro cliente escreveu primeiro.     |
| `profile.replace`  | writer | Reescreve uma substring dentro de uma seção (422 `SUBSTRING_NOT_FOUND` quando ela não existe).         |
| `profile.render`   | viewer | Renderiza todas as seções (ou os `labels` indicados, nessa ordem) como um fragmento XML ou Markdown.   |
| `profile.activity` | viewer | Quais clientes leram o perfil, e quando (`lookback_days`, limitado a 30).                              |

Listas de campos: [referência de operações](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#profile).

- **Limite.** O `char_limit` padrão é 8,192 caracteres, medidos no JSON canônico do valor;
  respostas mais longas retornam 422 `CHAR_LIMIT_EXCEEDED`.
- **Escopo.** Por padrão, `profile.render` lê seções de todos os agentes do namespace
  (`scope="namespace"`), de modo que uma persona escrita no painel chega a todo cliente MCP.
  `scope="this_agent"` se limita às seções do próprio chamador. Sob `read_scope` `own_user` ou
  `own_session`, apenas as seções daquele usuário (ou sessão) e as de nível de namespace
  (`user_id` `-`) são renderizadas.
- **Busca.** Linhas de perfil ficam fora de `memory.search`. A ferramenta `loom_search` tem
  uma etapa de perfil que pontua as seções por significado quando a pergunta é sobre o usuário;
  o vetor de cada seção é atualizado fora do caminho da requisição após cada escrita. Veja
  [busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md).
- **Incorporando fatos a um perfil.** O worker `profile_consolidation` (desativado por padrão,
  `LOOM_PROFILE_CONSOLIDATION_ENABLED`) incorpora as memórias de preferência e decisão de um
  usuário a uma seção versionada (label `consolidated_profile`) por meio de uma chamada a um
  modelo. Linhas marcadas com um atributo protegido ficam de fora. Sua proveniência é `derived`
  (`source_trace` `profile.fold:<label>:<memory ids>`, os fatos que ele leu).

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.profile.set("persona", "Terse. Cite ticket ids. Never promise refunds.")
    await bot.profile.append("preferences", "Prefers EUR invoices.", user_id="u-42")
    system = await bot.profile.render(user_id="u-42", read_scope="own_user")
```

<a id="structured-profiles"></a>

## Perfis estruturados

Um bot de chat que precisa conhecer o usuário antes da primeira resposta lê um único objeto em um formato
definido pelo operador, em vez de texto livre. Um admin atribui ao namespace um schema de perfil; o worker `structured_profile` mantém o perfil de cada usuário atualizado nesse formato
a partir das memórias do usuário; `profile.structured` o retorna com seu status. Uma leitura nunca
chama um modelo.

| Op                     | Papel  | O que faz                                                                                                                                                  |
| ---------------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile_schema.set`   | admin  | Define o schema do namespace (`json_schema`); auditado. Substitui o anterior.                                                                              |
| `profile_schema.get`   | viewer | O schema, seu `schema_sha256`, quem o definiu e quando.                                                                                                    |
| `profile_schema.clear` | admin  | Remove o schema; auditado. Os perfis já construídos permanecem como linhas de memória.                                                                     |
| `profile.structured`   | viewer | O perfil de um usuário: `profile`, `status`, `built_at`, `memory_id`, `version` (e `version_text`, o mesmo valor como string, exato em JavaScript).        |

O schema é um objeto JSON Schema com 1 a 64 `properties`, de no máximo 16 KiB. Cada
propriedade tem um `type` (`string`, `number`, `integer`, `boolean` ou `array` com `items`
de um desses tipos e um `maxItems` opcional de até 50) e uma `description`, que o
modelo lê. Uma resposta completa contém no máximo 256 valores, contando um por propriedade e
`maxItems` por array, de modo que caiba no limite de saída do modelo. Strings aceitam `enum`, `pattern`, `minLength` e `maxLength`; números aceitam
`minimum` e `maximum`, as mesmas restrições que os [registros tipados](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records) usam.
Toda propriedade está em todo perfil, com valor `null` quando as memórias não a definem, então o
schema não tem `required`; `additionalProperties` só pode ser `false`. Um `title` é
aceito em qualquer nível e ignorado.

```python
schema = {
    "type": "object",
    "properties": {
        "preferred_name": {"type": "string", "description": "What the user asks to be called"},
        "timezone": {"type": "string", "description": "IANA time zone, e.g. Europe/Berlin"},
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages they speak",
        },
    },
}
async with Agent(org="acme", namespace="support", agent="chat", api_key=ADMIN_TOKEN) as admin:
    await admin.client.profile_schema.set({"json_schema": schema})

async with Agent(org="acme", namespace="support", agent="chat") as bot:
    out = await bot.client.profile.structured({"user_id": "u-42"})
    # {"status": "ready", "profile": {"preferred_name": "Sam", "timezone": "Europe/Berlin",
    #  "languages": ["en", "de"]}, "built_at": "...", ...}
```

| `status`    | Significa                                                                                                                                                                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ready`     | Construído a partir das memórias atuais do usuário sob o schema atual; todos `null` para um usuário sem fatos.                                                                                                                              |
| `stale`     | Uma memória ou o schema mudou desde a construção, ou um de seus fatos não foi servido quando ela foi feita; servido como foi construído até que uma passada o reconstrua.                                                                   |
| `pending`   | Sem perfil ativo: ainda não construído, ou a linha construída expirou, teve o conteúdo apagado por privacidade (redacted; até o worker reconstruí-la) ou está rotulada `high` (retida até a chave passar para uma nova versão); toda propriedade lê `null`. |
| `no_schema` | O namespace não tem schema, ou o schema armazenado não é mais interpretado; `profile` é `{}`.                                                                                                                                               |

- **Armazenamento.** O perfil é a linha `profile` do usuário com a chave `structured_profile`
  (`agent` `-`), então `profile.render` e `context.pack` também o carregam, e
  [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory) lista suas versões.
  Sua proveniência é `derived`; `source_trace` é
  `profile.fold:structured_profile:<memory ids>`, as memórias a partir das quais foi construído. Só uma
  linha escrita pelo worker (seu `actor`) é servida: uma linha que um chamador grava na chave é lida como
  sem perfil (`pending`), quaisquer que sejam suas tags, e o worker a reconstrói por cima, a menos que esteja
  fixada, bloqueada ou rotulada `high`.
- **Fontes.** Os fatos `semantic`, `preference` e `decision` atuais do usuário, sob o
  mesmo filtro do fold em texto livre: nunca uma linha `high`, um duplicado (conta sua linha
  canônica), um fato sobre outra pessoa (`about:other`), um atributo protegido ou um pedido armazenado
  de esquecer. Cada fato é datado de quando se tornou verdadeiro (`valid_at`), senão de quando mudou
  pela última vez, e uma construção lê primeiro os mais confiáveis e os alterados mais recentemente, de modo que
  uma edição é lida como a afirmação mais nova.
- **Obsolescência.** Cada perfil carrega o hash das propriedades do seu schema (um `title` ou
  a ordem das propriedades não muda nada), um digest de suas fontes (id, hash de conteúdo e confiança de cada uma, e a data que uma construção lê e usa para ordenar: `valid_at`, senão a última
  alteração) e a contagem delas. Um fato novo ou editado, uma reversão, uma restauração, um fato excluído, uma
  confiança ou data alterada, e uma mudança que move uma linha para dentro ou para fora das fontes (ela
  vira `high`, muda de tipo, ganha ou perde uma tag `about:other`, `attribute:` ou `forget`,
  expira ou passa do seu `valid_to`) alteram o digest. Uma reafirmação ou um
  reforço também, pois move o fato para a posição mais nova no que o modelo
  lê: custa uma reconstrução após o cooldown. O mesmo vale para `memory.lock` ou
  `memory.unlock` de um fato-fonte: grava uma nova versão com o mesmo conteúdo, que
  data o fato no momento do lock e o move para a posição mais nova no que o modelo lê
  e para o grupo dos fatos mais novos que uma construção lê. Entre dois fatos que discordam sob chaves diferentes,
  o que foi bloqueado ou desbloqueado por último passa a ser lido como o mais recente. Um recall ou um
  backfill, que mantém a última alteração da linha, não altera. O digest cobre todas as fontes,
  enquanto uma construção lê apenas os `LOOM_STRUCTURED_PROFILE_MEMORY_LIMIT` (100) mais confiáveis
  e mais novos: para um usuário com mais fatos que isso, uma mudança em um fora desse conjunto ainda
  custa uma chamada ao modelo que não pode alterar a resposta. Uma mudança de schema é reconstruída a partir da
  próxima passada, no máximo `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` perfis por passada. Uma
  exclusão (menos fontes do que o perfil contou) pula o cooldown e é reconstruída assim que
  o namespace for varrido de novo, no máximo `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`
  (600) depois, enquanto as passadas tiverem orçamento; `profile.structured` a informa como `stale` de
  imediato. As demais mudanças são reconstruídas após `LOOM_STRUCTURED_PROFILE_COOLDOWN_SECONDS` (600).
  Uma construção que ficou sem um fato que deveria ter lido (um dos fatos que ela contou, dentro do
  limite de memória, não foi servido: uma réplica do ClickHouse atrás daquela em que suas fontes
  foram contadas, ou, no Postgres, uma mudança que a autoridade mantém sobre esse fato ainda não havia
  chegado ao ClickHouse) é lida como `stale` e é construída de novo após o cooldown, uma chamada ao modelo
  a cada vez, enquanto essa mudança continuar faltando. Uma construção para a qual nenhum dos
  fatos contados foi servido não faz chamada ao modelo: substitui um perfil servido pelo perfil
  todo `null`, gravado de novo após cada cooldown, e não grava nada para um usuário sem perfil
  servido (`pending`). Um usuário cujos fatos sumiram todos recebe um perfil todo `null` sem
  chamada ao modelo. Uma linha de perfil expirada, ou cujo conteúdo foi apagado por privacidade (redacted), é lida como sem perfil (`pending`) e é
  reconstruída. Uma linha de perfil `high` também é lida como sem perfil, mas o worker a deixa
  como está: ela fica retida como se estivesse fixada até a chave passar para uma nova versão.
- **Limite de saída.** O modelo pode escrever a entrada `structured_profile` de `LLM_MAX_TOKENS`
  (32, alterada por `LOOM_LLM_MAX_TOKENS_OVERRIDES`) mais a estimativa do schema para uma
  resposta completa: cada chave com sua pontuação, e cada valor (um por propriedade, `maxItems`
  por array) dimensionado por seu `maxLength` ou pelo seu valor de `enum` mais longo a dois caracteres por
  token, 48 tokens para uma string sem nenhum dos dois, 12 para um número ou um booleano.
  `profile_schema.set` recusa um schema cuja estimativa passe de 16,384 tokens. Uma string
  maior que sua estimativa ainda pode ser cortada; dê a uma propriedade de texto livre um `maxLength`
  para abrir espaço para ela.
- **Validação.** A resposta do modelo é conferida com o schema antes de ser gravada: um valor
  de tipo errado, fora do seu enum ou de suas restrições, um número que não é finito (ou um
  inteiro acima de 2^53), ou uma propriedade que o schema não declara é descartado (`null`).
- **Passadas.** Uma passada constrói no máximo `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` (20)
  perfis. Ela percorre os namespaces com schema em ordem, a partir de onde a última passada
  parou, e cada um recebe uma parte igual. Um namespace sem nada a construir não gasta nenhuma,
  então a passada segue para os próximos namespaces até seu orçamento acabar ou até
  alcançá-los todos; o que um namespace deixa vai para os namespaces que usaram toda a sua
  parte, a partir dos usuários que a varredura deles já listou. Uma passada pode durar
  `LOOM_STRUCTURED_PROFILE_MAX_PASS_SECONDS` (600) antes de o supervisor abandoná-la, e
  não pega nenhum usuário na segunda metade desse tempo: a construção em andamento tem a segunda metade
  para terminar, e os usuários não alcançados continuam listados para a próxima passada. Uma varredura de um
  namespace lista até dez vezes a sua parte, e as passadas seguintes tiram dessa lista
  até ela esgotar, o schema mudar, ou ela ter
  `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) de idade; uma varredura que encontrou todos os usuários
  atrasados (menos do que pediu) não é repetida antes disso depois de esgotada, de modo que um
  namespace é varrido no máximo uma vez por essa idade enquanto ninguém mais estiver atrasado. Um usuário
  cuja construção falhou (um erro do modelo, uma resposta que não é um objeto JSON) espera
  `LOOM_STRUCTURED_PROFILE_BACKOFF_BASE_SECONDS` (300), dobrando até
  `LOOM_STRUCTURED_PROFILE_BACKOFF_CAP_SECONDS` (21600). Ele fica de fora das varreduras do namespace
  feitas enquanto espera, então é tentado de novo na primeira varredura depois do fim da espera:
  no máximo a espera mais `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) após a
  falha. Um namespace cuja varredura ou schema armazenado falha é contado e recua da mesma
  forma até a espera acabar ou o schema mudar; um namespace em espera
  não ocupa lugar na passada. Uma construção que a linha de perfil recusa (bloqueada, fixada, rotulada
  `high`, na lista de bloqueio), ou cuja resposta o modelo cortou no limite de saída, não é
  repetida até que as memórias do usuário, a linha de perfil (um unpin ou um unlock, também em uma
  linha com o conteúdo apagado por privacidade, redacted) ou o schema mudem, ou até passarem
  `LOOM_STRUCTURED_PROFILE_REFUSAL_RETRY_SECONDS` (21600), de modo que um lock que expira
  sozinho é tentado de novo. Essas esperas e as listas de varredura ficam no processo do worker
  e recomeçam quando outro processo assume o lease do worker. Uma passada que alcança todos os
  namespaces com schema e não encontra usuário atrasado espera
  `LOOM_STRUCTURED_PROFILE_IDLE_INTERVAL_SECONDS` (600) antes da próxima; uma que alcançou
  apenas parte do rodízio roda de novo no intervalo do worker.
- **Apagamento.** Uma construção não grava nada para um usuário sem fatos e sem linha de perfil, então um
  usuário apagado depois de uma varredura que o listou não recebe linha nova quando uma passada posterior o alcança. Uma
  construção também não grava nada quando um fato que ela leu deixou de ser atual depois que o modelo
  respondeu (apagado ou excluído enquanto ele rodava). Resta a pequena janela entre essa
  verificação e a gravação: uma linha gravada ali enquanto a saga de apagamento roda é uma linha ativa que seu
  censo final encontra, e a saga falha de forma fechada diante dela. `memory.redact` de um fato-fonte
  limpa toda versão de perfil cujo trace o cita, e é recusado quando a chave do perfil
  está bloqueada. Um redact repetido após uma falha alcança as mesmas chaves de perfil. Uma versão de perfil cujo trace não cita ids de memória (`profile.fold:<label>`
  apenas, como uma construção que não registra suas fontes a grava, incluindo uma construção mais antiga
  durante uma atualização gradual) não é alcançada e mantém o texto até o usuário ser apagado
  ([segurança](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).
- **Escopo.** A leitura é sempre para um `user_id`. Um token fixado a um usuário
  (`read_user_id`, um token de usuário final) lê apenas o perfil desse usuário, com ou sem
  `user_id` no corpo; outro usuário retorna 403. Um token limitado a uma sessão
  (`own_session`) é recusado (`IDENTITY_INCOMPLETE` sem um `session_id`,
  `AUTH_INSUFFICIENT` com um): um perfil abrange as sessões do usuário.
- **Chamadas ao modelo.** Só o worker chama o modelo, apenas para um namespace com schema,
  nunca para uma org cujas chamadas ao modelo estejam desativadas
  ([`org.set_model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#org)), e por meio do
  provedor configurado ou do gateway de inferência.

<a id="how-contextpack-loads-them"></a>

## Como `context.pack` os carrega

`context.pack` renderiza `working_memory` primeiro e `profile` em segundo, antes das
seções que dependem da consulta, de modo que o início do pack permanece o mesmo entre
perguntas e um cliente pode colocar um ponto de quebra do prompt-cache depois dele (`stable_prefix_chars`,
`stable_hash`). A memória de trabalho é organizada em camadas, da mais estreita para a mais ampla: um pack para uma run também inclui
os blocos da sessão (`run_id` `-`), e um pack para uma sessão também inclui os blocos
do agente (`session_id` `-`, `run_id` `-`). A nota "onde eu parei" de um plugin
(`session:<project>`) fica na camada do agente; com `session_block` definido, as notas
`session:*` de outros projetos são deixadas de fora. A ordem das seções, os orçamentos e as outras ops `context.*` estão
na [busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md).

<a id="settings"></a>

## Configurações

| Configuração                    | Efeito                                                                                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROFILE_CONSOLIDATION_*`  | O worker de fold de perfil ([Consolidação](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)).                                                                  |
| `LOOM_STRUCTURED_PROFILE_*`     | O worker de perfil estruturado ([Perfis estruturados](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#structured-profiles)).                                                |
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | Marca linhas com atributos protegidos, que o fold deixa de fora ([Sensibilidade e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#sensitivity-and-provenance)). |

<a id="limits"></a>

## Limites

- **Gravações acima do limite são recusadas, não truncadas.** Acompanhe `fill_ratio`.
- **A memória de trabalho não é recall.** Um bloco nunca é ranqueado nem gerado como embedding. Além de `context.pack` e das ops de render/get, somente uma chamada que nomeia uma run ou uma sessão carrega blocos: `memory.ask` coloca os blocos da run em seu prompt de síntese, e `memory.search` retorna os blocos da run e da sessão em `scratchpad`. Ambas leem dentro do escopo de leitura da requisição.
- **Uma seção de perfil é uma linha por label.** `profile.set` a substitui; as versões anteriores continuam legíveis por meio de [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory).
- **O render abrange todos os agentes por padrão**, a menos que `scope="this_agent"`.

<a id="related"></a>

## Relacionados

- [Escrita de memória](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md): fatos duráveis.
- [Conversas e sessões](../conversations-and-sessions.md): resumos de sessão.
- [Busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md): `context.*` e `loom_search`.
