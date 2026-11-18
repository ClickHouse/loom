<!-- i18n: source=docs/features/namespaces-and-sharing.md blob=7e1ba0ffe7a1ef671a51197f949ea03d8a321737 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md), which is authoritative.

<a id="namespaces-and-sharing"></a>

# 네임스페이스와 공유

네임스페이스는 org 안에서 격리와 공유의 단위입니다. 모든 읽기는 `(org, namespace)` 범위로 제한되고, 모든 권한 부여(grant)는 네임스페이스 하나를 지정하며, 네임스페이스별 설정(충돌 정책, 보존, 검색 기본값, 리랭커)은 `loom.namespaces`의 해당 행에 저장됩니다. 식별 모델은 [아이덴티티와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md)에서 설명합니다.

<a id="registering-a-namespace"></a>

## 네임스페이스 등록

`namespace.register`는 레지스트리 행을 만들고 호출자를 해당 네임스페이스의 admin으로 지정합니다. 어떤 principal이든 호출할 수 있으며(viewer), 이미 있는 네임스페이스를 다시 등록하려면 admin이어야 합니다. 행이 없지만 이미 데이터나 유효한 grant가 있는 이름은 호출자가 이미 그 이름의 admin이 아니면 거부됩니다([새 이름이 가져갈 수 있는 것](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#what-a-new-name-may-claim)). `LOOM_NAMESPACE_AUTOREGISTER_ENABLED`가 켜져 있으면(기본값) 행이 없는 네임스페이스에 처음 쓰기가 일어날 때 토큰 subject를 소유자로 하고 `retention_days = 0`으로 자동 등록합니다(이름이 이미 사용 중이면 소유자는 없습니다). 이 소유자는 다른 누구도 해당 이름에 유효한 grant를 갖고 있지 않은 동안 등록을 완료할 수 있습니다.

다시 등록하는 것은 부분 업데이트입니다. 생략한 필드는 저장된 값을 유지합니다. `tags`는 통째로 교체됩니다. `conflict_threshold`, `capture_generation_io`, `coverage_sweep`은 생략하면 저장된 선택을 유지하고, 명시적인 `null`을 주면 서버 기본값으로 돌아갑니다.

| 필드                                                            | 제어하는 대상                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `kind`                                                          | `memory`, `code`(인덱싱된 저장소와 브랜치 하나, 읽기 전용 참조 데이터) 또는 `bench`(유휴 상태가 되면 GC가 지우는 임시 하네스 네임스페이스). 새 행에서 생략하면 이름으로 결정됩니다. `LOOM_NAMESPACE_BENCH_PREFIXES`의 접두사로 시작하면 `bench`, 그 외에는 첫 쓰기 때와 같이 `memory`입니다.                                                                                                                                                                     |
| `visibility`, `join_policy`, `requires_approval`                | 누가 찾고 참여할 수 있는지: `private`(멤버만) 또는 `public`(`namespace.browse`에 표시). `join_policy`는 `open`, `approval`, `closed` 또는 `inherit`입니다.                                                                                                                                                                                                                                                                                                  |
| `default_conflict_policy`                                       | `auto_reinforce`(기본값), `manual_review`, `newest_wins` 또는 `append_only` — 쓰기를 같은 네임스페이스의 기존 항목과 비교해 판정하는 방식입니다. [충돌과 거부](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)를 참고하세요.                                                                                                                                                                                                                                                                         |
| `conflict_threshold`                                            | 가까운 쓰기를 충돌로 판단하는 코사인 값입니다. 명시적인 `null`은 `LOOM_CONFLICT_THRESHOLD`를 상속합니다.                                                                                                                                                                                                                                                                                                                                                      |
| `proposal_policy`                                               | 검증 중인 fact를 결정하는 방식입니다. 소유자가 설정한 `auto`만 Loom이 채점된 실행에서 이를 결정하도록 허용합니다. [메모리 쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md)를 참고하세요.                                                                                                                                                                                                                                                                                                                      |
| `retrieval_defaults`                                            | 고정되지 않은 `memory.search`가 실행될 때 쓰는 값(`search_mode`, `top_k`, `min_similarity`, `floor_mode`, `recency_pool_weight`, …)입니다. `{}`는 이를 비웁니다. [검색과 질의](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)를 참고하세요.                                                                                                                                                                                                                                                               |
| `rerank_endpoint`, `classify_endpoint`                          | 선택적 리랭커(내장 항목 또는 `LOOM_RERANK_ENDPOINTS_JSON`에 있는 이름이며 URL은 불가)와 분류기 URL(공개 주소의 https, 또는 `LOOM_MEMORY_CLASSIFY_PRIVATE_HOSTS`에 지정된 호스트. 게이트웨이가 필수인 동안에는 `LOOM_INFERENCE_GATEWAY_HOSTS`의 호스트. 게이트웨이가 필수가 아닐 때도 나열된 게이트웨이 호스트라면 기본 포트의 https)와 각각의 프롬프트 템플릿입니다. 둘 다 fail open으로 동작하며, 게이트웨이가 호출의 ID를 거부한 경우에는 쓰기가 실패합니다. |
| `retention_days`, `warm_days`, `tag_registry`                   | 네임스페이스 보존 및 티어링, 그리고 타입별 보존·중복 제거 임계값입니다. `0`은 영구 보관을 뜻합니다. [보존과 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)를 참고하세요.                                                                                                                                                                                                                                                                                                 |
| `conversation_logging_enabled`, `capture_generation_io`         | 대화 기록(transcript) 저장에 대한 동의, 그리고 프롬프트와 완성 텍스트를 Langfuse로 보내는 것에 대한 동의입니다. 둘 다 옵트인입니다.                                                                                                                                                                                                                                                                                                                                                        |
| `consolidation_enabled`, `daily_cost_cap_usd`, `coverage_sweep` | 백그라운드 통합, 네임스페이스의 업스트림 지출 한도, 추출 커버리지 스윕입니다.                                                                                                                                                                                                                                                                                                                                                                                             |

```python
from loom import Agent

async with Agent(org="acme", namespace="eng-tooling", agent="setup") as agent:
    await agent.namespaces.register(
        owner_email="platform@acme.example",
        description="Shared memory for the build tooling agents",
        visibility="public",
        default_conflict_policy="newest_wins",
        retrieval_defaults={"search_mode": "hybrid", "top_k": 8},
    )
    # Fields the SDK method does not take go through Agent.call:
    await agent.call(
        "namespace.register",
        owner_email="platform@acme.example",
        join_policy="approval",
        daily_cost_cap_usd=5.0,
    )
```

`namespace.delete`(admin)는 행을 비활성으로 표시하고 쓰기를 더 이상 받지 않지만, 데이터는 건드리지 않습니다. 데이터를 제거하려면 먼저 삭제를 실행하세요
(`erasure.request(scope_type="namespace")`, [보존과 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md) 참고). `active=true`로 `namespace.register`를 호출하면 다시 활성화됩니다.

<a id="membership-and-grants"></a>

## 멤버십과 권한 부여

grant는 주체(사람의 로그인 핸들, SSO 또는 연결된 계정의 검증된 이메일, 또는 서비스 계정 `sa:<name>`)에게 하나의 네임스페이스에 대한 역할을 부여합니다. `viewer`는 읽기, `writer`는 쓰기, `admin`은 grant와 설정을 관리합니다. 토큰에는 조직과 접근할 수 있는 네임스페이스가 담기며, 역할은 grant 테이블이 결정합니다.

| 작업                                                                 | 역할        | 설명                                                                                                                                                   |
| -------------------------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `grants.create`                                                      | admin       | 역할을 부여합니다. 주체의 현재 역할보다 낮은 역할로 생성하면 `replace=true`가 아닌 한 409 `GRANT_WOULD_DOWNGRADE`로 거부됩니다.                         |
| `grants.list`                                                        | viewer      | 누가 어떤 역할로 접근할 수 있는지, 그리고 `pending_role`을 보여줍니다.                                                                                 |
| `grants.revoke`                                                      | admin       | grant를 제거합니다.                                                                                                                                    |
| `grants.approve`                                                     | admin       | 대기 중인 요청을 승인하거나 거절하며, 선택적으로 다른 역할을 지정할 수 있습니다.                                                                       |
| `grants.pending_for_me`                                              | viewer      | 호출자가 관리하는 네임스페이스에서 대기 중인 모든 요청을 조직 전체 범위로 보여줍니다.                                                                  |
| `namespace.browse`                                                   | viewer      | 조직의 공개 네임스페이스를 보여주며 컬렉션은 포함하지 않습니다(`closed` 네임스페이스는 `include_closed`일 때만 포함).                                  |
| `namespace.join`                                                     | viewer      | 공개 네임스페이스에 직접 가입합니다. `viewer`는 즉시 부여되고, `writer` 또는 `admin` 요청은 항상 대기 상태가 되며 admin이 승인할 때까지 접근할 수 없습니다. |
| `namespace.leave`, `namespace.cancel_request`, `namespace.mark_seen` | viewer      | 자신의 grant를 포기하고, 대기 중인 요청을 철회하고, 초대를 확인 처리합니다.                                                                            |
| `namespace.list_mine`                                                | viewer      | 호출자가 속한 네임스페이스를 각각의 `kind`(컬렉션은 `collection`)와 `title`과 함께 보여줍니다.                                                         |
| `namespace.list`                                                     | 조직 admin  | 조직의 모든 네임스페이스를 보여줍니다.                                                                                                                 |

토큰 발급, 서비스 계정, 읽기 범위, 제한된 네임스페이스는 [토큰 및 권한 부여 가이드](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)에서 다룹니다.

<a id="sharing-facts-across-agents-and-namespaces"></a>

## 에이전트와 네임스페이스 간 사실 공유

네임스페이스 안에서는 기본적으로 모든 작성자의 행을 모든 독자가 볼 수 있습니다
(`agent="*"`). 경계를 넘을 때는 항상 공유 행이 아니라 계보(lineage)를 가진 복사본이 만들어집니다.

| 작업                   | 역할                 | 동작                                                                                                                                                                      |
| ---------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.promote`       | 관리자               | 비공개 메모리를 네임스페이스의 공유 계층(`loom.shared`)으로 복사하며, `memory.search(scope="shared")`로 읽을 수 있습니다. `reversible_for_days` 동안 되돌릴 수 있습니다(기본값 7). |
| `memory.demote`        | 관리자               | 공유 복사본과 해당 임베딩에 삭제 표시(tombstone)를 합니다. `was_reversible`은 되돌릴 수 있는 기간 안이었는지를 알려 줍니다.                                                                      |
| `memory.copy_to`       | 대상의 작성자        | 읽을 수 있는 메모리를 키로 지정해, 쓰기 권한이 있는 네임스페이스로 직접 복사합니다. 원본은 변경되지 않으며, 대상은 자체 임베더로 다시 임베딩됩니다.                        |
| `memory.cross_promote` | 양쪽 모두의 관리자   | 메모리 id로 지정하는 운영자용 형태이며, 대상에 `cross_promoted_from_*` 계보가 기록됩니다.                                                                            |

`memory.copy_to`는 원본 행을 키로 읽고 호출자를 복사의 행위자로 기록하므로, 로그인한 호출자가 필요합니다.
즉 `LOOM_AUTH_REQUIRED=true`이고, 원본을 읽고 대상에 쓸 수 있는 토큰이 있어야 합니다. 기본 단일 사용자
스택에서는 이 작업이 거부됩니다. Python SDK에서는 `agent.client`를 통해 사용할 수 있습니다.

```python
from loom import Agent
from loom.errors import NotFound

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    try:
        result = await agent.client.memory.copy_to(
            from_org="acme",
            from_namespace="support",
            memory_key="customer/4812/plan",
            to_namespace="billing",
            rationale="Billing checks invoices against this plan.",
        )
    except NotFound:
        ...  # no live row under that key in the source namespace
    else:
        print(result["copied"], result["target_key"])  # True customer/4812/plan
```

`target_key`로 다른 키를 지정하지 않으면 원본 키가 그대로 유지되고, 에이전트를 지정하지 않으면 대상에서
네임스페이스 소유(`target_agent="-"`)가 됩니다. `from_agent="*"`(기본값)는 해당 키에 가장 최근에 기록된 행을
복사합니다. 근거(rationale)는 텍스트로 저장되지 않으며, 감사 행에는 그 SHA-256과 길이만 남습니다.

네임스페이스 간 복사는 원자적이지 않습니다. 중간에 실패한 복사는 원본을 그대로 두고, 대상에는 해당 행이
없는 상태로 남깁니다. 승격할 가치가 있는 행을 제안하는 학습 큐(`learnings.*`)는
[프로시저와 학습](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/procedures-and-learnings.md)에서 다룹니다.

<a id="reading-a-namespaces-health"></a>

## 네임스페이스 상태 읽기

| 작업 | 답하는 내용 |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`namespace.inventory`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/namespace.md#namespaceinventory) | 네임스페이스에 무엇이 들어 있고 누가 등장하는지 보여줍니다. 메모리, 기록, 문서, 실행, 통합의 실시간 개수와 가장 최근 쓰기, 멤버 수, 메모리 행에서 관찰된 에이전트, 작성자, 대상, 응답한 프로세스가 실제 모델과 임베더를 사용하는지 여부를 포함합니다. |
| `namespace.summary` | 메모리 유형별 개수, 상위 엔티티, 최근 쓰기와 recall, 고유 에이전트, 마지막 활동입니다. |
| `namespace.validate` | 무결성 보고서입니다. 해결되지 않은 충돌, 낮은 신뢰도의 행, 임베딩을 기다리는 행을 보여줍니다. 쓰기를 한 번에 많이 수행한 뒤나 recall 결과가 이상할 때 실행하세요. |
| `namespace.audit` | 네임스페이스가 메모리로 쓰이는지 객체 저장소로 쓰이는지 확인합니다. 검색 대비 정확한 키 읽기, 해시 형태의 키, 이중 인코딩되었거나 크기가 큰 값, 메모리로 저장된 lease, 버전 변동을 살핍니다. 배포 자체의 상태(kind `system_state`: 누락된 텔레메트리, 인덱싱 적체)도 알려주며, 기간 내 쓰기의 기반이 된 호출을 원장이 하나도 기록하지 않았다면 원장 검사를 정상으로 보고하지 않고 건너뜁니다. 각 발견 사항에는 해결 방법이 포함됩니다. |
| `memory.inspect` | 한 명의 사용자, 에이전트 또는 세션을 봅니다. 네임스페이스의 나머지와 분리된 해당 행, 기간 내 호출, 그리고 그 메모리를 만드는 가이드, 카테고리, 추출 정책을 보여줍니다([아래](#inspecting-one-user-agent-or-session)). |
| `namespace.tune` | 네임스페이스의 메모리를 만드는 추출 규칙, 카테고리, 가이드와 문서의 청킹 및 추출기 설정에 대한 변경 초안을 제시하며, 각 초안에는 근거와 이를 적용할 쓰기가 함께 제공됩니다. 실제로 적용되는 것은 없습니다([아래](#drafting-changes-to-how-memories-are-made)). |
| `namespace.taxonomy` | 현재 사용 중인 어휘입니다. 상위 태그, 엔티티, 메모리 유형, 커뮤니티를 보여줍니다. |
| `agents.connected` | 기간(기본값 24시간) 동안 활동한 에이전트입니다. 에이전트별 마지막 및 처음 확인 시각, 작업 수, 접근한 네임스페이스, 비용을 보여줍니다. "내 클라이언트가 실제로 Loom과 통신하고 있는가?"에 답합니다. |

`namespace.inventory`는 각 집단을 따로 구분합니다. `members`는 권한 보유자의 수입니다. 대기 중인 요청, 그룹, 에이전트, 서비스 계정이 아니라 활성 권한을 가진 사람을 셉니다(네임스페이스와 컬렉션의 각 상위 항목에 대한 `grants.list`가 이들의 권한을 보여줍니다). 자체 클레임에 네임스페이스가 명시된 토큰은 권한 없이도 읽을 수 있으며 개수에 포함되지 않습니다. 부모의 권한은 모든 자식에 미치므로 컬렉션은 상위 항목의 권한 보유자도 함께 셉니다. 이 계보 어디에든 `everyone`, 그룹, 에이전트(그 아래에 로그인한 모든 최종 사용자)에 대한 권한이 있거나 조직 전체가 읽을 수 있는 코드 네임스페이스가 있으면, 그 멤버는 나열되지 않으므로 개수는 `complete: false`가 됩니다. `agents`, `authors`(행을 쓴 `actor`), `subjects`(행이 속한 `user_id`이며 설정되지 않은 `-`는 제외)는 실시간 메모리 행에서 관찰된 ID이며 디렉터리가 아닙니다. 개인 컬렉션의 소유자는 권한을 갖지 않으므로 멤버로 세지 않습니다(`total`은 null). 실패했거나 호출자가 읽을 수 없는 섹션은 `complete: false`와 사유로 표시됩니다. 통합 개수는 네임스페이스 관리자용이며, 한 사용자에 묶인 읽기 범위(`read_scope=own_user` 또는 `own_session`, 혹은 사용자에 고정된 토큰)에서는 메모리 및 실행 개수와 관찰된 에이전트, 작성자, 대상이 해당 사용자의 것입니다. 문서와 멤버는 `knowledge.*`와 `grants.list`가 읽는 것처럼 네임스페이스 전체 기준으로 세고, 기록(키당 하나, 마지막으로 쓴 사람과 무관)과 통합은 세지 않습니다.

모든 `namespace.audit` 및 `namespace.validate` 발견 사항은 동일한 근거 필드를 가지므로 CLI, 대시보드, MCP 클라이언트가 같은 형태를 읽습니다.

| 필드 | 의미 |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kind` | 저장된 값이 선언된 불변 조건이나 증명을 위반하면 `contract_violation`입니다. 어떤 패턴이 보통 recall이나 저장 공간에 손해를 주지만 정당한 설계도 해당될 수 있으면 `usage_heuristic`입니다. 휴리스틱에 걸렸다고 해서 오용이라는 증거는 아니며 경고일 뿐입니다. 발견 사항이 작성자가 아닌 배포 자체에 관한 것이면 `system_state`이며, 인덱싱 적체(`index_backlog`)나 원장에 기록되지 않은 쓰기(`telemetry_unavailable`, 원장 검사를 정상으로 보고하지 않고 건너뜀)가 여기에 해당합니다. 이들은 `namespace.audit`의 발견 사항이며, `namespace.validate`의 `unembedded` 검사는 `contract_violation`이고 유예 기간이 지난 벡터 없는 모든 행을 보류된 모델 호출까지 포함해 셉니다. |
| `rule_version`, `source_op` | 규칙의 버전(조건식이나 내장 임계값이 바뀌면 올라가므로 두 보고서는 같은 버전일 때만 비교합니다. `confidence_floor`나 값 크기 권고처럼 규칙이 읽는 설정이나 요청 입력은 버전에 포함되지 않습니다)과 이를 계산한 작업입니다. |
| `window_seconds` | 사용량 규칙이 집계한 원장 기간입니다. 현재 상태를 읽는 규칙에서는 null입니다. |
| `population`, `count` | `share`의 분모(발견 사항을 측정하는 기준이 되는 행 또는 호출 수이며, 규칙이 의도적으로 제외하는 행을 포함할 수 있음)와 일치한 개수입니다. `namespace.validate` 검사는 분모 없이 개수만 보고하며 `population`을 null로 둡니다. 요약 읽기가 건너뛰어진 audit 발견 사항도 마찬가지입니다. |
| `sample_limit`, `samples_complete` | 샘플 상한과, 샘플이 모든 일치 항목을 나열하는지 여부입니다. 상한 때문에 잘렸거나 모든 일치 항목을 지명할 수 없으면 false입니다. |
| `evidence` | 샘플이 지명한 각 행의 실시간 리비전입니다(아래 참조). 값은 포함하지 않습니다. |
| `exceptions`, `uncertainty` | 규칙이 의도적으로 제외하는 대상과, 해당 결과가 여전히 올바를 수 있는 이유입니다. |

각 `evidence` 항목은 샘플이 지명한 행의 실시간 리비전이며, 검사가 끝난 뒤에 읽습니다. 항목에는 `memory_key`, `memory_id`, `version`, `content_sha256`(`memory.get`이 제공하는 해시이며, 민감도가 높은 행, `LOOM_NAMESPACE_REPORT_HASH_MAX_BYTES`(64 KiB; [구성](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md))보다 긴 값, 또는 보고서가 읽는 동안 변경된 행에서는 비어 있음), 식별 컬럼, `memory_type`, `source_class`, `updated_at`이 들어 있습니다. 샘플은 `memory_id`로 행을 지명하며, 임베딩 테이블에서 온 경우에는 식별 컬럼으로 지명합니다. 여러 ID 아래에 있는 키는 일치한 것 하나만 인용합니다. 검사가 끝난 뒤 수정된 행은 더 새로운 리비전으로 인용되며, 이때 `updated_at`은 보고서의 `computed_at` 또는 `checked_at`(초 단위)과 같거나 그 이후입니다. 행의 상태만 바꾸는 쓰기는 `updated_at`을 유지할 수 있으므로 이 비교는 증명이 아니라 힌트입니다. 단일 행을 지명하지 않는 샘플(`exact_key_store` 핫 키, `version_churn` 키)과 더 이상 실시간이 아닌 행(고아 벡터)에는 항목이 없으며, Postgres 권한 하에서 읽은 버전으로 더 이상 권한이 제공하지 않는 행에도 항목이 없습니다([스토리지 권한](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)).

보고서의 `coverage`는 실행된 규칙(`rules_run`)과 실행할 수 없었던 규칙(`rules_skipped`, 사유와 함께 `skipped`에도 있음)을 나열합니다. 둘 다 읽은 근거(`evidence`)를 명시하며, `namespace.audit`의 경우 대부분의 발견 사항이 기준으로 삼는 요약 읽기와, Postgres 권한 하에서는 네임스페이스가 커밋한 내용을 ClickHouse가 게시하기를 기다리는 `projection`도 명시합니다([스토리지 권한](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md)). 이 중 하나라도 건너뛰어졌으면 `complete`는 false입니다.

`loom audit-namespace`는 셸에서 `namespace.audit`을 실행합니다. [네임스페이스 최적화 가이드](https://github.com/ClickHouse/loom/blob/main/docs/guides/optimize-namespace.md)는 그 발견 사항을 검토된 개선 계획으로 바꿉니다. 어휘는 `namespace.register_category`, `namespace.list_categories`, `namespace.search_categories`(카테고리)와 `namespace.register_ontology`, `namespace.list_ontology`([엔티티 그래프](https://github.com/ClickHouse/loom/blob/main/docs/features/entity-graph.md)의 엔티티 유형과 관계 동사)로 등록합니다.

스냅샷과 특정 시점으로의 롤백(`namespace.snapshot`, `namespace.restore`, `namespace.rollback`)은 [이력과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)에서 다룹니다.

<a id="inspecting-one-user-agent-or-session"></a>

## 사용자, 에이전트 또는 세션 하나 살펴보기

`memory.inspect`(viewer)는 `subject`(`user`, `agent` 또는 `session`)와 `id`(살펴볼 `user_id`, 에이전트 이름 또는 `session_id`)를 받습니다. 설정되지 않음을 뜻하는 `-`와 `*`는 아무도 가리키지 않으므로 거부됩니다. 이 op는 읽는 대상을 전혀 바꾸지 않으며 recall 크레딧도 주지 않습니다. `memory.list`와 마찬가지로, 나열한 행에 대해 노출 원장(exposure ledger) 행을 하나 기록합니다.

- **`own`**: 정체성이 해당 subject를 가리키는 활성 행입니다. 에이전트의 경우 그 이름으로 기록된 행입니다. 이름은 행이 어느 에이전트에서 왔는지를 알려 줄 뿐 읽기 경계가 아닙니다. 사용자의 경우 작성자와 관계없이 해당 `user_id`로 분류된 행이며, 사용자에 고정된 토큰(`read_scope: own_user`)이 읽는 범위입니다. 세션의 경우 어떤 사용자든 해당 `session_id`에서 기록한 행이며, 한 사용자와 세션에 고정된 토큰(`read_scope: own_session`)은 그 사용자의 부분을 읽습니다.
- **`others`**: 네임스페이스의 나머지 모든 활성 행입니다. 멤버는 이 행도 읽을 수 있으므로, `own`만으로는 사용자, 에이전트 또는 세션이 읽을 수 있는 전부를 나타내지 못합니다. 어느 뷰도 승격된 행(`memory.promote`)은 세지 않으며, 검색은 이 행을 모든 에이전트에게도 제공합니다.
- 각 뷰의 `definition`은 어떤 행을 담고 있는지 쉬운 말로 설명하고, `refs`는 그 뷰가 가리키는 정확한 컬럼을 나열하며, 정의에 읽기 범위나 op가 언급되면 그것도 나열합니다.
- 각 뷰는 행을 type, status, source class별로 세고, 상위 태그와 행이 분류된 등록 카테고리를 나열하며, 최신 행(`sample_limit`, 기본값 10)을 `memory_id`, `version`, `memory.get`이 제공하는 `content_sha256`, `actor`, `sensitivity`, 기록이 보관한 버전 수(`memory.history`가 제외하는 엔터티 태거의 재발행 포함), 200자 발췌와 함께 보여 줍니다. 민감도가 높은 행에는 발췌와 해시가 없습니다. 값이 `namespace_report_hash_max_bytes`보다 크거나(0이면 해시를 제시하지 않음), 목록 작성과 인용 읽기 사이에 수정된 행에도 해시가 없습니다. `complete`는 목록이 뷰보다 적은 행을 담고 있거나, 뷰의 집계를 읽을 수 없었거나, Postgres authority에서 네임스페이스가 커밋한 내용을 ClickHouse가 담고 있음을 확인할 수 없을 때 false입니다. 이는 대기 시간이 초과되었거나 Postgres가 응답하지 않은 경우입니다(`skipped`에 `projection`이 표시됨).
- **`activity`**: `window_seconds`(기본값 7일) 동안 원장이 subject 아래에 분류한 호출을 operation별로, 시도 횟수와 성공 횟수로 보여 줍니다. 워커 행과 대시보드 및 어시스턴트의 호출은 제외됩니다. 원장은 호출을 그 인자가 지정한 `agent`, `user_id`, `session_id` 아래에 분류하며, 범위가 좁혀진 토큰의 경우 토큰이 고정된 사용자와 세션 아래에 분류합니다. 따라서 누가 호출했든 subject를 지정한 호출은 집계되고, 아무것도 지정하지 않는 `memory.inspect` 호출은 집계되지 않습니다. 호출은 도구가 실행되었음을 보여 줄 뿐, 그 결과가 모델에 도달했음을 보여 주지는 않습니다. `gap`은 원장이 보관하는 기간이 윈도보다 짧을 때 이를 알려 줍니다(네임스페이스 자체의 `retention_days`, 없으면 배포의 값).
- **`settings`**: 가이드의 리비전과 요약, 등록된 카테고리 수, 그리고 조직 관리자에게만 보이는 운영자 추출 정책입니다(누락된 이유는 `extraction_policy_unavailable`이 알려 줍니다). 추출 지침은 요청마다 전달되며, 어떤 사용자, 에이전트 또는 세션 설정도 이를 저장하지 않습니다. `extraction_instructions_refs`는 이를 전달하는 op와 인자를 알려 줍니다.
- 읽을 수 없는 섹션은 `skipped`에 나열됩니다. 건너뛴 섹션은 빈 섹션과 다릅니다. 네임스페이스보다 좁게 범위가 제한된 토큰(`read_scope`)은 이 op를 호출할 수 없습니다.

<a id="drafting-changes-to-how-memories-are-made"></a>

## 메모리가 만들어지는 방식에 대한 변경안 작성

`namespace.tune` (viewer)은 네임스페이스의 감사 결과, 태그와 키 구조, 등록된 카테고리, 가이드를 읽고, org 관리자라면 적용 중인 운영자 추출 정책까지 읽어서, 메모리를 만드는 설정에 대한 변경안을 작성합니다. 사용자, 에이전트, 세션 하나가 아니라 네임스페이스 전체를 읽으며(하나만 보려면 `memory.inspect`를 사용합니다), 아무것도 적용하지 않고 모델도 호출하지 않습니다. 각 제안에는 다음 항목이 들어 있습니다.

- **`target`**: 변경 대상 설정. `extraction_instructions.request`(`memory.set_from_messages` 요청이 보내는 지침이며, 사용자, 에이전트, 세션 설정 중 어디에도 저장되지 않습니다), `namespace.category`, `namespace.guide`, 문서의 경우 `settings.knowledge_chunking`과 `settings.document_extractors_json`([아래](#document-drafts))입니다. 추출 규칙은 운영자 추출 정책으로는 절대 작성하지 않습니다. 적용 중인 정책은 다른 모든 지침보다 우선하고 추출의 복구 단계를 끄기 때문에, 그곳에 스타일 규칙을 넣으면 네임스페이스가 지금 유지하는 사실을 잃을 수 있습니다. 적용 중인 정책이 이미 정확히 같은 텍스트로 포함한 규칙은 다시 작성하지 않습니다.
- **`current`**와 **`proposed`**: 설정의 현재 값과 적용하면 설정될 값이며, 가이드에는 `current_revision`이 함께 들어 있습니다.
- **`evidence`**: 제안이 대응하는 감사 결과(`finding_codes`), 제안을 뒷받침하는 행의 수(`memory_id`, `version`, 그리고 `memory.get`이 제공하는 `content_sha256`로 인용한 처음 `sample_limit`개이며, 각 결과에서 차례로 하나씩 가져옵니다), 그리고 변경 후에도 계속 동작해야 하는 행의 수(`counter_count`: 검사가 읽은 가장 작은 모집단 가운데 어느 검사에서도 지적되지 않은 행의 수 이상이거나, 다른 카테고리가 이미 가진 태그된 행의 수입니다. 이 수를 읽을 수 없으면, 그리고 저장된 행을 바꾸지 않는 가이드에서는 null입니다)가 들어 있습니다.
- **`affected_agents`**: 변경의 영향을 받는 작성자이며 최대 20개이고, `affected_agents_total`이 함께 제공됩니다. 추출 지침의 경우 추출이 행을 기록한 에이전트이고, 카테고리나 가이드의 경우 유효한 행을 기록한 모든 에이전트입니다. 네임스페이스의 작성자를 읽을 수 없으면 `affected_agents_total`은 null입니다.
- **`effect`**(`future`)와 **`historical`**: 설정은 변경된 이후에 기록되는 메모리에 영향을 줍니다. 저장된 행을 다시 정리하는 작업(`memory.move`, `memory.assign_category`)은 별도의 승인이 필요한 별개의 작업입니다.
- **`apply`**: 일반 쓰기 작업, 그 인자(인자가 각 요청에 병합할 일부일 때는 `args_complete: false`), 필요한 역할, 보호 장치입니다. `namespace.guide_set`은 `expected_revision`보다 앞서 변경된 가이드를 거부합니다. `namespace.register_category`에는 리비전 검사가 없으므로 적용 직전에 카테고리를 다시 읽습니다.
- **`refs`**: 변경안의 텍스트가 평문으로 언급하는 정확한 이름 각각이며, 이를 바탕으로 동작하는 호출자를 위한 것입니다. 설정은 변경되는 방식 그대로 이름이 붙습니다. 실시간으로 변경되면 `settings.set <field>`, 환경 변수로만 설정되면 해당 `LOOM_` 변수입니다. 설정, 사라진 기능, 감사 결과, 오류 코드에 관한 `skipped` 사유, 또는 독자에게 조치를 안내하는 사유에는 괄호 안에 정확한 이름이 들어 있습니다. `projection:` 줄과 `audit.<check>` 줄은 출처의 텍스트를 그대로 담습니다.

변경안은 고정된 규칙에서 만들어집니다. 작성자 쪽 감사 결과(이중 인코딩된 값, JSON 접두 발췌, 과대 값, 에피소드성 blob, 조율 상태, 직접 만든 인덱스 행)마다 추출 규칙을 하나씩 작성하며, `memory.set_from_messages`의 추출이 네임스페이스에 기록하고(소스 트레이스가 있는 행) 결과가 인용한 행 중 하나 이상이 추출에서 나온 경우에만 작성합니다. 지침은 그 밖의 어떤 것에도 영향을 주지 않기 때문입니다. 인용된 행 중 추출이 기록한 것이 하나도 없는 결과, 감사가 유효한 행을 하나도 인용하지 않은 결과, 추출이 아무것도 기록하지 않는 네임스페이스에서는 변경안을 작성하지 않으며 `skipped`에 그 사실이 명시됩니다. 영향을 받는 에이전트는 추출이 행을 기록한 에이전트이고, 변경안은 추출이 기록한 지적된 행을 인용하며, 지적된 행의 수는 모든 작성자를 포함합니다. 키의 구조는 작성자가 정하므로(추출된 키는 정제됩니다) 키에 대한 규칙은 작성하지 않습니다. 그다음으로, 유효한 행이 `min_rows`개 이상이면서 같은 이름의 카테고리가 없는 태그마다 카테고리를 작성하고, `min_rows`개 이상의 행에 있는 키 접두사와 태그(또는 기존 가이드에 빠진 접두사)를 나열하는 가이드를 작성합니다. 가이드를 받지 않는 개인 컬렉션(`col-me-…`)에는 작성하지 않습니다. 일반 이름이 아닌 태그나 접두사는 작성 대상이 되지 않으므로, 행에 저장된 텍스트가 대상이나 인자를 조종할 수 없습니다. 행이 무엇에 관한 것인지가 아니라 어디서 왔는지를 나타내는 `<prefix>:` 태그(`source:`, `speaker:`, `agent:`)도 마찬가지로 제외됩니다. 감사가 실행하지 못한 검사는 `skipped`에 `audit.<check>`로 나열됩니다. 네임스페이스보다 좁게 제한된 토큰(`read_scope`)은 이를 호출할 수 없습니다.

<a id="document-drafts"></a>

### 문서 변경안

문서를 분할하고 추출하는 설정은 배포 환경의 것이며 그곳에만 존재합니다. 문서 변경안의 `scope`는 배포 환경의 모든 네임스페이스이고, 근거는 이 네임스페이스의 것이며, `apply`에는 설정을 변경하는 방법(`via`)이 명시됩니다.

- **청크 크기와 스트라이드**(`settings.knowledge_chunking`): 유효한 문서 최대 `namespace_tune_document_sample`개(기본값 50이며 `source_uri`의 해시로 선택)를 현재 `knowledge_chunk_size`와 `knowledge_chunk_stride`로 프로세스 내에서 다시 분할합니다. 각 문서는 해당 쓰기 경로가 쓰는 청커로 분할되며, 텍스트 문서는 텍스트를, 수집된 문서는 저장된 아티팩트를 사용하고, 텍스트와 아티팩트가 합계 8 MiB에 이를 때까지 불러옵니다. 그 이후의 문서, 2,000,000자를 넘는 텍스트, 8 MiB를 넘는 아티팩트는 `unread`로 집계되고 `load_cap`에 총량이 표시됩니다. 아무것도 기록하지 않고, 모델을 호출하지 않으며, 텍스트를 반환하지도 않습니다. `chunking`은 분할 결과를 보고합니다. 청크 수, 청크당 문자 수, 구조상 필요한 것보다 크기 때문에 더 많은 청크로 잘리는 섹션(`sections_cut`), 가장 큰 임베딩 입력의 토큰 수, 그리고 임베더의 한도를 넘는 입력의 수입니다(임베더의 제공자가 Loom이 확인할 한도를 밝히지 않으면 null). 입력이 한도를 넘으면(맞는 가장 큰 더 작은 크기이며 최소 200까지) 크기가 작성되고, 샘플링된 섹션의 4분의 1보다 많이 5개 이상의 문서에 걸쳐 잘리고 한도를 알 수 있을 때도 작성됩니다(현재 크기의 최대 4배까지, 4분의 3이 한도 안에서 온전히 유지되는 가장 작은 더 큰 크기). 각 후보 크기는 가까운 것부터 한 번씩 시험하며, 스트라이드는 크기와의 비율을 유지합니다. `preview`에는 두 값으로 분할한 샘플이 들어 있습니다. `apply`는 `expected`가 있는 `settings.set`(`POST /v1/admin/settings.set`, 플랫폼 관리자)이고, `apply_also`가 스트라이드를 설정합니다. 두 규칙 중 어느 것에도 해당하지 않는 샘플은 변경안을 작성하지 않으며 근거로만 남습니다. `knowledge_chunk_size`가 0 이하여서 윈도를 만들지 않는 경우(텍스트 문서는 청크 하나, 수집된 문서는 블록당 청크 하나)와, `knowledge_chunk_stride`가 0 이하여서 크기보다 긴 섹션이나 블록에 윈도를 적용하지 않는 경우에도 마찬가지이며, 둘 중 어느 쪽에도 크기를 작성하지 않습니다.
- **추출기 라우트**(`settings.document_extractors_json`): 소스의 현재 작업이 `format_unavailable`로 실패했거나 게시된 추출 결과가 불완전한 형식은, 해당 형식을 capabilities에 선언한 구성된 추출기로 라우팅됩니다. 소스가 현재 가는 서비스는 수집 단계와 같은 방식으로 형식과 미디어 타입에서 읽습니다. 변경안은 형식 자체에 대한 라우트를 추가하며, 이는 미디어 타입 라우트보다 우선하므로 그 형식의 모든 소스를 가져갑니다. 해당 형식을 선언한 서비스 중에서(불완전한 추출의 경우 그 추출을 만들지 않은 서비스 중에서) 라우트는 소스가 이미 사용 중인 서비스(전체가 추출되었거나, 해당 형식을 현재 선언한 서비스에서 실패한 소스), 그다음으로 미디어 타입 라우트가 닿는 서비스, 그다음으로 아무 서비스로 향하며, 각 단계에서 불완전한 추출을 만들지 않은 서비스가 먼저입니다. 라우트가 다른 서비스에서 온전히 추출되는 소스를 옮길 수도 있으며, 그 경우 근거(rationale)에 해당 소스가 명시됩니다. 불완전한 추출에 대응하지 못하는 라우트는 그 사실을 `skipped`에 밝힙니다. 변경안은 서비스를 URL이 아니라 URL을 담은 설정으로 지정합니다. URL에 자격 증명이 있을 수 있기 때문이며, 그래서 `args_complete`는 false입니다. `via`는 `environment`이며, 각 서버와 워커가 재시작되면 적용됩니다. 구성된 어떤 추출기도 선언하지 않은 형식은 변경안을 작성하지 않고 `skipped`(`extractor.<format>`)에 명시됩니다. 해당 형식을 현재 선언한 서비스에서 실패한 소스도 마찬가지이며, 이 소스는 대신 다시 수집됩니다.

결정 추출(`loom decisions extract`와 소스 동기화의 마이닝)은 고정된 텍스트로 프롬프트를 만들고 요청, 네임스페이스, org에서 지침을 받지 않으므로 이에 대한 지침은 작성하지 않으며, 네임스페이스에 문서가 있으면 `skipped`에 그 사실이 명시됩니다. 저장된 문서는 청크와 추출 결과를 그대로 유지합니다. 이를 다시 분할하거나 추출하는 것은 별도의 작업입니다(`historical`).

<a id="a-namespaces-guide"></a>

## 네임스페이스 가이드

네임스페이스 가이드는 해당 네임스페이스를 위한 Markdown README입니다. 네임스페이스에 무엇이 들어 있는지, 키 구성, 각 태그의 의미, 어떤 op를 호출해야 하는지를 담습니다. 에이전트는 첫 쓰기 전에 가이드를 읽고, 이러한 규칙 중 하나를 바꿀 때 가이드를 갱신합니다. 사람도 같은 텍스트를 읽습니다. 가이드에는 데이터에 대한 규칙만 담고 사실 자체는 담지 않습니다. 사실은 검색과 삭제가 적용되는 메모리에 남습니다.

| 작업                      | 역할   | 동작                                                                                                                          |
| ------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `namespace.guide_get`     | viewer | 현재 가이드(또는 과거의 `revision`)의 텍스트, 한 줄 요약, 리비전, 작성자, 메모를 반환합니다.                                   |
| `namespace.guide_set`     | writer | `expected_revision`이 현재 리비전이면 다음 리비전을 기록하고, 그렇지 않으면 병합할 현재 가이드와 함께 거부합니다.               |
| `namespace.guide_history` | viewer | 최신 리비전들을 요약, 메모, 작성자, 길이와 함께 반환합니다.                                                                   |

모든 저장은 어느 리비전에서 작업했는지를 명시합니다. 두 에이전트가 같은 리비전에서 편집하면 먼저 저장한 쪽이 반영되고, 나중에 저장한 쪽은 현재 가이드와 함께 `conflict: true` 응답을 받습니다. 에이전트는 그 텍스트에 자신의 변경을 병합한 뒤, 반환된 리비전으로 다시 저장합니다. 서버는 텍스트를 직접 병합하지 않습니다. 이미 반영된 저장을 재시도하면 자신의 텍스트가 이미 현재 상태이므로 아무것도 기록하지 않습니다. `summary`를 생략하고 저장하면 현재 요약이 유지되고, `content`를 비우면 가이드가 지워집니다. 최종 사용자의 개인 컬렉션(`col-me-…`)에는 가이드를 둘 수 없습니다. 모든 리비전이 보존되므로 `revision`을 지정한 `namespace.guide_get`으로 어느 리비전이든 읽을 수 있습니다.

가이드는 요청하지 않아도 에이전트에게 전달됩니다.

- **세션 시작 시** `context.pack`이 `namespace_guide` 섹션을 렌더링합니다. 가이드의 리비전, 작성자, 요약이 먼저 나오고, 이어서 섹션 예산(200 토큰)에 맞게 잘린 텍스트가 나옵니다. 이 텍스트는 불러온 데이터이며, 다른 작성자의 텍스트와 마찬가지로 이스케이프되어 하나의 항목 아래에 들여쓰기로 표시됩니다.
- **디렉터리에서** 공개 네임스페이스의 각 `namespace.browse` 행에는 `guide_revision`과 `guide_summary`가 포함되며, `query`는 가이드의 요약도 검색 대상으로 삼습니다. 따라서 여러 공개 네임스페이스를 살펴보는 에이전트는 필요한 내용을 설명하는 가이드를 가진 네임스페이스를 찾을 수 있습니다. 비공개(private) 행은 멤버가 아닌 사람에게 가이드를 보여주지 않으며, 제한(restricted) 행은 가이드를 전혀 보여주지 않습니다.
- **`namespace.get`** 은 `guide_revision`과 `guide_summary`를 포함합니다.

가이드를 읽으려면 해당 네임스페이스에 대한 읽기 권한이 필요합니다. 공개 행이 보인다고 해서 읽기 권한이 넓어지지는 않기 때문입니다. `namespace.browse`는 조직에 각 공개 네임스페이스의 요약을 보여주고, 가이드 전체는 멤버(`namespace.join`)를 위한 것입니다. 대시보드에서 Memories 페이지 헤더에는 **Add memory** 옆에 **Guide** 버튼이 있으며, 가이드의 리비전과 요약이 함께 표시됩니다. 가이드가 없는 네임스페이스에서는 writer를 바로 편집기로 안내합니다. 이 버튼이나 조직의 Namespaces에서 처음 만드는 가이드는 새로 온 사람이 묻는 내용(네임스페이스에 무엇이 들어 있는지, 키, 태그, 사용 방법)의 개요에서 시작하며, 이 개요를 바꾸지 않은 채로는 저장할 수 없습니다. 조직의 Namespaces에서는 공개 네임스페이스의 가이드 요약을 설명 아래에 보여줍니다. 소속된 네임스페이스에서는 요약을 누르면 가이드가 열리고, 요약이 표시되지 않는 경우에는 **Guide** 메뉴 항목으로 열 수 있습니다. writer는 두 곳 어디에서든 편집할 수 있으며, 에이전트와 마찬가지로 자신이 연 리비전을 기준으로 저장합니다. 네임스페이스를 삭제하면 해당 가이드의 모든 리비전이 삭제됩니다.

<a id="the-mentor"></a>

## 멘토

`mentor.brief`는 에이전트 하나에 대한 조언과 그 근거를 반환합니다. 근거에는 기간(기본 7일) 동안의 호출, 네임스페이스가 제공하는 플레인, 그리고 각 힌트를 발생시킨 횟수가 포함됩니다. `context.pack`은 세션을 시작할 때 최대 세 개의 힌트를 렌더링하며, 성공한 응답에는 최대 한 개의 힌트가 담깁니다(MCP에서는 `hint`, HTTP에서는 `X-Loom-Hint`). 이 개수는 `LOOM_MENTOR_MAX_HINTS_PER_SESSION`으로 제한됩니다. `loom mentor --org … --namespace … --agent …`는 브리프 전체를 출력합니다. [멘토 ops](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md)를 참고하세요.

브리프의 `usage_evidence`는 에이전트의 Loom 사용이 보여 주는 내용을 단계(rung)별로 하나씩 나열하며, 누가 기록했는지(`server`: Loom이 직접 확인함, `client`: 호출자가 보고했으며 검증되지 않음)를 함께 표시합니다. 각 단계는 캐시된 프로필이 아니라 브리프를 만들 때마다, 브리프가 보고하는 기간에 대해 읽습니다. 호출 원장(`attempt`, `success`, `committed_write`, `returned_hit`, 그리고 `memory.outcome` 레이블)에서 읽는 단계는 원장이 해당 에이전트 아래에 기록한 행을 셉니다. 여기서 에이전트란 호출 본문이 지정한 에이전트입니다(키로 호출한 경우에는 호출자가 누구든 그 행의 작성자). 본문에 에이전트가 없는 호출(id로 읽기, MCP `memory.update`, Python SDK의 기본 `memory.update`)이나, 에이전트 전체에 걸쳐 읽고 `*` 아래에 기록되는 호출(SDK의 `memory.search`와 `memory.ask`, MCP의 에이전트 간 읽기, 에이전트를 지정하지 않은 `memory.outcome`)은 집계되지 않습니다. 이 사다리(ladder)는 프로필의 집계를 전혀 사용하지 않으므로 멘토가 꺼져 있어도 답을 줄 수 있습니다. 특정 에이전트에 대한 것이 아닌 브리프에는 사다리가 없습니다.

| 단계                  | 출처     | 집계 대상                                                                                                                                                                                                                                                                                           |
| --------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `attempt`             | `server` | 응답 여부와 관계없이 수행된 호출. 대시보드나 어시스턴트의 호출, 브리프, 지연된 `memory.ask`가 남기는 판정은 제외                                                                                                                                                                                    |
| `success`             | `server` | 오류 없이 반환된 호출. 아무것도 찾지 못한 읽기도 포함                                                                                                                                                                                                                                               |
| `committed_write`     | `server` | 행을 저장한 `memory.set` 호출. no-op, 드라이 런, 재실행은 제외                                                                                                                                                                                                                                      |
| `indexed_write`       | `server` | 기간 내에 처음 생성된 메모리(사용자, 세션, 키당 하나). 어떤 쓰기 op로든 생성될 수 있으며, 현재 임베더의 벡터가 있고 제공 상태이며 만료되지 않은 것                                                                                                                                                  |
| `returned_hit`        | `server` | 기권하지 않은 `memory.search` 또는 `memory.ask`, 메모리·문서·대화 히트가 있는 `search.query`, 해당 키를 찾은 get                                                                                                                                                                                     |
| `client_acknowledged` | `client` | 에이전트가 의존했다고 보고한 고유 메모리 키: `run.complete`의 `memories_used`, 그리고 `used` 또는 `positive`로 id를 지정해 행에 반영된 `memory.outcome`. 키로 지정한 레이블(행 작성자의 에이전트 아래에 기록됨), Python SDK의 `outcome` 호출(에이전트 없음), `memory.outcome_run`은 제외                 |
| `task_outcome`        | `client` | 채점자가 보고한 대로 `pass` 또는 `fail`로 채점된(`run.grade`) 에이전트의 실행                                                                                                                                                                                                                       |

반환된 히트가 있다고 해서 그 답이 모델에 전달되었거나 모델의 작업을 바꾸었다는 뜻은 아닙니다. `ledger_from`은 기간 내 해당 에이전트의 가장 오래된 원장 행이고, `ledger_retention_days`는 설정된 보존 기간이 네임스페이스의 원장 행을 유지하는 일수입니다(0: 삭제할 때까지). 기간이 이 값보다 길면 원장에서 읽는 단계는 지정한 기간보다 적은 범위만 다루게 됩니다.

[`mentor.guide`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/mentor.md#mentorguide)는 모델을 위해 작성된 Loom 사용법을 반환합니다. 이는 `skills/loom/`의 `loom` 스킬이며, `part`로 `skill`(루프와 규칙), `reference`(모든 플레인과 그 ops), `all`을 지정할 수 있습니다. 모든 호출자에게 동일하므로 사용자 단위로 좁혀진 토큰과 HTTP를 통한 최종 사용자의 토큰을 포함해 어떤 viewer 토큰으로도 읽을 수 있습니다.

<a id="throwaway-namespaces"></a>

## 일회용 네임스페이스

벤치마크와 하니스는 `LOOM_NAMESPACE_BENCH_PREFIXES`에 지정한 접두사 아래의 네임스페이스에 데이터를 기록하며, 자동 등록은 이 네임스페이스에 `kind = bench`를 지정합니다. `namespace_gc` 워커는 활성 메모리가 있고 `LOOM_NAMESPACE_GC_IDLE_HOURS`(기본값 24) 동안 쓰기가 없는 bench 네임스페이스를 배치마다 목록 범위의 삭제 saga 하나로 제거합니다. `loom namespaces-gc --org <org>`는 후보를 나열하고, `--erase`는 이를 수동으로 제거합니다. `loom namespaces-adopt`는 메모리는 있지만 레지스트리 행이 없는 네임스페이스에 소유자 행을 부여합니다.

<a id="settings"></a>

## 설정

| 설정                                                           | 효과                                                                                    |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE_AUTOREGISTER_ENABLED`                          | 첫 번째 쓰기가 네임스페이스를 등록합니다.                                               |
| `LOOM_NAMESPACE_BENCH_PREFIXES`, `LOOM_NAMESPACE_GC_*`         | 어떤 네임스페이스가 일회용인지, 그리고 GC가 언제 이를 삭제하는지를 정합니다.            |
| `LOOM_MENTOR_TTL_SECONDS`, `LOOM_MENTOR_MAX_HINTS_PER_SESSION` | 멘토 캐시와 호출별 힌트에 대한 세션당 상한입니다. TTL이 0이면 멘토가 비활성화됩니다.    |
| `LOOM_CONFLICT_THRESHOLD`                                      | 네임스페이스가 상속하는 배포 수준의 충돌 임곗값입니다.                                  |

[구성: 네임스페이스 등록 및 GC](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#namespace-registration-and-gc)
및 [구성: 컨텍스트](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#context)를 참고하세요.

<a id="limits"></a>

## 제한 사항

- 네임스페이스 이름은 1–128자이며, kebab-case를 권장합니다.
- `namespace.delete`는 소프트 삭제입니다. 메모리를 제거하지 않으며, 메모리를 제거하는 것은 삭제 요청(erasure)입니다.
- `default_conflict_policy` 또는 `retrieval_defaults`를 변경하면 이후의 쓰기와 검색 동작이 달라집니다. 이미 저장된 행은 다시 판단하지 않습니다.
- `memory.promote`는 하나의 네임스페이스 안에서만 공유합니다. 다른 네임스페이스는 복사본(`memory.copy_to`, `memory.cross_promote`)을 통해서만 사실을 볼 수 있으며, 이 복사본은 원본의 이후 수정을 따라가지 않습니다.

<a id="related"></a>

## 관련 문서

- [아이덴티티와 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/concepts/identities-and-namespaces.md) — 모델 설명.
- [토큰과 grant](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md) — 접근 권한의 발급과 범위 지정.
- [대시보드](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md) — 조직의 네임스페이스, 설정, 관리자 드로어.
- [보안 아키텍처](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md) — 요청이 인가되는 방식.
- [오퍼레이션 레퍼런스: 네임스페이스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#namespace),
  [권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#grants), [멘토](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#mentor),
  [에이전트](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#agents).
