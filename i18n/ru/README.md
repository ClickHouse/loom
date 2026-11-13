<!-- i18n: source=README.md blob=46486c03a54d0fc37704491d37179cbb64ba871e model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/README.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/README.md), which is authoritative.

<h1 align="center">ClickHouse Loom</h1>

<p align="center">
  <b>Общая память, поиск по документам и анализ кода для ИИ-агентов.</b><br>
  Для Claude Code, Codex, Cursor и ваших собственных агентов.
</p>

<p align="center">
  <a href="https://pypi.org/project/clickhouse-loom/"><img alt="Python SDK on PyPI" src="https://img.shields.io/pypi/v/clickhouse-loom?label=python%20sdk"></a>
  <a href="https://www.npmjs.com/package/@clickhouse/loom"><img alt="TypeScript SDK on npm" src="https://img.shields.io/npm/v/%40clickhouse%2Floom?label=typescript%20sdk"></a>
  <a href="https://github.com/ClickHouse/loom/blob/main/LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://glama.ai/mcp/servers/ClickHouse/loom"><img alt="MCP server score" src="https://glama.ai/mcp/servers/ClickHouse/loom/badges/score.svg"></a>
</p>

<p align="center">
  <!-- languages -->
  <a href="../../README.md">English</a> · <a href="../zh-CN/README.md">简体中文</a> · <a href="../ja/README.md">日本語</a> · <a href="../ko/README.md">한국어</a> · <a href="../pt-BR/README.md">Português (Brasil)</a> · <a href="../es/README.md">Español</a> · Русский
  <!-- /languages -->
</p>

<p align="center">
  <a href="#what-loom-is-for">Для чего нужен Loom</a> ·
  <a href="#measured-results">Бенчмарки</a> ·
  <a href="#get-started">Начало работы</a> ·
  <a href="#why-loom">Возможности</a> ·
  <a href="#integrations">Интеграции</a> ·
  <a href="https://github.com/ClickHouse/loom/blob/main/docs/index.md">Документация</a>
</p>

