<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# Self-hosted память агентов на ClickHouse

Loom работает на вашей собственной инфраструктуре: в Docker или в Kubernetes с помощью Helm-чарта. Память агентов, документы, диалоги и индексы кода хранятся в виде таблиц в ClickHouse. При первом запуске для актуального состояния памяти и знаний в развёртывании можно выбрать Postgres. Данные хранятся только в ваших хранилищах (внешние провайдеры моделей и приёмники трассировок, которые вы настроили, получают лишь то, что они обрабатывают), а то, что знают ваши агенты, можно запрашивать с помощью SQL.

<a id="what-you-get"></a>

## Что вы получаете

- **Docker или Kubernetes.** Однострочный установщик запускает локальный стек; Helm-чарт
  разворачивает N одинаковых реплик за одним URL. [Развёртывание](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Запуск N реплик](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS или ClickHouse Cloud.** [Выбор ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud и Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **Единое хранилище для векторов, текста и аудита.** При ClickHouse в роли источника истины по умолчанию
  векторный, лексический и графовый поиск, фильтры, аудит и удаление данных выполняются как SQL в одной базе данных.
  [Источник истины в хранилище](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **Многопользовательский режим.** Организации, пространства имён, роли, токены и единый вход (single sign-on).
  [Идентификаторы и пространства имён](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [Единый вход](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **Локальные модели.** Выпущенный стек строит эмбеддинги на CPU со встроенной локальной моделью, а
  провайдер эмбеддингов принимает OpenAI-совместимый endpoint, который вы запускаете сами.
  [Модели и API-ключи](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## Результаты измерений

Синтетическая загрузка памяти с 2.4 версии на ключ и эмбеддингами размерности 3072 (два вектора на строку) заняла в ClickHouse около 13.6 КБ на ключ, причём почти весь объём приходится на строку с эмбеддингом. Эмбеддер меньшего размера занимает меньше места, а куски данных (parts) с небольшим числом строк занимают чуть больше места на строку, пока не объединятся.
[Память в масштабе](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## Начало работы

[Быстрый старт](../getting-started/quickstart.md) · [Требования](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[Обновления](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [Резервное копирование и восстановление](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
