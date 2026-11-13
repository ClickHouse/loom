<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=414b69b208ddf6610590f99678d2dc8e9bff8780 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# Анализ кода для C++, Python, TypeScript, Go, Rust, Java и инфраструктурного кода

Индекс кода Loom отвечает на вопросы агента-программиста о структуре кода на тех языках, которые есть в репозитории: где определён символ, кто его вызывает и что затрагивает изменение.
Нужен добавленный репозиторий и `LOOM_CODE_INGEST_ENABLED=true`. Для каждого языка есть быстрый уровень, который разбирается грамматиками tree-sitter или написанными вручную сканерами; для C/C++, Python, TypeScript и Go можно добавить уровень компилятора, который разрешает имена так же, как это делает компилятор. Полный список расширений и рёбер приведён в
[таблице поддержки языков](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support), а настройка уровней компилятора описана в
[эксплуатации индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).

В ответе указан уровень, на котором он получен (`tier: "scip"` для уровня компилятора, `"treesitter"`
для быстрого уровня), а `code.coverage` показывает, что содержит пространство имён по языкам и уровням. Прочитайте его, прежде чем доверять ответам `code.callers` или `code.impact` для языка, с которым вы ещё не использовали Loom.

<a id="c-and-c"></a>

## C и C++

Символы, вызовы, include-директивы, наследование и переопределения берутся из быстрого уровня; уровень компилятора (`scip-clang`) разрешает перегрузки, шаблоны и пространства имён по сборке проекта, поэтому `code.symbol` с `uses_brief: true` для квалифицированного имени, например `DB::IMergeTreeDataPart::getBytesOnDisk`, возвращает единственное определение, число мест вызова и основные места вызова с охватывающими функциями, каждое с пометкой `tier: "scip"`. Уровню компилятора нужна настройка сборки проекта через CMake.

<a id="python"></a>

## Python

Вызовы, импорты, наследование и переопределения берутся из быстрого уровня, включая ячейки кода в ноутбуках; уровень компилятора (`scip-python`) разрешает имена между модулями. В репозитории самого Loom `code.symbol` для `writer_actor` возвращает его определение в
`server/memory/app/provenance.py` с сигнатурой и docstring на уровне компилятора, а `uses_brief: true` добавляет места его вызова.

<a id="typescript-and-javascript"></a>

## TypeScript и JavaScript

Вызовы (включая `new`), импорты, наследование и переопределения берутся из быстрого уровня, так же как и блоки скриптов компонентов Vue, Svelte и Astro; шаблон Angular `.component.html` индексируется как единое целое со своим компонентом. Уровень компилятора (`scip-typescript`)
разрешает имена через `tsconfig.json`, `jsconfig.json` или `package.json` проекта.

<a id="go"></a>

## Go

Вызовы, встраивание интерфейсов и структурная реализация берутся из быстрого уровня, а SQL в строках Go превращается в ссылки на таблицы; уровень компилятора (`scip-go`) разрешает имена между пакетами, включая монорепозитории.

<a id="rust-and-java"></a>

## Rust и Java

Для Rust индексируются вызовы, импорты `use` и реализации трейтов; для Java — вызовы (имена связываются через импорты) и SQL в строках Java. Для обоих есть только быстрый уровень: вызов, связанный в том же файле, получает достоверность 1.0, через охватывающий класс — 0.8, а по совпадению имени в пределах репозитория — 0.6; вызов, который связать не удалось, отклоняется, а не угадывается, а `code.impact` добавляет сопоставленные тесты и файлы, изменяющиеся вместе с данным, на уровне файлов.

<a id="sql-and-migrations"></a>

## SQL и миграции

Файлы `.sql` по умолчанию индексируются для поиска и grep. Если включён `LOOM_CODE_SQL_STATEMENT_RECORDS`, каждый оператор становится записью со ссылками на таблицы, поэтому агент может спросить, какие миграции и запросы читают или изменяют таблицу. Чтобы включить это для уже проиндексированного пространства имён, нужно выполнить `loom code backfill --paths-file` для файлов SQL
([переключатели](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## Инфраструктура как код

Terraform и HCL, манифесты Kubernetes, чарты Helm и шаблоны Go, Jsonnet, CMake, Protocol Buffers и shell-скрипты индексируются вместе со ссылками, include-директивами и вызовами, поэтому агент может перейти от Helm `include` к его `define` или от ссылки Terraform к тому, что она называет. Dockerfile, Makefile, Jenkinsfile и задания CI YAML становятся символами при включённом `LOOM_CODE_HOST_ISLANDS`, после backfill этих файлов, если пространство имён уже проиндексировано.

<a id="start"></a>

## Начало работы

1. [Проиндексируйте репозиторий](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md). Какие уровни компилятора для него запускаются, описано в [эксплуатации индекса кода](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).
2. [Подключите агента-программиста](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) с набором инструментов `coding`.
3. Задайте вопрос о структуре; проверьте `code.coverage` для языков, на которые вы полагаетесь.

<a id="related"></a>

## См. также

- [Индекс кода для агентов-программистов](code-intelligence.md)
- [Операции с кодом](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Добавление поддержки языков](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
