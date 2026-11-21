<!-- i18n: source=docs/development/testing.md blob=abc5f046c927795cb7b9c4d6a81dc5bc34b9cc85 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/testing.md), which is authoritative.

<a id="testing"></a>

# Testes

Em qual suíte um teste se encaixa, o que cada suíte precisa para rodar, como os testes são ignorados quando o ambiente não está disponível, quais gates o CI aplica e como regenerar o que eles fixam.

<a id="the-suites"></a>

## As suítes

| Suíte                                           | Caminho                                                              | Requer                                                                        | Executar localmente                                            | No CI                                                          |
| ----------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------- | -------------------------------------------------------------- |
| Unitários                                       | `tests/unit/`                                                        | nada externo; sem ClickHouse                                                  | `make test-unit` (também executa `pytest bench -q`)            | `unit` em partes, em ordem aleatória; `unit-coverage`, cobertura ≥ 40% |
| Harnesses de benchmark                          | `bench/` (seus arquivos `test_*`)                                    | nada externo                                                                  | `pytest bench -q`                                              | `unit-coverage`                                                |
| Integração                                      | `tests/integration/`                                                 | um ClickHouse com migrações aplicadas e um servidor em execução               | `make test-integration` com `LOOM_BASE_URL`                    | `integration`                                                  |
| Corpus do analisador                            | `tests/analyzer/`                                                    | um ClickHouse com migrações aplicadas (sem servidor)                          | `pytest tests/analyzer -q`                                     | `integration`                                                  |
| Corpus das lanes de código                      | `tests/code_lanes/`, controlado por `tests/unit/test_code_lanes_corpus.py` | nada externo                                                            | `python -m tests.code_lanes.corpus`                            | `unit`                                                         |
| Painel                                          | `frontend/src/**/*.test.tsx`                                         | Node                                                                          | `npm --prefix frontend run test` (vitest, com axe)             | `frontend-vitest`                                              |
| Painel de ponta a ponta                         | `frontend/e2e/*.spec.ts`                                             | Node, Chromium (`npx playwright install chromium`)                            | `npm --prefix frontend run e2e`                                | `frontend-e2e`                                                 |
| Hooks de plugin                                 | `integrations/*/tests/*.test.mjs`                                    | Node                                                                          | `node --test integrations/claude-code-plugin/tests/*.test.mjs` | `agent-plugins`                                                |
| Adaptadores do SDK Python cujo framework é um extra | `sdk/python/tests/`                                              | `uv`; instala cada extra de adaptador em seu próprio venv em `.venv-sdk-adapters/` | `make test-sdk-adapters`                                  | `sdk-python-adapters`                                          |
| SDK TypeScript                                  | `sdk/typescript/`                                                    | Node                                                                          | `npm --prefix sdk/typescript run check`                        | `sdk-typescript`                                               |
| Benchmarks e gates                              | `bench/*.py`, `make bench-*`                                         | um servidor com provedores **reais**                                          | [executando benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)              | nunca (o embedder do CI é falso)                               |

**Por que os benchmarks ficam fora do CI.** O CI roda com `LOOM_EMBEDDING_MODEL=fake-ngram-1024`
e `LOOM_LLM_PROVIDER=fake`, então um número de recall ali não mede nada sobre a
recuperação real, e um gate de qualidade verde com embeddings falsos é pior do que
nenhum gate. Executá-los no CI exigiria um secret de provedor pago no repositório. Os gates
(`make bench-quick`, `bench-quality-gate`, `bench-code-intel`) são executados manualmente contra um
deployment com provedores reais, antes de alterar um prompt de extração, o modelo padrão
ou o embedder.

<a id="unit-tests"></a>

## Testes unitários

