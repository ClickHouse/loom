<!-- i18n: source=docs/guides/query-from-postgres.md blob=c49542f7fffa2ee2ced68d49d8ab0829854132f8 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/query-from-postgres.md), which is authoritative.

<a id="query-loom-from-postgres"></a>

# Consultar o Loom a partir do Postgres

Este guia lê as memórias, as conversas e o uso de um tenant a partir do Postgres da própria organização, onde podem ser combinados com as tabelas da aplicação. Ele usa a extensão de código aberto [`pg_clickhouse`](https://github.com/ClickHouse/pg_clickhouse), que importa tabelas e views do ClickHouse como foreign tables do Postgres e delega as consultas ao ClickHouse (pushdown). O que as views contêm e o que deixam de fora está na [referência da superfície SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md).

Os dados permanecem no ClickHouse: uma consulta a partir do Postgres lê as views no momento da consulta, sob as mesmas regras de escopo, de retenção e de atualização de qualquer outro cliente SQL. Este Postgres é o da aplicação, não a [autoridade de memória](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) do Loom: a extensão lê, o Loom nunca escreve por meio dela, e o banco de dados do próprio Loom não precisa da extensão. Use um banco de dados separado, ou um serviço separado, do que o `LOOM_PG_DSN` do Loom indica.

O CI testa a versão que `LOOM_PGCH_VERSION` indica em `.github/workflows/ci.yml` no Postgres 16 e 18 (`tests/integration/test_pg_clickhouse_live_ch.py`).

<a id="1-make-a-sql-user-for-the-tenant"></a>

## 1. Criar um usuário SQL para o tenant

No lado do Loom, como operador ([acesso](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)):

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
```

Ele exibe a senha do usuário uma única vez.

<a id="2-install-the-extension"></a>

## 2. Instalar a extensão

A extensão está nas imagens `ghcr.io/clickhouse/pg_clickhouse:<postgres major>-<version>` e no [registro PGXN](https://pgxn.org/dist/pg_clickhouse/), para compilar em um Postgres autogerenciado (`pgxn install pg_clickhouse`). Um serviço de Postgres gerenciado precisa fornecê-la e permiti-la. Três papéis participam:

- um superusuário para esta etapa, já que a extensão não é marcada como trusted (em um serviço gerenciado, o papel que o provedor autoriza a criar as extensões permitidas);
- um owner para as etapas 3 e 4: um papel com `CREATEROLE`, `CREATE` no banco de dados e `USAGE` no foreign data wrapper, que cria o papel de leitura e por isso pode definir seus padrões;
- o papel de leitura da aplicação (`app_reader` aqui) para a etapa 5, que precisa do user mapping da etapa 3 e das permissões ao final da etapa 4.

Como superusuário:

```sql
CREATE EXTENSION pg_clickhouse;
CREATE ROLE app_owner LOGIN CREATEROLE PASSWORD '<password>';
GRANT CREATE ON DATABASE app TO app_owner;
GRANT USAGE ON FOREIGN DATA WRAPPER clickhouse_fdw TO app_owner;
```

Como owner:

```sql
CREATE ROLE app_reader LOGIN PASSWORD '<password>';
```

<a id="3-point-it-at-the-clickhouse-service"></a>

## 3. Apontar para o serviço ClickHouse

```sql
CREATE SERVER loom_ch FOREIGN DATA WRAPPER clickhouse_fdw
  OPTIONS (driver 'http', host 'abc123.us-east-1.aws.clickhouse.cloud', dbname 'loom');

-- The import in step 4 connects as the role that runs it; the queries in step 5 as theirs.
CREATE USER MAPPING FOR CURRENT_USER SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
CREATE USER MAPPING FOR app_reader SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
```

- `dbname` é o banco de dados do Loom (`LOOM_CH_DATABASE`, `loom` por padrão).
- Em um host do ClickHouse Cloud, o padrão `secure 'auto'` da extensão ativa o TLS e a porta padrão do driver `http` é 8443, como diz a documentação dela; o teste aqui executa HTTP simples contra um servidor local. Em um servidor autogerenciado, defina `port` e `secure 'on'` de acordo com a porta TLS do servidor.
- Mapeie apenas os papéis do Postgres que podem ler os dados do tenant: o user mapping guarda a senha do ClickHouse, e toda consulta feita por meio do servidor é executada como esse usuário SQL.

Defina as configurações de sessão da extensão no papel, para que toda sessão envie as quatro que um usuário SQL da superfície pode alterar e nada além delas:

```sql
ALTER ROLE app_reader SET pg_clickhouse.session_settings =
  'join_use_nulls 1, group_by_use_nulls 1, final 1, transform_null_in 0';
```

Esses são os padrões da extensão, registrados explicitamente para que os padrões de uma versão posterior não possam acrescentar mais uma. Uma sessão que define outra configuração falha no ClickHouse com `READONLY`: o perfil do usuário SQL a recusa ([acesso](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#access)), seja qual for o que o papel diga.

<a id="4-import-the-views"></a>

## 4. Importar as views

```sql
CREATE SCHEMA loom;
IMPORT FOREIGN SCHEMA loom
  LIMIT TO (sql_v1_memories, sql_v1_memory_history, sql_v1_conversations,
            sql_v1_agent_events, sql_v1_usage_daily)
  FROM SERVER loom_ch INTO loom;
```

O primeiro `loom` é o banco de dados do ClickHouse e o segundo é o schema do Postgres. Nomeie as views com `LIMIT TO`, para que a importação fixe o conjunto `v1` e uma view `sql_v2_*` posterior não chegue sem ser pedida. Depois, permita que o papel de leitura as leia:

```sql
GRANT USAGE ON SCHEMA loom TO app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA loom TO app_reader;
```

Uma view `v1` pode ganhar colunas em uma atualização do Loom. Uma foreign table mantém as colunas com que foi importada; para obter as novas, remova o schema (e qualquer view do Postgres construída sobre as tabelas dele) e execute novamente a importação e as permissões: `DROP SCHEMA loom CASCADE; CREATE SCHEMA loom;`.

<a id="5-query-and-join"></a>

## 5. Consultar e combinar

```sql
-- The facts Loom holds about each account on the support team
SELECT a.email, m.memory_key, m.value, m.updated_at
FROM accounts AS a
JOIN loom.sql_v1_memories AS m ON m.user_id = a.loom_user_id
WHERE a.team = 'support' AND m.status = 'active'
ORDER BY m.updated_at DESC;

-- Model spend per day for the last month
SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= (now() AT TIME ZONE 'UTC')::date - 30
GROUP BY day ORDER BY day;
```

`value` é NULL quando o Loom o retém; `value_withheld` diz o motivo.

<a id="cost-and-scale"></a>

## Custo e escala

- `EXPLAIN (VERBOSE)` mostra qual parte de uma consulta é executada no ClickHouse. Um join entre uma foreign table e uma tabela local é executado no Postgres, que busca todas as linhas da view que os filtros delegados deixam passar; portanto, filtre cada view da forma mais restrita que a consulta permitir: por namespace, por período, por uma lista literal de ids.
- Um filtro delegado reduz o que o ClickHouse envia ao Postgres, não o que ele lê: cada view lê todo o escopo da permissão, e as views do ledger leem cerca de 31 dias de janela ([quanto custa uma leitura](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md#what-a-read-costs)).
- Uma instrução que ultrapassa os limites do usuário SQL (`LOOM_CH_ANALYSIS_MAX_EXECUTION_S`, `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES`) falha no ClickHouse em vez de ficar lenta, e o Postgres retorna o erro.
- Toda sessão do Postgres que consulta por meio dos mappings se autentica como o mesmo usuário SQL, e esse usuário executa no máximo `LOOM_CH_ANALYSIS_MAX_CONCURRENT` instruções por vez: dimensione o pool de sessões de leitura da aplicação abaixo desse valor, ou as instruções excedentes falham.

<a id="copies-and-erasure"></a>

## Cópias e exclusão

As foreign tables não guardam nada: toda consulta lê o ClickHouse, então uma exclusão no Loom as alcança imediatamente. Uma cópia feita neste Postgres (`CREATE TABLE … AS SELECT` a partir de uma foreign table, uma materialized view, um dump) são dados da própria organização e ficam fora da exclusão do Loom.

<a id="revoking"></a>

## Revogação

`loom sql-access revoke --user acme_bi` interrompe de uma vez toda consulta feita por meio dos user mappings: a próxima falha ao autenticar. Em seguida, como owner, `DROP SERVER loom_ch CASCADE` remove os dois user mappings e as foreign tables importadas.
