<!-- i18n: source=docs/use-cases/code-intelligence-by-language.md blob=ecd38c904b18ebd717798fb62bc55f139071429b model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/code-intelligence-by-language.md), which is authoritative.

<a id="code-intelligence-for-c-python-typescript-go-rust-java-and-infrastructure-code"></a>

# C++, Python, TypeScript, Go, Rust, Java 및 인프라 코드를 위한 코드 인텔리전스

Loom의 코드 인덱스는 저장소에 담긴 언어별로 코딩 에이전트의 구조 관련 질문에 답합니다. 심볼이 어디에 정의되어 있는지, 누가 호출하는지, 변경이 무엇에 영향을 주는지를 알려 줍니다.
저장소를 추가해야 하고 `LOOM_CODE_INGEST_ENABLED=true`가 필요합니다. 모든 언어는 빠른 티어를 제공하며, tree-sitter 문법이나 직접 작성한 스캐너로 파싱합니다. C/C++, Python, TypeScript, Go는 컴파일러처럼 이름을 해석하는 컴파일러 티어를 추가할 수 있습니다. 확장자와 엣지의 전체 목록은
[언어 지원 표](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)에, 컴파일러 티어 설정은
[code index operations](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)에 있습니다.

답변에는 근거가 된 티어가 표시되며(컴파일러 티어는 `tier: "scip"`, 빠른 티어는 `"treesitter"`), `code.coverage`는 네임스페이스에 언어별, 티어별로 무엇이 들어 있는지 보고합니다. Loom을 써 본 적 없는 언어에서 callers나 impact를 신뢰하기 전에 먼저 확인하세요.

<a id="c-and-c"></a>

## C 및 C++

심볼, 호출, include, 상속, 오버라이드는 빠른 티어에서 가져옵니다. 컴파일러 티어(`scip-clang`)는 프로젝트의 빌드를 바탕으로 오버로드, 템플릿, 네임스페이스를 해석합니다. 따라서 `DB::IMergeTreeDataPart::getBytesOnDisk` 같은 정규화된 이름에 `uses_brief: true`를 지정해 `code.symbol`을 호출하면 하나의 정의, 호출 지점 수, 그리고 상위 호출 지점과 그것을 둘러싼 함수가 반환되며, 각각 `tier: "scip"`로 표시됩니다. 컴파일러 티어에는 프로젝트의 CMake 빌드 설정이 필요합니다.

<a id="python"></a>

## Python

호출, import, 상속, 오버라이드는 빠른 티어에서 가져오며 노트북의 코드 셀도 포함됩니다. 컴파일러 티어(`scip-python`)는 모듈 간 이름을 해석합니다. Loom 자체 저장소에서 `writer_actor`에 `code.symbol`을 호출하면 컴파일러 티어 기준으로 `server/memory/app/provenance.py`의 정의가 시그니처 및 docstring과 함께 반환되고, `uses_brief: true`를 쓰면 호출 지점이 추가됩니다.

<a id="typescript-and-javascript"></a>

## TypeScript 및 JavaScript

호출(`new` 포함), import, 상속, 오버라이드는 빠른 티어에서 가져오며, Vue, Svelte, Astro 컴포넌트의 스크립트 블록도 마찬가지입니다. Angular `.component.html` 템플릿은 해당 컴포넌트와 함께 하나의 단위로 인덱싱됩니다. 컴파일러 티어(`scip-typescript`)는 프로젝트의 `tsconfig.json`, `jsconfig.json`, `package.json`을 통해 이름을 해석합니다.

<a id="go"></a>

## Go

호출, 인터페이스 임베딩, 구조적 구현은 빠른 티어에서 가져오며, Go 문자열 안의 SQL은 테이블 참조가 됩니다. 컴파일러 티어(`scip-go`)는 모노레포를 포함해 패키지 간 이름을 해석합니다.

<a id="rust-and-java"></a>

## Rust 및 Java

Rust는 호출, `use` import, 트레이트 구현을 지원하고, Java는 import가 이름을 바인딩하는 호출과 Java 문자열 안의 SQL을 지원합니다. 둘 다 빠른 티어만 제공합니다. 같은 파일에서 바인딩된 호출의 신뢰도는 1.0, 둘러싼 클래스를 통한 호출은 0.8, 저장소 전체의 이름 일치로 바인딩된 호출은 0.6이며, 바인딩할 수 없는 호출은 추측하지 않고 제외합니다. 또한 `code.impact`는 파일 수준에서 매핑된 테스트와 함께 변경되는 파트너를 추가합니다.

<a id="sql-and-migrations"></a>

## SQL 및 마이그레이션

`.sql` 파일은 기본적으로 검색과 grep을 위해 인덱싱됩니다. `LOOM_CODE_SQL_STATEMENT_RECORDS`를 켜면 각 구문이 테이블 참조와 함께 레코드가 되므로, 에이전트가 어떤 마이그레이션과 쿼리가 테이블을 읽거나 쓰는지 물어볼 수 있습니다. 이미 인덱싱된 네임스페이스에서 이를 켜려면 해당 SQL 파일에 대해 `loom code backfill --paths-file`을 실행해야 합니다
([스위치](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#language-support)).

<a id="infrastructure-as-code"></a>

## 코드형 인프라

Terraform과 HCL, Kubernetes 매니페스트, Helm 차트와 Go 템플릿, Jsonnet, CMake, Protocol Buffers, 셸 스크립트는 참조, include, 호출과 함께 인덱싱되므로, 에이전트는 Helm `include`에서 해당 `define`으로, 또는 Terraform 참조에서 그것이 가리키는 대상으로 따라갈 수 있습니다. Dockerfile, Makefile, Jenkinsfile, CI YAML 잡은 `LOOM_CODE_HOST_ISLANDS`를 켜면 심볼이 되며, 네임스페이스가 이미 인덱싱되어 있다면 해당 파일을 backfill한 뒤에 적용됩니다.

<a id="start"></a>

## 시작하기

1. [저장소를 인덱싱합니다](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/repositories.md). 어떤 컴파일러 티어가 실행되는지는 [code index operations](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md#compiler-tier)에 있습니다.
2. `coding` 툴셋에서 [코딩 에이전트를 연결합니다](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md).
3. 구조 관련 질문을 하고, 의존하는 언어에 대해 `code.coverage`를 확인합니다.

<a id="related"></a>

## 관련 문서

- [코딩 에이전트를 위한 코드 인덱스](code-intelligence.md)
- [코드 작업](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/operations.md)
- [언어 지원 추가하기](https://github.com/ClickHouse/loom/blob/main/docs/development/adding-language-support.md)
