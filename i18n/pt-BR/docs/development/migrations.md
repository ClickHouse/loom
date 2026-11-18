<!-- i18n: source=docs/development/migrations.md blob=bbea754e8750b1abe4dfb51af0636ead2246778b model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/migrations.md), which is authoritative.

<a id="migrations"></a>

# Migrações

Esta página reúne as regras para escrever uma migração. Como um deployment as aplica, e também os passos de atualização e rollback, estão em [atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#how-migrations-run). O arquivo [`schemas/README.md`](https://github.com/ClickHouse/loom/blob/main/schemas/README.md) descreve o diretório `schemas/` e lista as ferramentas do operador.

<a id="the-journal-and-the-ledger"></a>

## O journal e o ledger

- O schema é um journal de arquivos SQL numerados em `schemas/migrations/`, aplicados na
  ordem do prefixo numérico por `server/core/schema/migrate.py`. `000_migrations.sql` cria o ledger;
  `016_baseline.sql` é o schema inicial; todo arquivo posterior é um delta.
- Cada arquivo aplicado é registrado em `loom._migrations` com o SHA-256 do seu conteúdo.
  Uma nova execução aplica apenas os arquivos novos. Um arquivo alterado é um **checksum drift** e um arquivo que o
  repositório não tem mais é uma **linha só no ledger**; `loom migrate --dry-run --strict`
  (uma etapa de CI e o preflight de deploy) falha em qualquer um dos dois casos. Um arquivo renomeado com o texto
  inalterado não é uma linha só no ledger: o SHA-256 o identifica, e ele é registrado com o novo
  nome sem ser executado de novo.
- Todo arquivo endereça `loom.<table>` e `loom_telemetry.<table>`. O runner os mapeia
  para `LOOM_CH_DATABASE` e `LOOM_CH_TELEMETRY_DATABASE` ao enviar cada instrução
  (`server/core/ch_names.py`), de modo que o checksum é calculado sobre o arquivo como foi escrito. Um banco de dados
  é comparado por valor como `currentDatabase()` ou um nome associado (bound), nunca com um literal `'loom'`
  (`tests/unit/test_ch_names.py`). Um arquivo que altera a chave ou o TTL de uma tabela cujo
  nome começa com `_`, ou que renomeia ou troca uma delas, também atualiza sua linha em [layout de
  armazenamento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-layout.md) e o reshape guard de
  `tests/unit/test_storage_layout_rows.py`.

<a id="numbering"></a>

## Numeração

Um arquivo é nomeado como `NNN_description.sql`. O runner ordena pelo prefixo inteiro, não pelo nome (`server/core/schema/journal.py`, `migration_order`), então `1030_x.sql` é executado depois de `220_y.sql`. Pegue o próximo número livre, verificando `main` e os pull requests abertos, e mantenha-o: dois arquivos com o mesmo número ainda são aplicados, mas na ordem lexical dos seus sufixos, que não é a ordem que ninguém pretendia, e um rebase que renomeia um deles não reaplica nada. `tests/unit/test_migration_numeric_order.py` falha quando há um número compartilhado.

<a id="rules-for-writing-a-migration"></a>

## Regras para escrever uma migração

- **Nunca edite nem exclua uma migração já mergeada.** Os ledgers dos deployments guardam o hash dela. Uma correção é um novo arquivo.
- **Apenas aditivo.** Adicione tabelas, colunas, índices, projeções e views; remova um skip index ou uma projeção que nenhum código em `server/`, `scripts/`, `bench/` ou `dev/` cite (o build antigo perde uma otimização, nunca uma coluna); altere codecs, valores padrão ou configurações de tabela; amplie tipos; estenda um enum. Nunca remova nem renomeie uma tabela ou uma coluna, e nunca troque tabelas entre si (exchange). `tests/unit/test_migrations_additive.py` garante isso. Entre views e tabelas, ele permite apenas `DROP VIEW IF EXISTS` de uma materialized view de tempo de inserção e `DROP TABLE IF EXISTS` de uma tabela substituída, cada um quando nenhum arquivo em `server/`, `scripts/`, `bench/` ou `dev/` a cita: nem um build em produção nem uma ferramenta do operador (que rodam contra o serviço de produção) a lê. Restringir um tipo é uma questão de revisão, pois uma regex não consegue distingui-lo de uma ampliação.
- **Retire uma coluna em dois releases.** Primeiro publique um release cujo código não leia nem escreva mais a coluna; só a remova quando nenhum build em produção a citar.
- **Todo escritor carrega uma nova coluna.** Adicionar uma coluna a `loom.memory` ou `loom.shared` significa atualizar, na mesma mudança, os construtores de linhas em `server/memory/store/rows.py` (`build_memory_version_row`, `build_embedding_row`) e toda projeção que os alimenta: são tabelas `ReplacingMergeTree(version)` lidas com `FINAL`, e uma linha de versão que omite uma coluna a zera silenciosamente para todos os leitores. Uma nova coluna de `loom.memory` também deve ser adicionada a `loom.memory_versions` e ao `SELECT` da view `memory_versions_mv` na mesma migração, ou o histórico deixa de registrá-la (`tests/unit/test_memory_versions_schema.py` compara as listas de colunas).
- **Uma tabela que pode guardar conteúdo de memória** também precisa de uma etapa na saga de erasure e de uma linha no residue-census (`server/erasure/store/census.py`, `_CENSUS`); `tests/unit/test_every_table_is_classified.py` falha diante de uma nova tabela que não esteja em nenhuma lista ([adicionando uma tabela](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-a-table.md)).
- **DDL portável entre engines.** Declare colunas de vetor como `Array(BFloat16) CODEC(NONE)`; onde o codec `Quantized` puder ser usado, o runner as cria já com o codec ([configurações com gate](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md#gated-clickhouse-settings)). Use nomes de engine `MergeTree` simples, que o ClickHouse Cloud executa como `SharedMergeTree`.

**Por que aditivo.** O build que estava em produção antes de uma migração rodar precisa continuar lendo e escrevendo todas as tabelas que conhecia depois dela. Isso faz de um rollback blue/green uma troca de proxy em vez de um rollback de schema, e permite que a cor antiga continue atendendo enquanto a nova aplica as migrações na inicialização. O preço é pago aqui: dois releases para retirar uma coluna e rebuilds em vez de `ALTER` para mudanças de chave.

<a id="runner-directives"></a>

## Diretivas do runner

Um arquivo de migração pode conter diretivas em comentário que o runner lê:

| Diretiva                            | Efeito                                                                                                      |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `-- loom: tolerate-error <code>`    | o arquivo sobrevive a um erro nomeado do servidor (por exemplo, uma configuração que só alguns servidores aceitam) |
| `-- loom: rebuild-if-empty <table>` | declara uma chave de ordenação ou de partição alvo para `<table>` (abaixo)                                  |
| `-- loom: baseline covers N`        | o arquivo substitui todo arquivo numerado N ou menos que o repositório não mantém mais                      |

**Baselines.** Um baseline recebe o número N, posterior ao do arquivo mais novo que ele substitui. Um banco de dados novo o executa. Um banco de dados cujo ledger contém o arquivo N o registra sem executá-lo e mantém as linhas substituídas, que o modo strict relata como superseded (substituídas) em vez de erros. Assim, um squash não exige nenhuma edição manual do ledger, e um build anterior a ele ainda encontra todos os seus próprios arquivos aplicados. Um banco de dados cujo ledger contém alguns arquivos substituídos, mas não o arquivo N, parou no meio do caminho: `loom migrate` o recusa (`stranded`) e não altera nada, pois executar o baseline pularia os arquivos que faltam e registrá-lo afirmaria o schema deles. Aplique os arquivos que faltam com um build anterior ao squash e execute a migração de novo. Faça o squash em uma release, não a cada mudança; o squash gera o baseline a partir de `schemas/schema.sql` (o corpo do dump sem os objetos que os arquivos mantidos criam) e o valida em um servidor novo, em um ledger totalmente migrado e em um ledger parcial.

O runner também adapta instruções no momento da aplicação, sem alterar os arquivos nem seus checksums: remove a flag experimental de NLP onde o servidor a proíbe, cria colunas de vetor com o codec `Quantized` onde ele pode ser usado, removendo um codec geral encadeado depois dele onde `allow_suspicious_codecs` não pode ser ativado (`server/core/schema/vector_codec_ddl.py`), cria todas as views sob o usuário que executa a migração (`DEFINER = CURRENT_USER`: o dump nomeia seu próprio usuário, que outro servidor pode não ter) e espera todas as mutações que um arquivo emite antes de registrá-lo (`LOOM_MIGRATE_MUTATION_WAIT_S`).

**Pré-verificações.** Um arquivo que não deve ser executado enquanto os dados contiverem algo que só o arquivo não consegue enxergar registra uma verificação em `server/core/schema/prechecks.py`, indexada pelo nome dele sem o número, de modo que um arquivo renumerado a mantém. O runner executa a verificação antes do arquivo, inclusive em um dry run; uma verificação que recusa interrompe a execução com sua mensagem e não executa nada do arquivo. `097_mirror_names_reserved.sql` usa uma: ela recusa enquanto um namespace da forma reservada de espelho estiver registrado ou tiver linhas ativas.

<a id="changing-a-sort-key-or-a-partition-key"></a>

## Alterando uma sort key ou uma partition key

O ClickHouse não altera nenhuma das duas no lugar, então uma migração declara o destino e a tabela é reconstruída, nunca alterada:

```sql
-- loom: rebuild-if-empty code_test_facets
CREATE TABLE loom.code_test_facets ( ...every column, unchanged... ) ... ORDER BY (...);
```

Essa instrução é a única cópia do DDL de destino. O runner (`server/core/schema/table_rebuild.py`) compara a `sorting_key` e a `partition_key` da tabela em uso com ela: se coincidem, registra o arquivo; uma tabela vazia (uma instalação nova) é trocada na hora por uma gêmea com o DDL de destino; e uma tabela com linhas faz o boot falhar com uma mensagem que indica a ferramenta. Um boot nunca copia linhas. Em um deployment com dados, o operador executa a ferramenta primeiro, uma tabela por vez:

```bash
python -P -m scripts.ops.table_rebuild --table code_test_facets --dry-run
python -P -m scripts.ops.table_rebuild --table code_test_facets --yes
```

Toda ação que altera algo (`--yes` sozinho, `--rollback`, `--drop-pre-rebuild`) exibe o ClickHouse com o qual se comunica e o seu plano, e não altera nada sem `--yes`. A ferramenta copia todas as linhas (sem FINAL) para `loom.<t>_rebuild` em lotes limitados, copia as partes escritas nesse meio tempo, verifica a contagem de linhas com FINAL e a versão mais recente de uma amostra de chaves, troca com `EXCHANGE TABLES` e mantém a tabela antiga como `loom.<t>_pre_rebuild` para `--rollback` até `--drop-pre-rebuild`. Ela se recusa a fazer a troca, mantendo a gêmea, quando um `ALTER` alterou a tabela em uso durante a cópia: exit 6 para um que o ClickHouse executa como mutation (remover, renomear ou mudar o tipo de uma coluna, remover um índice, um TTL), exit 3 para um que apenas move o horário dos metadados (um índice, uma configuração, o default ou o codec de uma coluna). Uma coluna adicionada faz a cópia falhar (exit 1, sem troca), ou a execução termina com exit 3 após a última passada de verificação. Uma reconstrução não carrega um `ALTER` adiante: declare-o novamente na instrução de destino e execute de novo com `--restart`. Ela também se recusa a processar uma tabela que nenhuma migração declara.
`loom migrate --dry-run --strict` falha enquanto uma tabela declarada ainda tiver linhas no layout antigo, de modo que o preflight do deploy detecta uma etapa ignorada.

O destino mantém as colunas da tabela (para que a troca seja aditiva), e sua sort key mantém as colunas da chave antiga, reordenadas: essa chave também é aquela pela qual um `ReplacingMergeTree` faz o merge. Uma chave com outras colunas faz o merge de outras linhas e é listada com seu motivo em `tests/unit/test_migrations_additive.py`. Cite a reconstrução nas notas de versão como uma etapa do operador ([processo de release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#release-notes)).

<a id="postgres-migrations"></a>

## Migrações do Postgres

Os arquivos `schemas/pg_migrations/NNN_*.sql` seguem as mesmas regras: numerados, aditivos, nunca editados depois de mesclados, e todo `CREATE`, `ALTER` ou `DROP` de uma tabela nomeia `loom.<table>`, exceto o novo nome de um rename, em que o Postgres não aceita schema (`tests/unit/test_storage_layout_rows.py`). Uma inicialização os aplica sob um advisory lock do Postgres, cada arquivo em uma transação junto com sua linha no ledger. Uma migração espera no máximo 5 s por um lock de tabela (`lock_timeout`, com nova tentativa depois), de modo que um `ALTER` nunca enfileira todas as consultas da tabela atrás de uma transação longa. Um build opera com um ledger que contém arquivos de um build posterior, com um aviso que os nomeia; um arquivo do ledger que ele não possui, numerado igual ou abaixo do seu último arquivo, ou um cujo checksum mudou, faz a inicialização ser recusada.

Cada arquivo roda em uma única transação, então uma migração não cria índice em uma tabela grande: a criação bloquearia as escritas da tabela, com todo commit respondendo 503, até terminar. Esse índice exige `CREATE INDEX CONCURRENTLY` fora de uma transação, e uma inicialização que espera pelo advisory lock entra em deadlock com ele (uma sessão em espera mantém um snapshot que a criação aguarda terminar): uma mudança que adiciona esse arquivo também obtém o lock por meio de novas tentativas de `pg_try_advisory_lock`. Quando a primeira versão que escreve em uma tabela traz o arquivo, a tabela é pequena em qualquer lugar onde o arquivo rode, e seu índice pode ser criado na transação do arquivo (`010_revision_delivered_at.sql`).

<a id="schema-dump-and-check"></a>

## Dump e verificação do schema

`schemas/schema.sql` é o resultado do dump após aplicar todas as migrations: leia-o para entender a forma atual, nunca o edite à mão. Depois de uma mudança em uma migration, regenere-o em um ClickHouse novo da versão que o CI testa (`clickhouse/clickhouse-server:latest`):

```bash
bash dev/ci_start_clickhouse.sh     # that image with Loom's server config
make migrate
make schema-dump                    # rewrites schemas/schema.sql only when the dump differs
make schema-check                   # exit 1 with the head of the diff; writes nothing
```

Um dump que falha deixa o arquivo como estava. Outra versão do ClickHouse, um servidor em que o codec `Quantized` ou sua cadeia `LZ4` não pode ser ativado, ou um banco com gêmeas de rebuild produzem um dump diferente, e é por isso que o container fixado importa. Tabelas internas (`_migrations`, nomes que começam com `_` ou `.`) ficam de fora. O job `schema-check` do CI reexecuta todas as migrations em um servidor novo, roda o dry run estrito e depois `make schema-check`.

O CI segue o `latest`, então uma nova versão do ClickHouse que imprima o DDL de forma diferente deixa o `schema-check` vermelho sem nenhuma mudança no Loom. Leia o diff que o job imprime: quando for apenas formatação, execute os quatro comandos acima (eles baixam a nova versão) e faça o commit de `schemas/schema.sql` isoladamente. Uma mudança de significado (um tipo, um valor padrão, uma configuração de engine) é uma mudança de comportamento do ClickHouse que deve ser investigada antes de sobrescrever o dump; `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:A.B.C.D` executa os comandos acima na última versão que gerou o dump sem diferenças, para comparação.

<a id="checklist"></a>

## Lista de verificação

1. Use o próximo número livre; escreva o arquivo de forma aditiva.
2. Atualize os row builders, `loom.memory_versions`, o censo de remoção e os casos do analisador, se a mudança afetar algum deles.
3. Execute `make migrate` em um ClickHouse local, depois `make schema-dump` e faça o commit de `schemas/schema.sql` junto com a migração.
4. Execute `make test-unit` (os testes de aditividade, de numeração e de classificação do schema) e a suíte de integração para os caminhos que leem o novo formato.
5. Anote qualquer etapa do operador (uma reconstrução, um backfill) para as notas de versão.

<a id="related"></a>

## Relacionados

- [Atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md): como o runner aplica arquivos em um deployment
- [ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/architecture/clickhouse.md)
- [Referência do schema](https://github.com/ClickHouse/loom/blob/main/docs/reference/schema.md)
