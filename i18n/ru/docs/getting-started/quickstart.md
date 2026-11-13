<!-- i18n: source=docs/getting-started/quickstart.md blob=9acf8dfd8cddd013c2cec38468a13b3cdd0010e8 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# Быстрый старт

На этой странице Loom запускается на ноутбуке, и в него сохраняется первая память через HTTP, Python и MCP. Ключ API и учётная запись не нужны. Свежая установка выпущенного стека создаёт эмбеддинги небольшой локальной моделью, которая входит в образ, поэтому поиск возвращает осмысленные результаты без сети (обновлённая установка сохраняет эмбеддер, указанный в её `.env`). LLM при этом фиктивная, поэтому извлечение и ответы `memory.ask` ничего не выдают. В `.env` исходного репозитория для обоих компонентов используются фиктивные провайдеры.

**Стандартная установка Docker рассчитана на одного пользователя:** без входа, с доступом только с localhost и пространством имён `dev/local` по умолчанию. Один человек может подключить несколько агентов, не включая многопользовательский режим. **Командные развёртывания тоже поддерживаются в OSS**: с аутентифицированными учётными записями и управлением доступом к пространствам имён; [запустите в многопользовательском режиме](#start-in-multi-user-mode) или следуйте разделу [Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes). Оба режима поддерживают одни и те же возможности Loom; для реальных LLM и эмбеддингов в любом режиме нужно настроить провайдеров.

<a id="prerequisites"></a>

## Требования

| Что нужно                      | Для чего                                                                          |
| ------------------------------ | --------------------------------------------------------------------------------- |
| Docker с плагином compose      | Стек: ClickHouse и сервер Loom.                                                   |
| Python 3.12 или новее          | CLI `loom` (только для контрибьюторов). SDK поддерживает версии 3.11+.            |
| Node.js 20 или новее           | SDK для TypeScript (необязательно).                                               |
| Свободный порт `7777`          | Loom. При разработке из исходников также открываются ClickHouse на `8123` и `9000`. |
| `jq`                           | Чтение вывода `curl` на шаге 4 (необязательно).                                   |

<a id="1-boot-the-stack"></a>

## 1. Запуск стека

<a id="fast-path-released-image-no-clone"></a>

### Быстрый путь: готовый образ, без клонирования

Требования: Docker Desktop или Docker Engine с Compose 2.20 или новее. В Windows запускайте
команду в терминале WSL 2, где работает `docker`: это Docker Desktop с включённой интеграцией
WSL для этого дистрибутива или Docker Engine, установленный в нём. Установщик — это
скрипт POSIX shell.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Команда сохраняет установщик в текущем каталоге и затем запускает этот файл, поэтому
запускайте её из домашнего каталога или другого каталога, в который можете писать только вы: в общем каталоге
кто-то другой может заменить `loom-install.sh` или указать на другой файл до запуска. Если
загрузка не удалась, команда останавливается с ошибкой curl и ненулевым кодом выхода; после неудачной установки исправьте
то, о чём сообщено, и запустите всю команду снова. Установщик скачивает
`loom-stack.tar.gz` из последнего релиза GitHub, проверяет его SHA-256, устанавливает
конфигурацию Compose и ClickHouse в `~/.loom`, загружает соответствующие образы
`ghcr.io/clickhouse/loom:X.Y.Z` и `ghcr.io/clickhouse/loom-extractor:X.Y.Z` (служба
извлечения документов) и ждёт ответа от `http://localhost:7777/v1/health`. Образ
сервера содержит API, воркеры, миграции и собранную React-панель управления в корне
сервера; установка Python или Node не нужна. Индексаторы Python, TypeScript и компиляторные индексаторы Go
входят в образ и включены. Для репозитория, проиндексированного индексатором TypeScript, зависимости
устанавливаются автоматически. Оба образа поддерживают Linux `amd64` и `arm64`,
в том числе Docker Desktop на Apple Silicon.

Когда Loom отвечает, установщик выполняет следующее:

1. **Проверяет память.** При новой установке он сохраняет тестовую память, читает её обратно и удаляет,
   что показывает работоспособность записи и чтения и не оставляет живой памяти (её удалённая версия
   видна в разделе Recently deleted). При повторном запуске после успешной проверки или при обновлении
   этот шаг пропускается.
2. **Выясняет, включён ли вход.** Если вход включён, он не подключает агентов и сообщает, где
   брать токены.
3. **Предлагает подключить ваших кодовых агентов.** В терминале он спрашивает про каждого найденного:
   Claude Code и Codex через их плагины, VS Code через `code --add-mcp`.
   Вместо вопросов отвечает `LOOM_CONNECT`, в том числе при запуске без терминала, и рассматриваются только
   названные в нём клиенты: список через запятую из `claude-code`, `codex` и `vscode`, `all`
   или одно `none`; любое другое значение останавливает установку. Без терминала и без
   `LOOM_CONNECT` ни один агент не проверяется и не изменяется. Плагин или MCP-сервер, настроенный ранее
   (плагином, `claude mcp add`, `codex mcp add` или `loom install`), остаётся как есть;
   для плагина Claude Code установщик выводит команду, которая направляет его на эту
   установку. Плагин Claude Code запускает Node.js 18 или новее из PATH, поэтому без него
   установщик не ставит плагин и выводит только строку `claude mcp add` для MCP-сервера;
   плагин Codex находит Node.js и через nvm и подобные средства, поэтому он устанавливается, а рядом
   выводится строка `codex mcp add`. Плагин Codex всегда использует `http://localhost:7777`, поэтому
   для установки в другом месте вместо этого выводится ссылка на страницу его настройки. VS Code не
   предлагается в удалённом сеансе или в WSL. Если клиент, названный в `LOOM_CONNECT`, отсутствует в PATH,
   об этом сообщается.
4. **Открывает панель управления** на настольной системе при новой установке; `LOOM_NO_OPEN=1` это пропускает.

При запуске из WSL эти шаги выполняются внутри WSL: агента, работающего в Windows, подключайте из
Windows по [руководству по подключению](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), а адрес панели управления,
который выводит установщик, открывайте в браузере Windows.

Ctrl-C останавливает запуск на любом шаге; клонирование, начатое CLI агента для его плагина, может
всё же завершиться в фоне.

Стек запускает собственный контейнер ClickHouse. Чтобы использовать уже работающий ClickHouse,
самостоятельно управляемый сервер или службу ClickHouse Cloud (26.4 или новее, с Keeper), передайте его
URL; установщик запросит пароль, если в URL его нет, создаст базу данных `loom`
и пользователя-читателя только для чтения или выведет SQL, который должен выполнить администратор
([выбор ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

`install.sh` каждого релиза устанавливает именно этот релиз, поэтому копия, которую отдаёт `latest`, устанавливает
последний релиз, а URL конкретного релиза, приведённый ниже, всегда устанавливает этот релиз. Поверх более новой
установки копия более старого релиза отказывается устанавливаться, чтобы не запускать более старые Loom и
ClickHouse на более новых данных; чтобы понизить версию намеренно, следуйте
[инструкции по понижению версии установки через установщик](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install).
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

Затем, когда вывод выглядит правильным, установите из проверенного пакета:

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

Значения `LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`, `LOOM_EXTRACTOR_IMAGE_REPOSITORY`
и `LOOM_CLICKHOUSE_IMAGE`, переданные установщику, записываются в `.env` каталога установки,
поэтому последующие команды `docker compose` в нём и последующие запуски установщика используют тот же
порт и те же образы. Значение `LOOM_BIND_HOST`, отличное от loopback, публикует Loom за пределами этого хоста,
и сервер отказывается запускаться так без `LOOM_PUBLIC_BASE_URL` и входа:
установите с `LOOM_SKIP_START=1`, задайте их в `.env` (см. ниже), затем снова запустите команду установки
без `LOOM_SKIP_START=1`.

Каждый релиз запускает тот релиз ClickHouse, который был последним на момент его сборки, поэтому повторный запуск
команды установки для той же версии Loom сохраняет её ClickHouse; установка более новой
версии Loom его обновляет, если только `.env` не фиксирует `LOOM_CLICKHOUSE_IMAGE` (удалите эту строку, чтобы
снова следовать релизам). Повторный запуск команды установки обновляет управляемые
файлы Compose и конфигурации и приводит контейнеры в соответствие. Он сохраняет `~/.loom/.env`,
добавляя в файл без них URL извлекателя и токен, а также именованные тома Docker. Чтобы
остановить без удаления данных:

```bash
docker compose --project-directory "$HOME/.loom" down
```

Добавляйте `--volumes` только для намеренного, необратимого сброса.

<a id="try-it-in-github-codespaces"></a>

### Попробовать в GitHub Codespaces

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

Dev-контейнер репозитория запускает последний релиз в codespace, ничего не требуя на вашей
машине: он устанавливает Loom описанным выше установщиком, запускает его и подключает Claude
Code. При первом запуске скачивается около 3 ГБ образов, это занимает несколько минут. Откройте
панель управления по адресу, который выводят последние строки журнала создания (`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, это перенаправленный адрес
порта 7777 на вкладке **Ports**); адрес установщика `http://127.0.0.1:7777/` выше него —
собственный адрес codespace. В терминале codespace выполните `cd ~ && claude` (вне
репозитория, чьи инструкции предназначены для работы над самим Loom), войдите и скажите «Remember
that I never deploy on Friday»; в новом сеансе `claude` спросите «When can I deploy?».

Пробная версия работает в однопользовательском режиме, без входа. В браузере порт 7777 перенаправляется
приватно: его адрес отвечает только вашей учётной записи GitHub. Оставьте его приватным: публичный
или приватный для организации доступ открывает сервер без входа всем, с кем он общий.
Агенты внутри codespace, например его `claude`, используют `http://localhost:7777`; в строках подключения
панели управления указан перенаправленный адрес, для которого нужен вход в GitHub, которого у
агентов в codespace нет. При открытии в настольном VS Code codespace
также перенаправляет порт на `localhost:7777` вашего ноутбука, где он так же открыт, как локальная
установка, для любой программы на ноутбуке, и агенты на ноутбуке могут обратиться к нему там; для
перенаправления этот локальный порт должен быть свободен, поэтому сначала остановите локальный Loom. Пользуйтесь панелью управления
по адресу в браузере: с `localhost:7777` ноутбука страница откроется, но сервер отклонит
её запросы, поэтому данных она не покажет; только перенаправленный адрес является собственной страницей этого сервера.

Установка находится в `/workspaces/.loom`, поэтому пересобранный codespace сохраняет свой `.env` и
проект Compose, хотя его памяти лежат в томах Docker, которые пересборка может не сохранить; пересборка также запускает
поверх неё установщик последнего релиза, который обновляет её до этого релиза.
Везде, где в этих документах названы `~/.loom` или `"$HOME/.loom"`, в codespace используется
`/workspaces/.loom`. Codespaces предлагает открыть порт 7777, как только он начинает слушать; панель
управления отвечает, когда журнал создания выводит «Loom is running in the codespace». Если
журнал вместо этого заканчивается ошибкой, выполните `sh .devcontainer/start-loom.sh` в терминале
codespace: он сохраняет то, что настроил первый запуск.

<a id="start-in-multi-user-mode"></a>

### Запуск в многопользовательском режиме

Для новой установки из готового образа с учётными записями и входом с первого запуска
подготовьте файлы стека, не запуская контейнеры:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Отредактируйте `~/.loom/.env` до запуска Docker. Замените существующие записи идентификации и аутентификации
и добавьте настройку регистрации:

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Сохраните сгенерированные `COMPOSE_PROJECT_NAME` и `LOOM_JWT_SECRET`. Затем загрузите образы и запустите
стек (со своим ClickHouse сначала выполните `ch-setup`, как сказал установщик):

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

Когда [проверки работоспособности](#check-the-stack) пройдены, откройте `http://localhost:7777/` и
зарегистрируйтесь. Клиентам SDK и MCP нужны токены; используйте организацию и пространства имён, выданные вашей
учётной записи, а не однопользовательские значения по умолчанию `dev/local` из примеров ниже.

Это включает многопользовательскую аутентификацию, но не публичный доступ по сети: стек по-прежнему привязан
к localhost. Прежде чем открывать его команде, настройте [доступ оператора](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)
и [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls). Также настройте [реальные модели и ключи API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md);
выбор многопользовательского режима не заменяет фиктивную LLM.

Для уже работающего однопользовательского стека следуйте [инструкциям по переключению](#2-open-the-dashboard);
пересоздание контейнеров сохраняет существующие тома данных.

<a id="contributor-path-build-a-checkout"></a>

### Путь для контрибьютора: сборка из рабочей копии

Требования: Docker Compose 2.34 или новее, который нужен файлу compose рабочей копии, чтобы
ежедневно загружать ClickHouse.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` делает три вещи:

1. Создаёт два файла, которые git не отслеживает: `.env` из `.env.example` и
   `deploy/clickhouse-config/named_collections.xml` из его шаблона. Он никогда не
   перезаписывает ни один из них; существующему `.env` без токена извлекателя он добавляет токен.
2. Запускает dev-стек compose с `--build`.
3. Опрашивает `http://localhost:7777/v1/health`, пока сервер не будет готов, выводя
   число ожидающих миграций. Он сдаётся по истечении `--timeout` (300 с), выводя последнее тело
   health и строки журнала.

Первый запуск контрибьютора собирает образ и применяет все миграции схемы, так что отведите
несколько минут. Затем `loom up` выводит URL панели управления (`http://localhost:7777/`), вызов `curl`
и фрагмент на Python, которые сохраняют первую память, и строку, подключающую Claude Code.
Если аутентификация включена, он также выпускает токен записи и подставляет его в эти фрагменты.

| Флаг         | Действие                          |
| ------------ | --------------------------------- |
| `--rebuild`  | Пересобрать после изменения кода. |
| `--no-build` | Использовать кэшированный образ.  |
| `--open`     | Открыть панель управления.        |
| `--port`     | Использовать другой порт Loom.    |
| `--env-file` | Указать другой файл env.          |

Повторный запуск `loom up` на работающем стеке только перепроверяет работоспособность. `make dev` делает то же
без CLI и выполняет:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

Когда `compose.yaml` указан первым, Compose читает `.env` из корня репозитория для подстановок
`${VAR:-default}`; `--env-file .env` лишь говорит об этом явно. `loom down`
останавливает стек и сохраняет его тома. `loom down --delete-data` безвозвратно
удаляет только именованные тома этой установки, и лишь после того, как вы введёте сгенерированное имя проекта
Compose; `--yes` — подтверждение без диалога. Цели Makefile
делают то же: `make down` сохраняет данные, а `make down-all` удаляет их.

<a id="what-the-stock-env-boots"></a>

### Что запускает стандартный `.env`

Шаблоны `.env` рабочей копии и релизного пакета оба выбирают однопользовательский режим.
Релизный пакет задаёт `LOOM_IDENTITY_PROVIDER=single-user` явно; рабочая копия
оставляет его пустым и выводит тот же режим из `LOOM_AUTH_REQUIRED=false`.

| Настройка                                                                    | Значение      | Эффект                                                                              |
| ---------------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (или пусто) | `single-user` | Без входа. Пространство имён по умолчанию `dev/local`. Вызовы SDK и MCP не требуют токена. |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | фиктивная LLM | Извлечение, разметка сущностей и синтез `memory.ask` ничего не дают.                |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (релизный пакет)      | bge-small     | Семантический поиск на CPU по файлам модели, входящим в образ.                      |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (рабочая копия) | фиктивный эмбеддер | Поиск ранжирует по символьным n-граммам. Достаточно для проверки связей, но не для полноты. |

Оба установщика генерируют уникальный `COMPOSE_PROJECT_NAME`, случайный секрет подписи JWT, случайный
токен извлекателя и `LOOM_BIND_HOST=127.0.0.1`. Поэтому отдельные клоны не
делят тома ClickHouse незаметно, а Loom и ClickHouse доступны только с этого
компьютера.

Для реальных провайдеров откройте **Settings → Models & keys**, выберите провайдеров и модели,
введите их ключи API, затем нажмите **Test & apply**. Один ключ OpenAI API может обслуживать и
LLM, и эмбеддинги. Перезапуск Docker не нужен; изменения эмбеддингов запускают проверяемую
пересборку, ход и повтор которой показаны в панели управления.

Для развёртывания через переменные окружения замените строки провайдеров в `~/.loom/.env`
(релизный стек) или `.env` (рабочая копия). Одна установка `OPENAI_API_KEY` ничего не меняет:
стандартные строки провайдеров называют своих провайдеров явно. Переход с локального или фиктивного
эмбеддера заново вычисляет эмбеддинги сохранённых данных.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Для Claude, OpenAI, размещённых эмбеддингов, клиентов только с подпиской и развёртываний с
одним провайдером LLM следуйте [настройке моделей и ключей API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).
Только после правки `.env` релизного стека примените его командой
`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`;
простой перезапуск не перечитывает окружение контейнера. Расширенные настройки описаны в
[Deployment → Providers](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers).

<a id="check-the-stack"></a>

### Проверка стека

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` возвращает `status: ok`, когда ClickHouse доступен и ни одна проблема, кроме
  `code_tier_failing` (сбойный уровень индекса кода репозитория), не является ошибкой.
  `fake_embedding`, `fake_llm` и `providers_degraded` показывают, в каком вы режиме;
  `problems` перечисляет каждую проблему с её исправлением; `schema.pending` перечисляет непримёненные миграции.
- `loom doctor` выводит по одной строке на каждую серверную проверку. `--json` выдаёт JSON. Для стека из исходной
  рабочей копии выполните `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`.

<a id="2-open-the-dashboard"></a>

## 2. Откройте панель управления

`http://localhost:7777/` сразу открывает панель управления в режиме `single-user`;
в многопользовательском режиме сначала зарегистрируйтесь или войдите. В режиме `single-user`
панель управления и значения по умолчанию для агентов используют `dev/local` (`LOOM_DEFAULT_NAMESPACE` переименовывает
пространство имён). Это удобный режим, но не граница изоляции пользователей: любой, кто может
подключиться к серверу, получает доступ к его данным и администрированию. Используйте его только на localhost.

Чтобы вместо этого попробовать регистрацию (`trust-network`: email и пароль, а первый, кто
займёт имя пространства имён, становится его администратором), замените эти записи в `~/.loom/.env`
(релизный стек) или в `.env` (checkout):

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Одной настройки `LOOM_AUTH_REQUIRED=true` недостаточно, если явно выбран `single-user`:
в этом режиме аутентификация остаётся выключенной. Сохраните сгенерированный установщиком
`LOOM_JWT_SECRET` и существующие тома, затем пересоздайте сервер, чтобы он загрузил переменные окружения:

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
так что любой, кто достигает порта, сможет зарегистрироваться. Прежде чем открывать сервер для
команды, следуйте [руководству по развёртыванию](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) в части
доступа оператора и [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls).

<a id="3-get-a-token"></a>

## 3. Получите токен

В однопользовательском режиме токен необязателен. Если выпустить его всё равно, ваши скрипты продолжат работать после включения аутентификации при условии, что они указывают свои `org` (или передают его пустым), `namespace` и `agent`: вызов, который отправляет учётные данные, как это делает SDK, когда находит их ([Python
SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)), не получает ни одного из них подставленным, а пустой `org` берётся из самого токена.

- **Панель управления:** **Settings → Tokens → New token**. По умолчанию токен читает все
  пространства имён и репозитории, к которым вы относитесь (ограниченный репозиторий только если вы его добавите),
  но не читает ни одной коллекции, и сохраняет в пространство имён, в котором вы находитесь. Режим _Only what you choose_ ограничивает
  токен теми пространствами имён, коллекциями и репозиториями, которые вы отметите (сначала ни одного); отмеченные
  просматриваются в перечисленном порядке, который меняется стрелками строки, а на
  десктопе ещё и перетаскиванием за ручку строки. Результат показывает токен один раз вместе с настройкой
  для вашего MCP-клиента или скрипта.
- **CLI, от имени оператора:** выполните команду внутри контейнера сервера, в котором хранится секрет подписи:

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  Добавьте `--ttl-days 30` для всего, что покидает вашу машину. `loom mint-token --help`
  перечисляет параметры ограничения области (`--namespace-role`, `--read-scope`, `--traffic-class`).

- **Бот, который регистрируется сам** (только в доверенной сети):
  `loom init --agent nightly-triage --server-url http://localhost:7777` создаёт
  пространство имён `agent-nightly-triage`, сохраняет токен с ролью writer в `~/.loom/token`, а URL
  в `~/.loom/config.json`, и выводит код привязки. Человек один раз вводит его на
  `/oauth/agent-claim` со своим паролем, чтобы стать владельцем.

Сохраните токен там, где его ищут SDK, CLI и плагины:

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

Claims, роли и права доступа описаны в разделе [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="4-first-memory-over-http"></a>

## 4. Первая память через HTTP

Каждая операция — это `POST /v1/<op>` с телом в формате JSON. Свежая установка со стандартным
`.env`, из установщика или из чекаута, работает в однопользовательском режиме: токен не нужен, а обычный вызов памяти
или поиска (`memory.set`, `memory.get`, `memory.search` и ещё несколько), в котором не указаны `org` и `namespace`, выполняется в собственных org `dev` и пространстве имён `local` этой установки — тех, которые используют
MCP и панель управления. `memory.set` без `agent` записывается от имени агента
`http`, а поиск читает память всех агентов. Поэтому вызовам ниже нужны только их собственные поля
([какие операции](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)). Если вызов без токена возвращает 401
`AUTH_INVALID`, значит, авторизация включена: добавьте `-H "Authorization: Bearer $LOOM_API_KEY"` и укажите
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
`memory.get` читает один ключ: `found` показывает, содержит ли ключ актуальное значение, а
`value` — это значение в том виде, в каком оно было записано. Ответ поиска содержит `results` и `abstain`,
который показывает, достаточно ли сильно лучшее совпадение, чтобы отвечать на его основе. Поиск читает память
всех агентов в пространстве имён, включая добавленную через панель управления; укажите
`agent`, чтобы читать только память этого агента.
`GET /v1/operations` перечисляет все операции, а <http://localhost:7777/docs> позволяет попробовать
каждую из браузера. Форматы описаны в [справочнике по HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).

<a id="5-first-memory-from-python"></a>

## 5. Первая память из Python

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

Релиз, которого ещё нет на PyPI, устанавливается из его артефактов
([установка из GitHub Release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)).

В примере указаны `org` и `namespace`, поэтому он работает как с токеном из шага 3, так и без него: SDK отправляет токен, если находит его. На однопользовательском сервере вызов без токена может их опустить: `Agent(agent="quickstart", base_url=...)` тоже выполняется в `dev`/`local`.

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

## 6. Первая память из кодингового агента

Если установщик подключил вашего агента, перейдите к последнему абзацу этого раздела.

Для Claude Code установите плагин Loom. Он добавляет хуки (контекст в начале сессии,
захват в конце) и навыки поверх инструментов MCP:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

На односерверной установке для одного пользователя на этой машине оставьте URL сервера по умолчанию, а поле токена
пустым, когда Claude Code запросит настройки плагина.

Без плагина добавьте запись MCP напрямую:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

Обе команды подключаются к серверу для одного пользователя на этой машине без вашего токена.

Если включена аутентификация, создайте токен в **Settings → Tokens** и выберите **Claude Code** в разделе
**Connect**: диалог покажет настройку для плагина и для записи `claude mcp add`,
уже с токеном. Затем:

1. Перезапишите `~/.loom/config.json` блоком `config.json` из диалога и добавьте
   пространство имён, в которое сохраняет токен (`"namespace": "<namespace>"`): значения `org` или `namespace`,
   оставшиеся в файле после сессии для одного пользователя, имеют приоритет над токеном, а без них плагин
   пишет в первое пространство имён токена, отличное от `*`, или в `coding`. Верните обратно
   `code_repo` или `agent`, если вы их задавали.
2. Оставьте `LOOM_API_KEY` пустым (на шаге 3 могла быть выполнена его экспортация: `unset LOOM_API_KEY`) и параметр плагина
   `token` тоже пустым, либо задайте в них новый токен: плагин читает оба
   раньше, чем этот файл
   ([конфигурация](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. Удалите запись `claude mcp add`, созданную без токена (`claude mcp remove loom -s user`),
   перед запуском команды из диалога: она отказывается работать, если имя уже существует.

`loom install --client <name>` записывает запись MCP для Claude Desktop, Claude Code,
Codex, Cursor, VS Code, Cline, Windsurf и Zed; без флага она подключает всех найденных клиентов.
О Codex и Cursor рассказано в разделе [Подключение кодингового агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md).

Начните новую сессию и скажите _«Запомни, что я никогда не деплою по пятницам»_. Агент вызовет
`loom_memory_set`. В новой сессии спросите _«Когда мне можно деплоить?»_ Агент вызовет
`loom_search` и ответит по сохранённому факту. Когда сохранять и искать,
модель решает по описаниям инструментов; ключевых слов-триггеров нет.

<a id="7-see-it-in-the-dashboard"></a>

## 7. Посмотрите в панели управления

| Страница          | Что показывает                                              |
| ----------------- | ----------------------------------------------------------- |
| `/memories`       | Память пространства имён с указанием автора записи и времени. |
| `/admin/activity` | Журнал аудита: какой агент записал какой ключ и когда.      |
| `/admin/usage`    | Стоимость по операциям и агентам.                           |

Остальное описано в разделе [Панель управления](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

<a id="optional-index-a-repository-on-the-released-stack"></a>

## Необязательно: индексация репозитория на выпущенном стеке

Добавление репозитория из панели управления на этом стеке отклоняется с ошибкой `SANDBOX_UNAVAILABLE`:
для него нужна [песочница для заданий над репозиториями](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox),
которую этот стек оставляет выключенной, потому что стандартная изоляция Docker её блокирует. Указывайте
только репозитории, которым вы доверяете: их шаги сборки (установка зависимостей TypeScript, загрузка модулей Go,
CMake) выполняются без песочницы, в контейнере `loom`, в окружении которого находятся секрет JWT
и ключи провайдеров.

`LOOM_CODE_REPOS_JSON` — это одна строка, содержащая все репозитории в виде JSON-массива
([Настройка записи](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
В `~/.loom/.env` раскомментируйте эту строку (добавьте её, если её нет) и отредактируйте.
`dev` — это организация, от имени которой работает стек для одного пользователя, и организация, в которую
регистрируются учётные записи в многопользовательском режиме (`LOOM_SELF_SIGNUP_ORG`); если вы это изменили,
используйте здесь и в `--org` ниже организацию вашей учётной записи:

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom выбирает индексаторы уровня компилятора по файлам сборки репозитория (`go.mod`,
  `package.json`, `pyproject.toml`, корневой `CMakeLists.txt`, …); вместо этого индексатор можно указать
  в `scip_producer` ([Уровень компилятора](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  Для C/C++ также нужно настроенное дерево сборки, поэтому без него репозиторий на C/C++ получает только
  уровень парсера.
- Для приватного репозитория добавьте токен GitHub (`LOOM_CODE_GITHUB_TOKEN` в `.env` или
  `github_token` в записи) и уберите `"public":true`: этот флаг предназначен для репозиториев, публичных
  на своём хосте, и позволяет любому участнику организации сразу присоединиться к индексу как читатель из
  раздела **Repositories**. Решите это до первого `loom code init`: он задаёт политику присоединения
  для пространства имён по значению `public`, и последующее изменение `public` её не меняет (сопровождающий
  меняет её через `code.repo.set_access`).

Затем пересоздайте контейнер `loom` и запустите первую индексацию:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` работает на локальной модели эмбеддингов выпущенного стека. С фиктивной моделью эмбеддингов (`.env`
из локальной копии репозитория или обновлённая установка, где всё ещё указано `fake-ngram-*`) команда отказывается
выполняться и завершается; чтобы всё равно проверить связку, добавьте `--allow-fake-embedder`. Выбор модели позже в
**Settings → Models & keys** заново строит эмбеддинги индекса кода; после изменения `.env` их нужно перестроить
отдельно, как описано в разделе [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).

В многопользовательском режиме у добавленного в список репозитория нет сопровождающего, поэтому без `"public":true`
запрос читателя на присоединение остаётся без ответа: подтвердить его некому. После `loom code init` предоставьте
кому-нибудь доступ от имени оператора с помощью токена, выпущенного в контейнере: роль `admin` делает пользователя
сопровождающим, который с этого момента подтверждает читателей, а роль `viewer` позволяет читать. Субъект — это
пространство имён, с которым зарегистрировалась учётная запись (её логин, субъект её токенов); адрес электронной почты
при самостоятельной регистрации никогда не проверяется, поэтому доступ, выданный на него, никого не пропустит:

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

`loom demo` загружает файл `AGENTS.md` из этого репозитория, обрабатывает его с помощью `knowledge.ingest` (его разбирает эталонный экстрактор стека) и задаёт по нему вопрос через `knowledge.synthesize`, после чего выводит
ответ и фрагменты, на которые он ссылается. Если используется фиктивная LLM, команда вместо этого выводит лучшие
результаты `knowledge.search`. Флаги `--file` и `--question` позволяют указать ваш собственный документ и вопрос.

<a id="common-problems"></a>

## Частые проблемы

| Симптом                                                                       | Причина и решение                                                                                                                                                                                                                                                            |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` не записывает факты                                       | Используется поддельная LLM. Настройте настоящую по инструкции [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).                                                                                                                                                                                    |
| Результаты поиска выглядят случайными                                         | Используется поддельная модель эмбеддингов (`fake-ngram-*` в `.env`). Переключите её, как описано в [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md); сохранённые данные будут пересчитаны заново. Не удаляйте тома, чтобы сменить модели.                                                                               |
| 401 `AUTH_INVALID` после включения аутентификации                             | Токена нет, либо он подписан другим `LOOM_JWT_SECRET`. Выпустите токен заново командой Compose из раздела [Получение токена](#3-get-a-token).                                                                                                                                                   |
| Панель управления возвращает 503                                              | Сервер запущен из исходников (`make server`) без собранного фронтенда. Выполните `make frontend-build`.                                                                                                                                                                             |
| Стек из релиза не переходит в состояние healthy                               | Выполните `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`. См. [Troubleshooting](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md).                                                                                                                             |
| При добавлении репозитория возвращается `SANDBOX_UNAVAILABLE`                 | Стек запущен без песочницы для заданий над репозиториями. Индексируйте репозиторий через `LOOM_CODE_REPOS_JSON` ([Индексирование репозитория](#optional-index-a-repository-on-the-released-stack)).                                                                                           |
| Установщик сообщает, что порт `7777` занят                                    | Этот порт слушает другая программа или другой экземпляр Loom. Остановите её либо повторите команду установки, указав перед `sh loom-install.sh` переменную `LOOM_PORT=7778`; `.env` сохранит порт.                                                                                                    |
| Установщик сообщает, что порт `7777` не отвечает                              | Порт занят программой, которая не отвечает, либо `LOOM_BIND_HOST`, `LOOM_PORT` или файрвол не дают этому хосту к нему обратиться.                                                                                                                                                       |
| Установщик не смог проверить порт `7777`                                      | Curl завершился с ошибкой, причина указана в его коде выхода; проверьте `LOOM_BIND_HOST` и `LOOM_PORT`.                                                                                                                                                                                      |
| В Docker Desktop на WSL установка останавливается с `Ports are not available` | Windows занимает или резервирует порт, а проверка установщика внутри WSL этого не видит. Команда `netsh interface ipv4 show excludedportrange protocol=tcp` показывает зарезервированные диапазоны; повторите команду установки, указав перед `sh loom-install.sh` значение `LOOM_PORT` вне этих диапазонов. |
| Порт `8123` или `9000` в клонированном репозитории занят                      | Переназначьте порты ClickHouse на хосте ([local stack](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                                                                                                                                                                       |
| Установщик не смог загрузить образы                                           | Docker сам обращается к реестру: за прокси задайте демону Docker [настройки прокси](https://docs.docker.com/engine/daemon/proxy/).                                                                                                                               |

<a id="next"></a>

## Дальше

- [Оценка Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): превратите этот smoke-тест в оценку качества,
  задержки и стоимости под вашу рабочую нагрузку.
- [Подключение агента для написания кода](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): Claude Code, Codex или Cursor
  на этом стеке.
- [Ваш первый агент](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): идентификация, обработка abstain, сессии, context packs
  и запуски (runs) из SDK.
- [Развёртывание](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): запуск Loom для команды.
