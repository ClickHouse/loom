<!-- i18n: source=docs/features/memory/working-memory-and-profiles.md blob=bedd8394f653eb3b5cd589b4bd7aed2749be116b model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md), which is authoritative.

<a id="working-memory-and-profiles"></a>

# 워킹 메모리와 프로필

에이전트에게 검색으로 찾게 하지 않고 매번 직접 전달하는 텍스트를 보관하는 두 개의 플레인이 있습니다.

- **워킹 메모리**: 리스, 커서, 체크포인트, "어디까지 했는지" 같은 정보를 담는 이름이 붙은 작은 블록입니다. 블록은 임베딩, 랭킹, 검색의 대상이 아닙니다.
- **프로필**: 사용자 또는 에이전트에 대한 상시 로드 컨텍스트(페르소나, 선호, 집중 영역)로, 하나의 프롬프트 조각으로 렌더링됩니다.

이후 세션에서 질의로 찾아야 하는 내용에는 `memory.set`을 사용하세요. 이후 세션이 묻지 않아도 알아야 하는 내용에는 이 플레인들을 사용하세요. 스크래치패드 내용을 사실로 기록하면 모든 검색과 충돌 검사에서 실제 사실과 경쟁하게 됩니다.

<a id="working-memory"></a>

## 작업 메모리

블록은 `loom.working_memory`에 저장되며, 식별 튜플과 `run_id`, `block_key`(기본값 `scratchpad`, 최대 80자)가 키가 됩니다. 블록은 재시작 후에도 유지되고 TTL이 없으므로, 긴 실행의 재개 지점으로 사용할 수 있습니다
([추적 및 재개](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md)). 블록은 작성자가 삭제하거나 삭제 요청(erasure)이 해당 블록을 포함할 때만 사라집니다.

| Op                       | 역할   | 동작                                                                            |
| ------------------------ | ------ | ------------------------------------------------------------------------------- |
| `working_memory.set`     | writer | 블록의 내용을 교체합니다. `char_limit`이 적용됩니다.                            |
| `working_memory.append`  | writer | `addition`을 `separator`(기본값은 줄바꿈)와 함께 덧붙입니다.                    |
| `working_memory.replace` | writer | 블록 안에서 `old`와 일치하는 모든 부분을 `new`로 교체합니다.                    |
| `working_memory.delete`  | writer | 블록에 삭제 표시(tombstone)를 합니다.                                           |
| `working_memory.get`     | viewer | 블록 하나를 `fill_ratio`와 함께 읽습니다.                                       |
| `working_memory.render`  | viewer | 범위 내의 블록(또는 이름을 지정한 최대 20개의 `block_keys`)을 XML 또는 Markdown으로 렌더링합니다. |

