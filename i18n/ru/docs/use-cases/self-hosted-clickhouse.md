<!-- i18n: source=docs/use-cases/self-hosted-clickhouse.md blob=347e84ca2fb7bc0a85662a875e99064979582695 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/self-hosted-clickhouse.md), which is authoritative.

<a id="self-hosted-agent-memory-on-clickhouse"></a>

# Самостоятельно размещаемая память агентов на ClickHouse

Loom работает на вашей собственной инфраструктуре, в Docker или в Kubernetes с помощью Helm-чарта, и
хранит память агентов, документы, диалоги и индексы кода в виде таблиц в ClickHouse. При первом запуске
развёртывание может выбрать Postgres для актуального состояния памяти и знаний. Данные
хранятся только в ваших хранилищах (размещённые у провайдеров модели и приёмники трассировок, которые вы настраиваете,
получают то, что они обрабатывают), а то, что знают ваши агенты, можно запрашивать с помощью SQL.

<a id="what-you-get"></a>

## Что вы получаете

- **Docker или Kubernetes.** Однострочный установщик запускает локальный стек; Helm-чарт
  запускает N одинаковых реплик за одним URL. [Развёртывание](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md) ·
  [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) · [Запуск N реплик](https://github.com/ClickHouse/loom/blob/main/docs/operations/replicas.md)
- **ClickHouse OSS или ClickHouse Cloud.** [Выбор ClickHouse](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud и Managed Postgres](https://github.com/ClickHouse/loom/blob/main/docs/operations/clickhouse-cloud.md)
- **Одно хранилище для векторов, текста и аудита.** При источнике истины ClickHouse по умолчанию
  векторный, лексический и графовый поиск, фильтры, аудит и удаление выполняются через SQL в одной базе данных.
  [Источник истины хранилища](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)
- **Многопользовательский режим.** Организации, пространства имён, роли, токены и единый вход.
  [Идентичности и пространства имён](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) ·
  [Единый вход](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md)
- **Локальные модели.** Выпущенный стек строит эмбеддинги на CPU с помощью встроенной локальной модели, а
  провайдер эмбеддингов принимает совместимую с OpenAI конечную точку, которую вы запускаете сами.
  [Модели и ключи API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)

<a id="measured"></a>

## Измерения

Синтетическая нагрузка из записей памяти с 2.4 версии на ключ и эмбеддингами размерности 3072 (два
вектора на строку) заняла в ClickHouse около 13.6 КБ на ключ, почти всё это приходится на строку
эмбеддинга; меньший эмбеддер занимает меньше, а куски с небольшим числом строк занимают
чуть больше на строку, пока не будут слиты.
[Память в масштабе](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)

<a id="start"></a>

## Начало работы

[Быстрый старт](../getting-started/quickstart.md) · [Предварительные требования](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/overview.md#prerequisites) ·
[Обновления](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md) · [Резервное копирование и восстановление](https://github.com/ClickHouse/loom/blob/main/docs/operations/backup-and-recovery.md)