![Панель управления Loom с общими памятями агентов, закреплённым фактом и помеченными конфликтами](https://github.com/ClickHouse/loom/raw/main/docs/images/dashboard-memories.png)

Loom — это самостоятельно размещаемый MCP-сервер с открытым исходным кодом, который даёт вашим агентам долговременную общую память: то, что один агент узнал за сессию, может найти следующий агент или инструмент.

Агенты подключаются через MCP, HTTP, Python или TypeScript. Loom можно развернуть самостоятельно с помощью Docker или [в Kubernetes с помощью Helm-чарта](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md); контекст агентов и данные индекса хранятся в [ClickHouse](docs/use-cases/self-hosted-clickhouse.md), а для живого состояния памяти и знаний при развёртывании можно выбрать [Postgres при первом запуске](https://github.com/ClickHouse/loom/blob/main/docs/operations/postgres.md). Панель управления объединяет памяти, репозитории, задачи и запуски в одном месте.

<a id="what-loom-is-for"></a>

## Для чего нужен Loom

- **Долговременная память, общая для ваших агентов для написания кода**, с плагинами, которые загружают контекст
  при старте сессии и фиксируют сессию по ходу работы.
  [Память агента](docs/use-cases/agent-memory.md) ·
  [Подключение агента для написания кода](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)
- **Память для агентного приложения**, отдельная для каждого пользователя, через Python или TypeScript
  SDK. [Ваш первый агент](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)
- **Многопользовательский MCP-сервер памяти** с пространствами имён, ролями и токенами.
  [Многопользовательский режим](docs/getting-started/quickstart.md#start-in-multi-user-mode) ·
  [Пространства имён и совместный доступ](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)
- **Индекс кода ваших репозиториев**, к которому агенты для написания кода обращаются за определениями,
  вызывающим кодом и оценкой влияния изменений. [Индекс кода](docs/use-cases/code-intelligence.md)
- **Меньшая стоимость модели для агентов для написания кода**, как [показано в измерениях](#measured-results) ниже.
  [Стоимость агента для написания кода](docs/use-cases/coding-agent-cost.md)
- **Поиск по документам с цитатами (RAG)**, включая файлы PDF, `.docx` и `.pptx`, которые
  преобразует сервис извлечения. [Поиск по документам](docs/use-cases/document-search.md)
- **Общий контекст для мультиагентных рабочих процессов**: общая память, входящие задачи и
  решения. [Мультиагентные сценарии](docs/use-cases/multi-agent.md)
- **Память, поиск, аудит и удаление данных для автономных агентов.**
  [Автономные агенты](docs/use-cases/autonomous-agents.md)
- **История работы агентов**: диалоги с возможностью поиска (если запись включена),
  краткие сводки сессий для возобновления работы, запуски с их целями, результатами и стоимостью модели, если они
  переданы, а также история PR и трекера.
  [Запуски и история PR](docs/use-cases/runs-and-pr-history.md)

Все сценарии использования и случаи, когда лучше подходит другой инструмент: [сценарии использования](docs/use-cases/index.md).

Loom не является встраиваемой библиотекой для краткосрочной памяти одного агента и не является фреймворком
для агентов. [Когда Loom не подходит](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#when-it-does-not-fit)

<a id="measured-results"></a>

## Результаты измерений

| Оценка                              | С Loom    | Без Loom | Выигрыш в эффективности                  | Охват                                  |
| ----------------------------------- | --------- | -------- | ---------------------------------------- | -------------------------------------- |
| Качество ответов coding-агента      | **0.84**  | 0.78     | **На 20% ниже** стоимость модели         | 24 вопроса × 3 парных запуска          |
| Поиск файлов coding-агентом         | **96.4%** | 91.4%    | **На 21% ниже** стоимость модели         | 36 вопросов × 10 парных запусков       |
| Ответы по памяти и по полной истории | **94%**   | 90%      | **На 91% меньше** токенов в промпте reader | 100 парных вопросов LongMemEval-S      |

Обе конфигурации для кода используют Claude Code (`claude-sonnet-5`) на кодовой базе ClickHouse;
стоимость — это весь счёт за модель за сессию, без учёта вызовов модели на стороне сервера Loom, хостинга
и индексации. Качество ответов
измеряет охват ключевых пунктов проверенного ответа со штрафом за противоречия.
Поиск файлов засчитывается, если нужный файл входит в число не более чем трёх файлов, которые агента
просят назвать в конце ответа.

В строке про память сравниваются извлечённые воспоминания и полная история диалога при
одном и том же reader `gpt-5`. Экономия токенов не включает построение памяти; разница в точности
94% и 90% носит описательный характер и не является статистически установленной.

**Отдельно: конфигурация Loom `memory.ask` показала точность ответов 96.6% на всех
500 вопросах LongMemEval-S.** Это продуктовая оценка со своими настроенными
моделями ответов, а не сравнение с фиксированным reader, приведённое выше.

Результаты относятся только к протестированным нагрузкам.

[Протоколы и воспроизводимые артефакты](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md) ·
[подробные результаты](#benchmark-details) ·
[оценка на вашей нагрузке](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)

<a id="get-started"></a>

## Начало работы

<a id="1-choose-your-docker-setup"></a>

### 1. Выберите вариант установки Docker

- **Личное использование — по умолчанию:** без входа, доступ только с localhost и пространство имён `dev/local`.
  Один человек может подключить несколько агентов.
- **Командное использование:** аккаунты с аутентификацией и управление доступом к пространствам имён.
  [Запустите в многопользовательском режиме с первой загрузки](docs/getting-started/quickstart.md#start-in-multi-user-mode).

Оба режима поддерживают одни и те же возможности Loom, включая настроенные LLM и эмбеддинги.
Однопользовательский режим — не урезанная версия. Вы также можете
[переключить существующую установку, не удаляя её данные](docs/getting-started/quickstart.md#2-open-the-dashboard);
выдайте новым аккаунтам доступ к существующим пространствам имён.

Чтобы попробовать Loom без установки, откройте его в codespace:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)
([что он запускает и кто имеет к нему доступ](docs/getting-started/quickstart.md#try-it-in-github-codespaces))

Для личной установки по умолчанию нужен установленный Docker (в Windows — в терминале WSL 2,
где работает `docker`: Docker Desktop с включённой интеграцией WSL или Docker Engine,
установленный в дистрибутиве):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Запускайте команду из домашнего каталога или из другого
[каталога, в который можете писать только вы](docs/getting-started/quickstart.md#fast-path-released-image-no-clone).
Установщик запускает Loom и ClickHouse, применяет миграции и выводит адрес панели управления:
**http://localhost:7777/**; при новой установке он затем проверяет, что память сохраняется и
читается обратно. В терминале он также предлагает подключить найденных кодинг-агентов (Claude Code, Codex,
VS Code) и при новой установке открывает панель управления. Образ включает сервер,
воркеры, панель управления и индексаторы-компиляторы для Python, TypeScript и Go; зависимости
TypeScript подготавливаются автоматически. [Быстрый старт и закреплённые релизы](docs/getting-started/quickstart.md)

Установка находится в `~/.loom` (`LOOM_INSTALL_DIR` задаёт другой каталог): файл
Compose и настройки запуска в `~/.loom/.env`. Повторный запуск команды установки
обновляет Loom на месте и сохраняет настройки в вашем `.env`.

Оставляйте однопользовательский режим доступным только с localhost. Для общего доступа следуйте
[руководству по развёртыванию для команды](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes), включая TLS.

<a id="2-configure-models-and-api-keys"></a>

### 2. Настройте модели и API-ключи

**Стандартная установка запускается без ключей и сети.** Хранилище, панель управления,
структурная индексация кода и семантический поиск работают на небольшой локальной модели эмбеддингов,
которая входит в образ; фиктивная LLM не даёт ответов, сгенерированных ИИ.

| Компонент  | Что настроить для реального использования          | Без настройки                                                               |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| Эмбеддинги | Провайдер, модель эмбеддингов, размерность и API-ключ. | `local` (bge-small на CPU): семантический поиск, слабее размещённой модели. |
| LLM        | Провайдер, модель и API-ключ.                      | `fake-echo-1`: нет осмысленного извлечения, сводок и ответов.               |

Откройте **Settings → Models & keys**, выберите обоих провайдеров и модели, введите их
ключи, затем нажмите **Test & apply**. Перезапуск Docker не нужен; смена пространства
эмбеддингов перестраивает существующий индекс. В панели управления для эмбеддингов доступны
размещённые провайдеры: чтобы оставить `local` и добавить LLM, задайте строки LLM в `~/.loom/.env`
([Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md#claude-for-the-llm)).

**Один API-ключ OpenAI может обслуживать обе роли.** Для Claude нужен отдельный провайдер эмбеддингов.
Поддерживаются OpenAI, Gemini и OpenAI-совместимые эндпоинты эмбеддингов. Вход через CLI вашего агента
или его подписка не заменяют ключи провайдеров для Loom.

Предпочитаете настройку через переменные окружения? Отредактируйте `~/.loom/.env`, затем пересоздайте сервер:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
```

Один лишь ключ не меняет закреплённые модели (локальный эмбеддер и фиктивную LLM); задайте также
провайдера и модель.
Сохранённые настройки панели управления имеют приоритет над `.env`. Рецепты для провайдеров и проверки
работоспособности описаны в
[моделях и API-ключах](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).

**Всё остальное — в одном файле.** [`loom.example.yaml`](https://github.com/ClickHouse/loom/blob/main/loom.example.yaml) перечисляет
каждую настройку со значением по умолчанию и описанием по областям: подключение к ClickHouse,
тайм-ауты и бюджеты, ограничения частоты запросов, воркеры и переключатели `*_enabled`, которые включают и выключают
возможности. Задайте любую из них как `LOOM_<NAME>` в `~/.loom/.env` и пересоздайте
сервер; развёртывание, которое монтирует файл конфигурации, может задать их в копии этого файла
([как читаются настройки](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#how-settings-are-read)). Настройка с пометкой
`live` также меняется во время работы Loom: `loom settings set <name> <value>` или Admin → Deployment
settings. `loom settings list --changed` показывает, что отличается от значений по умолчанию и
откуда взято каждое значение ([настройки времени выполнения](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)).

<a id="3-connect-your-agents"></a>

### 3. Подключите агентов

Если установщик подключил вашего агента, пропустите команды ниже. На машине, где работает
личная установка, плагинам токен не нужен:

```bash
# Claude Code
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom

# Codex
codex plugin marketplace add ClickHouse/loom
codex plugin add loom@loom
```

Начните новую сессию; первая подключится к `http://localhost:7777`. В Codex один раз запустите
`codex` в интерактивном режиме и подтвердите хуки плагина; до этого работают только
инструменты. Расскажите агенту что-нибудь, что стоит сохранить, а затем спросите об этом в следующей сессии.

Для Cursor на машине, где работает личная установка, кнопка ниже добавляет
[локальную запись MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md#mcp-tools), которой токен не нужен:

[![Add Loom to Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/en/install-mcp?name=loom&config=eyJ1cmwiOiJodHRwOi8vbG9jYWxob3N0Ojc3NzcvbWNwP3Rvb2xzZXQ9Y29kaW5nIn0%3D)

Для режима агента VS Code на машине, где работает личная установка, кнопки ниже
добавляют [локальную запись MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/vscode.md#local-single-user-server),
которой токен не нужен:

[![Add Loom to VS Code](https://img.shields.io/badge/VS_Code-Add_Loom-0098FF?logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)
[![Add Loom to VS Code Insiders](https://img.shields.io/badge/VS_Code_Insiders-Add_Loom-24bfa5?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522loom%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522http%253A%252F%252Flocalhost%253A7777%252Fmcp%2522%257D)

Для командного сервера передайте `--config server_url=… --config token=…` в Claude Code или
настройте Codex, как описано в разделе
[подключение кодинг-агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md). Создавайте
токены в **Settings → Tokens**. Cursor и другие клиенты MCP, а также
[SDK](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md) тоже описаны там.

<a id="why-loom"></a>

## Почему Loom

- **Память, которая сохраняется между сессиями.** Делитесь решениями и находками между агентами и
  сессиями. Каждый факт фиксирует, кто его записал, история версий и проверка конфликтов делают
  исправления прослеживаемыми, а пространство имён можно откатить к любому моменту времени.
  [Память](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)
- **Анализ кода с подтверждениями.** Находите определения, вызывающий код, влияние изменений и
  релевантные тесты; связывайте код с коммитами, pull request и задачами. Результаты указывают
  свою ревизию, расположение в исходном коде и полноту. [Индекс кода](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md)
- **Поиск с опорой на источники.** Ищите по доступным воспоминаниям, документам, диалогам и
  записям трекеров. Ответы ссылаются на источники; при низкой уверенности поиск может воздержаться от ответа.
  [Поиск](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md)
- **Передача работы между несколькими агентами.** Назначайте работу через постоянные очереди задач с
  арендой, повторными попытками, правилами проверки и результатами. Ваши агенты выполняют рабочий
  процесс, а Loom сохраняет передачи. [Очереди задач](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md)
- **Контроль над данными агентов.** Управляйте доступом к пространствам имён, происхождением данных, сроками хранения, записями
  аудита и проверяемым удалением. Контекст остаётся доступным для запросов в ClickHouse.
  [Безопасность](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

<a id="integrations"></a>

## Интеграции

| Интеграция                                                                                                                                                                                                                                                                                   | Что подключает                                                                |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md), [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md), [Cursor](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/cursor.md) и [другие MCP-клиенты](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) ([все интеграции](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md)) | Память и анализ кода в вашем текущем рабочем процессе с агентами.             |
| [Python](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md), [TypeScript](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md) и [HTTP](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)                                                                                                                                                         | Ваши собственные агенты и приложения.                                         |
| [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)                                                                                                                                                                                                                                          | Трассировки запросов, стоимость моделей, оценки поиска и привязка к версиям промптов. |
| [ClickStack / HyperDX](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)                                                                                                                                                                                                                            | Трассировки, логи и метрики OpenTelemetry в ClickHouse.                       |

Langfuse и ClickStack — необязательные интеграции, они не входят в состав Docker-стека.

<a id="benchmark-details"></a>

## Подробности бенчмарков

<a id="detailed-retrieval-reader-and-code-search-results"></a>

### Подробные результаты поиска, ридера и поиска по коду

| Оценка                                                | Результат                                                                                          |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| LongMemEval-S, поиск доказательств (500 вопросов)     | **98.6%** вопросов, для которых хотя бы одна подтверждающая сессия попала в первые 30 воспоминаний. |
| LongMemEval-S, фиксированный ридер `gpt-5` (500 вопросов) | **92.0%** точность ответов по 200 лучшим воспоминаниям.                                        |
| LoCoMo (все 10 диалогов)                              | **89.6%** точность ответов; **90.8%** корректных отказов отвечать на состязательные вопросы.       |
| MemoryAgentBench, выборка фактологических вопросов (500 вопросов) | **99.0%** SubEM; **95.7%** F1 по пересечению токенов на сгенерированных ответах.       |
| Концептуальный поиск по коду (36 вопросов)            | Полнота по эталонным файлам **91.2%** на 5 / **97.2%** на 10; MRR **0.91**.                        |
| Локализация файлов в SWE-bench Verified (200 задач)   | Задачи, в которых изменённый файл попал в первые 10: **82%** против **57%** у BM25; **76.5%** в первых 5. |
| Обнаружение дубликатов в трекере (104 пары)           | Зафиксированный более старый дубликат был на первом месте в **87.5%** случаев.                     |

Эти оценки измеряют разные задачи, а не дают единый итоговый балл. SWE-bench измеряет
локализацию файлов, а не решение задач. [Таблица результатов](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)
содержит определения метрик, оценки на малых выборках, более слабые и отрицательные результаты,
а также полные протоколы, модели и артефакты.

[Методология](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md) ·
[покрытие и известные пробелы](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md) ·
[запуск бенчмарков](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)

<a id="documentation-and-contributing"></a>

## Документация и участие в разработке

Чтобы разобраться с общим контекстом агентов, анализом кода и поиском с учётом прав доступа, начните с
[документации](https://github.com/ClickHouse/loom/blob/main/docs/index.md). Требования к развёртыванию описаны в разделе
[предварительных требований](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites).

[Развёртывание](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) · [FAQ](https://github.com/ClickHouse/loom/blob/main/docs/faq.md) ·
[Участие в разработке](https://github.com/ClickHouse/loom/blob/main/CONTRIBUTING.md) · [Сообщения об уязвимостях](https://github.com/ClickHouse/loom/blob/main/SECURITY.md) ·
[`llms.txt`](https://github.com/ClickHouse/loom/blob/main/llms.txt)

Apache-2.0 · [Лицензия](https://github.com/ClickHouse/loom/blob/main/LICENSE) · [Уведомление](https://github.com/ClickHouse/loom/blob/main/NOTICE)