필드 목록: [operations 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#working_memory).

<a id="size-limits"></a>

### 크기 제한

| 제한                 | 값                                                                        |
| -------------------- | ------------------------------------------------------------------------- |
| 기본 `char_limit`    | 블록당 4,096자                                                            |
| 최대 `char_limit`    | 16,384자                                                                  |
| 제한 초과 시         | 422 `WORKING_MEMORY_LIMIT_EXCEEDED`, `value_length`와 `char_limit` 포함   |
| 렌더링당 블록 수     | `limit` 1–100 (기본값 20)                                                 |

모든 쓰기는 `value_length`와 `fill_ratio`(`len(content) / char_limit`)를 반환하므로, 호출자는 쓰기가 거부되기 전에 내용을 줄일 수 있습니다. `old` 텍스트가 없는 `replace`는 422 `SUBSTRING_NOT_FOUND`로 응답하고, 존재하지 않는 블록에 대한 같은 작업은 `WORKING_MEMORY_BLOCK_NOT_FOUND`로 응답합니다. 존재하지 않는 블록에 대한 `working_memory.get`은 `found=false`로 응답합니다.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.working_memory.set(
        "cursor: ticket 4812\nlease: holds refunds queue", block_key="state", run_id="run-7"
    )
    await bot.working_memory.append("step 3 done", block_key="state", run_id="run-7")
    print(await bot.working_memory.render(run_id="run-7"))
    # <working-memory block="state">cursor: ticket 4812 ...</working-memory>
```

<a id="profiles"></a>

## 프로필

프로필 섹션은 `memory_type='profile'`이고 키가 섹션 `label`인 메모리 행입니다. `profile.set`, `profile.append`, `profile.replace`는 `memory.set`을 얇게 감싼 래퍼로, 해당 타입과 `char_limit`을 고정합니다. 섹션 하나를 읽을 때는 레이블을 키로 하여 `memory.get`을 사용합니다. Python SDK는 이를 `agent.profile.get(label)`, `agent.profile.list()`, `agent.profile.delete(label)`로 감쌉니다(SDK 메서드이며 wire op가 아닙니다).

| Op                 | 역할   | 동작                                                                                         |
| ------------------ | ------ | -------------------------------------------------------------------------------------------- |
| `profile.set`      | writer | `label`의 섹션을 생성하거나 교체합니다(동시에 쓰는 writer가 있으면 `if_match_sha256` 사용).   |
| `profile.append`   | writer | 섹션에 내용을 덧붙입니다. 다른 클라이언트가 먼저 쓴 경우 compare-and-swap으로 재시도합니다.    |
| `profile.replace`  | writer | 섹션 안의 부분 문자열을 다시 씁니다(해당 문자열이 없으면 422 `SUBSTRING_NOT_FOUND`).          |
| `profile.render`   | viewer | 모든 섹션(또는 지정한 `labels`, 지정한 순서대로)을 하나의 XML 또는 Markdown 조각으로 렌더링합니다. |
| `profile.activity` | viewer | 어떤 클라이언트가 프로필을 언제 읽었는지 보여줍니다(`lookback_days`, 최대 30).                |

필드 목록: [operations 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#profile).

- **제한.** 기본 `char_limit`은 8,192자이며, 값의 정규(canonical) JSON을 기준으로 측정합니다. 이를 넘으면 422 `CHAR_LIMIT_EXCEEDED`로 응답합니다.
- **범위.** `profile.render`는 기본적으로(`scope="namespace"`) 네임스페이스 안의 모든 에이전트의 섹션을 읽으므로, 대시보드에서 작성한 페르소나가 모든 MCP 클라이언트에 전달됩니다. `scope="this_agent"`는 호출자 자신의 섹션만 읽습니다. `read_scope`가 `own_user` 또는 `own_session`이면 해당 사용자(또는 세션)의 섹션과 네임스페이스 수준 섹션(`user_id`가 `-`)만 렌더링됩니다.
- **검색.** 프로필 행은 `memory.search`에 나타나지 않습니다. `loom_search` 도구에는 프로필 단계가 있어, 질문이 사용자에 관한 것일 때 섹션을 의미 기준으로 점수화합니다. 각 섹션의 벡터는 쓰기가 끝날 때마다 요청 경로 밖에서 갱신됩니다. [통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)을 참고하세요.
- **사실을 프로필로 통합.** `profile_consolidation` 워커(기본값은 꺼짐, `LOOM_PROFILE_CONSOLIDATION_ENABLED`)는 모델 호출로 사용자의 선호 및 결정 메모리를 버전이 있는 섹션(레이블 `consolidated_profile`)으로 통합합니다. 보호 속성이 태그된 행은 제외됩니다. 출처(provenance)는 `derived`입니다(`source_trace`는 `profile.fold:<label>:<memory ids>`이며, 읽은 사실을 가리킵니다).

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.profile.set("persona", "Terse. Cite ticket ids. Never promise refunds.")
    await bot.profile.append("preferences", "Prefers EUR invoices.", user_id="u-42")
    system = await bot.profile.render(user_id="u-42", read_scope="own_user")
```

<a id="structured-profiles"></a>

## 구조화된 프로필

첫 응답 전에 사용자를 알아야 하는 챗봇은 자유 형식 텍스트 대신 운영자가 정의한 형태의 객체 하나를 읽습니다. 관리자가 네임스페이스에 프로필 스키마를 지정하면, `structured_profile` 워커가 사용자의 메모리로부터 각 사용자의 프로필을 그 형태로 최신 상태로 유지하고, `profile.structured`가 상태와 함께 이를 반환합니다. 읽기는 모델을 호출하지 않습니다.

| Op                     | 역할   | 동작                                                                                                                                         |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile_schema.set`   | admin  | 네임스페이스의 스키마(`json_schema`)를 설정합니다. 감사 대상이며 기존 스키마를 대체합니다.                                                   |
| `profile_schema.get`   | viewer | 스키마, 해당 `schema_sha256`, 설정한 사람과 시각을 반환합니다.                                                                               |
| `profile_schema.clear` | admin  | 스키마를 제거합니다. 감사 대상입니다. 이미 만들어진 프로필은 메모리 행으로 남습니다.                                                         |
| `profile.structured`   | viewer | 한 사용자의 프로필: `profile`, `status`, `built_at`, `memory_id`, `version` (그리고 같은 값을 문자열로 나타내 JavaScript에서도 정확한 `version_text`). |

스키마는 `properties`가 1개에서 64개인, 최대 16 KiB의 JSON Schema 객체입니다. 각 속성에는 `type`(`string`, `number`, `integer`, `boolean`, 또는 이 중 하나를 `items`로 갖고 선택적으로 최대 50까지의 `maxItems`를 갖는 `array`)과 모델이 읽는 `description`이 있어야 합니다. 전체 응답에는 속성마다 하나, 배열마다 `maxItems`개로 세어 최대 256개의 값이 들어가므로 모델의 출력 한도 안에 들어갑니다. 문자열에는 `enum`, `pattern`, `minLength`, `maxLength`를 쓸 수 있고, 숫자에는 `minimum`과 `maximum`을 쓸 수 있습니다. 이는 [타입이 있는 레코드](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md#typed-records)가 사용하는 제약입니다.
모든 속성은 모든 프로필에 들어 있으며, 메모리로 확정되지 않으면 `null`입니다. 따라서 스키마에는 `required`가 없고, `additionalProperties`는 `false`만 허용됩니다. `title`은 어느 수준에서든 허용되지만 무시됩니다.

```python
schema = {
    "type": "object",
    "properties": {
        "preferred_name": {"type": "string", "description": "What the user asks to be called"},
        "timezone": {"type": "string", "description": "IANA time zone, e.g. Europe/Berlin"},
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages they speak",
        },
    },
}
async with Agent(org="acme", namespace="support", agent="chat", api_key=ADMIN_TOKEN) as admin:
    await admin.client.profile_schema.set({"json_schema": schema})

