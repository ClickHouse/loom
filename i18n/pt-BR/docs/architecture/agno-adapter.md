<!-- i18n: source=docs/architecture/agno-adapter.md blob=3cda341b0d2ad39a80b6546d3002b103a90de53d model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/agno-adapter.md), which is authoritative.

<a id="agno-user-memory-database"></a>

# Banco de dados de memória de usuário do Agno

O adaptador do Agno implementa os métodos de memória de usuário da sua interface `BaseDb` 3.2
por meio do SDK síncrono do Loom. A dependência opcional, fixada na versão minor, fica fora
do SDK principal e do servidor. O `MemoryManager` aceita esse banco de dados independentemente
do banco de dados de sessão de um agente. Capacidades que não envolvem memória são recusadas
em vez de usar um armazenamento padrão do framework.

<a id="invariants"></a>

## Invariantes

Cada adaptador vincula um usuário e um armazenamento da aplicação. Toda chamada ao SDK carrega
esse usuário e uma sessão derivada; as chaves incluem o mesmo digest de escopo mais um digest
do id nativo. Um usuário explícito diferente é recusado. As memórias de usuário nativas são
JSON em linhas episódicas do Loom, preservando ids, metadados e revisões sem mesclagem
semântica de registros nativos diferentes. A procedência de ator e de origem vem do caminho
normal de escrita do Loom. Aplicam-se as regras comuns de exclusão de memória, apagamento e
esquema contínuo.

A listagem segue páginas limitadas e projetadas, remove duplicatas de sobreposição e hidrata
valores truncados antes de convertê-los em objetos nativos. Filtros de conteúdo, tópicos e
atribuição, ordenação, paginação e estatísticas são executados sobre esse snapshot de
usuário/armazenamento com escopo definido. Não há estado persistente local. Edições
concorrentes mantêm a última escrita do servidor; operações em lote são uma sequência de
escritas e podem terminar parcialmente em caso de falha. Concessões de namespace isolam
aplicações que não confiam umas nas outras.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `sdk/python/loom/integrations/agno.py`: `LoomMemoryDb`.
- `sdk/python/tests/test_agno.py`: comportamento nativo do gerenciador de memória.
- `tests/integration/test_sdk_agno_memory.py`: escopo de memória e procedência em ambiente real.
