<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=0ed7353714a116300c522e3520e5b1986b56ca81 model=claude-sonnet-5-5 -->
> Эта страница — машинный перевод [английской страницы](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md); при расхождениях верна английская версия.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# Анализ кода для C++, Python, TypeScript, Go, Rust, Java и инфраструктурного кода

Индекс кода в Loom отвечает на структурные вопросы кодирующего агента на тех языках, которые есть в репозитории: где определён символ, кто его вызывает и что затрагивает изменение.
Требуется добавленный репозиторий и `LOOM_CODE_INGEST_ENABLED=true`. Для каждого языка работает быстрый уровень, который разбирает код с помощью грамматик tree-sitter или написанных вручную сканеров; для C/C++, Python, TypeScript и Go можно добавить уровень компилятора, который разрешает имена так же, как это делает компилятор. Полный список расширений и рёбер приведён в
[таблице поддержки языков](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support), а настройка уровней компилятора описана в
[операциях с индексом кода](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier).

Ответ указывает уровень, на котором он получен (`tier: "scip"` для уровня компилятора, `"treesitter"`
для быстрого уровня), а `code.coverage` сообщает, что содержит пространство имён по каждому языку и уровню. Прочитайте этот ответ, прежде чем доверять результатам callers или impact для языка, с которым вы ещё не использовали Loom.

<a id="c-and-c"></a>

## C и C++

Символы, вызовы, include-директивы, наследование и переопределения берутся из быстрого уровня; уровень компилятора (`scip-clang`) разрешает перегрузки, шаблоны и пространства имён на основе сборки проекта, поэтому `code.symbol` с `uses_brief: true` для квалифицированного имени, например `DB::IMergeTreeDataPart::getBytesOnDisk`, возвращает единственное определение, число мест вызова и основные места вызова вместе с охватывающими их функциями, каждое с пометкой `tier: "scip"`. Для уровня компилятора нужна настройка сборки проекта через CMake.

<a id="python"></a>

## Python

Вызовы, импорты, наследование и переопределения берутся из быстрого уровня, включая ячейки кода в ноутбуках; уровень компилятора (`scip-python`) разрешает имена между модулями. В собственном репозитории Loom `code.symbol` для `writer_actor` возвращает его определение в `server/memory/app/provenance.py` вместе с сигнатурой и docstring на уровне компилятора, а `uses_brief: true` добавляет места его вызова.

<a id="typescript-and-javascript"></a>

## TypeScript и JavaScript

Вызовы (включая `new`), импорты, наследование и переопределения берутся из быстрого уровня, так же как и блоки script в компонентах Vue, Svelte и Astro; шаблон Angular `.component.html` индексируется как единое целое вместе со своим компонентом. Уровень компилятора (`scip-typescript`) разрешает имена через `tsconfig.json`, `jsconfig.json` или `package.json` проекта.

<a id="go"></a>

## Go

Вызовы, встраивание интерфейсов и структурная реализация берутся из быстрого уровня, а SQL в строках Go превращается в ссылки на таблицы; уровень компилятора (`scip-go`) разрешает имена между пакетами, включая монорепозитории.

<a id="rust-and-java"></a>

## Rust и Java

Для Rust Loom разбирает вызовы, импорты `use` и реализации трейтов; для Java — вызовы (импорты связывают имена) и SQL в строках Java. Для обоих языков доступен только быстрый уровень: вызов, связанный в том же файле, получает уверенность 1.0, через охватывающий класс — 0.8, а по совпадению имени в пределах репозитория — 0.6. Вызов, который не удалось связать, отклоняется, а не угадывается, а `code.impact` дополнительно добавляет на уровне файлов сопоставленные тесты и партнёров по совместным изменениям.

<a id="sql-and-migrations"></a>

## SQL и миграции

Файлы `.sql` по умолчанию индексируются для поиска и grep. Если включить `LOOM_CODE_SQL_STATEMENT_RECORDS`, каждая инструкция становится записью со ссылками на таблицы, поэтому агент может спросить, какие миграции и запросы читают или изменяют таблицу. Чтобы включить это для пространства имён, которое уже проиндексировано, нужно выполнить `loom code backfill --paths-file` для SQL-файлов ([переключатели](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## Инфраструктура как код

Terraform и HCL, манифесты Kubernetes, чарты Helm и шаблоны Go, Jsonnet, CMake, Protocol Buffers и shell-скрипты индексируются вместе со своими ссылками, подключениями (includes) и вызовами, поэтому агент может перейти от Helm `include` к соответствующему `define`, а от ссылки в Terraform — к тому, что она обозначает. Dockerfile, Makefile, Jenkinsfile и задания CI YAML становятся символами при включённом `LOOM_CODE_HOST_ISLANDS`, после повторной индексации (backfill) этих файлов, если пространство имён уже проиндексировано.

<a id="start"></a>

## Начало работы

1. [Проиндексируйте репозиторий](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md); компиляторные уровни
   запускаются, когда в репозитории есть их файл сборки.
2. [Подключите агента для написания кода](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md) с набором инструментов `coding`.
3. Задайте вопрос о структуре кода; проверьте `code.coverage` для языков, на которые вы полагаетесь.

<a id="related"></a>

## Связанные материалы

- [Индекс кода для агентов программирования](code-intelligence.md)
- [Операции с кодом](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [Добавление поддержки языков](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