async with Agent(org="acme", namespace="support", agent="chat") as bot:
    out = await bot.client.profile.structured({"user_id": "u-42"})
    # {"status": "ready", "profile": {"preferred_name": "Sam", "timezone": "Europe/Berlin",
    #  "languages": ["en", "de"]}, "built_at": "...", ...}
```

| `status`    | 의미                                                                                                                                                                                                  |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ready`     | 현재 스키마에 따라 사용자의 현재 메모리로부터 만들어졌습니다. 사실이 없는 사용자는 모두 `null`입니다.                                                                                                 |
| `stale`     | 만들어진 이후 메모리나 스키마가 바뀌었거나, 만들 때 사실 중 하나가 제공되지 않았습니다. 패스가 다시 만들 때까지 만들어진 그대로 제공됩니다.                                                           |
| `pending`   | 유효한 프로필이 없습니다. 아직 만들어지지 않았거나, 만들어진 행이 만료되었거나, 편집(redact)되었거나(워커가 다시 만들 때까지), `high`로 표시되어 있습니다(키가 새 버전으로 넘어갈 때까지 보류). 모든 속성이 `null`로 읽힙니다. |
| `no_schema` | 네임스페이스에 스키마가 없거나 저장된 스키마가 더 이상 파싱되지 않습니다. `profile`은 `{}`입니다.                                                                                                    |

