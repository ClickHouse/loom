<!-- i18n: source=docs/concepts/identities-and-namespaces.md blob=05f85261bcf9106427aff88cb70b9f1b5e978a92 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md), which is authoritative.

<a id="identities-and-namespaces"></a>

# 아이덴티티와 네임스페이스

Loom은 모든 호출에서 두 가지 질문에 답합니다. 이 행은 누구의 것인가(아이덴티티 튜플과 서버가 기록하는 두 개의 컬럼), 그리고 호출자가 이 행에 접근해도 되는가(토큰, 네임스페이스의 권한 부여, 그리고 네임스페이스의 분류)입니다. 이 페이지에서는 그 모델을 설명합니다. 토큰을 발급하고 폐기하는 방법은 [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)에 있으며, 적용 방식은 [보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)에서 다룹니다.

<a id="where-namespaces-fit"></a>

## 네임스페이스의 위치

- **org**는 테넌트입니다.
- **네임스페이스**는 org 안에서 공유와 접근의 단위입니다. 일반적으로 하나의 배포에는 팀 또는 프로젝트마다 하나, 격리가 필요한 봇마다 하나, 사람마다 `personal-<slug>` 하나, 인덱싱된 브랜치마다 `code-<repo>[-<branch>]` 하나가 있습니다.
- 네임스페이스의 모든 멤버는 그 안의 모든 행을 읽을 수 있습니다.
- `agent`는 행을 작성한 주체를 기록할 뿐, 경계는 아닙니다.
- `user_id` / `read_scope=own_user`는 하나의 봇 네임스페이스 안에서 최종 사용자를 구분합니다.
- 접근 권한은 네임스페이스에 대한 역할(viewer/writer/admin)이며, 토큰에 담겨 있거나 grant로 추가됩니다.

<a id="the-identity-tuple"></a>

## 아이덴티티 튜플

모든 행에는 다섯 개의 아이덴티티 컬럼이 있습니다:

```
(org, namespace, agent, user_id, session_id)
```

| 항목         | 의미                                                                                         | 기본값                                                                                                |
| ------------ | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `org`        | 테넌트입니다. 와일드카드 운영자 토큰을 쓰지 않는 한 어떤 것도 org 경계를 넘지 않습니다.        | (필수, 단일 사용자 모드에서는 아래와 같이 자동으로 채워짐)                                                          |
| `namespace`  | org 안에서 격리와 공유의 단위입니다. 모든 읽기는 `(org, namespace)` 범위로 제한됩니다. | (필수, 단일 사용자 모드에서는 아래와 같이 자동으로 채워짐)                                                          |
| `agent`      | 행을 쓴 주체입니다(`support-bot`, `claude-code`). 읽기 경계가 아니라 작성자 정보입니다.           | (필수, 단일 사용자 HTTP에서는 일부 op에서 자동으로 채워짐, [HTTP 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)) |
| `user_id`    | 행이 다루는 대상 최종 사용자입니다.                                                           | `-`                                                                                                   |
| `session_id` | 하나의 대화 또는 대화 실행 한 번입니다.                                                       | `-`                                                                                                   |

많은 operation은 쓰기나 읽기를 하나의 작업 실행으로 한정하는 `run_id`(기본값 `-`)도 받습니다. 토큰은 호출자가 접근할 수 있는 org와 네임스페이스를 고정하며, 튜플의 나머지는 각 요청 본문에 담겨 전달됩니다. SDK는 클라이언트를 생성할 때 `(org, namespace, agent)`를 바인딩하고, `user_id`, `session_id`, `run_id`는 호출마다 전달하며, 호출에서 지정하지 않은 값은 `-`로 둡니다. 이 필드 중 하나가 읽기를 필터링하는 경우(검색, ask, 시작 팩, 대화 및 실행 목록, `memory.update`의 키 조회), `-`는 필터 없음을 뜻합니다. run을 지정하지 않은 검색은 모든 run의 행을 읽습니다. 필드가 행의 주소인 경우(`memory.get`, `memory.list`, `memory.stack`, 작업 메모리, 프로필 쓰기, 저장된 메시지), `-`는 사용자, 세션, run 없이 기록된 행을 뜻합니다.

**읽기는 모든 작성자에 걸칩니다.** 읽기의 기본값은 `agent="*"`로, 누가 썼는지와 상관없이 네임스페이스의 모든 행을 대상으로 합니다. `agent`를 지정하면 범위가 좁아질 뿐입니다. 읽기에서 `user_id`와 `session_id`는 해당 값으로 기록된 행만 걸러냅니다.

