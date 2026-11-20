<!-- i18n: source=docs/use-cases/runs-and-pr-history.md blob=8a6ae75f4719bf82572f8fbb0496949da479d685 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/runs-and-pr-history.md), which is authoritative.

<a id="agent-runs-traces-and-pr-history"></a>

# Ejecuciones de agentes, trazas e historial de PR

Loom registra el trabajo que hacen los agentes de código, como ejecuciones y conversaciones de sesión con búsqueda, y lo conecta con el historial de tu código. Las ejecuciones incluyen objetivos y resultados, y los costos de los modelos cuando el plugin de Claude Code informa del uso por turno de la sesión (activado por defecto). El historial abarca los pull requests fusionados, los comentarios de revisión, los issues de GitHub y, con una fuente de tracker, los incidentes y las ejecuciones de CI. El registro de conversaciones, el historial de PR y el tracker están desactivados hasta que se activan. Después, un agente o una persona puede preguntar qué se intentó antes, qué se decidió y qué cambio introdujo una regresión.

<a id="what-you-get"></a>

## Qué obtienes

- **Ejecuciones.** `run.begin` y `run.complete` registran el objetivo de una tarea, su estado, las memorias en las que se apoyó el agente y lo que escribió. [Ejecuciones](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **Conversaciones y resúmenes de sesión.** Las sesiones registradas admiten búsqueda, y un resumen de sesión permite que la siguiente sesión continúe. El registro está desactivado hasta que el propietario del espacio de nombres lo activa con `conversation.set_logging(enabled=true)`. [Conversaciones y sesiones](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md) ·
  [Trazar y reanudar ejecuciones largas de agentes](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)
- **Historial de PR.** Historial de un símbolo, una ruta o una línea, resúmenes de revisión, una comprobación de duplicados previa al PR y candidatos para bisect, como en la [página del índice de código](code-intelligence.md).
- **Registros del tracker.** Comprobaciones de issues duplicados, vínculos entre issues y PR y tests inestables, leídos en vivo desde el ClickHouse del propio equipo. Requiere `LOOM_TRACKER_ORG` y una fuente de tracker registrada. [Plano del tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/overview.md)
- **Trazas y costo.** Loom emite OpenTelemetry y registra su propio gasto en modelos por operación; Langfuse y ClickStack son destinos opcionales.
  [Telemetría](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md) ·
  [Costo y uso](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)

<a id="measured"></a>

## Medición

En los issues públicos de ClickHouse, `tracker.duplicate_check` colocó primero el issue más antiguo en 91 de 104 duplicados registrados (0.875) y marcó como duplicados 2 de 100 issues sin vínculo registrado. El resultado corresponde a un solo tracker, y sus issues (trazas de pila, códigos de error, duplicados vinculados de forma consistente) son más fáciles que los de muchos otros trackers.
[Comprobación de duplicados en issues públicos de ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

<a id="start"></a>

## Primeros pasos

[Conectar un agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) instala los plugins que capturan las sesiones una vez activado el registro; el [índice de código](code-intelligence.md) necesita que se agregue un repositorio y `LOOM_CODE_INGEST_ENABLED=true`.

Algunas operaciones de esta página están marcadas como experimentales en la [página de estado](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md).