- **저장.** 프로필은 키가 `structured_profile`인 사용자의 `profile` 행입니다(`agent`는 `-`). 따라서 `profile.render`와 `context.pack`에도 포함되며, [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory)가 그 버전을 나열합니다.
  출처(provenance)는 `derived`이고, `source_trace`는 `profile.fold:structured_profile:<memory ids>`로, 프로필을 만든 바탕 메모리들입니다. 워커가 쓴 행(해당 `actor`)만 제공됩니다. 호출자가 그 키에 쓴 행은 태그와 관계없이 프로필 없음(`pending`)으로 읽히며, 고정(pin)되거나 잠기거나 `high`로 표시되지 않았다면 워커가 그 위에 다시 만듭니다.
- **소스.** 사용자의 현재 `semantic`, `preference`, `decision` 사실이며, 자유 형식 폴드와 같은 필터를 적용합니다. `high` 행, 중복(그 정본 행이 대신 집계됨), 다른 사람에 관한 사실(`about:other`), 보호 속성, 저장된 잊기 요청은 포함되지 않습니다. 각 사실은 참이 된 시점(`valid_at`)으로, 없으면 마지막으로 바뀐 시점으로 날짜가 매겨지며, 빌드는 가장 신뢰도가 높고 가장 최근에 바뀐 것부터 읽으므로 수정은 가장 새로운 진술로 읽힙니다.
- **오래됨 판정.** 각 프로필은 스키마 속성의 해시(`title`이나 속성 순서는 해시를 바꾸지 않음), 소스의 다이제스트(각 소스의 id, 콘텐츠 해시, 신뢰도, 빌드가 읽고 정렬하는 날짜인 `valid_at` 또는 마지막 변경), 그리고 소스 개수를 가집니다. 새 사실이나 수정된 사실, 되돌리기, 복원, 삭제된 사실, 바뀐 신뢰도나 날짜, 그리고 행을 소스 안팎으로 옮기는 변경(`high`가 되거나, 타입이 바뀌거나, `about:other`, `attribute:`, `forget` 태그를 얻거나 잃거나, 만료되거나 `valid_to`를 지나는 경우)은 다이제스트를 바꿉니다. 재진술이나 강화도 마찬가지인데, 사실을 모델이 읽는 내용 중 가장 새로운 위치로 옮기기 때문이며, 쿨다운 뒤 한 번의 재빌드 비용이 듭니다. 소스 사실에 대한 `memory.lock`이나 `memory.unlock`도 같습니다. 내용은 같지만 새 버전이 쓰이고, 그 사실이 잠금 시점으로 날짜가 매겨져 모델이 읽는 내용 중 가장 새로운 위치와 빌드가 읽는 사실 중 가장 새로운 쪽으로 옮겨집니다. 서로 다른 키 아래에서 상충하는 두 사실 중에서는 마지막으로 잠기거나 잠금 해제된 쪽이 가장 최근 것으로 읽힙니다. 행의 마지막 변경을 유지하는 recall이나 backfill은 다이제스트를 바꾸지 않습니다. 다이제스트는 모든 소스를 포함하지만 빌드는 가장 신뢰도가 높고 새로운 `LOOM_STRUCTURED_PROFILE_MEMORY_LIMIT`(100)개만 읽습니다. 따라서 사실이 그보다 많은 사용자의 경우, 그 집합 밖의 사실이 바뀌어도 답을 바꿀 수 없는 모델 호출이 한 번 발생합니다. 스키마 변경은 다음 패스부터 패스당 최대 `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS`개의 프로필씩 다시 만들어집니다. 삭제(프로필이 센 것보다 소스가 적음)는 쿨다운을 건너뛰고, 패스에 예산이 남아 있는 동안 네임스페이스가 다음에 스캔될 때(최대 `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`(600) 뒤) 한 번 다시 만들어지며, `profile.structured`는 즉시 `stale`로 보고합니다. 그 밖의 변경은 `LOOM_STRUCTURED_PROFILE_COOLDOWN_SECONDS`(600) 뒤에 다시 만들어집니다.
  읽어야 했던 사실이 빠진 빌드(센 사실 중 메모리 한도 안의 하나가 제공되지 않은 경우: 소스를 센 복제본보다 뒤처진 ClickHouse 복제본, 또는 Postgres 아래에서 그 사실에 대해 권한 저장소가 가진 변경이 아직 ClickHouse에 도달하지 않은 경우)는 `stale`로 읽히며, 그런 변경이 여전히 빠져 있는 동안은 쿨다운 뒤 매번 모델 호출과 함께 다시 만들어집니다. 센 사실을 하나도 제공받지 못한 빌드는 모델을 호출하지 않습니다. 제공 중인 프로필을 모두 `null`인 프로필로 교체하고 쿨다운마다 다시 쓰며, 제공되는 프로필이 없는 사용자(`pending`)에게는 아무것도 쓰지 않습니다. 사실이 모두 사라진 사용자는 모델 호출 없이 모두 `null`인 프로필을 갖게 됩니다. 만료되거나 편집(redact)된 프로필 행은 프로필 없음(`pending`)으로 읽히고 다시 만들어집니다. `high` 프로필 행도 프로필 없음으로 읽히지만 워커는 그대로 두며, 키가 새 버전으로 넘어갈 때까지 고정처럼 보류됩니다.
