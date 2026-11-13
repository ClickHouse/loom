<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Execuções de agentes, traces e histórico de PRs

O Loom registra o trabalho feito por agentes de código, como execuções e conversas de sessão pesquisáveis, e o conecta ao histórico do seu código. As execuções carregam objetivos e resultados, além de custos de modelo quando o plugin do Claude Code informa o uso de turnos da sessão (ativado por padrão). O histórico cobre pull requests mescladas, comentários de revisão, issues do GitHub e, com uma fonte de tracker, incidentes e execuções de CI. O registro de conversas, o histórico de PRs e o tracker ficam desativados até serem ativados. Um agente ou uma pessoa pode então perguntar o que já foi tentado, o que foi decidido e qual mudança introduziu uma regressão.

<a id="what-you-get"></a>

## O que você recebe

- **Execuções.** `run.begin` e `run.complete` registram o objetivo de uma tarefa, o status, as memórias em que o agente se apoiou e o que ele escreveu. [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Conversas e resumos de sessão.** As sessões gravadas são pesquisáveis, e um resumo de sessão permite que a próxima sessão continue o trabalho. O registro fica desativado até o proprietário do namespace ativá-lo com `conversation.set_logging(enabled=true)`. [Conversations and sessions](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Trace and resume long agent runs](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **Histórico de PRs.** Histórico de um símbolo, caminho ou linha, resumos de revisão, verificação de duplicidade antes do PR e candidatos a bisect, como na [página do índice de código](code-intelligence.md).
- **Registros do tracker.** Verificações de issues duplicadas, vínculos entre issue e PR e testes instáveis, lidos em tempo real do ClickHouse da própria equipe. Requer `LOOM_TRACKER_ORG` e uma fonte de tracker registrada. [Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **Traces e custo.** O Loom emite OpenTelemetry e registra o próprio gasto com modelos por operação; Langfuse e ClickStack são destinos opcionais.
  [Telemetry](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Cost and usage](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## Medições

Em issues públicas do ClickHouse, `tracker.duplicate_check` colocou a issue mais antiga em primeiro lugar em 91 de 104 duplicatas registradas (0.875) e classificou como duplicata 2 de 100 issues sem vínculo registrado. O resultado é de um único tracker, e suas issues (stack traces, códigos de erro, duplicatas vinculadas de forma consistente) são mais fáceis do que as de muitos trackers.
[Duplicate check on public ClickHouse issues](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## Primeiros passos

[Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) instala os plugins que capturam as sessões depois que o registro é ativado; o [índice de código](code-intelligence.md) exige que um repositório seja adicionado e `LOOM_CODE_INGEST_ENABLED=true`.

Algumas ops desta página estão marcadas como experimentais na [página de status](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
