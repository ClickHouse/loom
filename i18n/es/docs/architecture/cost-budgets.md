<!-- i18n: source=docs/architecture/cost-budgets.md blob=932ba3cd2049b60a49294a5dd87fa51b9401b693 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cost-budgets.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cost-budgets.md), which is authoritative.

<a id="cost-budgets"></a>

# Presupuestos de costo

Los límites por espacio de nombres y por usuario con nombre usan el registro de costo móvil de 24 horas en `agent_events`.
Una política positiva del espacio de nombres sobrescribe el valor por defecto correspondiente del servidor; cero hereda,
y cero en ambos niveles desactiva ese límite. El límite de un usuario con nombre es independiente del
tope del espacio de nombres. La admisión rechaza la síntesis de pago cuando se alcanza cualquiera de los dos, y el enrutamiento
de modelos usa la mayor fracción de gasto. `memory.ask`, incluida su ruta de documento completo,
`code.ask`, `knowledge.synthesize` y la expansión de consultas de `code.search` aportan el
usuario lógico registrado en el registro de costo. Los workers de todo el espacio de nombres conservan su contabilidad por espacio de nombres;
nunca se sustituye por el sujeto de un token a un usuario sin atribuir. Estos controles blandos
no reservan gasto ni establecen una identidad de facturación.

