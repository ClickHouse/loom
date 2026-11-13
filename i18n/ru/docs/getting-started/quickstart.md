<!-- i18n: source=docs/getting-started/quickstart.md blob=b7af76d7843e0c8a0e3cb4146bb5e3a9f57ba395 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# Быстрый старт

На этой странице Loom запускается на ноутбуке, и в него сохраняется первая память через HTTP, Python и MCP. Ключ API и учётная запись не нужны. Свежая установка выпущенного стека создаёт эмбеддинги небольшой локальной моделью, которая входит в образ, поэтому поиск возвращает осмысленные результаты без сети (обновлённая установка сохраняет эмбеддер, указанный в её `.env`). LLM в ней фиктивная, поэтому извлечение и ответы `memory.ask` ничего не дают. В `.env` из checkout для обоих компонентов используются фиктивные провайдеры.

**Стандартная установка Docker рассчитана на одного пользователя:** без входа, с доступом только с localhost и пространством имён `dev/local` по умолчанию. Один человек может подключить несколько агентов, не включая многопользовательский режим. **Командные развёртывания тоже поддерживаются в OSS**: с аутентифицированными учётными записями и контролем доступа к пространствам имён; [начните в многопользовательском режиме](#start-in-multi-user-mode) или перейдите к разделу [Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes).
Оба режима поддерживают одни и те же возможности Loom; для настоящих LLM и эмбеддингов в любом режиме нужно настроить провайдеров.

<a id="prerequisites"></a>

## Требования

| Что нужно                      | Для чего                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------- |
| Docker с плагином compose      | Стек: ClickHouse и сервер Loom.                                                       |
| Python 3.12 или новее          | CLI `loom` только для контрибьюторов. SDK поддерживает версии 3.11+.                  |
| Node.js 20 или новее           | SDK для TypeScript (необязательно).                                                   |
| Свободный порт `7777`          | Loom. При разработке из checkout также открываются ClickHouse на `8123` и `9000`.     |
| `jq`                           | Чтение вывода `curl` на шаге 4 (необязательно).                                       |

<a id="1-boot-the-stack"></a>

## 1. Запуск стека

<a id="fast-path-released-image-no-clone"></a>

### Быстрый путь: готовый образ, без клонирования

Требование: Docker Desktop или Docker Engine с Compose 2.20 или новее.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Команда сохраняет установщик в текущем каталоге и затем запускает этот файл, поэтому выполняйте
её из домашнего каталога или другого каталога, в который можете писать только вы: в общем каталоге
кто-то другой может заменить `loom-install.sh` или направить его на другой файл до запуска. Если
загрузка не удалась, команда останавливается с ошибкой curl и ненулевым кодом возврата; после
неудачной установки исправьте то, о чём сообщает установщик, и запустите всю команду заново.
Установщик скачивает `loom-stack.tar.gz` из последнего GitHub Release, проверяет его SHA-256,
устанавливает конфигурацию Compose и ClickHouse в `~/.loom`, загружает соответствующие образы
`ghcr.io/clickhouse/loom:X.Y.Z` и `ghcr.io/clickhouse/loom-extractor:X.Y.Z` (сервис извлечения
документов) и ждёт ответа от `http://localhost:7777/v1/health`. Образ сервера содержит API,
воркеры, миграции и собранную React-панель управления в корне сервера; установка Python или Node
не нужна. Индексаторы Python, TypeScript и Go включены и работают. Для репозитория,
проиндексированного индексатором TypeScript, зависимости устанавливаются автоматически. Оба образа
поддерживают Linux `amd64` и `arm64`, в том числе Docker Desktop на Apple Silicon.

Когда Loom отвечает, установщик выполняет следующее:

1. **Проверяет память.** При новой установке он сохраняет тестовую память, читает её и удаляет,
   что показывает, что запись и чтение работают, и не оставляет действующей памяти (её удалённая
   версия отображается в разделе Recently deleted). Повторный запуск после успешной проверки или
   обновление пропускают этот шаг.
2. **Выясняет, включён ли вход.** Если вход включён, он не подключает ни одного агента и сообщает,
   где взять токены.
3. **Предлагает подключить ваших кодинг-агентов.** В терминале он спрашивает про каждого найденного
   агента: Claude Code и Codex подключаются через их плагины, VS Code через `code --add-mcp`.
   Вместо вопросов отвечает `LOOM_CONNECT`, в том числе при запуске без терминала, и проверяются
   только названные в нём клиенты: список через запятую из `claude-code`, `codex` и `vscode`,
   `all` или только `none`; любое другое значение останавливает установку. Без терминала и без
   `LOOM_CONNECT` ни один агент не проверяется и не изменяется. Плагин или MCP-сервер, настроенный
   ранее (плагином, `claude mcp add`, `codex mcp add` или `loom install`), остаётся как есть; для
   плагина Claude Code установщик выводит команду, которая направляет его на эту установку. Плагин
   Claude Code запускает Node.js 18 или новее из PATH, поэтому без него установщик не ставит плагин
   и выводит только строку `claude mcp add` для MCP-сервера; плагин Codex находит Node.js также
   через nvm и подобные инструменты, поэтому он устанавливается, а рядом выводится строка
   `codex mcp add`. Плагин Codex всегда использует `http://localhost:7777`, поэтому для установки
   в другом месте вместо этого выводится ссылка на его страницу настройки. VS Code не предлагается
   в удалённом сеансе или WSL. Если клиент, названный в `LOOM_CONNECT`, отсутствует в PATH,
   установщик сообщает об этом.
4. **Открывает панель управления** на десктопе при новой установке; `LOOM_NO_OPEN=1` это отключает.

Ctrl-C останавливает запуск на любом шаге; клонирование, которое CLI агента начал для своего
плагина, может ещё завершиться в фоне.

Стек запускает собственный контейнер ClickHouse. Чтобы использовать уже работающий у вас
ClickHouse, самостоятельно развёрнутый сервер или сервис ClickHouse Cloud (26.4 или новее, с
Keeper), передайте его URL; если в URL нет пароля, установщик запросит его, создаст базу данных
`loom` и пользователя только для чтения либо выведет SQL, который должен выполнить администратор
([выбор ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

`install.sh` каждого релиза устанавливает именно этот релиз, поэтому копия, которую отдаёт
`latest`, устанавливает последний релиз, а URL конкретного релиза, приведённый ниже, каждый раз
устанавливает этот релиз. Поверх более новой установки копия более старого релиза отказывается
устанавливаться, чтобы не запускать более старые Loom и ClickHouse на более новых данных; чтобы
намеренно понизить версию, следуйте инструкции
[downgrading an installer install](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install).
Чтобы зафиксировать релиз, используйте его собственный URL:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Чтобы всё проверить перед запуском, скачайте файлы и проверьте их:

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

Затем, когда выведенное выглядит корректно, установите из проверенного архива:

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

Значения `LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`, `LOOM_EXTRACTOR_IMAGE_REPOSITORY`
и `LOOM_CLICKHOUSE_IMAGE`, переданные установщику, записываются в `.env` каталога установки, так
что последующие команды `docker compose` в нём и последующие запуски установщика используют тот же
порт и те же образы. Значение `LOOM_BIND_HOST`, отличное от loopback, публикует Loom за пределами
этого хоста, и в таком виде сервер отказывается запускаться без `LOOM_PUBLIC_BASE_URL` и входа:
установите с `LOOM_SKIP_START=1`, задайте их в `.env` (ниже), затем снова выполните команду
установки без `LOOM_SKIP_START=1`.

Каждый релиз работает с тем релизом ClickHouse, который был последним на момент его сборки,
поэтому повторный запуск команды установки для той же версии Loom сохраняет её ClickHouse;
установка более новой версии Loom обновляет его, если только `.env` не фиксирует
`LOOM_CLICKHOUSE_IMAGE` (удалите эту строку, чтобы снова следовать релизам). Повторный запуск
команды установки обновляет управляемые файлы Compose и конфигурации и приводит контейнеры в
нужное состояние. Он сохраняет `~/.loom/.env`, добавляя в файл без них URL экстрактора и токен, а
также именованные тома Docker. Чтобы остановить без удаления данных:

```bash
docker compose --project-directory "$HOME/.loom" down
```

Добавляйте `--volumes` только для намеренного необратимого сброса.

<a id="try-it-in-github-codespaces"></a>

### Попробовать в GitHub Codespaces

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

Dev-контейнер репозитория запускает последний релиз в codespace, ничего не устанавливая на вашу
машину: он устанавливает Loom описанным выше установщиком, запускает его и подключает к нему
Claude Code. При первом запуске загружается около 3 ГБ образов, это занимает несколько минут.
Откройте панель управления по адресу, который выводят последние строки журнала создания (`Loom is
running in the codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, это
перенаправленный адрес порта 7777 на вкладке **Ports**); адрес установщика `http://127.0.0.1:7777/`
выше него является собственным адресом codespace. В терминале codespace выполните `cd ~ && claude`
(вне checkout, инструкции которого предназначены для работы над самим Loom), войдите и скажите
"Remember that I never deploy on Friday"; в новом сеансе `claude` спросите "When can I deploy?".

Пробная версия работает в однопользовательском режиме, без входа. В браузере порт 7777
перенаправляется как приватный: его адрес отвечает только вашему аккаунту GitHub. Оставьте его
приватным: публичный порт или приватный для организации открывает сервер без входа для всех, кому
он доступен. Агенты внутри codespace, например его `claude`, используют `http://localhost:7777`;
строки подключения в панели управления называют вместо этого перенаправленный адрес, для которого
нужен вход в GitHub, которого у агентов в codespace нет. При открытии в десктопном VS Code
codespace также перенаправляет порт на `localhost:7777` вашего ноутбука, где он открыт так же, как
локальная установка, для каждой программы на ноутбуке, и агенты на ноутбуке могут достучаться до
него там; для перенаправления этот локальный порт должен быть свободен, поэтому сначала остановите
локальный Loom. Используйте панель управления по адресу в браузере: с `localhost:7777` ноутбука
страница открывается, но сервер отклоняет её запросы, поэтому данных она не показывает; собственной
страницей этого сервера является только перенаправленный адрес.

Установка находится в `/workspaces/.loom`, поэтому пересобранный codespace сохраняет свои `.env` и
проект Compose, хотя его память хранится в томах Docker, которые пересборка может не сохранить;
пересборка также запускает поверх неё установщик последнего релиза, что переводит её на этот
релиз. Везде, где в этой документации упоминается `~/.loom` или `"$HOME/.loom"`, в codespace
используется `/workspaces/.loom`. Codespaces предлагает открыть порт 7777, как только он начинает
слушать; панель управления отвечает после того, как журнал создания выведет "Loom is running in the
codespace". Если журнал вместо этого заканчивается ошибкой, выполните в терминале codespace
`sh .devcontainer/start-loom.sh`: он сохраняет то, что настроил первый запуск.

<a id="start-in-multi-user-mode"></a>

### Запуск в многопользовательском режиме

Для новой установки из готового образа с учётными записями и входом с первой загрузки подготовьте
файлы стека, не запуская контейнеры:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Отредактируйте `~/.loom/.env` до запуска Docker. Замените существующие записи идентификации и
аутентификации и добавьте параметр регистрации:

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Оставьте сгенерированные `COMPOSE_PROJECT_NAME` и `LOOM_JWT_SECRET`. Затем загрузите образы и
запустите стек (со своим ClickHouse сначала выполните `ch-setup`, как сообщил установщик):

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

После того как [проверки состояния](#check-the-stack) пройдены, откройте `http://localhost:7777/` и
зарегистрируйтесь. Клиентам SDK и MCP нужны токены; используйте организацию и пространства имён,
выданные вашей учётной записи, а не однопользовательские значения по умолчанию `dev/local` из
примеров ниже.

Это включает многопользовательскую аутентификацию, но не публичный доступ по сети: стек по-прежнему
привязан к localhost. Прежде чем открывать его команде, настройте [доступ оператора](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)
и [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls). Настройте также [реальные модели и ключи API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md);
выбор многопользовательского режима не заменяет фиктивный LLM.

Для уже работающего однопользовательского стека следуйте вместо этого [инструкции по переключению](#2-open-the-dashboard);
пересоздание контейнеров сохраняет существующие тома данных.

<a id="contributor-path-build-a-checkout"></a>

### Путь для контрибьюторов: сборка из checkout

Требование: Docker Compose 2.34 или новее, который нужен compose-файлу checkout, чтобы ежедневно
загружать ClickHouse.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` делает три вещи:

1. Создаёт два файла, которые git не отслеживает: `.env` из `.env.example` и
   `deploy/clickhouse-config/named_collections.xml` из его шаблона. Он никогда не перезаписывает
   ни один из них; существующему `.env` без токена экстрактора он добавляет токен.
2. Запускает dev-стек compose с `--build`.
3. Опрашивает `http://localhost:7777/v1/health`, пока сервер не будет готов, выводя число
   ожидающих миграций. Он сдаётся по истечении `--timeout` (300 с), выводя последнее тело ответа
   health и строки журнала.

Первая загрузка для контрибьютора собирает образ и применяет все миграции схемы, поэтому
рассчитывайте на несколько минут. Затем `loom up` выводит URL панели управления
(`http://localhost:7777/`), вызов `curl` и фрагмент на Python, которые сохраняют первую память, а
также строку, подключающую Claude Code. Когда аутентификация включена, он также создаёт токен
записи и подставляет его в эти фрагменты.

| Флаг         | Действие                                |
| ------------ | --------------------------------------- |
| `--rebuild`  | Пересобрать после изменения кода.       |
| `--no-build` | Использовать кешированный образ.        |
| `--open`     | Открыть панель управления.              |
| `--port`     | Использовать другой порт Loom.          |
| `--env-file` | Указать другой файл окружения.          |

Повторный запуск `loom up` на работающем стеке лишь заново проверяет состояние. `make dev` делает
то же самое без CLI и выполняет:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

Поскольку `compose.yaml` указан первым, Compose читает `.env` из корня репозитория для подстановок
`${VAR:-default}`; `--env-file .env` лишь указывает это явно. `loom down` останавливает стек и
сохраняет его тома. `loom down --delete-data` безвозвратно удаляет только именованные тома этой
установки, и лишь после того, как вы введёте сгенерированное имя проекта Compose; `--yes` является
подтверждением без диалога. Цели Makefile делают то же самое: `make down` сохраняет данные, а
`make down-all` удаляет их.

<a id="what-the-stock-env-boots"></a>

### Что запускает стандартный `.env`

Шаблоны `.env` из checkout и из релизного архива оба выбирают однопользовательский режим.
Релизный архив задаёт `LOOM_IDENTITY_PROVIDER=single-user` явно; checkout оставляет его пустым и
определяет тот же режим по `LOOM_AUTH_REQUIRED=false`.

| Параметр                                                                     | Значение      | Эффект                                                                           |
| ---------------------------------------------------------------------------- | ------------- | -------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (or empty)  | `single-user` | Без входа. Пространство имён по умолчанию `dev/local`. Вызовам SDK и MCP токен не нужен. |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | фиктивный LLM | Извлечение, тегирование сущностей и синтез `memory.ask` ничего не производят.    |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (release bundle)      | bge-small     | Семантический поиск на CPU по файлам модели, которые входят в образ.             |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (checkout) | фиктивный эмбеддер | Поиск ранжирует по символьным n-граммам. Достаточно, чтобы проверить связность, но не полноту. |

Оба установщика генерируют уникальный `COMPOSE_PROJECT_NAME`, случайный секрет подписи JWT,
случайный токен экстрактора и `LOOM_BIND_HOST=127.0.0.1`. Поэтому отдельные клоны не разделяют
молча тома ClickHouse, а Loom и ClickHouse доступны только с этого компьютера.

Для реальных провайдеров откройте **Settings → Models & keys**, выберите провайдеров и модели,
введите их ключи API, затем нажмите **Test & apply**. Один ключ OpenAI API может обслуживать и LLM,
и эмбеддинги. Перезапуск Docker не нужен; изменения эмбеддингов запускают проверяемую пересборку, а
ход выполнения и повторная попытка показываются в панели управления.

Для развёртывания на основе переменных окружения замените строки провайдеров в `~/.loom/.env`
(стек из релиза) или в `.env` (checkout). Одна лишь установка `OPENAI_API_KEY` ничего не меняет:
стандартные строки провайдеров называют своих провайдеров явно. Переход с локального или
фиктивного эмбеддера заново вычисляет эмбеддинги для сохранённых данных.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Для Claude, OpenAI, размещённых эмбеддингов, клиентов только с подпиской и развёртываний с одним
провайдером LLM следуйте [Configure models and API keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).
Только после правки `.env` стека из релиза примените её командой
`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`;
обычный перезапуск не перезагружает окружение контейнера. Расширенные настройки описаны в
[Deployment → Providers](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers).

<a id="check-the-stack"></a>

### Проверка стека

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` возвращает `status: ok`, когда ClickHouse доступен и ни одна проблема, кроме
  `code_tier_failing` (сбойный уровень индекса кода репозитория), не является ошибкой.
  `fake_embedding`, `fake_llm` и `providers_degraded` показывают, в каком вы режиме; `problems`
  перечисляет каждую проблему с её исправлением; `schema.pending` перечисляет неприменённые
  миграции.
- `loom doctor` выводит по одной строке на каждую серверную проверку. `--json` даёт JSON. Для стека
  из исходного checkout выполните `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`.

<a id="2-open-the-dashboard"></a>

## 2. Откройте панель управления

`http://localhost:7777/` сразу открывает панель управления в режиме `single-user`;
в многопользовательском режиме сначала зарегистрируйтесь или войдите. В режиме `single-user`
панель управления и значения по умолчанию для агента используют `dev/local` (`LOOM_DEFAULT_NAMESPACE` переименовывает
пространство имён). Это режим для удобства, а не граница изоляции пользователей: любой, кто может
подключиться к серверу, получает доступ к его данным и администрированию. Используйте его только на localhost.

Чтобы вместо этого попробовать регистрацию (`trust-network`: email и пароль, а первый, кто
займёт имя пространства имён, становится его администратором), замените эти строки в `~/.loom/.env`
(выпущенный стек) или `.env` (checkout):

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Одного `LOOM_AUTH_REQUIRED=true` недостаточно, если явно выбран `single-user`:
в этом режиме аутентификация остаётся выключенной. Сохраните сгенерированный установщиком
`LOOM_JWT_SECRET` и существующие тома, затем пересоздайте сервер, чтобы он загрузил окружение:

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

После этого панель управления открывается на странице входа со вкладкой **Sign up**, а каждый вызов SDK
и MCP требует токен. Режимы идентификации описаны в разделе [Identities and
namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes). Явное указание
`trust-network` также позволяет запустить сервер с `LOOM_BIND_HOST`, отличным от loopback, при открытой регистрации,
поэтому любой, кто достучится до порта, сможет зарегистрироваться. Прежде чем открывать сервер
для команды, следуйте [руководству по развёртыванию](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) по
доступу оператора и [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls).

<a id="3-get-a-token"></a>

## 3. Получите токен

В однопользовательском режиме токен необязателен. Но если выпустить его всё равно, ваши скрипты продолжат работать после включения аутентификации при условии, что они указывают свои `org` (или передают его пустым), `namespace` и `agent`: вызов, который отправляет учётные данные, как это делает SDK, когда находит их ([Python
SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)), не получает автоматически ни одного из этих значений, а пустой `org` заменяется собственным значением токена.

- **Панель управления:** **Settings → Tokens → New token**. По умолчанию токен читает все
  пространства имён и репозитории, к которым вы относитесь (ограниченный репозиторий только если вы его добавите),
  но ни одной коллекции, и сохраняет данные в пространство имён, в котором вы находитесь. Режим _Only what you choose_ ограничивает токен
  пространствами имён, коллекциями и репозиториями, которые вы отметите (сначала ни одного); отмеченные
  просматриваются при поиске в порядке списка, а порядок меняется стрелками строки или, на
  компьютере, перетаскиванием за её ручку. В результате токен показывается один раз вместе с настройкой
  для вашего MCP-клиента или скрипта.
- **CLI, от имени оператора:** выполните команду внутри контейнера сервера, где хранится секрет
  подписи:

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  Добавьте `--ttl-days 30` для всего, что покидает вашу машину. `loom mint-token --help`
  выводит список параметров ограничения области (`--namespace-role`, `--read-scope`, `--traffic-class`).

- **Бот, который регистрируется сам** (только в доверенной сети):
  `loom init --agent nightly-triage --server-url http://localhost:7777` создаёт
  пространство имён `agent-nightly-triage`, сохраняет токен с ролью writer в `~/.loom/token`, а URL
  в `~/.loom/config.json`, и выводит код подтверждения. Человек один раз вводит его на
  `/oauth/agent-claim` со своим паролем, чтобы стать владельцем.

Сохраните токен там, где его ищут SDK, CLI и плагины:

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

Claims, роли и гранты описаны в разделе [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="4-first-memory-over-http"></a>

## 4. Первая память по HTTP

Каждая операция — это `POST /v1/<op>` с телом JSON. Свежая установка со стандартным
`.env`, из установщика или из checkout, работает в однопользовательском режиме: без токена, и обычный вызов памяти
или поиска (`memory.set`, `memory.get`, `memory.search` и ещё несколько), в котором не указаны
`org` и `namespace`, выполняется в собственных org `dev` и пространстве имён `local` этой установки — тех, которые используют
MCP и панель управления. `memory.set` без `agent` записывается от имени агента
`http`, а поиск читает память всех агентов. Поэтому вызовам ниже нужны только их собственные поля
([какие операции](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)). Если вызов без токена возвращает 401
`AUTH_INVALID`, значит, включена аутентификация: добавьте `-H "Authorization: Bearer $LOOM_API_KEY"` и укажите
`org`, `namespace` и `agent` вашей учётной записи; заголовок с пустым токеном отклоняется.

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.get -H 'Content-Type: application/json' \
  -d '{"memory_key":"deploy_window"}' | jq '{found, value}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"query":"when can we deploy?"}' | jq '.results[0].content_excerpt'
```

Ответ `memory.set` содержит `memory_id`, `content_sha256` и `status`.
`memory.get` читает один ключ: `found` показывает, хранит ли ключ актуальное значение, а
`value` — это значение в том виде, в каком оно было записано. Ответ поиска содержит `results` и `abstain`,
который показывает, достаточно ли сильно лучшее совпадение, чтобы отвечать на его основе. Поиск читает память
всех агентов в пространстве имён, включая добавленную в панели управления; укажите
`agent`, чтобы читать только память этого агента.
`GET /v1/operations` перечисляет все операции, а <http://localhost:7777/docs> позволяет попробовать
каждую из браузера. Форматы описаны в [справочнике по HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).

<a id="5-first-memory-from-python"></a>

## 5. Первая память из Python

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

Релиз, которого ещё нет на PyPI, устанавливается из его ассетов
([установка из GitHub Release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)).

В примере указаны `org` и `namespace`, поэтому он работает как с токеном из шага 3, так и без него: SDK отправляет токен, если находит его. На односерверной установке для одного пользователя вызов без токена может их не указывать: `Agent(agent="quickstart", base_url=...)` тоже работает в `dev`/`local`.

```python
import asyncio

from loom import Agent


async def main():
    async with Agent(
        org="dev",
        namespace="local",
        agent="quickstart",  # names the writer in the audit log
        base_url="http://localhost:7777",
    ) as a:  # sends a credential it finds; none needed in single-user mode
        await a.memory.set("standup_time", "Daily standup is at 9:30.")
        row = await a.memory.get("standup_time")  # None when the key is unset
        print(row["value"] if row else "unset")
        for hit in await a.memory.search("when is standup?"):
            print(hit.content_excerpt)


asyncio.run(main())
```

Где SDK находит свой сервер и учётные данные, описано в
[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials).
TypeScript SDK устанавливается командой `npm install @clickhouse/loom`
([из клона репозитория](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)).

Клиентские интерфейсы описаны в справочниках [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) и
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md).

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. Первая память от агента для написания кода

Если установщик уже подключил вашего агента, перейдите к последнему абзацу этого раздела.

Для Claude Code установите плагин Loom. Он добавляет хуки (контекст при старте сессии,
сохранение в конце) и навыки поверх инструментов MCP:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

На односерверной установке для одного пользователя на этой машине оставьте URL сервера по умолчанию и не заполняйте токен, когда Claude Code спросит настройки плагина.

Без плагина добавьте запись MCP напрямую:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

Оба способа подключаются к серверу для одного пользователя на этой машине без вашего токена.

Если включена аутентификация, создайте токен в **Settings → Tokens** и выберите **Claude Code** в разделе
**Connect**: диалог покажет настройку для плагина и для записи `claude mcp add` уже с вашим токеном. Затем:

1. Перезапишите `~/.loom/config.json` блоком `config.json` из диалога и добавьте
   пространство имён, в которое сохраняет токен (`"namespace": "<namespace>"`): `org` или `namespace`, оставшиеся в файле от сессии для одного пользователя, имеют приоритет над токеном, а без них плагин пишет в первое пространство имён токена, отличное от `*`, либо в `coding`. Верните обратно `code_repo` или `agent`, если вы их задавали.
2. Оставьте `LOOM_API_KEY` пустым (на шаге 3 могла быть выполнена его экспортирующая команда: `unset LOOM_API_KEY`) и не заполняйте параметр плагина `token`, либо задайте в них новый токен: плагин читает оба значения раньше, чем этот файл
   ([конфигурация](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. Удалите запись `claude mcp add`, созданную без токена (`claude mcp remove loom -s user`), перед запуском команды из диалога: она отказывается работать, если такое имя уже существует.

`loom install --client <name>` записывает запись MCP для Claude Desktop, Claude Code,
Codex, Cursor, VS Code, Cline, Windsurf и Zed; без флага он подключает всех клиентов, которых найдёт. О Codex и Cursor рассказано в разделе [Connect a coding agent](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md).

Начните новую сессию и скажите _«Запомни, что я никогда не деплою по пятницам.»_ Агент вызовет
`loom_memory_set`. В новой сессии спросите _«Когда мне можно деплоить?»_ Агент вызовет
`loom_search` и ответит на основе сохранённого факта. Модель сама решает, когда сохранять и искать, по описаниям инструментов; триггерных слов нет.

<a id="7-see-it-in-the-dashboard"></a>

## 7. Посмотрите в панели управления

| Страница          | Что показывает                                              |
| ----------------- | ----------------------------------------------------------- |
| `/memories`       | Память пространства имён с указанием автора записи и времени. |
| `/admin/activity` | Журнал аудита: какой агент записал какой ключ и когда.      |
| `/admin/usage`    | Стоимость по операциям и агентам.                           |

Подробнее см. в разделе [Панель управления](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

<a id="optional-index-a-repository-on-the-released-stack"></a>

## Необязательно: индексация репозитория на выпущенном стеке

Добавление репозитория из панели управления на этом стеке отклоняется с ошибкой `SANDBOX_UNAVAILABLE`:
для него нужна [песочница заданий репозитория](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox),
которую этот стек оставляет выключенной, потому что стандартная изоляция Docker её блокирует. Указывайте только
репозитории, которым вы доверяете: их шаги сборки (установка зависимостей TypeScript, загрузка модулей Go,
CMake) выполняются без песочницы, в контейнере `loom`, в окружении которого находятся секрет JWT
и ключи провайдеров.

`LOOM_CODE_REPOS_JSON` — это одна строка, содержащая все репозитории в виде JSON-массива
([Настройка записи](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
В `~/.loom/.env` раскомментируйте эту строку (добавьте её, если её нет) и отредактируйте.
`dev` — это организация, от имени которой работает стек для одного пользователя, и организация, в которую
регистрируются учётные записи в многопользовательском режиме (`LOOM_SELF_SIGNUP_ORG`); если вы это изменили,
используйте организацию своей учётной записи здесь и в `--org` ниже:

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom выбирает индексаторы уровня компилятора по файлам сборки репозитория (`go.mod`,
  `package.json`, `pyproject.toml`, корневой `CMakeLists.txt`, …); вместо этого `scip_producer` задаёт
  один из них ([Уровень компилятора](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  Для C/C++ также нужно настроенное дерево сборки, поэтому без него для репозитория C/C++ доступен только
  уровень парсера.
- Для приватного репозитория добавьте токен GitHub (`LOOM_CODE_GITHUB_TOKEN` в `.env` или
  `github_token` в записи) и уберите `"public":true`: этот параметр предназначен для репозиториев, публичных на
  своём хостинге, и позволяет любому участнику организации сразу присоединиться к индексу как читатель
  в разделе **Repositories**. Решите это до первого `loom code init`: он задаёт политику присоединения
  пространства имён по значению `public`, и последующее изменение `public` её не меняет (сопровождающий
  меняет её через `code.repo.set_access`).

Затем пересоздайте контейнер `loom` и запустите первую индексацию:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` работает на локальной модели эмбеддингов выпущенного стека. При поддельной модели эмбеддингов (в `.env`
репозитория-чекаута или в обновлённой установке, где всё ещё указано `fake-ngram-*`) команда отказывается работать и
завершается; чтобы всё же проверить связку, добавьте `--allow-fake-embedder`. Выбор модели позже в
**Settings → Models & keys** заново строит эмбеддинги индекса кода; после изменения `.env` их нужно
перестроить отдельно, как описано в разделе [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).

В многопользовательском режиме у указанного в списке репозитория нет сопровождающего, поэтому без `"public":true` запрос
читателя на присоединение ждёт, а подтвердить его некому. После `loom code init` дайте кому-нибудь доступ от имени
оператора с помощью токена, созданного в контейнере: `admin` делает пользователя сопровождающим, который
затем подтверждает читателей, а `viewer` позволяет читать. Субъект — это пространство имён, с которым
зарегистрировалась учётная запись (её логин, субъект её токенов); адрес электронной почты при самостоятельной регистрации
никогда не подтверждается, поэтому доступ, выданный на него, никого не допустит:

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## Дополнительно: задайте вопрос по документу

```bash
loom demo
```

`loom demo` загружает файл `AGENTS.md` из этого репозитория, обрабатывает его с помощью `knowledge.ingest` (его разбирает эталонный экстрактор стека) и задаёт вопрос по нему через `knowledge.synthesize`, после чего выводит
ответ и фрагменты, на которые он ссылается. С фиктивной LLM вместо этого выводятся лучшие
результаты `knowledge.search`. Флаги `--file` и `--question` позволяют указать ваш собственный документ и вопрос.

<a id="common-problems"></a>

## Частые проблемы

| Симптом                                           | Причина и решение                                                                                                                                                                              |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` не записывает факты           | Используется фиктивная LLM. Настройте настоящую по инструкции [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).                           |
| Результаты поиска выглядят случайными             | Используется фиктивный эмбеддер (`fake-ngram-*` в `.env`). Переключите его, как описано в разделе [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md); сохранённые данные будут встроены заново. Не удаляйте тома, чтобы сменить модели. |
| 401 `AUTH_INVALID` после включения аутентификации | Токена нет, либо он подписан другим `LOOM_JWT_SECRET`. Выпустите токен заново командой Compose из раздела [Получите токен](#3-get-a-token).                                                      |
| Панель управления возвращает 503                  | Сервер запущен из исходников (`make server`) без собранного фронтенда. Выполните `make frontend-build`.                                                                                        |
| Стек из релиза не становится работоспособным      | Выполните `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`. См. [Устранение неполадок](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md). |
| При добавлении репозитория возвращается `SANDBOX_UNAVAILABLE` | Стек запущен без песочницы для задач репозитория. Проиндексируйте репозиторий через `LOOM_CODE_REPOS_JSON` ([Индексация репозитория](#optional-index-a-repository-on-the-released-stack)). |
| Установщик сообщает, что порт `7777` занят        | Его слушает другая программа или другой экземпляр Loom. Остановите её либо запустите команду установки снова, указав перед `sh loom-install.sh` переменную `LOOM_PORT=7778`; `.env` сохранит порт. |
| Установщик сообщает, что порт `7777` не отвечает  | Порт занят программой, которая не отвечает, либо `LOOM_BIND_HOST`, `LOOM_PORT` или брандмауэр не дают этому хосту до него добраться.                                                            |
| Установщик не смог проверить порт `7777`          | Curl завершился с ошибкой, которую показывает его код выхода; проверьте `LOOM_BIND_HOST` и `LOOM_PORT`.                                                                                        |
| В клоне занят порт `8123` или `9000`              | Переназначьте порты ClickHouse на хосте ([локальный стек](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                                |
| Установщик не смог загрузить образы               | Docker сам обращается к реестру: если вы работаете через прокси, задайте демону Docker [настройки прокси](https://docs.docker.com/engine/daemon/proxy/).                                        |

<a id="next"></a>

## Дальше

- [Оценка Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): превратите этот smoke-тест в оценку качества,
  задержки и стоимости под вашу нагрузку.
- [Подключение агента для написания кода](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): Claude Code, Codex или Cursor
  на этом стеке.
- [Ваш первый агент](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): идентичность, обработка abstain, сессии, context packs
  и запуски (runs) из SDK.
- [Развёртывание](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): запуск Loom для команды.
