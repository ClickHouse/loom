<!-- i18n: source=docs/architecture/server-layout.md blob=a488f9e3096ee9ff0f09303b9a382a9ce950bac7 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/server-layout.md), which is authoritative.

<a id="server-layout"></a>

# Layout do servidor

`server/` é organizado por domínio. Cada plano do produto tem um único pacote que reúne suas declarações de protocolo, handlers, lógica pura, acesso ao ClickHouse e loops em segundo plano, e um teste fixa em que direção os imports podem apontar. Esta página é o mapa para decidir onde colocar um novo módulo e para interpretar um erro de import vindo do teste de contrato.

<a id="packages"></a>

## Pacotes

| Pacote                        | Responsável por                                                                                                                                                                                             | Pode importar                                                                                               |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `server/main.py`              | Montagem da aplicação: o lifespan, os routers, os workers, os transportes. Junto com `server/boot.py`, `server/health.py`, `server/wiring.py`, `server/worker_main.py` e `server/__init__.py`, os únicos módulos na raiz | qualquer pacote                                                                                             |
| `server/boot.py`              | Os passos de inicialização e encerramento do lifespan: a migração de boot serializada, as linhas de namespace do tracker do líder, o snapshot de revogação e o keyring, o loop de lease do supervisor, o write drain | qualquer pacote                                                                                             |
| `server/health.py`            | `GET /v1/health`: a resposta de saúde em várias camadas (ClickHouse, provedores, o snapshot de boot, calibração, o limitador de taxa, e, sob demanda, os workers e a observabilidade)                        | qualquer pacote                                                                                             |
| `server/wiring.py`            | `wire()`: entrega a cada seam da plataforma a sua peça de domínio (o diretório de namespaces do gate de autenticação, os fatos dos medidores de estado); executado por `main.py`, pela CLI e pelas suítes de teste | qualquer pacote                                                                                             |
| `server/worker_main.py`       | A composition root do processo filho dos workers (`python -m server.worker_main`): o supervisor e o que seus workers usam, sem o lifespan de atendimento                                                    | qualquer pacote                                                                                             |
| `server/core/`                | Infraestrutura transversal: o cliente do ClickHouse (`ch`), `errors`, orçamentos, datetimes, hashing, nomenclatura, cursores, idempotência, limites de taxa, política de armazenamento de vetores, `schema/` (migrações e ferramentas de DDL) | `core`, `settings`                                                                                          |
| `server/settings/`            | Todas as configurações declaradas em `catalog.py`, uma seção por área, compostas em `Settings`                                                                                                              | `core`, `settings`                                                                                          |
| `server/operations/`          | O núcleo do registro: `Operation` e `Role` (`base.py`), o registro achatado (`registry.py`: `ALL_OPERATIONS`, `OPERATIONS_BY_NAME`), `toolsets.py`, projeção de leitura, o censo de alcance                  | `core`, pacotes da plataforma, o `api` de cada domínio                                                      |
| `server/transport/`           | HTTP e MCP: admissão, vinculação de requisições, listagens, o primer                                                                                                                                        | `core`, pacotes da plataforma; o `api`, `app` e `domain` de um domínio (nunca o `store` ou `workers`); não `cli` |
| `server/cli/`                 | O comando `loom`, um adaptador externo                                                                                                                                                                      | qualquer pacote                                                                                             |
| `server/workers/`             | O registro de workers (`registry.py`), o supervisor, o contexto de passagem, o status e seu store, o agendamento da primeira passagem, eventos de custo. Os loops dos workers ficam em seu domínio           | `core`, pacotes da plataforma; o `workers` de um domínio (as strings de módulo do registro)                 |
| `server/observability/`       | O ledger, eventos, linhas de auditoria, métricas, exportação de tracing                                                                                                                                     | `core`, pacotes da plataforma                                                                               |
| `server/auth/`                | Emissão e verificação de tokens, o gate de papel e namespace, a identidade por requisição                                                                                                                   | `core`, pacotes da plataforma                                                                               |
| `server/embedding/`, `llm/`   | Provedores plugáveis de embedder e de LLM                                                                                                                                                                   | `core`, pacotes da plataforma                                                                               |
| `server/clients/`, `storage/` | HTTP de saída (o pool compartilhado, GitHub); cláusulas de retenção                                                                                                                                         | `core`, pacotes da plataforma                                                                               |
| `server/memory/`              | O plano de memória: `memory.*`, busca, ask, extração, fatos, procedimentos, perfil, memória de trabalho, context packs, conflitos, blacklist, `loom_search`                                                  | veja [camadas](#layers)                                                                                     |
| `server/code/`                | O plano de código: `code.*`, o índice (`app/index/`, `domain/index/`, `store/index/`), seus parsers (`domain/extract/`), os workers de ingestão e da camada do compilador                                    | veja [camadas](#layers)                                                                                     |
| `server/tracker/`             | Namespaces de tracker: `tracker.*`, verificações de duplicidade, os leitores de fontes ao vivo e seu cliente, as tabelas de tracker próprias do Loom                                                        | veja [camadas](#layers)                                                                                     |
| `server/knowledge/`           | Documentos e chunks de conhecimento, conversas, documentos de repositório                                                                                                                                   | veja [camadas](#layers)                                                                                     |
| `server/erasure/`             | A saga de apagamento, seu censo, os backends federados, o worker de recuperação                                                                                                                             | veja [camadas](#layers)                                                                                     |
| `server/tasks/`               | A caixa de entrada de tarefas, o ledger de execuções, seus loops de despertar e de webhook                                                                                                                  | veja [camadas](#layers)                                                                                     |
| `server/identity/`            | Namespaces, grants, política de histórico, escopo de leitura, acesso restrito, a página do painel e a API do chamador (`/v1/me/*`)                                                                          | veja [camadas](#layers)                                                                                     |
| `server/insights/`            | Superfícies do operador: `ops.*`, `cost.*`, `agents.*`, `audit.list`, `ui.vitals`, `mentor.brief`, as rotas `/v1/admin/*`                                                                                   | veja [camadas](#layers)                                                                                     |
| `server/assistant/`           | O assistente: seu usuário de serviço do LibreChat e a definição do agente, configurados no boot                                                                                                             | veja [camadas](#layers)                                                                                     |

Os pacotes da plataforma são `core` e os demais pacotes não pertencentes a domínios acima:
`settings`, `operations`, `transport`, `cli`, `workers`, `observability`, `auth`,
`embedding`, `llm`, `clients` e `storage`. Além de `core` e `settings`, cada um pode importar
qualquer outro pacote da plataforma, e nenhum importa um pacote de domínio, exceto conforme
indicado na tabela.

<a id="layers"></a>

## Camadas

Um pacote de domínio tem até cinco subpacotes. Um módulo vai para onde está sua
responsabilidade principal; um domínio pequeno tem menos subpacotes.

| Camada     | Contém                                                                                                                           | Pode importar                                                                                                                                                           |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `api/`     | A interface externa: os modelos Pydantic de entrada e saída (`models*.py`) e as declarações `Operation` dos grupos de ops do domínio (`ops*.py`) | `core`, `operations`, `settings`; o `domain` e o `api` de qualquer domínio; seu próprio `app`, para o handler ao qual uma `Operation` se vincula                        |
| `app/`     | Handlers e orquestração: o que uma op ou uma passada de worker faz, em ordem, com I/O                                            | seu próprio `api`, `app`, `domain`, `store`; o `app`, o `domain` e os modelos de interface de outros domínios (`api/models*.py`); `core` e todos os pacotes de plataforma, exceto `transport` e `cli` |
| `domain/`  | Lógica pura, valores tipados e políticas: sem ClickHouse, sem rede                                                               | `core` (não seus módulos de I/O: `ch`, `schema`, `idempotency`, `ratelimit_shared`, `namespace_write_marks`), `settings`, o `domain` de qualquer domínio                 |
| `store/`   | Repositórios do ClickHouse e construtores de linhas: toda instrução que o domínio executa contra suas tabelas                    | `core`, `settings`, `observability`, `storage`, seu próprio `store`, o `domain` de qualquer domínio                                                                     |
| `workers/` | Loops em segundo plano, um módulo por linha do registro                                                                          | como `app`, com `workers` no lugar de `api`; o `app`, o `domain` e os modelos de interface de outros domínios                                                           |

Um módulo de `domain/` inicia um processo: `server/code/domain/extract/bounded_parse.py`, que
todo parser tree-sitter em `domain/` chama, analisa cada fonte primeiro em um processo
filho local, sob limites de recursos.

Duas dependências estão ausentes de propósito. `store/` nunca chama `app/`: um repositório
recebe valores e devolve linhas, de modo que um app pode ser testado com um store falso. E
nenhum domínio lê o `store/` de outro domínio: ele chama o `app/` desse domínio ou lê seus
tipos de `domain/`, de modo que o dono de uma tabela é o único autor de suas instruções.
Para chamar o handler de outro domínio, `app/` e `workers/` podem importar os modelos de
interface desse domínio (`api/models*.py`), nunca seu `api/ops*.py`. `app/` importa os
modelos de interface, nunca os módulos `ops`, para que as declarações `Operation` possam
vincular handlers sem um ciclo de importação.

<a id="the-import-contract"></a>

## O contrato de imports

`tests/unit/test_package_contracts.py` analisa cada módulo em `server/` com `ast` e classifica cada import (de nível superior, aninhado em `if`/`try`, dentro de uma função ou sob `TYPE_CHECKING`) pelo pacote e pela camada de ambas as pontas. Um import que as regras acima não permitem faz o teste falhar, informando o módulo que importa e o módulo importado.

Os imports que quebram o contrato hoje estão listados em `tests/unit/package_contract_allowlist.txt`, na forma `importer -> imported` pelo caminho do módulo. A lista só diminui: um par permitido que deixou de ocorrer também faz o teste falhar, então corrigir um import significa apagar a linha correspondente.

Uma segunda verificação roda ao lado: somente `server/transport/`, `server/cli/` e `server/main.py` importam `server.transport` (`tests/unit/test_transport_layering.py`). Nenhum módulo de domínio importa `server.cli`.

<a id="adding-a-module"></a>

## Adicionando um módulo

1. Escolha o domínio cuja tabela, grupo de ops ou plano o módulo atende. Um módulo que
   atende todos os planos (um helper de ClickHouse, uma regra de hashing) fica em `core/`; um que
   atende dois domínios fica naquele cujas linhas ele lê, e o outro o chama.
2. Escolha a camada pelo que o módulo faz, não por quem o chama: I/O ou um `async def`
   que aguarda algo vai em `app/` ou `store/`; uma função dos seus argumentos vai em `domain/`.
3. Nomeie-o pelo que ele contém dentro do seu pacote, sem repetir o caminho:
   `server/erasure/app/saga.py`, não `server/erasure/app/erasure_saga.py`.
4. O `__init__.py` de um pacote contém uma docstring de uma linha dizendo o que o pacote possui. Ele
   só reexporta nomes quando é a superfície pública do pacote
   (`server/memory/store/`, `server/code/store/index/`).
5. Um loop em segundo plano é um módulo em `<domain>/workers/` mais uma linha em
   `server/workers/registry.py` com o caminho completo do módulo.
6. Um alvo de mock ou monkeypatch, uma linha de registry e um piso de cobertura nomeiam módulos por
   caminho; escreva-os com o caminho real do módulo, nunca por meio de um reexport.
7. Encontre os arquivos que o servidor distribui (migrations, `schema.sql`, o build do frontend) por meio de
   `server.core.paths`, nunca contando os pais de `__file__`
   (`tests/unit/test_repo_paths.py`).

<a id="extractor-modules"></a>

## Módulos do extrator

Os arquivos listados em `extractor_stamp.STAGE_MODULES` são o carimbo de versão do índice de código. O carimbo calcula o hash do nome de cada arquivo e dos seus tokens de código, e um import conta como os nomes que ele vincula, não como o pacote de onde vêm. Um carimbo alterado faz o índice de todo namespace de código ser derivado novamente. Esses arquivos ficam em `server/code/domain/extract/`, junto com `extractor_stamp.py`, que os lê do próprio diretório, e mantêm seus nomes de arquivo. Os módulos das linguagens ficam ao lado deles. Mover um deles entre pacotes não altera o carimbo; renomeá-lo, reformatá-lo (`ruff format` os exclui) ou mudar seu código altera.

<a id="related"></a>

## Relacionados

- [Visão geral do sistema](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md) — os componentes do plano de controle e o fluxo de requisições.
- [Arquitetura do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/architecture/code-index-architecture.md) — o stamp do extrator e a reindexação.
- [Caminho da requisição](https://github.com/ClickHouse/loom/blob/main/docs/architecture/system-overview.md#request-path) — o caminho que uma requisição percorre pelo transporte, pelo registro e por um handler.
