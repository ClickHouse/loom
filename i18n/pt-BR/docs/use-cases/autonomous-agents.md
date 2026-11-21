<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=f7fe7a00d903f07102f0cb26de5dafd7f68efe8b model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# Memória, recall, auditoria e exclusão para agentes autônomos

O Loom dá aos agentes autônomos uma memória que podem recuperar depois, um registro de auditoria de quem escreveu o quê e a exclusão de um escopo em todos os lugares em que o Loom o mantém. Essas são operações do servidor, armazenadas no ClickHouse (ou no Postgres, para a memória ativa e o estado do conhecimento).

<a id="what-you-get"></a>

## O que você recebe

- **Memória e recall.** Fatos versionados com proveniência, busca que se abstém quando nada é próximo o bastante e um reforço no ranking para memórias nas quais execuções bem-sucedidas se apoiaram. [Memória](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Execuções](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Procedimentos.** Passo a passos reutilizáveis que o agente aprende ao executar: `procedure.add` armazena um como um grafo de etapas, `procedure.match` encontra os que se encaixam em uma tarefa, e as execuções que o agente reporta ensinam ao Loom o caminho comprovado e as etapas que falham. Um grafo revisado só é mantido se suas execuções não tiverem resultado pior.
  [Procedimentos e aprendizados](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)
- **Recusa.** Um fato fixado recusa gravações que o contradigam, e `blacklist.add` registra um padrão que nenhuma gravação pode armazenar. [Conflitos e recusa](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **Auditoria.** Cada operação que altera a memória deixa uma linha de auditoria com o SHA-256 de seu conteúdo, e um worker sela janelas de uma hora em uma cadeia de hashes, de modo que uma edição ou exclusão pode ser detectada. As gravações de auditoria do transporte são fail-open (uma inserção que falha perde a linha); sob a autoridade do Postgres, a linha de auditoria de uma revisão de memória é armazenada junto com a revisão.
  [Log de auditoria](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **Exclusão verificável.** `erasure.request` apaga um escopo de todas as tabelas que guardam conteúdo de memória (tombstones por padrão; `purge=true` exclui as linhas de um escopo `user_id` ou `namespace_list`) e termina com um censo de resíduos e um recibo.
  [Retenção e exclusão](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **Rollback.** `namespace.rollback` devolve cada memória de um namespace à sua versão em um determinado momento.
  [Histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **Operação headless.** Um bot recebe sua própria identidade (ele mesmo se cadastra onde o autocadastro está aberto), verifica sua conexão antes da primeira execução e pode ser diagnosticado quando fica em silêncio.
  [Bots headless](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

Auditoria e exclusão são primitivas de engenharia, não uma certificação de conformidade.
[Segurança](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

Algumas operações desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