- **Sem ClickHouse.** Um teste unitário usa um stub do cliente; qualquer coisa que precise da semântica real do SQL pertence à suíte de integração ou à suíte do analisador.
- **Ordem aleatória, em paralelo.** O CI executa a suíte com `-n auto -p randomly`, com um processo worker por núcleo, e imprime a seed primeiro; por isso, um teste deve passar depois de qualquer outro e ao lado de qualquer outro. [`tests/README.md`](https://github.com/ClickHouse/loom/blob/main/tests/README.md#rules-every-test-keeps)
  traz as regras e explica como reproduzir uma falha a partir da seed e do log.
- **Testes assíncronos esperam com `await asyncio.sleep(...)`**, nunca com `time.sleep`.
- **Datas do relógio real.** Uma fixture com data fixa que uma verificação de obsolescência compara com `now()` passa até que a data envelheça além do limite. Em vez disso, fixe o relógio pelo ponto de injeção de tempo do código, em vez de codificar uma data que a verificação lê em relação ao relógio real.

<a id="integration-tests"></a>

## Testes de integração

A suíte de integração roda contra um servidor ativo, do mesmo modo que o job `integration` do CI:

```bash
bash dev/ci_start_clickhouse.sh                      # or the dev stack's ClickHouse
make migrate
LOOM_INSTANCE_LABEL=dev:$USER:tests LOOM_BACKGROUND_WORKERS=off make server &
LOOM_BASE_URL=http://localhost:7777 make test-integration
```

- `tests/conftest.py` carrega o `.env` do repositório no processo de teste (preenchendo apenas as variáveis ausentes), de modo que os testes e o servidor escolham os mesmos provedores: linhas gravadas com um embedder ficam invisíveis para leituras feitas com outro.
- Cada teste recebe um token de escrita com org curinga em `LOOM_API_KEY`, a menos que você tenha exportado um (a fixture `agent` o usa), e a fixture `mint_test_token` gera um token para a org do próprio teste. Ambos são assinados com o `LOOM_JWT_SECRET` do processo de teste, que deve ser igual ao do servidor.
- Execute o servidor com `LOOM_BACKGROUND_WORKERS=off`: os testes que exercitam um worker chamam o `run_pass` dele no próprio processo, contra o namespace do teste, e os workers de um supervisor ativo não têm filtro de namespace e esvaziariam as linhas desses testes. Um worker cuja passada pega um item de uma fila compartilhada por todos os tenants, como `knowledge_ingest`, é exercitado executando o item do próprio teste como uma passada o executa (`ingest_run.take_and_run`).
- `tests/integration/test_document_object_store_live.py` também precisa de um armazenamento compatível com S3 em `LOOM_TEST_S3_ENDPOINT` (o CI executa um local, veja `.github/workflows/ci.yml`) e é ignorado sem ele.
- Os testes de publicação em `tests/integration/test_knowledge_ingest_live.py` executam o próprio job, que envia os originais a um extrator de documentos: eles precisam de um roteado para SQL e TOML (`LOOM_DOCUMENT_EXTRACTOR_URL` ou `LOOM_DOCUMENT_EXTRACTORS_JSON`, com o respectivo token; veja a [referência de configuração](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)) e são ignorados sem um.
  `tests/integration/test_knowledge_inspect_ingest.py` também verifica, por meio de `knowledge.inspect`, que o servidor tem um configurado, então dê ao servidor as mesmas configurações. O CI inicia o extrator de referência antes do servidor de cada parte (`.github/workflows/ci.yml`; [como executá-lo](https://github.com/ClickHouse/loom/blob/main/extractor/README.md)).
- `tests/integration/test_pg_authority.py` precisa de um servidor com autoridade Postgres: inicie o Postgres com `dev/ci_start_postgres.sh`, execute `make migrate` e defina `LOOM_STORAGE_AUTHORITY=postgres` e `LOOM_PG_DSN=postgres://loom:loomdev@localhost:5432/loom` (o banco do script; `LOOM_PG_HOST_PORT` muda a porta) tanto para o servidor quanto para o pytest, pois seus testes também executam o código da autoridade no processo de teste. Um Postgres iniciado com `LOOM_PG_STANDBY=0` (sem standby síncrono) precisa de `LOOM_PG_STANDBY=0` também no ambiente do pytest, para que o teste de saúde do standby seja ignorado. Contra um servidor com autoridade ClickHouse, todos os testes são ignorados, exceto os que usam apenas a API.
- `tests/integration/test_pg_clickhouse_live_ch.py` lê a [superfície SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md) de um Postgres com `pg_clickhouse`: inicie um com `dev/ci_start_pg_clickhouse.sh` (rede do host, porta 5433, portanto um host Docker Linux: o contêiner alcança o ClickHouse publicado em 127.0.0.1; `LOOM_PGCH_POSTGRES` e `LOOM_PGCH_VERSION` escolhem a imagem) e defina `LOOM_PGCH_DSN=postgres://app:appdev@127.0.0.1:5433/app` junto com os `LOOM_CH_*` desse ClickHouse. Sem eles, o teste é ignorado. Remova o contêiner depois (`docker rm -f loom-pgch`, ou o seu `LOOM_PGCH_CONTAINER`).
- O CI define `LOOM_LATENCY_BUDGET_MULTIPLIER=10`: os runners compartilhados são barulhentos o bastante para que os orçamentos padrão por op gerem violações.
- Os testes que dependem de autocadastro são ignorados no CI, que não tem `LOOM_SELF_SIGNUP=open`; prefira tokens gerados.
- **Camadas.** `tests/integration/conftest.py` registra quatro marcadores que uma passada local rápida pode excluir (`-m "not slow and not destructive"`); o CI executa o diretório inteiro, em partes (abaixo). `slow` gasta tempo real de propósito (expiração de leases, esgotamento de tentativas), `destructive` interrompe merges, executa `OPTIMIZE` ou apaga escopos inteiros, `provider` precisa de um embedder ou LLM reais e é ignorado com os falsos, `recovery` provoca uma falha e percorre o caminho de volta.
- **Partes.** `LOOM_TEST_SHARD=k/n` coleta apenas os arquivos de teste da k-ésima de n partes. Uma parte contém arquivos inteiros, então uma fixture de módulo é construída em apenas uma parte. Os arquivos são distribuídos pelos segundos que `tests/shard_weights.json` registra para cada um; um arquivo que ele não lista é distribuído pela sua contagem de funções de teste vezes os segundos registrados por função de teste, ou um segundo cada quando nada está registrado, como em todo arquivo de integração (`tests/_shards.py`). O CI divide a suíte em quatro partes, cada uma em sua própria stack, ao lado da parte dos gates de isolamento; uma falha na parte k se reproduz com `LOOM_TEST_SHARD=k/4 make test-integration`.
- **Diagnóstico de falhas.** O relatório de um teste que falha traz suas últimas 25 requisições ao Loom (método, URL, status, `X-Request-Id`, latência) com JWTs, bearer tokens e segredos ocultados, de modo que a linha de log correspondente do servidor possa ser encontrada pelo id da requisição.
- **Limpeza.** `unique_org(label)` (`tests/integration/_resources.py`) gera `it-<label>-<8 hex>`. No fim da sessão, todas as linhas de toda org gerada são apagadas de cada tabela que tenha uma coluna `org` e o resíduo é contado; resíduo faz a execução falhar. `LOOM_IT_KEEP_ROWS=1` mantém as linhas para depuração.

<a id="framework-adapters"></a>

## Adaptadores de framework

Os adaptadores do SDK cujo framework é um extra do SDK mantêm o framework fora do `uv.lock`, então
`make test-sdk-adapters` instala cada um em um venv próprio em `.venv-sdk-adapters/`
(dois frameworks podem fixar um mesmo pacote de forma incompatível) para os testes unitários deles
(`sdk/python/tests/test_<module>.py`); `make test-sdk-adapter-<extra>` executa um só. Os testes de
integração (`tests/integration/test_sdk_<framework>_*.py`) são ignorados sem o
framework; para executá-los, instale os requisitos do extra em `.venv`
(`uv pip install --python .venv/bin/python -r <(python scripts/gates/sdk_extra_requirements.py langchain)`)
e execute-os contra um servidor, como em [testes de integração](#integration-tests). `uv sync`
remove o framework novamente.

<a id="how-tests-skip"></a>

## Como os testes são ignorados

Um teste cujo ambiente está ausente é ignorado (skip) com um motivo; ele nunca falha por falta de uma variável. Use `os.environ.get(...) or pytest.skip(...)`, nunca `os.environ[...]`. As fixtures compartilhadas seguem a regra: `control_plane_url` é ignorada sem `LOOM_BASE_URL`. Um `LOOM_BASE_URL` definido que não responde 200 em `/v1/health` indica um servidor quebrado, não ausente, e faz o teste falhar com a URL e o erro.
`require_real_embedder` ignora um teste de qualidade de busca em um servidor que executa o embedder falso.
A suíte do analisador (`migrated_ch`) é ignorada, com o endereço e a correção no motivo, quando nenhum ClickHouse responde em `LOOM_CH_*` ou o destino está atrasado nas migrações, e falha sob `CI`, onde o job inicia um e executa as migrações antes.

<a id="the-analyzer-statement-corpus"></a>

## O corpus de instruções do analisador

As instruções ClickHouse do Loom são strings Python construídas por muitos builders sob muitas flags.
Stubs de unidade aceitam qualquer string, então um alias que sombreia uma coluna filtrada, uma CTE lida
sem o seu `WITH`, um alias externo usado dentro de uma subconsulta, ou um tipo de parâmetro que o driver
não consegue vincular chega a um servidor como uma perna que falha de forma aberta: linhas ausentes, e não um erro.
`tests/analyzer/` executa cada builder em todos os seus eixos, deduplica as instruções por
formato e passa cada uma por `EXPLAIN QUERY TREE` nas tabelas migradas e vazias,
o que rejeita todos esses casos em milissegundos. `shapes.txt` é a superfície de revisão versionada:
um pull request que altera o formato de uma instrução mostra o novo cabeçalho no seu diff.

```bash
pytest tests/analyzer -q                              # what CI runs
python -m tests.analyzer.corpus --write               # regenerate shapes.txt (review the diff)
python -m tests.analyzer.corpus --case erasure --errors
```

O [README do analisador](https://github.com/ClickHouse/loom/blob/main/tests/analyzer/README.md) cobre o formato dos casos, o requisito de
destino migrado e a importação de formatos de produção capturados com
`LOOM_CH_CAPTURE_STATEMENTS`.

<a id="ci-gates"></a>

## Gates de CI

A maioria dos gates são testes unitários, então `make test-unit` os executa; o restante são etapas de `make lint`.
Cada gate indica o que executar quando ele falha.

| Gate                            | Onde                                                                                  | Falha quando                                                                                                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Texto público                   | `tests/unit/test_public_copy.py`                                                      | a árvore cita um produto adjacente, uma pessoa, um host ou namespace interno, cita um artigo ou um registro de design; páginas públicas citam um concorrente                                  |
| Links da documentação           | `tests/unit/test_doc_links.py`                                                        | um link relativo ou `#anchor` no Markdown da raiz, em `docs/` ou nos READMEs de integração não resolve                                                                                         |
| Referência de configurações     | `tests/unit/test_settings_reference.py`, `settings_reference.py --check`              | a referência de configuração gerada difere das fontes de configurações, ou um campo não tem linha                                                                                              |
| Referência da documentação      | `tests/unit/test_docs_reference.py`, `docs_reference.py --check` (`make lint`)        | uma região gerada em `docs/reference/` está desatualizada, ou uma op ou um renderer não tem linha                                                                                              |
| Env padrão corresponde à doc    | `tests/unit/test_stock_env_matches_docs.py`                                           | os valores do `.env` padrão indicados no quickstart divergem de `.env.example`, ou um comando compose documentado não tem `--env-file .env`                                                    |
| Tamanho de função               | `scripts/gates/function_length_gate.py` (lint)                                        | um arquivo ganha uma função grande demais, ou uma função listada cresce além de sua linha de base                                                                                              |
| Catraca do mypy                 | `scripts/gates/mypy_gate.py` (`make typecheck`)                                       | algum arquivo tem mais erros do mypy em modo estrito do que `scripts/gates/mypy_baseline.json`                                                                                                 |
| Segredos                        | `scripts/gates/check_no_secrets.py` (lint), `tests/unit/test_no_secrets_guard.py`     | um arquivo versionado contém um valor com formato de credencial                                                                                                                                |
| Acessibilidade do painel        | `biome lint frontend/src` (`make lint`, job de CI `format`)                           | um `.tsx` do painel adiciona uma violação de acessibilidade                                                                                                                                    |
| Vazamento entre principals      | `tests/integration/test_cross_principal_leakage.py`                                   | qualquer op de leitura, executada como outro tenant com nonces canário, retorna um nonce ou um 500; os payloads são sintetizados a partir do modelo de entrada de cada op, então uma nova op de leitura é testada no dia em que chega |
| Snapshot do registro de workers | `tests/unit/test_worker_registry_snapshot.py`, `snapshots/worker_registry.json`       | o conjunto de workers habilitados, a ordem, a cadência ou o prazo mudam nos perfis `defaults`, `all_on` ou `production`                                                                        |
| Paridade do registro de workers | `tests/unit/test_worker_registry_parity.py`                                           | os workers listados por `/v1/admin/workers.status` e os executados pelo supervisor diferem                                                                                                     |
| Migrações aditivas              | `tests/unit/test_migrations_additive.py`                                              | uma migração remove ou renomeia uma tabela ou coluna, ou troca tabelas fora de uma reconstrução declarada                                                                                      |
| Números de migração             | `tests/unit/test_migration_numeric_order.py`                                          | dois arquivos de migração compartilham o mesmo número                                                                                                                                          |
| Verificação do schema           | job `schema-check`: dry run estrito, depois `make schema-check`                       | o ledger contém um arquivo que o repositório não tem, ou `schemas/schema.sql` difere de uma reexecução de todas as migrações                                                                   |
| Snapshot da superfície da CLI   | `tests/unit/test_cli_surface_snapshot.py`, `snapshots/cli_tree.json`, `cli_help.json` | qualquer parser, argumento, valor padrão ou texto de `--help` do `loom` muda                                                                                                                   |
| Censo de alcance das ops        | `tests/unit/test_op_reach_census.py`, `op_reach_census.py --check`                    | uma op não tem caminho de alcance além de HTTP puro e não é experimental, uma op não tem nem ponto de chamada no painel nem entrada em `server/operations/dashboard.py`, ou `reach_manifest.py` está desatualizado |
| Superfícies das ops             | `tests/unit/test_op_surfaces.py`                                                      | o roteamento MCP de uma op, sua recusa como op oculta ou sua entrada em `server/operations/dashboard.py` discorda de seu alcance                                                               |
| Orçamento da listagem MCP       | `tests/unit/test_mcp_listing_budget.py`                                               | o `tools/list` padrão cresce além do limite de tokens ou aponta um modelo para uma ferramenta que ele não consegue chamar                                                                      |
| Acesso às configurações         | `tests/unit/test_settings_attribute_access.py`                                        | o código lê uma configuração com `getattr(settings, "<field>", default)`                                                                                                                       |
| Cópias geradas de SDK e plugin  | `sdk_py_gen.py`, `sdk_ts_gen.py`, `agent_plugin_sync.py` `--check` (lint)             | um arquivo gerado difere de sua fonte ([geração de código](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md))                                                 |
| Formatação                      | job `format`, mais `ruff format --check` em `lint` (`make format-check` localmente)   | ruff, Biome, Prettier, Taplo ou shfmt alterariam um arquivo                                                                                                                                    |

Para regenerar um snapshot após uma mudança intencional:

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
LOOM_UPDATE_WORKER_SNAPSHOT=1 python -m pytest tests/unit/test_worker_registry_snapshot.py
python scripts/gates/function_length_gate.py --update   # only ever lowers the baselines
python scripts/gates/mypy_gate.py --update
```

Uma linha de base de catraca nunca sobe: `--update` grava apenas contagens menores, de modo que a dívida não pode migrar
entre arquivos.

<a id="the-ci-workflow"></a>

## O workflow de CI

O `.github/workflows/ci.yml` é executado em todo pull request para `main` ou para uma linha `release/X.Y`
e em pushes para qualquer uma delas, com Python 3.12 (`sdk-python-package` adiciona outras versões) e
Node 22. Um push mais novo em um pull request cancela o workflow em execução desse pull request; todo
push para `main` ou para uma linha de release é executado até o fim em seu próprio grupo de concorrência, de modo que uma
falha em um merge commit nunca é ocultada pelo seguinte. Toda action é fixada em um SHA de commit completo
(com a versão em um comentário), e o workflow concede aos seus jobs acesso somente leitura
ao repositório (`permissions: contents: read`).

| Job                    | Executa                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `clickhouse`           | resolve a versão do ClickHouse que `dev/clickhouse_release.sh` indica, para que `unit`, `schema-check` e `integration` testem a mesma                                                                                                                                                                                                                                                        |
| `lint`                 | `ruff check`, `ruff format --check`, o `--check` de `sdk_py_gen.py`, `sdk_py_client_gen.py`, `sdk_py_admin_gen.py`, `agent_plugin_sync.py`, `settings_reference.py`, `settings_schema.py`, `docs_reference.py` e `op_reach_census.py`, `mypy --strict` no SDK, o mypy ratchet, e os gates de tamanho de função e de segredos                                                                  |
| `unit`                 | `pytest tests/unit` em seis partes (`LOOM_TEST_SHARD`), cada uma em seu próprio runner, em ordem aleatória em todos os núcleos com cobertura                                                                                                                                                                                                                                                  |
| `unit-coverage`        | a cobertura das partes combinada (piso de 40%) e os pisos por módulo, depois `pytest bench` e a amostra de integração genérica (`integrations/generic/test.sh`); seu artefato `shard-weights` traz os segundos por arquivo de unit da execução, para ser commitado como `tests/shard_weights.json` quando as partes ficarem desbalanceadas                                                     |
| `sandbox`              | a suíte hostil do sandbox de jobs de repositório sob bubblewrap real, em que um skip falha (`LOOM_TEST_REQUIRE_SANDBOX=1`)                                                                                                                                                                                                                                                                  |
| `schema-check`         | um ClickHouse novo (`dev/ci_start_clickhouse.sh`), todas as migrations, o dry run estrito, `make schema-check`                                                                                                                                                                                                                                                                               |
| `ts-typecheck`         | o build do painel (`tsc -b` + Vite) e seu orçamento de bundle (`npm run budget`)                                                                                                                                                                                                                                                                                                             |
| `format`               | verificações de Biome, Prettier, Taplo e shfmt em versões fixadas, e o lint de acessibilidade do Biome em `frontend/src`                                                                                                                                                                                                                                                                     |
| `helm`                 | `helm lint --strict` e kubeconform em todo overlay, kubeconform em `deploy/okf-sync/cronjob.yaml`, os valores que o chart deve recusar, e `scripts/gates/helm_extractor_isolation.py`, `helm_metadata_egress.py` e `helm_assistant_isolation.py`                                                                                                                                              |
| `agent-plugins`        | `node --test` para os hooks dos plugins e o grep shim, a amostra de hook do Cursor (`integrations/cursor/test.sh`), uma verificação de sintaxe de todo script de hook, de biblioteca e auxiliar de `bin/` dos dois plugins e do shim, e uma verificação de que os pontos de entrada do grep shim são executáveis                                                                              |
| `frontend-vitest`      | vitest (unit, component, axe), em partes em runners separados                                                                                                                                                                                                                                                                                                                                |
| `frontend-e2e`         | Playwright, em partes em runners separados                                                                                                                                                                                                                                                                                                                                                   |
| `integration`          | partes, cada uma com seu próprio ClickHouse, migrations, extrator de referência e servidor iniciado (provedores falsos, workers desligados): as partes numeradas dividem `pytest tests/integration` (`LOOM_TEST_SHARD`), a parte dos gates de isolamento cobre os testes de staging de provedores, os gates com auth ligada e `pytest tests/analyzer`                                         |
| `integration-postgres` | as suítes de memória, conhecimento, isolamento, idempotência, apagamento e ids restaurados e os gates com auth ligada contra um servidor com `LOOM_STORAGE_AUTHORITY=postgres`, em Postgres 16 e 18 com um standby síncrono ([Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md))                                                                           |
| `mixed-version`        | `scripts/gates/mixed_version.py`: compila N-1 e N lado a lado em um mesmo ClickHouse, sendo N-1 o `HEAD~1`: em um pull request, o primeiro pai do seu merge commit, a branch de destino como estava quando o merge foi feito, e em um push, o commit anterior                                                                                                                                  |
| `sdk-typescript`       | `sdk_ts_gen.py --check`, `npm run check` (lint, tipos, versão, exemplos do README, testes), `npm run check:dist`                                                                                                                                                                                                                                                                             |
| `sdk-python-adapters`  | `make test-sdk-adapters`: para cada adapter cujo framework é um extra do SDK, em um venv próprio com esse framework, fora do `uv.lock`, seus testes e `mypy --strict` no seu módulo e nos adapters que não precisam de extra (`__init__.py`, `langgraph.py`, `strands.py`); em Python 3.12, e em 3.11 com todo requisito direto (os do próprio SDK, os do extra, pytest) no limite inferior |
| `sdk-python-package`   | compila o wheel do SDK com uv, o instala em um venv limpo e executa `scripts/gates/sdk_wheel_smoke.py`, em Python 3.11, 3.12 e 3.14, mais 3.11 e 3.14 com toda dependência declarada no limite inferior                                                                                                                                                                                      |

O `.github/workflows/plugin-platforms.yml` é executado quando uma alteração toca `integrations/`, um
marketplace de plugins, `.gitattributes` ou o próprio workflow: as suítes dos plugins e do grep shim
em Linux (x86_64, arm64, Alpine), macOS (Apple silicon, Intel), Windows (x86_64,
arm64) e WSL 2 (Ubuntu, Node 18), depois sessões reais do Claude Code e do Codex com o
plugin instalado a partir do checkout (`tests/e2e/agent-session.mjs` sob o plugin do Claude Code:
um falso local responde tanto à API do modelo quanto ao Loom), também com `node` fora do
`PATH` do agente nos runners Ubuntu e macOS, sem nenhum `node` (todo hook falha
sem bloquear o turno) nos runners Ubuntu, macOS e Windows, e sem Git Bash
no Windows.

O `.github/workflows/codeql.yml` executa o CodeQL em pull requests para `main` ou uma linha `release/X.Y`,
em pushes para elas, semanalmente e sob demanda: uma verificação `codeql (<language>)` para cada um de
Python, JavaScript/TypeScript e os workflows. Seus achados são alertas de code scanning
(a aba Security do repositório), triados por um mantenedor; as verificações não bloqueiam nenhum merge. Os
caminhos que ele deixa de fora, e por quê, estão no cabeçalho do workflow.

O `.github/workflows/scorecard.yml` executa as verificações do OpenSSF Scorecard em pushes para `main`,
semanalmente e sob demanda. Ele publica a pontuação que o badge do README mostra e adiciona os achados
ao code scanning; não é executado em pull requests e não bloqueia nenhum merge.

O `.github/workflows/docs-site.yml` compila o site de documentação com `mkdocs build --strict`
em pull requests para `main` que tocam `docs/`, `mkdocs.yml`, `llms.txt`,
`llms-install.md`, `scripts/docs_site/`, `scripts/codegen/page_summary.py` ou o próprio
workflow, e em todo push para `main`, de modo que um link ou âncora quebrado faz a verificação
`docs site / build` falhar. Ele publica o site no GitHub Pages somente a partir de `main` e somente enquanto a
variável de repositório `DOCS_SITE_ENABLED` for `true`.

Um pull request empilhado sobre outra branch não recebe verificações até ser redirecionado para `main` ou
uma linha `release/X.Y`, e um pull request com conflito de merge também não recebe nenhuma: "no checks
reported" após um push geralmente significa um dos dois casos.

<a id="related"></a>

## Relacionados

- [Desenvolvimento local](https://github.com/ClickHouse/loom/blob/main/docs/development/local-development.md), [geração de código](https://github.com/ClickHouse/loom/blob/main/docs/development/code-generation.md),
  [migrações](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md)
- [Metodologia de benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)
