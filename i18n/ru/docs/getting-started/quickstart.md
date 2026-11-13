<!-- i18n: source=docs/getting-started/quickstart.md blob=4175c372ea5183fffd31de1d59658aae09018c47 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# Быстрый старт

На этой странице Loom запускается на ноутбуке, и первая память сохраняется через HTTP, Python и MCP. Ключ API и учётная запись не нужны. Свежая установка выпущенного стека строит эмбеддинги небольшой локальной моделью, которая входит в образ, поэтому поиск возвращает осмысленные результаты без сети (обновлённая установка сохраняет эмбеддер, указанный в её `.env`). LLM при этом поддельная, поэтому извлечение и ответы `memory.ask` ничего не дают. В `.env` рабочей копии репозитория для обоих компонентов используются поддельные провайдеры.

**Стандартная установка Docker рассчитана на одного пользователя:** без входа в систему, с доступом только с localhost и пространством имён `dev/local` по умолчанию. Один человек может подключить несколько агентов, не включая многопользовательский режим. **Командные развёртывания в OSS тоже поддерживаются**: с аутентифицированными учётными записями и контролем доступа к пространствам имён; [запустите в многопользовательском режиме](#start-in-multi-user-mode) или следуйте разделу [Развёртывание → Режимы идентификации](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes).
Оба режима поддерживают одни и те же возможности Loom; для настоящих LLM и эмбеддингов в любом режиме нужно настроить провайдеров.

<a id="prerequisites"></a>

## Требования

| Что нужно                      | Для чего                                                                          |
| ------------------------------ | --------------------------------------------------------------------------------- |
| Docker с плагином compose      | Стек: ClickHouse и сервер Loom.                                                   |
| Python 3.12 или новее          | CLI `loom` только для контрибьюторов. SDK поддерживает версии 3.11+.              |
| Node.js 20 или новее           | SDK для TypeScript (необязательно).                                               |
| Свободный порт `7777`          | Loom. При разработке из checkout также открываются ClickHouse на `8123` и `9000`. |
| `jq`                           | Чтение вывода `curl` на шаге 4 (необязательно).                                   |

<a id="1-boot-the-stack"></a>

## 1. Запуск стека

<a id="fast-path-released-image-no-clone"></a>

### Быстрый путь: готовый образ, без клонирования

Требование: Docker Desktop или Docker Engine с Compose 2.20 или новее. В Windows запускайте
команду в терминале WSL 2, где работает `docker`: это Docker Desktop с включённой
интеграцией WSL для этого дистрибутива или Docker Engine, установленный в нём. Установщик
— это POSIX-скрипт для shell.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Команда сохраняет установщик в текущем каталоге и затем запускает этот файл, поэтому
выполняйте её из домашнего каталога или другого каталога, в который можете писать только
вы: в общем каталоге кто-то другой может подменить `loom-install.sh` или направить его на
другой файл до запуска. Неудачная загрузка останавливается с ошибкой curl и ненулевым кодом
выхода; после неудачной установки исправьте то, о чём она сообщает, и запустите всю команду
заново. Установщик скачивает `loom-stack.tar.gz` из последнего релиза на GitHub, проверяет
его SHA-256, устанавливает конфигурацию Compose и ClickHouse в `~/.loom`, загружает
соответствующие образы `ghcr.io/clickhouse/loom:X.Y.Z` и
`ghcr.io/clickhouse/loom-extractor:X.Y.Z` (сервис извлечения текста из документов) и ждёт
ответа от `http://localhost:7777/v1/health`. Образ сервера содержит API, воркеры, миграции и
собранную панель управления на React в корне сервера; установка Python или Node не нужна.
Индексаторы уровня компилятора для Python, TypeScript и Go входят в образ и включены, как и
индексатор для C++ (`scip-clang`) на `amd64`. Для репозитория, проиндексированного
индексатором TypeScript, зависимости устанавливаются автоматически. Оба образа
поддерживают Linux `amd64` и `arm64`, включая Docker Desktop на Apple Silicon.

Когда Loom отвечает, установщик:

1. **Проверяет память.** При новой установке он сохраняет тестовую память, читает её и
   удаляет, что показывает, что запись и чтение работают, и не оставляет действующей памяти
   (её удалённая версия видна в разделе «Недавно удалённые»). Повторный запуск после
   успешной проверки или обновление пропускают этот шаг.
2. **Выясняет, включён ли вход.** Если вход включён, он не подключает ни одного агента и
   сообщает, откуда брать токены.
3. **Предлагает подключить ваших кодинг-агентов.** В терминале он спрашивает про каждого
   найденного: Claude Code и Codex через их плагины, VS Code через `code --add-mcp`.
   Вместо вопросов отвечает `LOOM_CONNECT`, в том числе при запуске без терминала, и
   проверяются только названные в нём клиенты: список через запятую из `claude-code`,
   `codex` и `vscode`, либо `all`, либо отдельно `none`; любое другое значение останавливает
   установку. Без терминала и без `LOOM_CONNECT` ни один агент не проверяется и не
   изменяется. Плагин или MCP-сервер, настроенные ранее (плагином, через `claude mcp add`,
   `codex mcp add` или `loom install`), остаются как есть; для плагина Claude Code
   установщик выводит команду, которая направляет его на эту установку. Плагин Claude Code
   запускает Node.js 18 или новее из PATH, поэтому без него установщик не ставит плагин и
   выводит только строку `claude mcp add` для MCP-сервера; плагин Codex находит Node.js
   также через nvm и подобные средства, поэтому он устанавливается, а рядом выводится строка
   `codex mcp add`. Плагин Codex всегда использует `http://localhost:7777`, поэтому для
   установки в другом месте вместо этого выводится ссылка на страницу его настройки. VS Code
   не предлагается в удалённом сеансе или в WSL. Клиент, названный в `LOOM_CONNECT`, но
   отсутствующий в PATH, будет назван в сообщении.
4. **Открывает панель управления** на десктопе при новой установке; `LOOM_NO_OPEN=1`
   пропускает этот шаг.

При запуске из WSL эти шаги выполняются внутри WSL: подключите агент, работающий в Windows,
из Windows по [руководству по подключению](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md), а адрес панели управления, который
выводит установщик, откройте в браузере Windows.

Ctrl-C останавливает запуск на любом шаге; клонирование, начатое CLI агента для его плагина,
может завершиться в фоне.

Стек запускает собственный контейнер ClickHouse. Чтобы использовать уже работающий у вас
ClickHouse, собственный сервер или сервис ClickHouse Cloud (26.4 или новее, с Keeper),
передайте его URL; установщик запросит пароль, если в URL его нет, создаст базу `loom` и
пользователя-читателя только для чтения или выведет SQL, который должен выполнить
администратор
([выбор ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

`install.sh` каждого релиза устанавливает именно этот релиз, поэтому копия, которую отдаёт
`latest`, устанавливает последний релиз, а URL конкретного релиза, ниже, каждый раз
устанавливает именно его. Поверх более новой установки копия более старого релиза
отказывается ставиться, чтобы не запускать более старые Loom и ClickHouse на более новых
данных; чтобы намеренно понизить версию, следуйте
[инструкции по понижению версии установки через установщик](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install).
Чтобы зафиксировать релиз, используйте его собственный URL:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Чтобы всё проверить перед запуском, скачайте файлы и сверьте их:

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

Затем, когда вывод выглядит правильно, установите из проверенного пакета:

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

Значения `LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`,
`LOOM_EXTRACTOR_IMAGE_REPOSITORY` и `LOOM_CLICKHOUSE_IMAGE`, переданные установщику,
записываются в `.env` каталога установки, поэтому последующие команды `docker compose` там
и последующие запуски установщика используют тот же порт и те же образы. Значение
`LOOM_BIND_HOST`, отличное от loopback, публикует Loom за пределами этого хоста, и сервер
отказывается так стартовать без `LOOM_PUBLIC_BASE_URL` и входа: установите с
`LOOM_SKIP_START=1`, задайте их в `.env` (ниже), затем запустите команду установки снова
без `LOOM_SKIP_START=1`.

Каждый релиз запускает тот релиз ClickHouse, который был последним на момент его сборки,
поэтому повторный запуск команды установки для той же версии Loom сохраняет её ClickHouse;
установка более новой версии Loom переводит его на новый, если только `.env` не закрепляет
`LOOM_CLICKHOUSE_IMAGE` (удалите эту строку, чтобы снова следовать релизам). Повторный
запуск команды установки обновляет управляемые файлы Compose и конфигурации и приводит
контейнеры в соответствие. Он сохраняет `~/.loom/.env`, добавляя в файл без них URL
экстрактора и токен, а также именованные тома Docker. Чтобы остановить стек без удаления
данных:

```bash
docker compose --project-directory "$HOME/.loom" down
```

Добавляйте `--volumes` только для намеренного необратимого сброса.

<a id="try-it-in-github-codespaces"></a>

### Попробовать в GitHub Codespaces

[![Открыть в GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

Dev-контейнер репозитория запускает последний релиз в codespace, ничего не требуя на вашей
машине: он устанавливает Loom описанным выше установщиком, запускает его и подключает к нему
Claude Code. Первый запуск скачивает около 3 ГБ образов и занимает несколько минут. Откройте
панель управления по адресу, который печатают последние строки журнала создания (`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, это перенаправленный
адрес порта 7777 на вкладке **Ports**); адрес установщика `http://127.0.0.1:7777/` выше
него — собственный адрес codespace. В терминале codespace выполните `cd ~ && claude` (вне
checkout, инструкции которого предназначены для работы над самим Loom), войдите и скажите
«Remember that I never deploy on Friday»; в новом сеансе `claude` спросите «When can I
deploy?».

Пробный запуск работает в однопользовательском режиме, без входа. В браузере порт 7777
перенаправляется приватно: его адрес отвечает только вашей учётной записи GitHub.
Оставьте его приватным: публичный порт или приватный для организации открывает сервер без
входа всем, кому он доступен. Агенты внутри codespace, например его `claude`, используют
`http://localhost:7777`; строки подключения в панели управления называют вместо этого
перенаправленный адрес, для которого нужен вход в GitHub, а у агентов в codespace его нет.
При открытии в десктопном VS Code codespace также перенаправляет порт на `localhost:7777`
вашего ноутбука, где он так же открыт, как локальная установка, для каждой программы на
ноутбуке, и агенты на ноутбуке могут обращаться к нему там; для перенаправления нужен
свободный локальный порт, поэтому сначала остановите локальный Loom. Пользуйтесь панелью
управления по адресу из браузера: с `localhost:7777` ноутбука страница откроется, но
сервер отклонит её запросы, поэтому данных она не покажет; собственной страницей этого
сервера является только перенаправленный адрес.

Установка находится в `/workspaces/.loom`, поэтому пересобранный codespace сохраняет свои
`.env` и проект Compose, хотя его память хранится в томах Docker, которые пересборка может
не сохранить; пересборка также запускает поверх неё установщик последнего релиза, что
переводит её на этот релиз. Везде, где в этой документации упомянуты `~/.loom` или
`"$HOME/.loom"`, в codespace используется `/workspaces/.loom`. Codespaces предлагает открыть
порт 7777, как только он начинает слушать; панель управления отвечает, когда журнал
создания напечатает «Loom is running in the codespace». Если журнал вместо этого
заканчивается ошибкой, выполните `sh .devcontainer/start-loom.sh` в терминале codespace:
он сохраняет то, что настроил первый запуск.

<a id="start-in-multi-user-mode"></a>

### Запуск в многопользовательском режиме

Для новой установки из готового образа с учётными записями и входом с первого запуска
подготовьте файлы стека, не запуская контейнеры:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Отредактируйте `~/.loom/.env` перед запуском Docker. Замените существующие записи
идентификации и аутентификации и добавьте настройку регистрации:

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Сохраните сгенерированные `COMPOSE_PROJECT_NAME` и `LOOM_JWT_SECRET`. Затем загрузите
образы и запустите стек (со своим ClickHouse сначала выполните `ch-setup`, как сказал
установщик):

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

После того как [проверки состояния](#check-the-stack) пройдены, откройте
`http://localhost:7777/` и зарегистрируйтесь. Клиентам SDK и MCP нужны токены; используйте
организацию и пространства имён, выданные вашей учётной записи, а не однопользовательские
значения `dev/local` по умолчанию из примеров ниже.

Это включает многопользовательскую аутентификацию, а не публичный доступ по сети: стек по
-прежнему привязан к localhost. Прежде чем открывать его команде, настройте
[операторский доступ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)
и [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls). Настройте также [реальные модели и ключи API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md);
выбор многопользовательского режима не заменяет поддельный LLM.

Для уже работающего однопользовательского стека следуйте вместо этого
[инструкциям по переключению](#2-open-the-dashboard); пересоздание контейнеров сохраняет
существующие тома данных.

<a id="contributor-path-build-a-checkout"></a>

### Путь для контрибьютора: сборка из checkout

Требование: Docker Compose 2.34 или новее, который нужен файлу compose в checkout, чтобы
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
   перезаписывает ни один из них; существующему `.env` без токена экстрактора он его
   добавляет.
2. Запускает dev-стек compose с `--build`.
3. Опрашивает `http://localhost:7777/v1/health`, пока сервер не будет готов, выводя число
   ожидающих миграций. Он сдаётся после `--timeout` (300 с), показав последнее тело ответа
   health и строки журнала.

Первый запуск для контрибьютора собирает образ и применяет все миграции схемы, поэтому
отведите на него несколько минут. Затем `loom up` выводит URL панели управления
(`http://localhost:7777/`), вызов `curl` и фрагмент на Python, которые сохраняют первую
память, и строку, подключающую Claude Code. Когда включена аутентификация, он также создаёт
токен записи и подставляет его в эти фрагменты.

| Флаг         | Действие                          |
| ------------ | --------------------------------- |
| `--rebuild`  | Пересобрать после изменения кода. |
| `--no-build` | Использовать кэшированный образ.  |
| `--open`     | Открыть панель управления.        |
| `--port`     | Использовать другой порт Loom.    |
| `--env-file` | Указать другой файл env.          |

Повторный запуск `loom up` на работающем стеке только заново проверяет состояние. `make dev`
делает то же без CLI и выполняет:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

Когда `compose.yaml` указан первым, Compose читает `.env` из корня репозитория для
подстановок `${VAR:-default}`; `--env-file .env` лишь говорит об этом явно. `loom down`
останавливает стек и сохраняет его тома. `loom down --delete-data` безвозвратно удаляет
только именованные тома этой установки, и только после того, как вы введёте сгенерированное
имя проекта Compose; `--yes` — подтверждение без диалога. Цели Makefile делают то же:
`make down` сохраняет данные, а `make down-all` удаляет их.

<a id="what-the-stock-env-boots"></a>

### Что запускает стандартный `.env`

Шаблоны `.env` из checkout и из пакета релиза выбирают однопользовательский режим.
Пакет релиза явно задаёт `LOOM_IDENTITY_PROVIDER=single-user`; checkout оставляет его
пустым и выводит тот же режим из `LOOM_AUTH_REQUIRED=false`.

| Настройка                                                                    | Значение      | Эффект                                                                              |
| ---------------------------------------------------------------------------- | ------------- | ----------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (или пусто) | `single-user` | Без входа. Пространство имён по умолчанию `dev/local`. Вызовы SDK и MCP не требуют токена. |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | поддельный LLM | Извлечение, разметка сущностей и синтез `memory.ask` ничего не производят.          |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (пакет релиза)        | bge-small     | Семантический поиск на CPU по файлам модели, которые входят в образ.                |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (checkout) | поддельный эмбеддер | Поиск ранжирует по символьным n-граммам. Достаточно, чтобы проверить связность, но не полноту. |

Оба установщика генерируют уникальный `COMPOSE_PROJECT_NAME`, случайный секрет подписи JWT,
случайный токен экстрактора и `LOOM_BIND_HOST=127.0.0.1`. Поэтому отдельные клоны не
разделяют тома ClickHouse втихую, а Loom и ClickHouse доступны только с этого компьютера.

Для реальных провайдеров откройте **Settings → Models & keys**, выберите провайдеров и
модели, введите их ключи API, затем нажмите **Test & apply**. Один ключ OpenAI API может
обслуживать и LLM, и эмбеддинги. Перезапуск Docker не нужен; изменение эмбеддингов запускает
проверяемую перестройку, прогресс и повтор которой показаны в панели управления.

Для развёртывания через переменные окружения замените строки провайдеров в `~/.loom/.env`
(стек из релиза) или в `.env` (checkout). Одна лишь установка `OPENAI_API_KEY` ничего не
меняет: стандартные строки провайдеров называют своих провайдеров явно. Переход с локального
или поддельного эмбеддера заново вычисляет эмбеддинги всего сохранённого.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Про Claude, OpenAI, размещённые эмбеддинги, клиенты только с подпиской и развёртывания с
одним провайдером LLM читайте в [Настройка моделей и ключей API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).
Только после правки `.env` стека из релиза примените его командой
`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`;
обычный перезапуск не перечитывает окружение контейнера. Расширенные настройки описаны в
[Развёртывание → Провайдеры](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers).

<a id="check-the-stack"></a>

### Проверка стека

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` возвращает `status: ok`, когда ClickHouse доступен и ни одна проблема, кроме
  `code_tier_failing` (сбойного уровня индекса кода репозитория), не является ошибкой.
  `fake_embedding`, `fake_llm` и `providers_degraded` показывают, в каком вы режиме;
  `problems` перечисляет каждую проблему с её исправлением; `schema.pending` перечисляет
  непримёненные миграции.
- `loom doctor` выводит по одной строке на каждую серверную проверку. `--json` даёт JSON. Для
  стека из исходного checkout выполните `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`.

<a id="2-open-the-dashboard"></a>

## 2. Откройте панель управления

`http://localhost:7777/` сразу открывает панель управления в режиме `single-user`;
в многопользовательском режиме сначала зарегистрируйтесь или войдите. В режиме `single-user`
панель управления и значения по умолчанию для агентов используют `dev/local` (`LOOM_DEFAULT_NAMESPACE` переименовывает
пространство имён). Это удобный режим, а не граница изоляции пользователей: любой, кто может
подключиться к серверу, получает доступ к его данным и администрированию. Используйте его только на localhost.

Чтобы вместо этого попробовать регистрацию (`trust-network`: email и пароль, а первый, кто
займёт имя пространства имён, становится его администратором), замените эти записи в `~/.loom/.env`
(выпущенный стек) или `.env` (локальная копия репозитория):

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
namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes). Именно указание
`trust-network` позволяет запустить сервер с `LOOM_BIND_HOST`, отличным от loopback, при открытой регистрации,
поэтому любой, кто достучится до порта, сможет зарегистрироваться. Прежде чем открывать сервер для
команды, следуйте [руководству по развёртыванию](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) по
доступу оператора и [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls).

<a id="3-get-a-token"></a>

## 3. Получите токен

В однопользовательском режиме токен необязателен. Но если выпустить его всё равно, ваши скрипты продолжат работать после включения аутентификации, при условии что в них указаны `org` (или он передан пустым), `namespace` и `agent`: вызов, который отправляет учётные данные, как это делает SDK, когда находит их ([Python SDK → Учётные данные](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)), не получает ни одно из этих значений автоматически, а пустой `org` — это собственная организация токена.

- **Панель управления:** **Settings → Tokens → New token**. По умолчанию токен читает все пространства имён и репозитории, к которым вы относитесь (ограниченный репозиторий — только если вы его добавите), но не читает ни одной коллекции, и сохраняет данные в то пространство имён, в котором вы находитесь. Вариант _Only what you choose_ ограничивает токен теми пространствами имён, коллекциями и репозиториями, которые вы отметите (сначала не отмечено ничего); отмеченные ищутся в указанном порядке, а менять порядок можно стрелками в строке или, на компьютере, перетаскиванием за ручку строки. Результат показывает токен один раз вместе с настройкой для вашего MCP-клиента или скрипта.
- **CLI, от имени оператора:** выполните команду внутри контейнера сервера, где хранится секрет подписи:

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  Добавьте `--ttl-days 30` для всего, что покидает вашу машину. `loom mint-token --help`
  выводит список параметров ограничения области (`--namespace-role`, `--read-scope`, `--traffic-class`).

- **Бот, который регистрируется сам** (только для доверенной сети):
  `loom init --agent nightly-triage --server-url http://localhost:7777` создаёт
  пространство имён `agent-nightly-triage`, сохраняет токен с ролью writer в `~/.loom/token`, а URL
  в `~/.loom/config.json`, и выводит код подтверждения. Человек один раз вводит его на
  `/oauth/agent-claim` со своим паролем, чтобы получить права владельца.

Сохраните токен там, где его ищут SDK, CLI и плагины:

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

Claims, роли и права доступа описаны в разделе [Токены и права доступа](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="4-first-memory-over-http"></a>

## 4. Первая память через HTTP

Каждая операция — это `POST /v1/<op>` с телом JSON. Свежая установка со стандартным
`.env`, из установщика или из клона репозитория, работает в однопользовательском режиме: токен не нужен, а обычный вызов памяти
или поиска (`memory.set`, `memory.get`, `memory.search` и ещё несколько), в котором не указаны `org` и `namespace`, выполняется в собственных org и пространстве имён установки: org `dev` и пространство имён `local`, их же используют MCP и панель управления. Вызов `memory.set` без `agent` записывается от имени агента
`http`, а поиск читает память всех агентов. Поэтому вызовам ниже нужны только их собственные поля
([какие операции](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)). Если вызов без токена отвечает 401
`AUTH_INVALID`, значит, включена аутентификация: добавьте `-H "Authorization: Bearer $LOOM_API_KEY"` и укажите `org`, `namespace` и `agent` вашей
учётной записи; заголовок с пустым токеном отклоняется.

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
`value` — это значение в том виде, в каком оно было записано. Ответ поиска содержит `results` и `abstain`;
последнее показывает, достаточно ли надёжен лучший результат, чтобы отвечать на его основе. Поиск читает
память всех агентов в пространстве имён, включая добавленную в панели управления; укажите
`agent`, чтобы читать только память этого агента.
`GET /v1/operations` перечисляет все операции, а <http://localhost:7777/docs> позволяет попробовать
каждую из браузера. Формы запросов и ответов описаны в [справочнике по HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).

<a id="5-first-memory-from-python"></a>

## 5. Первая память из Python

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

Релиз, которого ещё нет на PyPI, устанавливается из его артефактов
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

Где SDK находит свой сервер и учётные данные, описано в разделе
[Python SDK → Учётные данные](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials).
TypeScript SDK устанавливается командой `npm install @clickhouse/loom`
([из клона репозитория](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)).

Клиентские интерфейсы описаны в справочниках [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) и
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md).

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. Первая память от кодингового агента

Если установщик подключил вашего агента, перейдите к последнему абзацу этого раздела.

Для Claude Code установите плагин Loom. Он добавляет хуки (контекст в начале сессии, сохранение в конце) и навыки поверх инструментов MCP:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

На односерверной установке для одного пользователя на этой машине оставьте URL сервера по умолчанию и не заполняйте токен, когда Claude Code запросит настройки плагина.

Без плагина добавьте запись MCP напрямую:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

Обе схемы подключаются к серверу для одного пользователя на этой машине без вашего токена.

Если авторизация включена, создайте токен в **Settings → Tokens** и выберите **Claude Code** в разделе **Connect**: диалог покажет настройку для плагина и для записи `claude mcp add` с вашим токеном. Затем:

1. Перезапишите `~/.loom/config.json` блоком `config.json` из диалога и добавьте пространство имён, в которое сохраняет токен (`"namespace": "<namespace>"`): значения `org` или `namespace`, оставшиеся в файле от сессии одного пользователя, имеют приоритет над токеном, а без них плагин пишет в первое пространство имён токена, отличное от `*`, или в `coding`. Верните `code_repo` и `agent`, если вы их задавали.
2. Оставьте `LOOM_API_KEY` (шаг 3 мог его экспортировать: `unset LOOM_API_KEY`) и параметр плагина `token` пустыми либо задайте в них новый токен: плагин читает оба до этого файла
   ([конфигурация](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. Удалите запись `claude mcp add`, созданную без токена (`claude mcp remove loom -s user`), прежде чем запускать команду из диалога: она отказывается работать, если имя уже существует.

`loom install --client <name>` записывает запись MCP для Claude Desktop, Claude Code, Codex, Cursor, VS Code, Cline, Windsurf и Zed; без флага он подключает всех найденных клиентов. О Codex и Cursor рассказано в разделе [Подключение кодингового агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md).

Начните новую сессию и скажите _«Запомни, что я никогда не деплою по пятницам.»_ Агент вызовет `loom_memory_set`. В новой сессии спросите _«Когда мне можно деплоить?»_ Агент вызовет `loom_search` и ответит по сохранённому факту. Модель решает, когда сохранять и искать, по описаниям инструментов; никаких триггерных слов нет.

<a id="7-see-it-in-the-dashboard"></a>

## 7. Просмотр в панели управления

| Страница          | Что показывает                                              |
| ----------------- | ----------------------------------------------------------- |
| `/memories`       | Память пространства имён с указанием автора записи и времени. |
| `/admin/activity` | Журнал аудита: какой агент записал какой ключ и когда.      |
| `/admin/usage`    | Стоимость по операциям и агентам.                           |

Остальное описано в разделе [Панель управления](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

<a id="optional-index-a-repository-on-the-released-stack"></a>

## Необязательно: индексация репозитория на выпущенном стеке

Добавление репозитория из панели управления на этом стеке отклоняется с ошибкой `SANDBOX_UNAVAILABLE`:
для него нужна [песочница для заданий репозитория](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox),
которую этот стек оставляет выключенной, потому что стандартная изоляция Docker её блокирует. Добавляйте
только репозитории, которым доверяете: их шаги сборки (установка зависимостей TypeScript, загрузка модулей Go,
CMake) выполняются без песочницы, в контейнере `loom`, в окружении которого находятся секрет JWT
и ключи провайдеров.

`LOOM_CODE_REPOS_JSON` — это одна строка, содержащая все репозитории в виде массива JSON
([Настройка записи](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
В `~/.loom/.env` раскомментируйте эту строку (добавьте её, если её нет) и отредактируйте.
`dev` — это организация, от имени которой работает однопользовательский стек и в которую регистрируются
учётные записи многопользовательского режима (`LOOM_SELF_SIGNUP_ORG`); если вы это изменили, укажите организацию
своей учётной записи здесь и в `--org` ниже:

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom выбирает индексаторы уровня компилятора по файлам сборки репозитория (`go.mod`,
  `package.json`, `pyproject.toml`, корневой `CMakeLists.txt`, …); вместо этого `scip_producer` задаёт
  индексатор явно ([Уровень компилятора](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  Для C/C++ также нужно настроенное дерево сборки, поэтому без него для репозитория C/C++ доступен только
  уровень парсера.
- Для приватного репозитория добавьте токен GitHub (`LOOM_CODE_GITHUB_TOKEN` в `.env` или
  `github_token` в записи) и уберите `"public":true`: этот флаг предназначен для репозиториев, публичных
  на своём хосте, и позволяет любому участнику организации сразу присоединиться к индексу как читатель
  из раздела **Repositories**. Решите это до первого `loom code init`: он задаёт политику присоединения
  для пространства имён по значению `public`, и последующее изменение `public` её не меняет (сопровождающий
  меняет её через `code.repo.set_access`).

Затем пересоздайте контейнер `loom` и запустите первую индексацию:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` работает на локальной модели эмбеддингов выпущенного стека. При поддельной модели эмбеддингов (в `.env`
из репозитория или в обновлённой установке, где всё ещё указано `fake-ngram-*`) команда отказывается работать и
завершается; чтобы всё равно проверить связку, добавьте `--allow-fake-embedder`. Выбор модели позже в
**Settings → Models & keys** заново строит эмбеддинги индекса кода; после изменения `.env` их нужно перестроить
отдельно, как описано в разделе [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).

В многопользовательском режиме у репозитория из списка нет сопровождающего, поэтому без `"public":true` запрос
читателя на присоединение ждёт, а утвердить его некому. После `loom code init` предоставьте
кому-нибудь доступ от имени оператора с помощью токена, выпущенного в контейнере: `admin` делает пользователя
сопровождающим, который далее утверждает читателей, а `viewer` позволяет читать. Субъект — это пространство имён,
с которым зарегистрирована учётная запись (её логин, субъект её токенов); адрес электронной почты при
самостоятельной регистрации никогда не подтверждается, поэтому доступ, выданный на него, никого не допустит:

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

<a id="optional-ask-a-question-about-a-document"></a>

## Необязательно: задайте вопрос о документе

```bash
loom demo
```

`loom demo` загружает файл `AGENTS.md` из этого репозитория, обрабатывает его через `knowledge.ingest` (его разбирает эталонный экстрактор стека) и задаёт по нему вопрос через `knowledge.synthesize`, после чего выводит
ответ и фрагменты, на которые он ссылается. С фиктивной LLM вместо этого выводятся
лучшие результаты `knowledge.search`. Флаги `--file` и `--question` позволяют указать ваш собственный документ и вопрос.

<a id="common-problems"></a>

## Частые проблемы

| Симптом                                                                       | Причина и решение                                                                                                                                                                                                                                                            |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` не записывает факты                                       | LLM поддельная. Настройте настоящую по инструкции [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).                                                                                                                                                                                    |
| Результаты поиска выглядят произвольными                                      | Эмбеддер поддельный (`fake-ngram-*` в `.env`). Переключите его, как описано в [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md): сохранённые данные будут пересчитаны заново. Не удаляйте тома, чтобы сменить модели.                                                                               |
| 401 `AUTH_INVALID` после включения аутентификации                             | Токена нет, либо он подписан другим `LOOM_JWT_SECRET`. Выпустите токен заново командой Compose из раздела [Получите токен](#3-get-a-token).                                                                                                                                                   |
| Панель управления возвращает 503                                              | Сервер запущен из исходников (`make server`) без собранного фронтенда. Выполните `make frontend-build`.                                                                                                                                                                             |
| Релизный стек не становится работоспособным                                   | Выполните `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`. См. [Устранение неполадок](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md).                                                                                                                             |
| Добавление репозитория отвечает `SANDBOX_UNAVAILABLE`                         | Стек запущен без песочницы для задач с репозиториями. Индексируйте репозиторий через `LOOM_CODE_REPOS_JSON` ([Индексация репозитория](#optional-index-a-repository-on-the-released-stack)).                                                                                           |
| Установщик сообщает, что порт `7777` занят                                    | Его слушает другая программа или другой экземпляр Loom. Остановите её либо запустите команду установки снова с `LOOM_PORT=7778` перед `sh loom-install.sh`; `.env` сохранит порт.                                                                                                    |
| Установщик сообщает, что порт `7777` не отвечает                              | Порт занят программой, которая не отвечает, либо `LOOM_BIND_HOST`, `LOOM_PORT` или брандмауэр не дают этому хосту до него достучаться.                                                                                                                                                       |
| Установщик не смог проверить порт `7777`                                      | Curl завершился с ошибкой, которую называет его код выхода; проверьте `LOOM_BIND_HOST` и `LOOM_PORT`.                                                                                                                                                                                      |
| В Docker Desktop на WSL установка останавливается с `Ports are not available` | Windows занимает или резервирует порт, а проверка установщика внутри WSL этого не видит. `netsh interface ipv4 show excludedportrange protocol=tcp` выводит зарезервированные диапазоны; запустите команду установки снова, указав перед `sh loom-install.sh` значение `LOOM_PORT` вне этих диапазонов. |
| Порт `8123` или `9000` клона занят                                            | Переназначьте порты ClickHouse на хосте ([локальный стек](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                                                                                                                                                                       |
| Установщик не смог загрузить образы                                           | Docker сам обращается к реестру: за прокси задайте демону Docker [настройки прокси](https://docs.docker.com/engine/daemon/proxy/).                                                                                                                               |

<a id="next"></a>

## Дальше

- [Оценка Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): превратите этот smoke-тест в оценку качества,
  задержки и стоимости под вашу рабочую нагрузку.
- [Подключение кодирующего агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): Claude Code, Codex или Cursor
  на этом стеке.
- [Ваш первый агент](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): идентичность, обработка abstain, сессии, пакеты контекста
  и запуски из SDK.
- [Развёртывание](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): запуск Loom для команды.
