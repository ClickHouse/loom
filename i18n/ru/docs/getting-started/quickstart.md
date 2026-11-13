<!-- i18n: source=docs/getting-started/quickstart.md blob=2dcc795de559021866a2affe24bef8e4e8f41f81 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/quickstart.md), which is authoritative.

<a id="quickstart"></a>

# Быстрый старт

На этой странице Loom запускается на ноутбуке, а первое воспоминание сохраняется через HTTP, Python и MCP. Ключ API и учётная запись не нужны. Свежая установка выпущенного стека строит эмбеддинги небольшой локальной моделью, которая входит в образ, поэтому поиск возвращает осмысленные результаты без сети (обновлённая установка сохраняет эмбеддер, указанный в её `.env`); LLM в ней фиктивная, поэтому извлечение и ответы `memory.ask` ничего не дают. В `.env` из репозитория для обоих компонентов используются фиктивные провайдеры.

**Стандартная установка Docker рассчитана на одного пользователя:** без входа в систему, с доступом только с localhost и пространством имён `dev/local` по умолчанию. Один человек может подключить несколько агентов, не включая многопользовательский режим. **Командные развёртывания тоже поддерживаются в OSS**: с аутентифицированными учётными записями и контролем доступа к пространствам имён; [начните в многопользовательском режиме](#start-in-multi-user-mode) или обратитесь к разделу [Deployment → Identity modes](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes).
Оба режима поддерживают одни и те же возможности Loom; для реальных LLM и эмбеддингов в любом режиме нужно настроить провайдеров.

<a id="prerequisites"></a>

## Требования

| Что нужно                      | Для чего                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------- |
| Docker с плагином compose      | Стек: ClickHouse и сервер Loom.                                                       |
| Python 3.12 или новее          | CLI `loom` только для контрибьюторов. SDK поддерживает Python 3.11+.                  |
| Node.js 20 или новее           | TypeScript SDK (необязательно).                                                       |
| Свободный порт `7777`          | Loom. При разработке из репозитория также открываются порты ClickHouse `8123` и `9000`. |
| `jq`                           | Чтение вывода `curl` на шаге 4 (необязательно).                                       |

<a id="1-boot-the-stack"></a>

## 1. Запуск стека

<a id="fast-path-released-image-no-clone"></a>

### Быстрый путь: готовый образ, без клонирования

Требование: Docker Desktop или Docker Engine с Compose 2.20 или новее.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Команда сохраняет установщик в текущем каталоге и затем запускает этот файл, поэтому запускайте
её из домашнего каталога или другого каталога, в который можете писать только вы: в общем каталоге
кто-то другой может заменить `loom-install.sh` или направить его на другой файл до запуска. Если загрузка не удалась,
команда останавливается с ошибкой curl и ненулевым кодом возврата; после неудачной установки исправьте
то, о чём сообщает установщик, и запустите всю команду заново. Установщик скачивает
`loom-stack.tar.gz` из последнего GitHub Release, проверяет его SHA-256, устанавливает
конфигурацию Compose и ClickHouse в `~/.loom`, загружает соответствующие образы
`ghcr.io/clickhouse/loom:X.Y.Z` и `ghcr.io/clickhouse/loom-extractor:X.Y.Z` (сервис
извлечения текста из документов) и ждёт ответа от `http://localhost:7777/v1/health`. Образ
сервера содержит API, воркеры, миграции и собранную панель управления на React в корне
сервера; установка Python или Node не нужна. Индексаторы Python, TypeScript и компилятора Go
входят в образ и включены. Для репозитория, проиндексированного индексатором TypeScript,
зависимости устанавливаются автоматически. Оба образа поддерживают Linux `amd64` и `arm64`,
включая Docker Desktop на Apple Silicon.

Стек запускает собственный контейнер ClickHouse. Чтобы использовать уже работающий у вас ClickHouse,
самостоятельно развёрнутый сервер или сервис ClickHouse Cloud (26.4 или новее, с Keeper), передайте его
URL; если в URL нет пароля, установщик запросит его, создаст базу данных `loom`
и пользователя только для чтения либо выведет SQL, который должен выполнить администратор
([выбор ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

`install.sh` каждого релиза устанавливает именно этот релиз, поэтому копия, которую отдаёт `latest`, устанавливает
последний релиз, а URL конкретного релиза, ниже, каждый раз устанавливает этот релиз. Поверх более новой
установки копия более старого релиза отказывается ставиться, чтобы не запускать более старые Loom
и ClickHouse на более новых данных; чтобы понизить версию намеренно, следуйте
[инструкции по понижению версии установки через установщик](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md#downgrading-an-installer-install).
Чтобы зафиксировать релиз, используйте его собственный URL:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Чтобы всё проверить перед запуском, скачайте файлы и просмотрите их:

```bash
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz
```

Затем, когда выведенное выглядит правильно, установите из проверенного архива:

```bash
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh && rm loom-install.sh
```

Значения `LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`, `LOOM_EXTRACTOR_IMAGE_REPOSITORY`
и `LOOM_CLICKHOUSE_IMAGE`, переданные установщику, записываются в `.env` каталога установки,
поэтому последующие команды `docker compose` в нём и последующие запуски установщика используют тот же
порт и те же образы. Значение `LOOM_BIND_HOST`, отличное от loopback, публикует Loom за пределами этого хоста,
и сервер откажется так запускаться без `LOOM_PUBLIC_BASE_URL` и входа в систему:
установите с `LOOM_SKIP_START=1`, задайте их в `.env` (ниже), затем снова запустите команду установки
без `LOOM_SKIP_START=1`.

Каждый релиз использует тот релиз ClickHouse, который был последним на момент сборки, поэтому повторный запуск
команды установки для той же версии Loom сохраняет её ClickHouse; установка более новой
версии Loom обновляет его, если только `.env` не закрепляет `LOOM_CLICKHOUSE_IMAGE` (удалите эту строку, чтобы
снова следовать релизам). Повторный запуск команды установки обновляет управляемые
файлы Compose и конфигурации и приводит контейнеры в соответствие. Он сохраняет `~/.loom/.env`,
добавляя в файл без них URL экстрактора и токен, а также именованные тома Docker. Чтобы
остановить стек без удаления данных:

```bash
docker compose --project-directory "$HOME/.loom" down
```

Добавляйте `--volumes` только для намеренного, необратимого сброса.

<a id="try-it-in-github-codespaces"></a>

### Попробовать в GitHub Codespaces

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/ClickHouse/loom?quickstart=1)

Dev-контейнер репозитория запускает последний релиз в codespace, ничего не устанавливая на вашу машину: он ставит Loom
описанным выше установщиком, запускает его и подключает к нему Claude
Code. Первый запуск загружает около 3 ГБ образов и занимает несколько минут. Откройте
панель управления по адресу, который выведен в последних строках журнала создания (`Loom is running in the
codespace. Dashboard: https://<codespace name>-7777.app.github.dev`, это
переадресованный адрес порта 7777 на вкладке **Ports**); адрес установщика `http://127.0.0.1:7777/` выше него — это
собственный адрес codespace. В терминале codespace выполните `cd ~ && claude` (вне
checkout, инструкции которого предназначены для работы над самим Loom), войдите в систему и скажите «Remember
that I deploy only on Tuesdays»; в новой сессии `claude` спросите «When do I deploy?».

Пробная версия работает в однопользовательском режиме, без входа в систему. В браузере порт 7777 переадресован
как приватный: его адрес отвечает только вашей учётной записи GitHub. Оставьте его приватным: публичный доступ
или приватный в пределах организации открывает сервер без входа в систему всем, кому он доступен.
Агенты внутри codespace, например его `claude`, используют `http://localhost:7777`; строки подключения
в панели управления называют вместо этого переадресованный адрес, для которого нужен вход через GitHub,
а у агентов в codespace его нет. При открытии в настольном VS Code codespace
также пробрасывает порт на `localhost:7777` вашего ноутбука, где он так же открыт, как локальная
установка, для любой программы на ноутбуке, и агенты на ноутбуке могут обращаться к нему там; для проброса
этот локальный порт должен быть свободен, поэтому сначала остановите локальный Loom. Пользуйтесь панелью управления
по адресу в браузере: по `localhost:7777` ноутбука страница откроется, но сервер отклонит
её запросы, поэтому данных она не покажет; собственная страница этого сервера доступна только по переадресованному адресу.

Установка находится в `/workspaces/.loom`, поэтому пересозданный codespace сохраняет свои `.env` и
проект Compose, хотя его воспоминания хранятся в томах Docker, которые пересоздание может не сохранить; при пересоздании
также запускается установщик последнего релиза поверх существующей установки, что обновляет её до этого релиза.
Везде, где в этой документации упоминается `~/.loom` или `"$HOME/.loom"`, в codespace используется
`/workspaces/.loom`. Codespaces предлагает открыть порт 7777, как только он начинает слушать; панель
управления отвечает, когда журнал создания выводит «Loom is running in the codespace». Если
журнал вместо этого заканчивается ошибкой, выполните `sh .devcontainer/start-loom.sh` в терминале
codespace: он сохраняет то, что настроил первый запуск.

<a id="start-in-multi-user-mode"></a>

### Запуск в многопользовательском режиме

Для новой установки из готового образа с учётными записями и входом в систему с первого запуска
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

После успешных [проверок состояния](#check-the-stack) откройте `http://localhost:7777/` и
зарегистрируйтесь. Клиентам SDK и MCP нужны токены; используйте организацию и пространства имён, выданные вашей
учётной записи, а не однопользовательские значения по умолчанию `dev/local` из примеров ниже.

Это включает многопользовательскую аутентификацию, но не публичный доступ по сети: стек по-прежнему привязан
к localhost. Прежде чем открывать его команде, настройте [операторский доступ](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)
и [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls). Настройте также [реальные модели и ключи API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md);
выбор многопользовательского режима не заменяет фиктивную LLM.

Для уже работающего однопользовательского стека следуйте [инструкциям по переключению](#2-open-the-dashboard);
пересоздание контейнеров сохраняет существующие тома данных.

<a id="contributor-path-build-a-checkout"></a>

### Путь для контрибьюторов: сборка из checkout

Требование: Docker Compose 2.34 или новее, он нужен файлу compose из checkout, чтобы
ежедневно загружать ClickHouse.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` делает три вещи:

1. Создаёт два файла, которые git не отслеживает: `.env` из `.env.example` и
   `deploy/clickhouse-config/named_collections.xml` из его шаблона. Он никогда
   не перезаписывает ни один из них; существующему `.env` без токена экстрактора он добавляет токен.
2. Запускает dev-стек compose с `--build`.
3. Опрашивает `http://localhost:7777/v1/health`, пока сервер не будет готов, выводя
   число ожидающих миграций. Через `--timeout` (300 с) он сдаётся, выводя последнее тело
   ответа health и строки журнала.

Первый запуск для контрибьютора собирает образ и применяет все миграции схемы, поэтому закладывайте
несколько минут. Затем `loom up` выводит URL панели управления (`http://localhost:7777/`), вызов `curl`
и фрагмент на Python, которые сохраняют первое воспоминание, и строку, подключающую Claude Code.
Если включена аутентификация, он также выпускает токен записи и подставляет его в эти фрагменты.

| Флаг         | Действие                          |
| ------------ | --------------------------------- |
| `--rebuild`  | Пересобрать после изменения кода. |
| `--no-build` | Использовать кэшированный образ.  |
| `--open`     | Открыть панель управления.        |
| `--port`     | Использовать другой порт Loom.    |
| `--env-file` | Указать другой env-файл.          |

Повторный запуск `loom up` на работающем стеке только заново проверяет состояние. `make dev` делает то же самое
без CLI и выполняет:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

Когда `compose.yaml` указан первым, Compose читает `.env` из корня репозитория для подстановок
`${VAR:-default}`; `--env-file .env` лишь говорит об этом явно. `loom down`
останавливает стек и сохраняет его тома. `loom down --delete-data` безвозвратно
удаляет именованные тома только этой установки после того, как вы введёте сгенерированное имя проекта
Compose; `--yes` — подтверждение без диалога. Цели Makefile
делают то же самое: `make down` сохраняет данные, а `make down-all` удаляет их.

<a id="what-the-stock-env-boots"></a>

### Что запускает стандартный `.env`

Шаблоны `.env` из checkout и из архива релиза выбирают однопользовательский режим.
Архив релиза явно задаёт `LOOM_IDENTITY_PROVIDER=single-user`; checkout
оставляет его пустым и выводит тот же режим из `LOOM_AUTH_REQUIRED=false`.

| Настройка                                                                    | Значение      | Эффект                                                                                     |
| ---------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------------------------ |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (или пусто) | `single-user` | Без входа. Пространство имён по умолчанию `dev/local`. Вызовам SDK и MCP токен не нужен.   |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | фиктивная LLM | Извлечение, разметка сущностей и синтез `memory.ask` ничего не производят.                 |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (архив релиза)        | bge-small     | Семантический поиск на CPU по файлам модели, которые входят в образ.                       |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (checkout) | фиктивный эмбеддер | Поиск ранжирует по символьным n-граммам. Достаточно, чтобы проверить связность, но не полноту. |

Оба установщика генерируют уникальный `COMPOSE_PROJECT_NAME`, случайный секрет подписи JWT, случайный
токен экстрактора и `LOOM_BIND_HOST=127.0.0.1`. Поэтому отдельные клоны не
делят тома ClickHouse незаметно, а Loom и ClickHouse доступны только с этого
компьютера.

Для реальных провайдеров откройте **Settings → Models & keys**, выберите провайдеров и модели,
введите их ключи API, затем нажмите **Test & apply**. Один ключ OpenAI API может обслуживать и
LLM, и эмбеддинги. Перезапуск Docker не нужен; изменение эмбеддингов запускает проверенную
перестройку, ход которой и повтор показываются в панели управления.

Для развёртывания через переменные окружения вместо этого замените строки провайдеров в `~/.loom/.env`
(стек из релиза) или `.env` (checkout). Одна лишь установка `OPENAI_API_KEY` ничего не меняет:
стандартные строки провайдеров называют своих провайдеров явно. Переход с локального или фиктивного
эмбеддера заново вычисляет эмбеддинги всего сохранённого.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

Для Claude, OpenAI, размещённых эмбеддингов, клиентов только с подпиской и развёртываний с
одним провайдером LLM следуйте [руководству по настройке моделей и ключей API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).
Только после правки `.env` стека из релиза примените его командой
`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`;
простой перезапуск не перезагружает окружение контейнера. Расширенные настройки описаны в
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
  `problems` перечисляет каждую проблему с её исправлением; `schema.pending` перечисляет непримённые миграции.
- `loom doctor` выводит по одной строке на каждую серверную проверку. `--json` выдаёт JSON. Для стека из исходного
  checkout выполните `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`.

<a id="2-open-the-dashboard"></a>

## 2. Откройте дашборд

`http://localhost:7777/` открывает дашборд сразу же в режиме `single-user`;
в многопользовательском режиме сначала зарегистрируйтесь или войдите. В режиме `single-user`
дашборд и агенты по умолчанию используют `dev/local` (`LOOM_DEFAULT_NAMESPACE` переименовывает
namespace). Это режим для удобства, а не граница изоляции пользователей: любой, кто может
подключиться к серверу, получает доступ к его данным и администрированию. Используйте его только на localhost.

Чтобы вместо этого попробовать регистрацию (`trust-network`: email и пароль, а первый, кто
займёт имя namespace, становится его администратором), замените эти записи в `~/.loom/.env`
(релизный стек) или `.env` (checkout):

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

После этого дашборд открывается на странице входа со вкладкой **Sign up**, а каждый вызов SDK
и MCP требует токен. Режимы идентификации описаны в разделе [Identities and
namespaces](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md#identity-modes). Явное указание
`trust-network` также позволяет запустить сервер с `LOOM_BIND_HOST`, отличным от loopback, при открытой регистрации,
так что любой, кто достучится до порта, сможет зарегистрироваться. Прежде чем открывать сервер
для команды, следуйте [руководству по развёртыванию](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes) по
доступу операторов и настройке [TLS](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#proxy-and-tls).

<a id="3-get-a-token"></a>

## 3. Получите токен

В однопользовательском режиме токен необязателен. Но если выпустить его сейчас, ваши скрипты продолжат работать после включения аутентификации, при условии что в них указаны `org` (или он передан пустым), `namespace` и `agent`: вызов, который передаёт учётные данные, как это делает SDK, если находит их ([Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials)), не получает ни одного из этих значений автоматически, а пустой `org` означает собственную организацию токена.

- **Дашборд:** **Settings → Tokens → New token**. По умолчанию токен читает все пространства имён и репозитории, к которым вы относитесь (ограниченный репозиторий — только если вы его добавите), но не читает ни одной коллекции, и сохраняет данные в пространство имён, в котором вы находитесь. Режим _Only what you choose_ ограничивает токен теми пространствами имён, коллекциями и репозиториями, которые вы отметили (сначала не отмечено ничего); отмеченные просматриваются при поиске в указанном порядке, а менять порядок можно стрелками в строке или, на десктопе, перетаскиванием за ручку строки. Результат показывает токен один раз вместе с настройкой для вашего MCP-клиента или скрипта.
- **CLI, от имени оператора:** выполните команду внутри контейнера сервера, в котором хранится секрет для подписи:

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  Добавьте `--ttl-days 30` для всего, что покидает вашу машину. `loom mint-token --help` выводит параметры ограничения доступа (`--namespace-role`, `--read-scope`, `--traffic-class`).

- **Бот, который регистрируется сам** (только в доверенной сети):
  `loom init --agent nightly-triage --server-url http://localhost:7777` создаёт пространство имён `agent-nightly-triage`, сохраняет токен writer в `~/.loom/token`, а URL в `~/.loom/config.json`, и выводит код для подтверждения владения. Человек один раз вводит его на странице `/oauth/agent-claim` со своим паролем, чтобы стать владельцем.

Сохраните токен там, где его ищут SDK, CLI и плагины:

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

Claims, роли и гранты описаны в разделе [Tokens and grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md).

<a id="4-first-memory-over-http"></a>

## 4. Первая запись в память через HTTP

Каждая операция — это `POST /v1/<op>` с JSON-телом. Свежая установка со стандартным
`.env`, из установщика или из checkout, работает в однопользовательском режиме: токен не нужен, а обычный вызов памяти
или поиска (`memory.set`, `memory.get`, `memory.search` и ещё несколько), в котором не указаны
`org` и `namespace`, выполняется в собственных org и namespace установки — org `dev` и namespace `local`, тех же, которые используют
MCP и дашборд. `memory.set` без `agent` записывается от имени агента
`http`, а поиск читает память всех агентов. Поэтому в вызовах ниже нужны только их собственные поля
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
`memory.get` читает обратно одну запись по ключу: `found` показывает, содержит ли ключ актуальное значение, а
`value` — значение в том виде, в котором оно было записано. Ответ поиска содержит `results` и `abstain`;
последнее показывает, достаточно ли сильно лучшее совпадение, чтобы отвечать на его основе. Поиск читает память
всех агентов в namespace, включая записи, добавленные в дашборде; чтобы читать только память одного агента, укажите
его в `agent`.
`GET /v1/operations` возвращает список всех операций, а на странице <http://localhost:7777/docs> каждую из них можно попробовать
из браузера. Структуры запросов и ответов описаны в [справочнике по HTTP API](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md).

<a id="5-first-memory-from-python"></a>

## 5. Первая запись памяти из Python

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

Релиз, которого ещё нет на PyPI, устанавливается из его assets
([установка из GitHub Release](https://github.com/ClickHouse/loom/blob/main/docs/development/release-process.md#installing-from-a-github-release)).

В примере указаны `org` и `namespace`, поэтому он работает и с токеном из шага 3, и без него: SDK отправляет токен, если находит его. На однопользовательском сервере вызов без токена может обойтись без этих параметров: `Agent(agent="quickstart", base_url=...)` тоже работает в `dev`/`local`.

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

О том, где SDK находит сервер и учётные данные, читайте в разделе
[Python SDK → Credentials](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md#credentials).
TypeScript SDK устанавливается командой `npm install @clickhouse/loom`
([из клона репозитория](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md#install)).

Интерфейсы клиентов описаны в справочниках [Python SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/python-sdk.md) и
[TypeScript SDK](https://github.com/ClickHouse/loom/blob/main/docs/reference/typescript-sdk.md).

<a id="6-first-memory-from-a-coding-agent"></a>

## 6. Первая память от coding-агента

Для Claude Code установите плагин Loom. Он добавляет хуки (контекст при старте сессии,
сохранение в конце) и skills поверх инструментов MCP:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

На однопользовательском сервере на этой машине оставьте URL сервера по умолчанию и не заполняйте токен,
когда Claude Code запросит настройки плагина.

Без плагина добавьте запись MCP напрямую:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

Оба способа подключаются к однопользовательскому серверу на этой машине без вашего токена.

Если включена аутентификация, создайте токен в **Settings → Tokens** и выберите **Claude Code** в разделе
**Connect**: в диалоге показана настройка для плагина и для записи `claude mcp add`
с вашим токеном. Затем:

1. Перезапишите `~/.loom/config.json` блоком `config.json` из диалога и добавьте
   namespace, в который сохраняет токен (`"namespace": "<namespace>"`): значения `org` или `namespace`,
   оставшиеся в файле от однопользовательской сессии, имеют приоритет над токеном, а без них плагин
   пишет в первый namespace токена, отличный от `*`, или в `coding`. Верните обратно `code_repo`
   или `agent`, если вы их задавали.
2. Оставьте `LOOM_API_KEY` пустой (на шаге 3 она могла быть экспортирована: `unset LOOM_API_KEY`) и
   параметр плагина `token` тоже пустым либо задайте в них новый токен: плагин читает оба значения
   раньше, чем этот файл
   ([конфигурация](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md#configuration)).
3. Удалите запись `claude mcp add`, созданную без токена (`claude mcp remove loom -s user`),
   прежде чем выполнять команду из диалога: она отказывается работать, если имя уже существует.

`loom install --client <name>` записывает MCP-запись для Claude Desktop, Claude Code,
Codex, Cursor, VS Code, Cline, Windsurf и Zed; без флага она подключает всех найденных
клиентов. О Codex и Cursor рассказано в статье [Подключение coding-агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md).

Начните новую сессию и скажите _«Запомни, что я никогда не деплою по пятницам.»_ Агент вызывает
`loom_memory_set`. В новой сессии спросите _«Когда мне можно деплоить?»_ Агент вызывает
`loom_search` и отвечает на основе сохранённого факта. Модель решает, когда сохранять и
искать, по описаниям инструментов; триггерных слов нет.

<a id="7-see-it-in-the-dashboard"></a>

## 7. Посмотрите результат в дашборде

| Страница          | Что показывает                                              |
| ----------------- | ----------------------------------------------------------- |
| `/memories`       | Memories в namespace с указанием автора записи и времени.   |
| `/admin/activity` | Журнал аудита: какой агент записал какой ключ и когда.      |
| `/admin/usage`    | Стоимость в разрезе операций и агентов.                     |

Остальное описано в разделе [Dashboard](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md).

<a id="optional-index-a-repository-on-the-released-stack"></a>

## Дополнительно: индексирование репозитория на выпущенном стеке

Добавление репозитория из дашборда на этом стеке отклоняется с ошибкой `SANDBOX_UNAVAILABLE`:
для него нужна [песочница для задач репозитория](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#repository-job-sandbox),
а этот стек её не включает, потому что стандартная изоляция Docker её блокирует. Добавляйте только те
репозитории, которым доверяете: их шаги сборки (установка зависимостей TypeScript, загрузка модулей Go,
CMake) выполняются без песочницы, в контейнере `loom`, в окружении которого находятся секрет JWT
и ключи провайдеров.

`LOOM_CODE_REPOS_JSON` — одна строка, содержащая все репозитории в виде JSON-массива
([Настройка записи](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#configure-the-entry)).
В `~/.loom/.env` раскомментируйте эту строку (добавьте её, если её нет) и отредактируйте.
`dev` — это организация, от имени которой работает однопользовательский стек, и организация, в которую
регистрируются учётные записи в многопользовательском режиме (`LOOM_SELF_SIGNUP_ORG`); если вы это
изменили, укажите здесь и в `--org` ниже организацию вашей учётной записи:

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom выбирает индексаторы уровня компилятора по файлам сборки репозитория (`go.mod`,
  `package.json`, `pyproject.toml`, `CMakeLists.txt` в корне, …); вместо этого индексатор можно задать
  через `scip_producer` ([Уровень компилятора](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)).
  Для C/C++ также нужно настроенное дерево сборки, поэтому без него для репозитория на C/C++
  работает только уровень парсера.
- Для приватного репозитория добавьте токен GitHub (`LOOM_CODE_GITHUB_TOKEN` в `.env` или
  `github_token` в записи) и уберите `"public":true`: этот флаг предназначен для репозиториев, открытых
  на своём хосте, и позволяет любому участнику организации сразу присоединиться к индексу как читателю
  в разделе **Repositories**. Решите это до первого `loom code init`: он задаёт политику присоединения
  для пространства имён по значению `public`, и последующее изменение `public` её не меняет
  (maintainer меняет её через `code.repo.set_access`).

Затем пересоздайте контейнер `loom` и запустите первое индексирование:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` работает на локальной модели эмбеддингов выпущенного стека. При фиктивной модели
эмбеддингов (в `.env` из checkout или в обновлённой установке, где всё ещё указано `fake-ngram-*`) команда
отказывается работать и завершается; чтобы всё равно проверить связку, добавьте `--allow-fake-embedder`.
Выбор модели позже в **Settings → Models & keys** пересчитывает эмбеддинги индекса кода; после изменения
`.env` пересчёт нужно запустить отдельно, как описано в разделе [Модели и ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).

В многопользовательском режиме у перечисленного репозитория нет maintainer, поэтому без `"public":true`
запрос читателя на присоединение остаётся без ответа: подтверждать его некому. После `loom code init`
выдайте кому-нибудь доступ от имени оператора с помощью токена, созданного в контейнере: роль `admin`
делает пользователя maintainer, который в дальнейшем подтверждает читателей, а роль `viewer` позволяет
читать. Субъект — это пространство имён, с которым зарегистрирована учётная запись (её логин, субъект её
токенов); адрес электронной почты при самостоятельной регистрации никогда не проверяется, поэтому выдача
доступа на него никого не допустит:

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

`loom demo` загружает файл `AGENTS.md` из этого репозитория, обрабатывает его через `knowledge.ingest` (его разбирает референсный экстрактор стека) и задаёт вопрос по нему через `knowledge.synthesize`, после чего выводит
ответ и фрагменты, на которые он ссылается. С фиктивной LLM вместо этого выводятся лучшие результаты
`knowledge.search`. Флаги `--file` и `--question` позволяют указать ваш собственный документ и вопрос.

<a id="common-problems"></a>

## Частые проблемы

| Симптом                                           | Причина и решение                                                                                                                                                                              |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` не записывает факты           | Используется поддельная LLM. Настройте настоящую по инструкции [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md).                                                                                         |
| Результаты поиска выглядят случайными             | Используется поддельный эмбеддер (`fake-ngram-*` в `.env`). Переключите его, как описано в [Models and keys](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md); сохранённые данные будут переэмбеддированы. Не удаляйте тома, чтобы сменить модели. |
| 401 `AUTH_INVALID` после включения аутентификации | Токена нет или он подписан другим `LOOM_JWT_SECRET`. Выпустите токен заново командой Compose из раздела [Get a token](#3-get-a-token).                                                          |
| Дашборд возвращает 503                            | Сервер запущен из исходников (`make server`) без собранного фронтенда. Выполните `make frontend-build`.                                                                                        |
| Стек из релиза не становится healthy              | Выполните `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`. См. [Troubleshooting](https://github.com/ClickHouse/loom/blob/main/docs/operations/troubleshooting.md).                                                |
| Добавление репозитория отвечает `SANDBOX_UNAVAILABLE` | Стек запущен без песочницы для заданий по репозиториям. Проиндексируйте репозиторий через `LOOM_CODE_REPOS_JSON` ([Index a repository](#optional-index-a-repository-on-the-released-stack)). |
| Установщик сообщает, что порт `7777` занят        | Его слушает другая программа или другой экземпляр Loom. Остановите её либо повторите команду установки, указав перед `sh loom-install.sh` значение `LOOM_PORT=7778`; порт сохранится в `.env`. |
| Установщик сообщает, что порт `7777` не отвечает  | Порт занят программой, которая не отвечает, либо `LOOM_BIND_HOST`, `LOOM_PORT` или файрвол не дают этому хосту до него достучаться.                                                              |
| Установщик не смог проверить порт `7777`          | Curl завершился с ошибкой, которую называет его код выхода; проверьте `LOOM_BIND_HOST` и `LOOM_PORT`.                                                                                          |
| Порт `8123` или `9000` в клоне занят              | Переназначьте порты ClickHouse на хосте ([local stack](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#local-stack-from-a-clone)).                                                                                                  |
| Установщик не смог загрузить образы               | Docker сам обращается к реестру: за прокси задайте демону Docker [настройки прокси](https://docs.docker.com/engine/daemon/proxy/).                                                              |

<a id="next"></a>

## Дальше

- [Оценка Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md): превратите этот smoke-тест в оценку качества,
  задержек и стоимости под вашу рабочую нагрузку.
- [Подключение coding-агента](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md): Claude Code, Codex или Cursor
  с этим стеком.
- [Ваш первый агент](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md): идентичность, обработка abstain, сессии, context packs
  и запуски (runs) из SDK.
- [Развертывание](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md): запуск Loom для команды.