- **출력 한도.** 모델은 `LLM_MAX_TOKENS`의 `structured_profile` 항목(32, `LOOM_LLM_MAX_TOKENS_OVERRIDES`로 변경)에 스키마가 추정한 전체 응답 분량을 더한 만큼 쓸 수 있습니다. 추정은 각 키와 구두점, 그리고 각 값(속성마다 하나, 배열마다 `maxItems`개)을 `maxLength` 또는 가장 긴 `enum` 값 기준으로 토큰당 두 글자로 계산하고, 둘 다 없는 문자열은 48토큰, 숫자나 boolean은 12토큰으로 계산합니다. `profile_schema.set`은 추정치가 16,384토큰을 넘는 스키마를 거부합니다. 추정치보다 긴 문자열은 여전히 잘릴 수 있으니, 자유 형식 속성에는 `maxLength`를 지정해 공간을 확보하세요.
- **검증.** 모델의 답은 쓰이기 전에 스키마에 맞는지 검사됩니다. 타입이 틀렸거나, enum이나 제약을 벗어났거나, 유한하지 않은 숫자(또는 2^53을 넘는 정수)이거나, 스키마에 선언되지 않은 속성인 값은 버려집니다(`null`).
- **패스.** 한 패스는 최대 `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS`(20)개의 프로필을 만듭니다. 스키마가 있는 네임스페이스를 지난 패스가 멈춘 곳부터 순서대로 훑으며 각각 동일한 몫을 받습니다. 만들 것이 없는 네임스페이스는 몫을 쓰지 않으므로, 패스는 예산을 다 쓰거나 모든 네임스페이스에 도달할 때까지 다음 네임스페이스로 계속 읽습니다. 네임스페이스가 남긴 몫은 자기 몫을 모두 쓴 네임스페이스에 돌아가며, 이미 스캔이 나열한 사용자 중에서 가져갑니다. 패스는 감독자(supervisor)가 포기하기 전까지 `LOOM_STRUCTURED_PROFILE_MAX_PASS_SECONDS`(600) 동안 실행될 수 있고, 그 시간의 후반부에는 새 사용자를 받지 않습니다. 진행 중인 빌드는 후반부 동안 끝낼 수 있고, 도달하지 못한 사용자는 다음 패스를 위해 목록에 남습니다. 네임스페이스를 한 번 스캔하면 자기 몫의 최대 열 배까지 나열하며, 이후 패스들은 목록이 비거나, 스키마가 바뀌거나, 목록이 `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`(600)만큼 오래될 때까지 그 목록에서 가져갑니다. 뒤처진 모든 사용자를 찾아낸(요청한 것보다 적게 찾은) 스캔은 목록이 소진된 뒤 그 시점 전에는 반복되지 않으므로, 다른 뒤처진 사용자가 없는 동안 네임스페이스는 그 기간당 최대 한 번만 스캔됩니다. 빌드가 실패한 사용자(모델 오류, JSON 객체가 아닌 응답)는 `LOOM_STRUCTURED_PROFILE_BACKOFF_BASE_SECONDS`(300) 동안 기다리며, 이는 `LOOM_STRUCTURED_PROFILE_BACKOFF_CAP_SECONDS`(21600)까지 두 배씩 늘어납니다. 기다리는 동안의 네임스페이스 스캔에서는 제외되므로, 대기가 끝난 뒤 첫 스캔에서 다시 시도되며, 이는 실패 후 최대 대기 시간에 `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS`(600)를 더한 시점입니다. 스캔이나 저장된 스키마가 실패한 네임스페이스는 집계되며, 대기가 끝나거나 스키마가 바뀔 때까지 같은 방식으로 백오프합니다. 기다리는 네임스페이스는 패스에서 자리를 차지하지 않습니다. 프로필 행이 거부한 빌드(잠김, 고정, `high`로 표시, 블랙리스트)나 모델이 출력 한도에서 답을 자른 빌드는, 사용자의 메모리, 프로필 행(고정 해제나 잠금 해제, 편집된 행 포함) 또는 스키마가 바뀌거나 `LOOM_STRUCTURED_PROFILE_REFUSAL_RETRY_SECONDS`(21600)가 지날 때까지 재시도되지 않으므로, 저절로 풀리는 잠금은 재시도됩니다. 이러한 대기와 스캔 목록은 워커의 프로세스에 보관되며 다른 프로세스가 워커 리스를 가져가면 처음부터 다시 시작합니다. 스키마가 있는 모든 네임스페이스에 도달하고 뒤처진 사용자를 찾지 못한 패스는 다음 패스 전에 `LOOM_STRUCTURED_PROFILE_IDLE_INTERVAL_SECONDS`(600)를 기다리며, 순회의 일부에만 도달한 패스는 워커의 간격으로 다시 실행됩니다.
- **삭제(Erasure).** 사실도 프로필 행도 없는 사용자에게는 빌드가 아무것도 쓰지 않으므로, 스캔이 나열한 뒤에 삭제된 사용자는 이후 패스가 도달해도 새 행이 생기지 않습니다. 또한 모델이 답한 뒤 읽었던 사실이 더 이상 현재 상태가 아니면(실행 중에 삭제됨) 빌드는 아무것도 쓰지 않습니다. 남는 것은 그 확인과 쓰기 사이의 짧은 틈입니다. 삭제 사가(saga)가 실행되는 동안 그 틈에 쓰인 행은 사가의 최종 집계가 찾아내는 유효 행이고, 사가는 이에 대해 fail closed로 동작합니다. 소스 사실에 대한 `memory.redact`는 트레이스에 그 사실이 명시된 모든 프로필 버전을 지우며, 프로필 키가 잠겨 있으면 거부됩니다. 실패 후 재시도한 redact는 같은 프로필 키에 도달합니다. 트레이스에 메모리 id가 없는 프로필 버전(소스를 기록하지 않는 빌드가 쓴 `profile.fold:<label>`만 있는 경우로, 롤링 업그레이드 중의 이전 빌드 포함)은 도달하지 못하며 사용자가 삭제될 때까지 텍스트를 유지합니다([보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).
- **범위.** 읽기는 항상 하나의 `user_id`에 대한 것입니다. 사용자에게 고정된 토큰(`read_user_id`, 최종 사용자의 토큰)은 본문에 `user_id`가 있든 없든 그 사용자의 프로필만 읽으며, 다른 사용자는 403입니다. 한 세션으로 제한된 토큰(`own_session`)은 거부됩니다(`session_id`가 없으면 `IDENTITY_INCOMPLETE`, 있으면 `AUTH_INSUFFICIENT`). 프로필은 사용자의 여러 세션에 걸치기 때문입니다.
- **모델 호출.** 모델은 워커만 호출하며, 스키마가 있는 네임스페이스에 대해서만, 모델 호출이 꺼진 조직([`org.set_model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md#org))에는 절대 호출하지 않고, 설정된 프로바이더 또는 추론 게이트웨이를 통해 호출합니다.

<a id="how-contextpack-loads-them"></a>

## `context.pack`이 이를 불러오는 방식

`context.pack`은 `working_memory`를 먼저, `profile`을 그다음에 렌더링하며, 둘 다 쿼리에 따라 달라지는 섹션보다 앞에 배치됩니다. 따라서 질문이 달라져도 팩의 시작 부분은 동일하게 유지되고, 클라이언트는 그 뒤에 프롬프트 캐시 중단점을 둘 수 있습니다(`stable_prefix_chars`, `stable_hash`). 워킹 메모리는 가장 좁은 범위부터 계층화됩니다. 실행(run)용 팩에는 세션의 블록(`run_id` `-`)도 포함되고, 세션용 팩에는 에이전트의 블록(`session_id` `-`, `run_id` `-`)도 포함됩니다. 플러그인의 "where was I" 노트(`session:<project>`)는 에이전트 계층에 있으며, `session_block`을 설정하면 다른 프로젝트의 `session:*` 노트는 제외됩니다. 섹션 순서, 예산, 그 밖의 `context.*` op는 [통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)에서 확인할 수 있습니다.

<a id="settings"></a>

## 설정

| 설정                            | 효과                                                                                                                                                        |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROFILE_CONSOLIDATION_*`  | 프로필 폴드 워커입니다 ([통합](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#consolidation)).                                                                  |
| `LOOM_STRUCTURED_PROFILE_*`     | 구조화된 프로필 워커입니다 ([구조화된 프로필](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#structured-profiles)).                                                |
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | 보호 속성이 있는 행에 태그를 붙이며, 폴드는 이러한 행을 제외합니다 ([민감도와 출처](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#sensitivity-and-provenance)). |

<a id="limits"></a>

## 제한 사항

- **한도를 초과한 쓰기는 잘리지 않고 거부됩니다.** `fill_ratio`를 확인하세요.
- **작업 메모리는 회상(recall)이 아닙니다.** 블록은 순위가 매겨지거나 임베딩되지 않습니다. `context.pack`과 render/get op를 제외하면, 실행(run) 또는 세션을 지정한 호출만 블록을 가져옵니다. `memory.ask`는 해당 run의 블록을 합성 프롬프트에 넣고, `memory.search`는 해당 run과 세션의 블록을 `scratchpad`로 반환합니다. 두 호출 모두 요청의 읽기 범위 안에서 읽습니다.
- **프로필 섹션은 레이블당 한 행입니다.** `profile.set`은 해당 행을 대체하며, 이전 버전은 [`memory.history`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory)를 통해 계속 읽을 수 있습니다.
- **render는 기본적으로 모든 에이전트를 대상으로 합니다.** `scope="this_agent"`를 지정한 경우는 예외입니다.

<a id="related"></a>

## 관련 문서

- [메모리 쓰기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/writing.md): 지속되는 사실.
- [대화와 세션](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md): 세션 요약.
- [통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md): `context.*` 및 `loom_search`.
