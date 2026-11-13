<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Ejecuciones de agentes, trazas e historial de PR

Loom registra el trabajo que realizan los agentes de programación, como ejecuciones y conversaciones de sesión en las que se puede buscar, y lo conecta con el historial de tu código. Las ejecuciones incluyen objetivos y resultados, y costos de modelo cuando el plugin de Claude Code informa del uso por turno de la sesión (activado por defecto). El historial abarca los pull requests fusionados, los comentarios de revisión, los issues de GitHub y, con una fuente de tracker, los incidentes y las ejecuciones de CI. El registro de conversaciones, el historial de PR y el tracker están desactivados hasta que los actives. Así, un agente o una persona puede preguntar qué se probó antes, qué se decidió y qué cambio introdujo una regresión.

<a id="what-you-get"></a>

## Qué obtienes

- **Runs.** `run.begin` y `run.complete` registran el objetivo de una tarea, su estado, las memorias en las que se apoyó el agente y lo que escribió. [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Conversaciones y resúmenes de sesión.** Las sesiones grabadas permiten búsqueda, y un resumen de sesión permite que la siguiente sesión continúe donde quedó la anterior. La grabación está desactivada hasta que el propietario del namespace la activa con `conversation.set_logging(enabled=true)`. [Conversaciones y sesiones](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Traza y reanuda ejecuciones largas de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **Historial de PR.** Historial de un símbolo, una ruta o una línea, resúmenes de revisión, una comprobación de duplicados previa al PR y candidatos para bisect, como se describe en la [página del índice de código](code-intelligence.md).
- **Registros del tracker.** Comprobaciones de incidencias duplicadas, vínculos entre incidencias y PR, y tests inestables (flaky), leídos en vivo desde el propio ClickHouse del equipo. Requiere `LOOM_TRACKER_ORG` y una fuente de tracker registrada. [Tracker plane](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **Trazas y costes.** Loom emite OpenTelemetry y registra su propio gasto en modelos por operación; Langfuse y ClickStack son destinos opcionales.
  [Telemetría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Costes y uso](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## Medición

En issues públicos de ClickHouse, `tracker.duplicate_check` colocó primero el issue más antiguo en 91
de 104 duplicados registrados (0.875) y marcó como duplicado 2 de 100 issues sin ningún vínculo registrado. El resultado corresponde a un solo tracker, y sus issues (stack traces, códigos de error, duplicados vinculados de forma consistente) son más fáciles que los de muchos otros trackers.
[Duplicate check on public ClickHouse issues](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## Inicio

[Conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) instala los
plugins que capturan sesiones una vez activada la grabación; el [índice de código](code-intelligence.md)
requiere que se haya añadido un repositorio y `LOOM_CODE_INGEST_ENABLED=true`.

Algunas ops de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
