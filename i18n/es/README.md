<!-- i18n: source=README.md blob=7d127b3e0da860afac15179ad31d456d9592dc96 model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/README.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>Memoria compartida, búsqueda de documentos e inteligencia de código para agentes de IA.</b><br>
  Para Claude Code, Codex, Cursor y tus propios agentes.
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="Python SDK on PyPI" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="TypeScript SDK on npm" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="https://github.com/ClickHouse/loom/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP server score" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
</p>

<p align="center">
  <!-- languages -->
  <a href="../../README.md">English</a> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a> · <a href="../ko/README.md">한국어</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · Español · <a href="../ru/README.md">Русский</a>
  <!-- /languages -->
</p>

<p align="center">
  <a href="#what-loom-is-for">Para qué sirve Loom</a> ·
  <a href="#measured-results">Benchmarks</a> ·
  <a href="#get-started">Primeros pasos</a> ·
  <a href="#why-loom">Capacidades</a> ·
  <a href="#integrations">Integraciones</a> ·
  <a href="https://github.com/ClickHouse/loom/blob/main/docs/index.md">Documentación</a>
</p>

![Panel de Loom que muestra memorias compartidas de agentes, un dato fijado y conflictos marcados](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom es un servidor MCP de código abierto y autoalojado que da a tus agentes memoria compartida a largo plazo: lo que un agente aprende en una sesión, el siguiente agente o herramienta puede encontrarlo.

Los agentes se conectan mediante MCP, HTTP, Python o TypeScript. Puedes autoalojarlo con Docker o [en Kubernetes con el Helm chart](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md); el contexto de los agentes y los datos del índice viven en [ClickHouse](docs/use-cases/self-hosted-clickhouse.md), y un despliegue puede elegir [Postgres en el primer arranque](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md) para el estado de la memoria y del conocimiento en vivo. El panel reúne memorias, repositorios, tareas y ejecuciones en un solo lugar.

<a id="what-loom-is-for"></a>

## Para qué sirve Loom

- **Memoria a largo plazo compartida entre tus agentes de programación**, con plugins que cargan contexto
  al inicio de la sesión y capturan la sesión mientras se ejecuta.
  [Memoria de agentes](docs/use-cases/agent-memory.md) ·
  [Conectar un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **Memoria para una aplicación de agentes**, por usuario, desde el SDK de Python o TypeScript.
  [Tu primer agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **Un servidor de memoria MCP multiusuario** con espacios de nombres, roles y tokens.
  [Modo multiusuario](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [Espacios de nombres y uso compartido](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **Un índice de código de tus repositorios** que los agentes de programación consultan para obtener definiciones,
  llamadores e impacto de los cambios. [Índice de código](docs/use-cases/code-intelligence.md)
- **Menor costo de modelo para agentes de programación**, según se [midió](#measured-results) más abajo.
  [Costo de los agentes de programación](docs/use-cases/coding-agent-cost.md)
- **Búsqueda de documentos con citas (RAG)**, incluidos los archivos PDF, `.docx` y `.pptx` que
  convierte el servicio extractor. [Búsqueda de documentos](docs/use-cases/document-search.md)
- **Contexto compartido para flujos de trabajo con varios agentes**: memoria compartida, bandejas de tareas y
  decisiones. [Multiagente](docs/use-cases/multi-agent.md)
- **Memoria, recuperación, auditoría y borrado para agentes autónomos.**
  [Agentes autónomos](docs/use-cases/autonomous-agents.md)
- **Un registro del trabajo de los agentes**, con conversaciones con búsqueda (cuando la grabación está activada),
  resúmenes de sesión desde los que retomar, ejecuciones con sus objetivos, resultados y costos de modelo cuando
  se proporcionan, e historial de PR y del gestor de incidencias.
  [Ejecuciones e historial de PR](docs/use-cases/runs-and-pr-history.md)

Todos los casos de uso, y cuándo conviene otra herramienta: [casos de uso](docs/use-cases/index.md).

Loom no es una biblioteca dentro del proceso para la memoria efímera de un solo agente, ni un framework
de agentes. [Cuándo Loom no encaja](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## Resultados medidos

| Evaluación                                | Con Loom  | Sin Loom | Mejora de eficiencia                       | Alcance                                  |
| ----------------------------------------- | --------- | -------- | ------------------------------------------ | ---------------------------------------- |
| Calidad de respuesta del agente de código | **0.84**  | 0.78     | **20% menos** de costo del modelo          | 24 preguntas × 3 ejecuciones pareadas    |
| Localización de archivos del agente de código | **96.4%** | 91.4%    | **21% menos** de costo del modelo          | 36 preguntas × 10 ejecuciones pareadas   |
| Memoria frente a respuestas con historial completo | **94%**   | 90%      | **91% menos** de tokens en el prompt del lector | 100 preguntas pareadas de LongMemEval-S |

Ambas configuraciones de código usan Claude Code (`claude-sonnet-5`) sobre el código base de ClickHouse;
el costo es la factura completa del modelo de la sesión, sin incluir las llamadas a modelos del lado del servidor de Loom, el alojamiento
ni la indexación. La calidad de respuesta
mide la cobertura de los puntos clave de una respuesta verificada, con una penalización por contradicciones.
La localización de archivos cuenta el archivo correcto entre tres archivos nombrados.

La fila de memoria compara las memorias recuperadas con el historial completo de la conversación usando
el mismo lector `gpt-5`. El ahorro de tokens no incluye la construcción de la memoria; la diferencia de precisión de 94% frente a 90%
es descriptiva, no está establecida estadísticamente.

**Por separado, la configuración `memory.ask` de Loom obtuvo un 96.6% de precisión de respuesta en las
500 preguntas de LongMemEval-S.** Es una evaluación de producto con sus propios modelos de respuesta configurados, no la comparación con lector fijo anterior.

Los resultados son específicos de las cargas de trabajo evaluadas.

[Protocolos y artefactos reproducibles](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[resultados detallados](#benchmark-details) ·
[evalúa con tu carga de trabajo](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## Primeros pasos

<a id="1-choose-your-docker-setup"></a>

### 1. Elige tu configuración de Docker

- **Uso personal (el valor predeterminado):** sin inicio de sesión, acceso solo desde localhost y un
  espacio de nombres `dev/local`. Una persona puede conectar varios agentes.
- **Uso en equipo:** cuentas autenticadas y controles de acceso por espacio de nombres.
  [Inicia en modo multiusuario desde el primer arranque](docs/getting-started/quickstart.md#start-in-multi-user-mode).

Ambos modos admiten las mismas funciones de Loom, incluidos los LLM y los embeddings configurados.
El modo de un solo usuario no es una edición con funciones reducidas. También puedes
[cambiar una instalación existente sin borrar sus datos](docs/getting-started/quickstart.md#2-open-the-dashboard);
concede a las cuentas nuevas acceso a los espacios de nombres existentes.

Para probarlo sin instalar nada, ábrelo en un codespace:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
([qué ejecuta y quién puede acceder](docs/getting-started/quickstart.md#try-it-in-github-codespaces))

Para la instalación personal predeterminada, con Docker instalado:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Ejecútalo desde tu directorio personal u otro
[directorio en el que solo tú puedas escribir](docs/getting-started/quickstart.md#fast-path-released-image-no-clone).
El instalador inicia Loom y ClickHouse, aplica las migraciones e imprime la URL del dashboard:
**http://localhost:7777/**; en una instalación nueva, después comprueba que una memoria se guarda y se lee
correctamente. En una terminal, también ofrece conectar los agentes de programación que encuentre (Claude Code, Codex,
VS Code) y, en una instalación nueva, abre el dashboard. La imagen incluye el servidor,
los workers, el dashboard y los indexadores de compilador de Python, TypeScript y Go; las dependencias
de TypeScript se preparan automáticamente. [Guía de inicio rápido y versiones fijadas](docs/getting-started/quickstart.md)

La instalación reside en `~/.loom` (`LOOM_INSTALL_DIR` permite elegir otro directorio): el
archivo Compose y los ajustes de arranque en `~/.loom/.env`. Si vuelves a ejecutar el comando de instalación,
se actualiza en el mismo lugar y se conservan los ajustes de tu `.env`.

Mantén el modo de un solo usuario accesible solo desde localhost. Para el acceso compartido, sigue la
[guía de despliegue para equipos](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes), incluido TLS.

<a id="2-configure-models-and-api-keys"></a>

### 2. Configura los modelos y las claves de API

**La instalación estándar arranca sin claves ni red.** El almacenamiento, el dashboard,
la indexación estructural de código y la búsqueda semántica funcionan, con un pequeño modelo de embeddings local que
incluye la imagen; el LLM falso no genera respuestas con IA.

| Componente | Configurar para uso real                                  | Sin configuración                                                              |
| ---------- | --------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Embeddings | Proveedor, modelo de embeddings, dimensiones y clave de API. | `local` (bge-small en CPU): recuperación semántica, más débil que un modelo alojado. |
| LLM        | Proveedor, modelo y clave de API.                         | `fake-echo-1`: sin extracción, resúmenes ni respuestas útiles.                 |

Abre **Settings → Models & keys**, elige ambos proveedores y modelos, introduce sus
claves y haz clic en **Test & apply**. No hace falta reiniciar Docker; si cambias el espacio de
embeddings, se reconstruye el índice existente. Las opciones de embeddings del dashboard son
proveedores alojados: para mantener `local` y añadir un LLM, define las líneas del LLM en `~/.loom/.env`
([Modelos y claves](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)).

**Una sola clave de API de OpenAI puede cubrir ambos roles.** Claude necesita un proveedor de embeddings aparte.
Se admiten OpenAI, Gemini y endpoints de embeddings compatibles con OpenAI. El inicio de sesión de la CLI
o la suscripción de tu agente no proporciona las claves de proveedor de Loom.

¿Prefieres configurar mediante variables de entorno? Edita `~/.loom/.env` y luego recrea el servidor:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

Una clave por sí sola no cambia los modelos fijados (el embedder local y el LLM falso); define también
el proveedor y el modelo.
Los ajustes guardados en el dashboard tienen prioridad sobre `.env`. Consulta
[modelos y claves de API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md) para ver recetas por proveedor
y comprobaciones de estado.

**Todo lo demás está en un solo archivo.** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) enumera
cada ajuste con su valor predeterminado y lo que hace, por áreas: la conexión con ClickHouse,
tiempos de espera y presupuestos, límites de tasa, workers y los interruptores `*_enabled` que activan y desactivan
funciones. Define cualquiera de ellos como `LOOM_<NAME>` en `~/.loom/.env` y recrea el
servidor; un despliegue que monte un archivo de configuración puede definirlos en una copia de este archivo
([cómo se leen los ajustes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)). Los marcados
como `live` también cambian mientras Loom se ejecuta: `loom settings set <name> <value>`, o Admin → Deployment
settings. `loom settings list --changed` muestra qué difiere de los valores predeterminados y
de dónde proviene cada valor ([ajustes en tiempo de ejecución](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)).

<a id="3-connect-your-agents"></a>

### 3. Conecta tus agentes

Si el instalador conectó tu agente, omite los comandos siguientes. En la máquina que ejecuta una
instalación personal, los plugins no necesitan token:

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

Inicia una sesión nueva; la primera se conecta a `http://localhost:7777`. En Codex, ejecuta
`codex` de forma interactiva una vez y aprueba los hooks del plugin; hasta entonces solo funcionan
las herramientas. Cuéntale al agente algo que valga la pena conservar y pregúntale por ello en la siguiente sesión.

Para Cursor en la máquina que ejecuta una instalación personal, el botón siguiente añade la
[entrada MCP local](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools), que no necesita token:

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

Para el modo agente de VS Code en la máquina que ejecuta una instalación personal, los botones siguientes
añaden la [entrada MCP local](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server),
que no necesita token:

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

Para un servidor de equipo, pasa `--config server_url=… --config token=…` a Claude Code, o
apunta Codex hacia él como se describe en
[conectar un agente de programación](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md). Crea
los tokens en **Settings → Tokens**. Cursor y otros clientes MCP, y
[el SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md), también se tratan allí.

<a id="why-loom"></a>

## Por qué Loom

- **Memoria que se mantiene en el tiempo.** Comparte decisiones y hallazgos entre agentes y
  sesiones. Cada dato registra quién lo escribió, el historial de versiones y la revisión de conflictos
  hacen que las correcciones sean trazables, y un espacio de nombres puede revertirse a cualquier punto en el tiempo.
  [Memoria](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Inteligencia de código con evidencia.** Encuentra definiciones, llamadores, impacto de cambios y
  pruebas relevantes; conecta el código con commits, pull requests e issues. Los resultados identifican
  su revisión, ubicación en el código y grado de completitud. [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **Recuperación fundamentada.** Busca en memorias, documentos, conversaciones y
  registros de seguimiento a los que tengas acceso. Las respuestas citan sus fuentes; la recuperación con baja confianza puede abstenerse de responder.
  [Recuperación](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **Traspasos entre varios agentes.** Asigna trabajo mediante bandejas de tareas duraderas con arrendamientos (leases),
  reintentos, reglas de revisión y resultados. Tus agentes ejecutan el flujo de trabajo; Loom
  conserva los traspasos. [Colas de tareas](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Control sobre los datos de los agentes.** Gestiona el acceso a los espacios de nombres, la procedencia, la retención, los registros
  de auditoría y el borrado verificado. El contexto sigue siendo consultable en ClickHouse.
  [Seguridad](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## Integraciones

| Integración                                                                                                                                                                                                                                                                                   | Qué conecta                                                                   |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md), [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md), [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) y [otros clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ([todas las integraciones](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)) | Memoria e inteligencia de código en el flujo de trabajo de tu agente actual.  |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md), [TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) y [HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | Tus propios agentes y aplicaciones.                                           |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | Trazas de solicitudes, costos de modelos, puntuaciones de recuperación y atribución de versiones de prompts. |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | Trazas, logs y métricas de OpenTelemetry en ClickHouse.                       |

Langfuse y ClickStack son integraciones opcionales, no servicios incluidos en el stack de Docker.

<a id="benchmark-details"></a>

## Detalles de los benchmarks

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### Resultados detallados de recuperación, lector y búsqueda de código

| Evaluación                                                           | Resultado                                                                                                          |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Recuperación de evidencia en LongMemEval-S (500 preguntas)           | **98.6%** de las preguntas con al menos una sesión de apoyo entre las primeras 30 memorias.                        |
| LongMemEval-S con lector `gpt-5` fijo (500 preguntas)                | **92.0%** de precisión en las respuestas a partir de las 200 memorias principales.                                 |
| LoCoMo (las 10 conversaciones)                                       | **89.6%** de precisión en las respuestas; **90.8%** de abstenciones correctas en preguntas adversariales.          |
| Muestra de preguntas de hechos de MemoryAgentBench (500 preguntas)   | **99.0%** de SubEM; **95.7%** de F1 de solapamiento de tokens en las respuestas generadas.                         |
| Búsqueda conceptual de código (36 preguntas)                         | Recall del archivo de referencia **91.2%** en 5 / **97.2%** en 10; MRR **0.91**.                                   |
| Localización de archivos en SWE-bench Verified (200 issues)          | Issues con un archivo modificado entre los 10 primeros: **82%** frente a **57%** de BM25; **76.5%** entre los 5 primeros. |
| Detección de duplicados en el tracker (104 pares)                    | El duplicado anterior registrado quedó en primer lugar el **87.5%** de las veces.                                  |

Estas pruebas miden tareas distintas, no una puntuación agregada. SWE-bench mide
la localización de archivos, no la resolución de issues. El [registro de resultados](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)
incluye las definiciones de puntuación, las evaluaciones con muestras pequeñas, los hallazgos más débiles y negativos,
y los protocolos completos, los modelos y los artefactos.

[Metodología](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[cobertura y limitaciones conocidas](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[ejecutar los benchmarks](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## Documentación y contribución

Para contexto compartido entre agentes, inteligencia de código y recuperación con permisos, empieza por la
[documentación](https://github.com/ClickHouse/loom/blob/main/docs/index.md). Para conocer los requisitos de despliegue, consulta los
[requisitos previos](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites).

[Despliegue](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [Preguntas frecuentes](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[Cómo contribuir](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [Reporte de seguridad](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [Licencia](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [Aviso](https://github.com/ClickHouse/loom/blob/main/NOTICE)