**튜플이 테이블의 맨 앞에 오는 이유.** `loom.memory`와 `loom.memory_embeddings`의 정렬 키는 `(org, namespace, agent, user_id, session_id)`로 시작하므로, 하나의 네임스페이스로 한정된 읽기는 ClickHouse에서 스캔이 아니라 기본 키 범위 조회가 되며, agent를 지정하면 범위가 더 좁아집니다. 기본값인 여러 agent에 걸친 읽기는 네임스페이스 범위 안에서 `user_id`와 `session_id`를 필터링합니다. 이때 키는 agent 컬럼이 허용하는 만큼만 이 값들로 그래뉼을 건너뛸 수 있고, 이들을 다루는 스킵 인덱스는 없습니다. 범위가 걸치는 모든 파트에서는 범위가 한정된 읽기라도 최소 한 그래뉼은 읽습니다
([대규모 메모리](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#memory-at-scale)).

<a id="what-the-server-stamps"></a>

### 서버가 기록하는 값

다른 두 컬럼은 모든 쓰기에서 서버가 결정하며 요청 본문에서 읽지 않습니다. 다만 호출자는 `source_class`를 격하할 수 있습니다
(`actor`는 `server/memory/app/provenance.py`, `source_class`는 `server/memory/domain/provenance.py`):

| 컬럼           | 값                                                                     | 결정 주체                                                                                                |
| -------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `actor`        | `human:<subject>` 또는 `process:<subject>`(서비스 계정)                | 검증된 토큰(`writer_actor`). 큐에 들어간 작업이나 CLI 가져오기는 그것을 시작한 주체로 기록됩니다. |
| `source_class` | `asserted`, `extracted`, `observed`, `derived`, `imported`, `external` | operation과 `source_trace` 규칙(`source_class_for`).                                                     |

요청은 `source_class`의 격하만 선언할 수 있습니다: `external`(도구나 페이지의 콘텐츠), `imported`, `derived`. 그 외의 값을 요청하면 422가 반환됩니다. 출처 정보가 랭킹과 `memory.ask`의 근거 선택에 쓰이기 때문입니다. 호출자가 긁어온 텍스트에 `asserted`라는 라벨을 붙일 수 있다면 그 라벨은 의미가 없어집니다. `agent`는 시스템 내 역할을 나타내므로 호출자가 지정하고, `actor`는 그 뒤에 있는 인증된 주체입니다.

<a id="orgs-and-namespaces"></a>

## 조직과 네임스페이스

**조직(org)**은 테넌트이고, **네임스페이스**는 행이 저장되는 위치이자 접근 권한을 부여하는 단위입니다. 네임스페이스는 등록(`namespace.register`, 호출자가 해당 네임스페이스의 `admin`이 됩니다), 가입 시 클레임, 서버의 프로비저닝(단일 사용자 네임스페이스, 그리고 로그인한 각 사용자를 위한 `personal-<slug>` 네임스페이스), 또는 인덱싱된 리포지토리를 위한 생성을 통해 만들어집니다. `loom.namespaces`의 디렉터리 행에는 설명, 종류, 공개 범위, 가입 정책, 충돌 및 제안 정책, 비용 상한, 보존 기간, 검색 기본값 등의 설정이 들어 있습니다.

| 네임스페이스 종류 | 저장 대상                                          | 읽는 주체                                                                      |
| -------------- | ------------------------------------------------- | ---------------------------------------------------------------------------- |
| `memory`       | 사실, 문서, 대화: 기본 종류 | 메모리, 지식, 대화 관련 operation 및 팬아웃                  |
| `code`         | 인덱싱된 리포지토리 하나와 브랜치                 | 이름으로 지정하는 `code.*` operation, 코드 범위에서의 `loom_search` |
| `bench`        | 벤치마크 코퍼스                                | 벤치마크. 트래픽은 `bench`로 분류됩니다                                   |

호출자의 모든 네임스페이스에 걸쳐 팬아웃하는 검색은 `memory` 종류만 대상으로 합니다(`LOOM_MEMORY_SEARCH_FANOUT_KINDS`). 개인적인 사실을 찾으려고 모든 코드 인덱스를 훑으면 인덱스마다 쿼리 한 번의 비용이 들고 아무것도 찾지 못합니다.

**에이전트별 저장소가 아니라 네임스페이스를 쓰는 이유.** 일반적인 경우는 같은 사실을 봐야 하는 여러 에이전트와 사람입니다. 팀의 배포 시간대나 고객의 이력이 그 예입니다. 네임스페이스를 읽기 경계로 삼으면 네임스페이스 안에서는 공유가 기본이고 경계를 넘어서는 공유되지 않으며, 사람이나 봇에게는 행을 복사하는 대신 네임스페이스에 대한 역할을 부여하여 접근 권한을 줍니다.

<a id="visibility-joining-and-restriction"></a>

### 공개 범위, 가입, 제한

| 설정             | 값                                          | 효과                                                                                                                                    |
| ------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `visibility`        | `private`(기본값), `public`                   | 공개 네임스페이스는 `namespace.browse`에 표시되며 가입할 수 있습니다. 공개 범위만으로는 읽기 권한이 부여되지 않습니다.                                |
| `join_policy`       | `open`, `approval`, `closed`, `inherit`(미설정) | `namespace.join`의 동작을 정합니다. `inherit`는 승인이 필요 없는 공개 네임스페이스에서는 `open`으로, 그 외에는 `approval`로 결정됩니다. |
| `requires_approval` | `true`, `false`                                 | `inherit`일 때만 참조됩니다. 이 경우 모든 가입이 관리자의 승인(`grants.approve`)을 기다립니다.                                                         |
| `classification`    | `standard`(기본값), `restricted`              | 권한을 넓히는 모든 기본 동작을 끕니다(아래 참조).                                                                                              |

`open` 네임스페이스에서는 `viewer` 가입이 즉시 활성화되고, `writer` 또는 `admin` 가입은 관리자가 승인할 때까지 보류됩니다. `approval`에서는 모든 가입이 보류되며, `closed`는 가입을 거부합니다.

**제한된(restricted)** 네임스페이스(디렉터리 행의 `classification=restricted` 또는 `LOOM_CODE_REPOS_JSON`의 해당 항목)는 다음 조건을 모두 만족할 때만 호출을 허용합니다. 토큰이 해당 네임스페이스의 조직에 속해야 하고, 네임스페이스를 (`*`가 아닌) 이름 그대로 지정해야 하며, 그곳에서 operation이 요구하는 역할에 도달하는 역할을 가져야 하고, 토큰 자신의 subject가 유효한 직접 grant를 가져야 합니다. 조직 읽기 규칙, 공개 범위, 와일드카드, 상속된 서비스 계정 grant, `org_admin`, `platform_admin` 중 어느 것도 이 조건을 우회할 수 없으며, 거부 응답은 네임스페이스의 존재 여부를 드러내지 않습니다. 팬아웃, 컨텍스트 팩, 롤업은 호출자가 읽을 수 없는 제한된 네임스페이스를 건너뛰고, 허용된 각 요청은 감사 기록에 남습니다. 단계별 설정 방법은 [토큰과 grant](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted)에 있습니다.

<a id="roles"></a>

## 역할

역할에는 순서가 있으며, 상위 역할은 하위 역할의 모든 검사를 만족합니다. 각
오퍼레이션은 필요한 최소 역할을 선언합니다(`GET /v1/operations`에서 확인할 수 있습니다).

| 역할             | 권한                                                                                              | 부여 방식                                                                                                                                                                                     |
| ---------------- | ------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `viewer`         | 읽기.                                                                                             | 토큰 클레임 또는 네임스페이스 권한 부여.                                                                                                                                                      |
| `writer`         | 읽기, 그리고 메모리, 지식 및 기타 데이터 플레인에 대한 쓰기.                                      | 토큰 클레임 또는 네임스페이스 권한 부여.                                                                                                                                                      |
| `admin`          | 하나의 네임스페이스 관리: 권한 부여, 구성, 정책, 복원, 공유 계층으로의 승격.                      | 토큰 클레임 또는 네임스페이스 권한 부여. 네임스페이스를 등록하면 해당 네임스페이스의 admin이 됩니다.                                                                                          |
| `org_admin`      | 조직 전체 보기(예: 네임스페이스 없이 비용 조회).                                                  | 인정되는 게이트이지만 제품 내에서 할당하는 경로는 없습니다.                                                                                                                                   |
| `platform_admin` | 조직 간 운영자 보기.                                                                              | `LOOM_PLATFORM_ADMINS`에 있는 핸들과 확인된 이메일에는 로그인 시 부여되고, `--platform-admin`으로 등록된 봇의 토큰에도 부여됩니다. 서비스 계정에만, 만료 기한과 함께 발급됩니다.              |

권한 부여에는 `viewer`, `writer`, `admin`만 사용할 수 있으며, Loom이 발급하는 토큰도 마찬가지입니다. 단, `platform_admin` 토큰은 예외입니다. 조직 전체에 대한 권한은 구성 또는 봇 등록 시 운영자가 결정하는 사항이며, 토큰에 담아 가져갈 수 있는 것이 아닙니다. 코드 네임스페이스에는 읽기 권한자(`viewer`)와 유지관리자(`admin`)가 있으며 `writer`는 없습니다.

<a id="who-may-call-what"></a>

## 누가 무엇을 호출할 수 있는가

`(org, namespace)`에 대한 호출은 다음 두 조건을 모두 만족할 때 허용됩니다. 첫째, (네임스페이스에 제한이 없거나, 제한된 네임스페이스에 대한 검사를 통과한다). 둘째, (토큰 자체가 그 네임스페이스에 대해 가진 role이 연산의 최소 role 이상이거나, 유효한 grant의 role이 연산의 최소 role 이상이다). 네임스페이스 제한 검사는 이 두 role 조건의 바깥에 있는 별도의 관문입니다.

- **토큰**(`sub`, `org`, `namespaces`, `roles`, 선택 항목인 `ns_roles`와 `ns_write`)은 호출자가 어떤 네임스페이스를 어떤 우선순위로, 어떤 role로 읽는지 나타냅니다. **범위 지정(scoped)** 토큰(`ns_roles`, 네임스페이스별 role)은 토큰에 나열된 네임스페이스에 대해서만 권한을 가집니다. **일반(flat)** 토큰은 토큰에 명시된 모든 네임스페이스에 `roles`를 적용합니다. `ns_write`는 쓰기를 일부 네임스페이스로 좁히므로, 하나의 토큰으로 팀 네임스페이스는 읽고 자신의 네임스페이스에만 쓸 수 있습니다.

  ```
  namespaces = ["team-shared", "agent-private"]
  ns_write   = ["agent-private"]
  ```

- **Grant**(`loom.namespace_grants`, `(org, namespace, subject)`당 한 행)는 토큰이 가진 role 위에 한 네임스페이스에 대한 role을 추가합니다.
  - **클레임은 grant를 넘어서지 못합니다.** 토큰이 명시한 네임스페이스에 대해 주체가 상태와 관계없이 grant 행을 가지고 있다면, 클레임은 그 grant 이상으로 확장되지 않습니다. grant가 철회되거나 낮아지면(`grants.revoke`, `replace=true`인 `grants.create`, `namespace.leave`, 해제된 클레임, 복원) 그보다 오래된 모든 토큰의 해당 네임스페이스 클레임이 끝나고, 대기 중이거나 거절된 요청은 접근 불가로 제한합니다. 범위 지정 토큰은 자체 role을 유지하되 유효한 grant를 상한으로 합니다.
  - **영향을 받지 않는 경우.** 주체가 grant를 가진 적이 없는 네임스페이스는 클레임이 유지됩니다. 이를 없애려면 토큰을 철회하세요.
  - **적용 시점.** grant를 변경한 프로세스는 즉시 적용하고, 다른 모든 프로세스는 이 서버든 다른 서버든 `LOOM_ACL_WATERMARK_POLL_SECONDS`(기본값 1초) 이내에 적용합니다.
- **서비스 계정은 상속합니다.** 어떤 네임스페이스에 자체 grant가 없는 서비스 계정은 그 네임스페이스에서 생성자의 grant를 사용합니다. 따라서 대시보드에서 발급한 토큰은 제한된 네임스페이스를 제외하고 만든 사람을 따릅니다.
- **조직 전체 id로 지정하는 연산**(예: `memory.get_by_id`)은 해당 행을 가진 범위에서 호출자를 인가합니다. 호출자가 행의 네임스페이스에서 연산에 필요한 role이 없으면 행이 없는 경우와 동일하게 응답하므로, 거부 응답으로 id의 존재가 확인되지 않습니다([행 범위 연산](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#row-scoped-operations)).

토큰과 grant를 함께 쓰는 이유: 토큰은 조회 없이 매 호출마다 로컬에서 검증되므로 핫 패스 비용이 낮고, grant를 사용하면 관리자가 토큰을 재발급하지 않고도 접근 권한을 부여하고 회수할 수 있습니다. 철회 정보는 ClickHouse를 통해 공유되며 철회 캐시 간격 이내에 모든 레플리카에 반영됩니다.

<a id="reading-several-namespaces"></a>

## 여러 네임스페이스 읽기

검색은 여러 네임스페이스를 한 번에 읽을 수 있습니다. `namespace_scope`는 `this`(지정한 네임스페이스, 기본값) 또는 `all_mine`(지정한 네임스페이스에 호출자 본인의 네임스페이스를 더한 것)입니다.
`loom_search` MCP 도구와 `context.pack`은 같은 팬아웃을 사용합니다. 메모리 검색은 대상 범위에 든 네임스페이스를 `namespaces_searched`에 나열하고(티어 검색은 마지막 네임스페이스 전에 멈출 수 있습니다), 상한 때문에 빠진 네임스페이스를 `namespaces_skipped`에 나열합니다.

호출자 본인의 네임스페이스는 토큰에 따라 달라집니다.

| 토큰                                                        | `all_mine`이 추가하는 네임스페이스                                                             |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Flat (대부분의 토큰; `loom mint-token`, 셀프 가입)          | 토큰에 명시된 네임스페이스와, 주체가 grant를 가진 모든 네임스페이스                            |
| Scoped (`ns_roles`; _Only what you choose_ 용 토큰)         | 토큰에 나열된 네임스페이스만, 나열된 순서대로. 나중에 추가된 grant는 포함되지 않습니다          |
| `*`로 위임됨 (_Everything you belong to_ 용 토큰)           | 생성자의 현재 grant. 그 사람이 나중에 가입한 네임스페이스도 검색됩니다                         |
| 네임스페이스를 지정해 위임됨                                | 지정된 네임스페이스 중 생성자가 여전히 grant를 가진 것                                         |
| 없음, 인증 꺼짐 (`single-user`)                             | 단일 사용자가 grant를 가진 네임스페이스: 기본 네임스페이스와 사용자가 등록한 모든 네임스페이스 |

그런 다음 목록을 좁힙니다.

- **종류.** `LOOM_MEMORY_SEARCH_FANOUT_KINDS`(기본값 `memory`)에 있는 종류의 네임스페이스만 검색합니다. 코드 및 벤치마크 네임스페이스는 이름으로 지정하거나 전용 도구로 읽습니다. 예외는 코드 범위에서의 MCP `loom_search`입니다. 호출자가 읽을 수 있다면 범위에 지정된 리포지토리의 트렁크 코드 네임스페이스도 문서와 결정 사항을 위해 읽습니다([통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#which-namespaces-it-reads)).
- **제한된 네임스페이스**는 호출자가 제한 확인을 통과할 때만 남습니다.
- **상한.** 지정한 네임스페이스 외에 최대 `LOOM_MEMORY_SEARCH_FANOUT_MAX_NAMESPACES`(기본값 32)개를 호출자의 목록 순서대로 읽습니다. 순서는 scoped 토큰의 순서, 위임 토큰의 지정 목록 순서이며, 그 외에는 이름순입니다. scoped 토큰은 앞쪽 네임스페이스를 유지하고, 그 외의 경우에는 공개 네임스페이스가 비공개 네임스페이스보다 먼저 제외됩니다. 메모리, 지식, 대화 검색은 나머지를 `namespaces_skipped`에 나열하고(페이지를 나눈 메모리 검색은 모든 페이지에서도), `loom_search`는 텍스트로 알려 줍니다. 읽으려면 이름으로 직접 지정하세요.

지정한 네임스페이스는 항상, 그리고 가장 먼저 검색됩니다. 팬아웃은 검색 범위를 넓힐 뿐입니다.
이후 메모리 검색은 티어별로 읽습니다. scoped 토큰은 나머지 네임스페이스를 토큰의 순서대로 따르며, 확실한 일치를 찾은 첫 네임스페이스에서 검색을 멈춥니다. 그 외의 경우 지정한 네임스페이스와 비공개 네임스페이스를 먼저 읽고, 첫 단계의 결과가 부족할 때만 공개 네임스페이스를 읽습니다.

호출자가 읽을 수 있어도 본인 소유가 아니어서 검색 대상이 되지 않는 네임스페이스가 있습니다. `*` 클레임이나 조직 또는 플랫폼 역할로 접근하는 네임스페이스, 가입하지 않은 공개 네임스페이스, 그리고 호출자가 쓰기를 통해 생성한 네임스페이스입니다(소유자 행은 생기지만 grant는 없으므로, 작업마다 네임스페이스를 만드는 하네스가 이후의 모든 검색을 넓히지 않습니다). 이런 네임스페이스는 이름으로 지정하세요. 검색은 토큰 본인의 조직 하나만 대상으로 합니다.

<a id="read-scope-inside-a-namespace"></a>

## 네임스페이스 내부의 읽기 범위

멤버십은 읽기 경계입니다. 이는 팀이 공유하는 메모리에는 적합하지만, 하나의 토큰으로 여러 최종 사용자를 상대하는 에이전트 하나에는 맞지 않습니다. 한 사용자의 프롬프트가 다른 사용자의 메모리를 끌어와 답변에 넣을 수 있기 때문입니다. `read_scope`는 네임스페이스 내부에서 읽기의 범위를 제한합니다.

| `read_scope`          | 읽기가 반환하는 것                                    |
| --------------------- | ----------------------------------------------------- |
| `namespace` (기본값)  | 네임스페이스의 모든 행                                |
| `own_user`            | 요청에 명시된 `user_id`로 기록된 행                   |
| `own_session`         | 요청에 명시된 `session_id`로 기록된 행                |

`memory.search`, `memory.search_trace`, `memory.ask`, `memory.recall`, `context.pack`,
`namespace.inventory`, `profile.render`, `profile.structured`가 이 값을 받습니다. `profile.structured`는
`own_session`을 거부합니다. 사용자의 프로필은 여러 세션에 걸치기 때문입니다. 토큰은
`read_scope` 클레임(그리고 고정된 `read_user_id`)을 가질 수 있으며, 이는 상한입니다. 요청은 범위를 좁힐 수는 있지만 넓힐 수는 없습니다. 범위가 지정된 읽기에서 바인딩 대상인 `user_id` 또는 `session_id`가 없으면 조용히 범위를 넓히는 대신 `IDENTITY_INCOMPLETE`로 거부됩니다. 여러 사용자를 다루는 에이전트에는 `own_user` 토큰을 발급하세요.

두 전송 방식 모두 호출을 받아들일 때 범위를 한 번 확정하며, 그 호출에서 메모리 행을 읽는 모든 동작은 어떤 op가 읽든 이 범위를 따릅니다. 해당 op는 `memory.get`,
`get_by_id`(다른 사용자의 id에는 `found=false`로 응답), `list`, `bulk_get`, `history`,
`timeline`, `related`, `evidence`, `conflicts.list`, `memory.proposals`, 세션 브리프,
다이제스트, 프로시저, 작업 메모리, `loom_search` 레그입니다. `namespace`보다 좁은 범위에서는 다음과 같이 동작합니다.

- `user_id="*"`는 `AUTH_INSUFFICIENT`로 거부되며, `own_session`에서는 `session_id="*"`도 마찬가지입니다.
- `read_user_id`로 고정된 토큰은 쓰기에서도 오직 그 사용자로만 동작합니다. 다른 `user_id`를 명시한 본문은 모든 op에서 `AUTH_INSUFFICIENT`로 거부되고, 아무것도 명시하지 않은(또는 `-`인) 본문은 고정된 사용자로 실행됩니다.
- 쓰기의 응답에는 범위 밖의 행이 드러나지 않습니다. 다른 사용자의 행으로의 병합, 그 행의 중복, 그 행과의 고정된 충돌은 해당 행의 id, 해시, 키 없이 판정 결과만 알려 주며, `conflicts.list`는 이런 상대를 `conflict_with`에서 제외합니다. 고정된 행은 어느 사용자의 가까운 쓰기든 여전히 막습니다(네임스페이스 전체에 적용되는 불변 조건). 충돌 거리 안에 있는 다른 사용자의 고정된 행은, 작성자 본인의 행이 더 가깝게 얼마나 많이 있든 이름을 밝히지 않고 쓰기를 거부합니다. 범위가 좁혀진 작성자의 검사는 자신의 범위 읽기와 함께 네임스페이스의 고정된 행(유형은 메모리 행에서 가져옴)도 읽으므로, 비용은 네임스페이스 전체 범위 작성자의 최대 약 두 배입니다. 지연된
  `memory.set_from_messages`(`defer=true` 또는 `extraction_defer_agents`)는 요청의 범위를 함께 저장하고 그 범위로 재실행됩니다. 다른 사용자의 `run_id`로 `run.begin`을 호출하면 새 id로 새 실행이 시작되고, 그 id에 대한 `run.complete`는 `NOT_FOUND`로 응답합니다. 호출자가 id를 직접 지정한 begin은 ClickHouse Keeper(`loom.agent_run_claims`)에서 그 id를 선점하고 실행 행이 기록되면 선점을 해제하므로, 같은 id에 대한 두 사용자의 동시 begin 중 정확히 하나만 id를 유지합니다. 행을 기록하기 전에 실패한 begin은 선점을 유지하므로 다른 사용자의 begin이 그 id를 가져갈 수 없습니다. Keeper가 실패하는 동안 begin은 실행 행만 확인합니다. 이때 범위가 좁혀진 토큰은 지정한 id를 자신의 기존 실행을 다시 여는 경우에만 유지하고, 새 실행에는 서버가 만든 id가 부여되므로 id를 직접 만드는 클라이언트는 반환된 `run_id`를 써야 합니다.
- 세션 브리프는 사용자별로(`own_session`에서는 세션별로) 보관됩니다. 두 사용자가 같은 `brief_key`로 저장하면 브리프가 두 개 생기고 각자 자신의 것만 보며, 네임스페이스 전체 범위 토큰은 `~<user>~`(또는 `~<user>~<session>~`)를 접두사로 한 키 아래에 이를 나열합니다.
- `code.search`는 본문이 어떤 `user_id`를 명시하든 범위 자체의 사용자의 최근 파일로 동점을 가립니다.
- `memory.search`의 용어 레그는 어떤 읽기 범위에서든 관리되는 정의를 제공합니다. 관리자가 네임스페이스의 모든 독자를 위해 게시한 것이기 때문입니다([검색의 관리되는 정의](#governed-definitions-in-search)).
- 범위가 제한할 수 없는 `context.pack` 레그(사용자 정보가 없는 대화, 그리고 사용자의 모든 세션을 읽는 실행 매니페스트)는 아무것도 반환하지 않습니다.
- `namespace.inventory`는 메모리와 실행, 그리고 메모리 행에 있는 에이전트, 작성자, 주체(subject, 즉 신원 주체 또는 principal)를 범위 안에서 집계합니다. 문서와 멤버는 네임스페이스 전체를 집계하며(`knowledge.*`와 `grants.list`가 읽는 방식과 동일), 레코드(키마다 하나이며 마지막으로 쓴 사람과 무관)와 통합(`webhook.list`는 거부됨)은 집계하지 않는 것으로 표시합니다(`complete: false`).
- 블랙리스트 패턴은 모든 사용자의 쓰기를 거부하지만 사용자를 기록하지 않습니다(`created_by`는 토큰 subject이며, 한 백엔드의 모든 사용자별 토큰이 이를 공유합니다). 따라서 모든 `blacklist.*` op는 `AUTH_INSUFFICIENT`로 거부됩니다. 패턴은 네임스페이스 전체 범위 토큰으로 추가, 조회, 제거, 스윕합니다. 패턴은 범위가 좁혀진 토큰의 쓰기도 여전히 거부하며, 그 422 응답에는 패턴 id나 이유가 나오지 않습니다.
- `policy.decisions`는 호출자 본인의 결정만 나열합니다. `memory.unlock`과 `memory.unpin`(관리자)은 `memory.lock`처럼 호출자 본인의 행에만 적용됩니다.
- 답변을 한 사용자로 제한할 수 없는 op(엔티티 그래프 `facts.*`, `conversation.*`, 네임스페이스 내보내기, 관리 및 큐레이션 op, `ops.projection_failures.*`, `erasure.*`, `audit.list`, `cost.agent_summary`, `agents.connected`)는 `AUTH_INSUFFICIENT`로 거부됩니다. 사용자 없이 키가 정해진 테이블에 쓰면 다른 사용자의 행을 덮어쓰게 되므로 그런 쓰기도 마찬가지로 거부됩니다. 해당 op는 `memory.record.write`, `decision.record`, `procedure.record_run`, `context.offload`입니다. `memory.set_from_messages`는 레코드로의 `record_schema` 프로젝션을 건너뛰고 `degraded`에 이를 알립니다. 범위가 확정되지 않는 토큰(`own_session` 상한인데 본문에 세션이 없는 경우, 또는 고정되지 않은 `own_user` 토큰인데 본문에 사용자가 없는 경우)은 사용자 `-`로 실행하는 대신, 그 범위가 적용되는 모든 읽기와 본문에 사용자나 세션이 있는 모든 쓰기(및 `cost.summary`)에서 거부됩니다. 따라서 `own_session`에서는 세션 필드가 없는 op(`cost.summary`, `memory.outcome`)가 항상 거부됩니다. 코드 인덱스 op(`code.*`)는 사용자를 귀속 용도로만 담고 있어 허용되지만, 그 안의 메모리 읽기는 거부됩니다. 네임스페이스 전체 규칙(`policy.set`, `policy.delete`, `memory_schema.register` / `propose` / `approve`, `blacklist.*`)에는 네임스페이스 범위 토큰이 필요하며, 모든 사용자의 호출에 대해 호출자의 규칙을 평가하는 `policy.check`도 마찬가지입니다. 목록은 `server/identity/domain/read_scope.py`(`SCOPED_OPS`, `NARROW_SAFE_OPS`)에 있으며, 여기에 없는 op는 거부됩니다.

<a id="agent-written-rows-and-the-shared-layer"></a>

## 에이전트가 쓴 행과 공유 레이어

네임스페이스 안에서 `loom.memory`의 행은 전체 튜플을 키로 사용합니다. 같은
`memory_key`를 두 에이전트가, 또는 두 사용자를 위해 쓰면 행이 두 개가 됩니다. 이것이 에이전트별
("비공개") 레이어이며, `memory.search`가 기본으로 읽는 대상입니다(`scope="private"`).
여기서 "비공개"는 _승격되지 않음_을 뜻하지, _숨김_을 뜻하지 않습니다. `read_scope`가 범위를 좁히지 않는 한
네임스페이스의 모든 독자가 이 행을 읽을 수 있습니다.

**공유 레이어**인 `loom.shared`는 `(org, namespace, memory_key)`마다 행을 하나씩 보관합니다. 이는 사실을 알게 된 에이전트에게 더 이상 속하지 않는, 네임스페이스가 소유하는 정본 버전입니다. `memory.promote`는 근거(rationale)와 선택적인 승인자와 함께 행을 이 레이어로 복사하고, `memory.demote`는 그 행을 폐기합니다(응답에는 승격의 `reversible_for_days` 기간 안에서 일어났는지가 나옵니다. 기본값은 7이고 최대 90입니다). `scope="shared"`로 검색하면 이 레이어를 읽으며, `memory.lineage`는 공유 행을 그 승격까지 거슬러 추적합니다. `memory.promote`와 `memory.demote`에는 `admin` 권한이 필요합니다. `memory.cross_promote`는 두 네임스페이스 모두에서 admin인 호출자를 위해, 출처 정보와 함께 행을 다른 네임스페이스로 복사합니다.

`definition`은 다음 조건에서만 승격됩니다.

- 네임스페이스 전체에 적용되는 활성 행(에이전트, 사용자, 세션이 `'-'`이고 `active`이며, 대체(supersession)로 폐기되지 않았고 `expires_at`이나 `valid_to`가 지나지 않은 행)에서 `definition/` 키 아래 같은 키로 승격하는 경우. 다른 타입의 행은 `definition/` 키로 승격되지 않습니다. 그렇지 않으면 422 `DEFINITION_PROMOTION_REFUSED`입니다.
- 승인하는 해시(`approved_sha256`, 즉 행의 `content_sha256`)를 함께 제공하는 경우. 없으면 거부되고(422 `VALIDATION_FAILED`), 텍스트가 더 이상 그 해시와 일치하지 않으면 409 `PRECONDITION_FAILED`입니다.
- 값이 비어 있지 않은 문자열 `term`을 가진 JSON 객체이고, 선택적으로 문자열 `aliases` 목록을 가진 경우. 그렇지 않으면 422 `VALIDATION_FAILED`입니다.
- `medium`으로 라벨이 지정되지 않았고 `medium`이나 `high`로 판독되지 않는 경우(422 `SENSITIVE_DEFINITION_REFUSED`. `high` 라벨은 다른 모든 행과 마찬가지로 먼저 `HIGH_SENSITIVITY_PROMOTION_REFUSED`로 응답합니다).
- `memory_shared_definitions_max` 이내인 경우. 상한에서 새 키를 추가하거나 네임스페이스가 상한을 넘은 상태에서 승격하면 거부됩니다(409 `DEFINITION_CEILING`).
- 다른 어떤 유효한 공유 정의도 용어나 별칭으로 쓰지 않는 이름인 경우(409 `DEFINITION_NAME_TAKEN`, 해당 키와 이름을 알려 줍니다). 이름은 정규화하여 비교합니다. 서식 문자를 제거하고, NFKC를 적용하고, 공백을 하나로 합치고, 대소문자를 접은 뒤, 다시 NFKC를 적용합니다.

한 네임스페이스의 정의 승격(상한 및 이름 검사 포함)과 정의 강등은 한 번에 하나씩 실행되며, 마지막으로 실행된 작업이 해당 정의의 공유 여부를 결정합니다(다른 작업이 10초 넘게 네임스페이스를 점유하고 있으면 503 `OVERLOADED`). 정의를 강등하면 그 이름과 자리를 반환합니다. 토큰의 actor가 `promoted_by`가 됩니다. `approver`와 `promoted_from_candidate_id`는 무시되며 `ignored_args`에 표시되고, 다른 키 아래에 유사한 공유 행이 있다는 이유로 거부되지는 않습니다. `learnings.review`는 `definition` 타입의 후보를 메모리로 승격하지 않습니다.

공유 정의가 **관리됨(governed)** 상태가 되려면 정확히 한 행, 즉 해당 키 아래의 네임스페이스 전체 행에서 승격되어야 하며, 그 행이 `active`이고, 대체로 폐기되지 않았고, `expires_at`이나 `valid_to`가 지나지 않았고, `medium`이나 `high`로 라벨이 지정되거나 등급이 매겨지지 않았고, 공유 복사본이 가진 텍스트와 같은 텍스트를 가져야 합니다(양쪽의 제공 해시로 비교하므로, 객체의 키를 다른 순서로 저장하는 저장소여도 변경으로 보지 않습니다). 편집, `medium`이나 `high`로의 재라벨 또는 원본의 tombstone 처리가 일어나면 관리됨 상태가 끝나며, 편집을 승인된 텍스트로 되돌리면 새 승격 없이 다시 관리됨 상태가 됩니다. `memory.search`는 네임스페이스 전체 원본이 사라졌거나(삭제되었거나 만료 정리로 tombstone 처리됨) `medium`이나 `high`로 라벨이 지정된 공유 정의를 제외합니다. 공유 레이어에는 자체 라벨이 없기 때문입니다. 원본을 읽을 수 없으면 모든 공유 정의를 제외합니다. 네임스페이스 전체 행이 아닌 다른 행에서 승격된 복사본은 해당 키 아래의 네임스페이스 전체 행으로 판단합니다. 그런 행이 없으면 제공하지 않고, 있으면 그 행의 라벨에 따라 제공합니다. 복사본 자체는 남아 있으므로, 라벨을 되돌리거나 원본을 다시 쓰면 다시 제공되며, `memory.demote`로 폐기할 수 있습니다. `memory.definitions.list`(admin)는 네임스페이스의 유효한 공유 정의를 가장 최근에 승격된 것부터 최대 `memory_shared_definitions_max`의 두 배까지 나열하고, 전체 개수를 `count`로 알려 줍니다. 각 항목에는 키와 `promoted_at`이 있으며, 상한 이내의 항목에는 용어와 별칭, `verdict`(`governed` 또는 `lapsed`), `lapse_reason`(`not_namespace_wide`, `no_term`, `gone`, `superseded`, `conflicted`, `duplicate`, 그 외 상태는 `not_active`, `expired`, `valid_to`가 지난 경우 `ended`, `changed`, `sensitive`)이 들어 있습니다. 관리됨 항목에는 `lost`도 있는데, 이는 그 이름을 가진 다른 관리됨 정의에 빼앗긴 각 이름을 `name`과 `winner`의 키로 나타낸 것입니다(가장 최근 승격이 이기며, 동률이면 승격 이벤트로 결정합니다). 상한을 넘은 항목은 `capped`이며 그 원본은 읽지 않습니다.

<a id="governed-definitions-in-search"></a>

### 검색에서의 관리되는 정의

`memory.search`는 쿼리가 관리되는 정의의 용어나 별칭을 언급하면 그 정의를 먼저 제공하며 `governed: true`로 표시합니다. 이 용어 레그(term leg)는 `memory.search`가 실행되는 모든 곳에서 실행됩니다. `memory.ask`와 `context.pack` 내부, 그리고 `loom_search`의 메모리 섹션도 포함합니다.

- **읽는 대상.** `namespace_scope`가 어디까지 확장되든, 지정된 네임스페이스의 유효한 공유 정의 이름만 최대 `memory_shared_definitions_max`개까지 읽습니다(그 이상이면 가장 최근에 승격된 것만 읽고 `term_leg`는 `capped`입니다). 쿼리가 언급한 정의와 그것과 이름을 공유하는 정의에 대해서만 전체 값을 읽고 원본을 읽어 등급을 판단하며, 그 외에는 읽지 않습니다. 순차 일관성은 없으므로, 방금 다른 복제본에서 일어난 승격은 검색에서 빠질 수 있습니다. 이 레그는 다른 레그와 나란히 실행되고, 다른 레그가 끝난 뒤 검색은 최대 `memory_term_leg_wait_ms`만큼 더 기다립니다(재진입 패스는 남은 시간을 넘지 않습니다). 정규화, 이름 비교, 검증을 위한 본문 디코딩에도 고정된 작업 예산이 적용됩니다. 작업이 과도하면 레그 전체를 `timed_out`으로 제외하며, 저장된 정의와 승격 규칙은 바뀌지 않습니다.
- **매칭.** 쿼리와 각 이름은 서식 문자를 잃고 NFKC를 거치며 공백이 하나로 합쳐집니다. 결합 문자는 앞의 글자나 숫자에 붙습니다. 이름은 전체 문자 단위로, 그 가장자리를 이어 가는 이웃이 없을 때 일치합니다. 라틴 문자, 숫자, `_`, `+`, `#` 옆에서는 그 가운데 다른 하나가 이웃이면 안 되고, 공백으로 띄어 쓰는 다른 문자 체계(키릴, 그리스, 데바나가리)의 글자 옆에서는 라틴 밖의 모든 글자나 숫자가 이웃이면 안 되며, 한글 앞에서는 한글 글자가 오면 안 되고 뒤에는 조사가 따르므로 아무것도 검사하지 않으며, 한자, 가나, 태국, 라오, 크메르, 미얀마 문자나 문장부호 옆에서는 아무것도 검사하지 않습니다. 따라서 `MRR`은 "什么是ＭＲＲ"에서 일치하고 `ARR`은 "ARR은"에서 일치하지만, `рост`는 "простой" 안에서는 일치하지 않습니다. 대소문자는 이름의 단어별로 판단합니다. 모두 대문자이거나 첫 글자 뒤에 대문자가 있는 단어는 쓰인 그대로만 일치하고(`IT`는 "what is it"과 일치하지 않음), 그 외에는 대소문자를 접어 일치합니다.
- **배치.** 이름이 언급된 정의는 쿼리에 이름이 처음 나타나는 순서대로 제공 목록의 맨 앞에 오며, 두 이름이 같은 위치에서 시작하면 더 긴 이름이 먼저이고, 그다음은 가장 최근 승격 순입니다. 파생 집계의 계산된 행은 그 위에 그대로 남습니다. 지정된 네임스페이스에서 같은 키 아래의 다른 히트는 제외됩니다. 이들은 `top_k`에 포함되며 마지막에 잘립니다. 잘린 것, 즉 관리되는 히트나 그것에 밀려난 히트는 다음 페이지의 맨 앞에 옵니다. 보류된 관리되는 히트는 제공하기 전에 원래 쿼리와 현재 승격 및 원본에 대해 다시 확인하며, 확인을 읽지 못하면 제외합니다. 전체 본문은 `body_budget_tokens`에 포함되며, 맞지 않는 정의는 잘림 표시가 된 발췌를 유지합니다. 어떤 하한, 임계값, 이후 단계도 이들을 제외하거나 순서를 바꾸지 않습니다. `min_similarity`, 보정, `loom_search`의 하한과 답변 우선 규칙, 트래커 판정의 메모리 상한(그 안에 포함됨), `context.pack`의 패킹, `memory.ask`의 반복된 값 확인도 마찬가지입니다.
- **점수.** `similarity`와 `distance`는 해당 정의의 공유 임베딩 값이며(임베딩이 없으면 `0`과 `1`), `rank_score`는 계산된 행을 제외한 모든 히트보다 높고 `below_threshold`는 설정되지 않습니다. 이를 제공하는 응답은 `abstain`이 false입니다. `retrieval_confidence`와 `retrieval_risk`는 다른 히트를 설명합니다.
- **꺼지는 경우.** 페이지 핸들, 그리고 다른 행을 요청하는 필드(`memory_type`, `tags` 또는 `category`(`tags_soft`나 `category_soft`와 함께인 경우 제외), `as_of`, `min_body_chars`, 0보다 큰 `recent_first` 또는 `anchor_first`, 또는 `memory`가 없는 `legs`)가 있으면 꺼집니다. 유효하지 않은 유니코드 쿼리는 제한된 유니코드 검사가 이를 읽을 때 꺼지며, 그 검사의 작업 한도를 넘는 쿼리는 검증 전에 `timed_out`으로 보고됩니다. 누구의 행을 읽을지 지정하는 필드(`agent`, `user_id`, `session_id`, `session_ids`, `run_id`, `scope`)는 이 레그를 켠 채로 두며, `exclude_keys`는 해당 키를 제외합니다.
- **`term_leg`**는 응답에서 이 레그가 한 일을 나타냅니다. `complete`, `empty`(정의 없음), `capped`, `timed_out`, `failed`(읽기 실패로 아무것도 제공되지 않음), `off` 중 하나입니다.
- **범위.** 모든 읽기 범위에서, 그리고 기본값인 `scope` `private`에서도 관리되는 정의를 제공합니다. admin이 네임스페이스의 모든 독자를 위해 게시했기 때문입니다.
- **미스.** 관리되는 정의를 제공한 검색, 또는 레그가 `complete`나 `empty`(`off` 포함)를 보고하지 않은 검색은 [knowledge
  gap](https://github.com/ClickHouse/loom/blob/main/docs/architecture/knowledge-gaps.md#recording-misses) 미스를 기록하지 않습니다.
- **저장된 답변.** 승격과 강등은 네임스페이스의 메모리 쓰기 마크를 이동시키므로, 마크가 반영된 뒤에는 `memory.ask`가 그 이전의 답변을 재사용하지 않습니다.

레이어가 둘인 이유: 에이전트는 자신의 신원으로 자유롭게, 종종 중복해서 쓰는 반면, 네임스페이스 전체가 신뢰해야 하는 정리된 사실에는 하나의 정본 키, 소유자의 결정, 감사 추적이 필요합니다. 승격이 바로 그 결정입니다.

<a id="identity-modes"></a>

## ID 모드

호출자가 토큰을 어떻게 얻는지는 배포의 ID 모드(`LOOM_IDENTITY_PROVIDER`)로 정해집니다.

- **`single-user`**: 인증이 없습니다. 네임스페이스를 지정하지 않은 호출은 MCP에서 `code.*`를 제외한 모든 op에 대해 하나의 네임스페이스(`LOOM_DEFAULT_NAMESPACE`, 기본값 `local`, 조직은 `LOOM_SELF_SIGNUP_ORG`, 기본값 `dev`)로 해석됩니다. HTTP에서는 토큰이 없는 호출이 `code.*`와 `tracker.*`를 제외한 모든 op에서 해당 조직을 받고, 일상적인 메모리 및 검색 op에서는 해당 네임스페이스를 받습니다. 어떤 op가 해당하는지와 각각에 부여되는 에이전트는 [HTTP 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#request)에 나와 있습니다. `all_mine` 검색은 사용자가 등록한 네임스페이스도 함께 읽습니다. 노트북 전용입니다.
- **`trust-network`**: 이메일과 비밀번호로 직접 가입합니다. 네임스페이스 이름을 가장 먼저 등록한 주체가 그 네임스페이스를 소유합니다. 에이전트는 스스로 가입할 수 있으며(`loom init --agent`), 사람은 에이전트가 출력한 코드로 나중에 이를 인수할 수 있습니다. 이 코드는 한 번만 사용할 수 있고, 인수한 사람의 비밀번호로 대체됩니다.
- **`gateway`**: 상위 ID 계층이 사용자를 인증하고 공유 시크릿으로 수명이 짧은 Loom 토큰을 발급하며, Loom은 검증만 합니다. `LOOM_TRUSTED_CALLERS`에 포함된 subject는 `X-Loom-On-Behalf-Of`로 최종 사용자를 대신해 동작할 수 있고, 모든 ledger 행에는 실제 호출자가 기록됩니다. 사용자에게 Loom 계정이 없는 서비스는 요청마다 `user:<id>`를 대상으로 수명이 짧은 토큰을 서명하며, 이를 위해 최종 사용자 발급자로 등록된 키를 사용합니다([서비스의 최종 사용자를 대신해 동작하기](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#act-for-a-services-end-users)).

<a id="collections"></a>

## 컬렉션

컬렉션은 `col-…`라는 이름의 네임스페이스이며, 접근 권한을 여는 것은 오직 grant뿐입니다. 토큰 클레임이나 org 관리자 역할로는 컬렉션에 접근할 수 없습니다. 컬렉션은 다른 컬렉션 안에 둘 수 있으며 그 grant를 상속합니다. 컬렉션의 grant에는 사람이나 서비스 계정 외에도 `user:`, `group:`, `agent:` 주체나 `everyone`(org에서 동작하는 모든 요청)을 지정할 수 있습니다. 사용자는 컬렉션을 제목으로 지정하며, 서버는 그 제목에서 `col-…` 이름을 만듭니다. 이 이름은 이후에도 컬렉션의 주소로 유지됩니다. [지식 공유](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md)에서 이 모델과 이름이 만들어지는 방식을 설명합니다. 사용자는 라이브러리에서 컬렉션을 제목과 위치로 컬렉션답게 보게 됩니다. 컬렉션을 저장하는 네임스페이스는 grant, 검색 범위, 감사, 삭제의 기준이 되는 단위이며, 대시보드의 네임스페이스 선택기에는 나타나지 않습니다.

`acl-` 뒤에 16진수 24자리가 붙은 이름의 네임스페이스는 미러입니다. 미러는 저장소의 권한을 따르는 문서 소스의 문서를 보관하며, 각 문서는 저장소가 허용한 독자에게만 보입니다([문서 소스](https://github.com/ClickHouse/loom/blob/main/docs/architecture/document-sources.md#the-mirror)). 어떤 토큰도 미러에 접근할 수 없고(플랫폼 관리자의 삭제만 미러를 지정할 수 있습니다), 따라서 `namespace.register`와 `namespace.restore`는 이 형식을 거부합니다.

`LOOM_IDENTITY_PROVIDER`가 비어 있으면 서버가 모드를 추론합니다. 인증이 꺼져 있으면 `single-user`, 인증이 켜져 있고 `LOOM_SELF_SIGNUP=open`이면 `trust-network`, 인증이 켜져 있고 그 외의 경우에는 `gateway`가 됩니다. 가입이 열려 있는 상태에서 루프백이 아닌 주소에 바인드하는 경우에도 `trust-network`를 명시적으로 설정해야 합니다. [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#identity-modes)에서 각 모드의 설정을 확인할 수 있습니다.

<a id="related"></a>

## 관련 문서

- [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md): 발급, 클레임, 권한 부여, 제한된
  네임스페이스, 기록 정책, 폐기를 단계별로 설명합니다.
- [네임스페이스와 공유](../features/namespaces-and-sharing.md): 네임스페이스
  작업과 대시보드의 공유 화면을 설명합니다.
- [보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md): 인증과 인가가 어떻게
  적용되는지 설명합니다.
- [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md): 튜플이 가리키는 행을 설명합니다.