`cost.budget_status` comparte con las protecciones la lectura de política y de gasto. El modo de usuario exige
un espacio de nombres e informa el límite y el veredicto del usuario, además de `namespace_over_cap`. El gasto exacto
del usuario y su porcentaje son `null` a menos que quien llama sea un administrador de plataforma verificado
sin restricciones de rol en el espacio de nombres. Esto se cumple incluso cuando todos los eventos son ordinarios;
variar la presencia de un número revelaría actividad marcada. La ausencia de autenticación no otorga acceso al gasto privado.
La contabilidad interna, la admisión y la presión presupuestaria incluyen todo el gasto,
incluidas las solicitudes marcadas. Los totales del espacio de nombres y de la organización siguen siendo numéricos.
La admisión de transporte aplica el usuario fijado de un token. Los modos de espacio de nombres y organización conservan
sus ámbitos de lectura existentes. La [guía de costos](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md#daily-spend-caps)
describe la configuración y el momento de las advertencias.

Un `budget_alert_inbox` opcional debe nombrar un inbox existente que pertenezca al espacio de nombres del presupuesto
en la misma organización. El registro lo valida y la entrega vuelve a comprobar la propiedad.
Las comprobaciones puntuales y las consultas de estado programan advertencias acotadas en segundo plano mediante el
flujo de eventos de tareas existente, con un máximo de 32 entregas pendientes por proceso. Las entregas de una misma
organización se ejecutan en serie dentro del proceso, de modo que una advertencia de espacio de nombres y una de usuario no compitan
entre sí. No hay un worker de descubrimiento. El ID del evento incluye organización, espacio de nombres, inbox, usuario seleccionado, límite, fracción de advertencia
y día UTC. Un claim distribuido y una lectura de eventos fuertemente consistente serializan
la comprobación y la inserción; un token de deduplicación más una lectura posterior gestionan un reintento incierto.
Los fallos de notificación no pueden cambiar el veredicto de un límite.

Las cargas de advertencia de usuario contienen el usuario seleccionado, el límite y la fracción de advertencia, con
`spent_24h_usd: null`. Esto se aplica al inbox, al polling, a los flujos y al webhook, incluso
cuando la comprobación de un operador desencadena la entrega: la autoridad de quien inicia no establece
la autoridad del destinatario. Las advertencias de espacio de nombres conservan su total de gasto del espacio de nombres.
El veredicto del límite de usuario y el cruce del umbral de advertencia revelan cotas del gasto; un administrador
que cambia los límites puede inferir cotas más estrictas. Es una señal de política aceptada, junto con la
[excepción de los totales de gasto marcado](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md), y no una no interferencia completa.
No otorga acceso al trabajo marcado, a los modelos ni a los resultados.

Las actualizaciones de tareas y los flujos de watch aplican a las advertencias el ámbito de usuario y de sesión de quien llama antes de
la paginación. Un ámbito incompleto no recibe advertencias; los eventos de tareas ordinarios siguen compartidos
dentro del inbox. El webhook del inbox registrado recibe el flujo de su espacio de nombres.

Cada entrega y cada borrado de eventos de tareas toman el mismo lease de escritor de advertencias a nivel de organización.
Las inserciones usan el plazo de inicio y el límite de ejecución del lado del servidor existentes; el TTL del lease
supera la ventana de trabajo más el límite completo de envío. Una inserción o cancelación incierta
deja que ese lease expire, de modo que el borrado no puede adquirirlo hasta que todo envío posible
haya terminado. El borrado mantiene su protocolo de mutación etiquetada, polling y reanudación. Las comprobaciones del registro
antes y después de la publicación invalidan una observación alcanzada por el borrado; la limpieza posterior a la escritura
nombra solo el evento y la secuencia exactos de esa entrega. Un registro ilegible
rechaza la publicación.

Las filas de advertencia llevan la tupla de identidad completa, un actor de proceso y una clase de origen derivada.
El borrado de usuario elimina sus eventos de tareas y el censo de residuos verifica la eliminación;
el borrado de espacio de nombres cubre tanto las tareas como los eventos. Las columnas añadidas tienen valores por defecto para escritores
más antiguos. La producción de advertencias espera a que todas las réplicas activas admitan la migración, de modo que
una implementación de borrado más antigua no pueda pasar por alto filas de advertencia con datos de usuario durante una actualización
progresiva. Un estado desconocido de la flota difiere la publicación hasta una comprobación posterior.

<a id="activation-and-rollback"></a>

## Activación y reversión

Los límites de usuario y las advertencias de presupuesto exigen que todas las compilaciones en servicio admitan la migración 107.
Mantén `LOOM_DEFAULT_DAILY_USER_COST_CAP_USD` y el `daily_user_cost_cap_usd` de cada espacio de nombres
en cero, y `budget_alert_inbox` vacío, mientras cualquier despliegue
que comparta los almacenes ejecute una compilación más antigua o se desconozca su versión. No confíes en los límites
de usuario durante ese intervalo. Aplica la migración y actualiza todos los procesos en servicio antes de
establecer estas políticas. La comprobación de flota de advertencias difiere la producción ante una flota antigua o desconocida;
no elimina las advertencias ya almacenadas.

Guarda el valor global por defecto del límite de usuario y el límite de usuario y el inbox de alertas de cada espacio de nombres antes de
la reversión. La reversión requiere un intervalo de mantenimiento:

1. Detén todos los procesos de atención de solicitudes y de segundo plano que puedan comprobar presupuestos o programar
   advertencias, en todos los despliegues que compartan los almacenes. Detén también el polling de estado.
   Desactivar los workers de segundo plano o vaciar una política de inbox por sí solo no detiene
   las entregas ya programadas con ese inbox.
2. Drena las entregas programadas y resuelve toda escritura en curso o incierta antes de
   la limpieza. Espera a que los leases de escritor de advertencias se liberen o expiren y a que
   terminen sus ventanas acotadas de inicio tardío y de ejecución del servidor. Un drenaje interrumpido o una
   lista vacía de consultas en ejecución no prueba que un envío retrasado no pueda llegar todavía.
3. Borra solo las filas de `loom.task_events` cuyo `event` sea `budget.warning`. Espera a que
   el borrado se complete en cada réplica de almacenamiento y verifica que no queden filas de advertencia
   en ninguna antes de iniciar el primer proceso más antiguo. Una réplica ilegible o una escritura
   sin resolver bloquea la reversión. El borrado de espacio de nombres o de usuario no es un sustituto: también
   elimina datos no relacionados.
4. Mantén desactivados los límites de usuario y las advertencias mientras sirvan compilaciones más antiguas. Cuando todos los procesos
   en servicio vuelvan a admitir la migración 107, restaura el valor global por defecto guardado y vuelve a aplicar
   los límites de usuario y los inboxes de alertas de los espacios de nombres con `namespace.register`. Lee las
   políticas de los espacios de nombres y comprueba los límites efectivos antes de reanudar la producción de advertencias.

Esto limita la activación durante la operación con versiones mixtas e intercambia disponibilidad por
privacidad durante la reversión. Los lectores de tareas más antiguos no filtran las advertencias privadas, y el
borrado de usuario más antiguo no las elimina. Los registros de espacio de nombres más antiguos tampoco incluyen las columnas de límite de usuario
y de inbox de alertas; sus filas de reemplazo restablecen estas políticas a sus valores por defecto.
El registro de espacios de nombres no conserva esas columnas a través de escrituras más antiguas.
Por tanto, el operador debe preservar y restaurar la política en lugar de confiar en que sobreviva a la reversión.

<a id="source-entry-points"></a>

## Puntos de entrada del código fuente

- `server/observability/budget.py`: `budget_snapshot` lee la política y el gasto móvil para las protecciones y el estado.
- `server/tasks/app/budget_notifications.py`: `deliver` gestiona los claims de advertencias y las barreras de borrado.
- `server/tasks/store/repo.py`: `events_since` aplica el ámbito de las advertencias antes de la paginación por cursor.
- `server/tasks/app/reads.py` y `server/tasks/app/watch_routes.py`: los lectores de polling y de streaming vinculan ese ámbito.
- `server/erasure/store/steps_ledgers.py`: los leases de escritor de advertencias protegen el borrado de eventos de tareas.

<a id="invariants"></a>

## Invariantes

- La asignación de un usuario con nombre nunca eleva el tope del espacio de nombres, y la atribución nunca adivina el sujeto de un token.
- Un lector restringido no recibe ninguna advertencia fuera de su ámbito de usuario y de sesión.
- Una escritura de advertencia incierta mantiene el borrado bloqueado hasta que su posible envío haya terminado.
- Un fallo de notificación nunca cambia la decisión sobre el límite.
