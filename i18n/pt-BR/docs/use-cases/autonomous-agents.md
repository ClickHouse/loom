<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# Memória, recall, auditoria e apagamento para agentes autônomos

O Loom dá a agentes autônomos uma memória que podem recuperar depois, um registro de auditoria de quem escreveu o quê e o apagamento de um escopo em todos os lugares onde o Loom o mantém. Essas são operações do servidor, armazenadas no ClickHouse (ou no Postgres, para a memória ativa e o estado de conhecimento).

<a id="what-you-get"></a>

## O que você recebe

- **Memória e recall.** Fatos versionados com procedência, busca que se abstém quando
  nada está próximo o suficiente e um reforço de ranqueamento para memórias nas quais
  execuções bem-sucedidas se apoiaram. [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Recusa.** Um fato fixado recusa gravações que o contradigam, e `blacklist.add` registra um
  padrão que nenhuma gravação pode armazenar. [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **Auditoria.** Cada operação que altera a memória deixa uma linha de auditoria com um SHA-256 do seu
  conteúdo, e um worker sela janelas de uma hora em uma cadeia de hashes, de modo que uma edição ou exclusão é
  detectável. As gravações de auditoria do transporte são fail-open (uma inserção que falha perde a linha);
  na autoridade Postgres, a linha de auditoria de uma revisão de memória é armazenada junto com a revisão.
  [Audit log](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **Eliminação verificável.** `erasure.request` elimina um escopo de todas as tabelas que guardam
  conteúdo de memória (lápides por padrão; `purge=true` exclui as linhas de um escopo `user_id` ou
  `namespace_list`) e termina com um censo de resíduos e um recibo.
  [Retention and erasure](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **Rollback.** `namespace.rollback` retorna cada memória de um namespace à sua versão
  em um determinado momento.
  [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Operação headless.** Um bot recebe sua própria identidade (ele mesmo se cadastra onde o autocadastro
  está aberto), verifica sua conexão antes da primeira execução e pode ser diagnosticado quando fica
  em silêncio.
  [Headless bots](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

Auditoria e eliminação são primitivas de engenharia, não uma certificação de conformidade.
[Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
