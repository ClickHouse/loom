<!-- i18n: source=docs/features/integrations/langfuse.md blob=6a704ef0765498c439dcf20e7c5ac032a353ecb4 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md), which is authoritative.

<a id="langfuse"></a>

# Langfuse

`LOOM_LANGFUSE_URL`과 `LOOM_LANGFUSE_ADMIN_TOKEN`이 필요하며, 두 값이 모두 설정되기 전에는 아무것도 전송되지 않습니다. 두 값이 설정되면 OpenTelemetry 스팬 트리가 OTLP를 통해 Langfuse로 전달됩니다.
Langfuse는 선택 사항이며 Loom의 로컬 Compose 스택에 포함되지 않습니다.

스팬 내보내기와 삭제는 셀프 호스팅된 **Langfuse 3.227.0**과 **Langfuse 4.76.0**에서 테스트되었습니다. Langfuse Cloud는 Langfuse 4를 실행합니다. 로컬 Langfuse 배포에는 PostgreSQL, ClickHouse, Redis/Valkey, S3 호환 오브젝트 스토리지도 함께 실행되므로, 두 서비스로 구성된 Loom 프로필보다 훨씬 큽니다.

Loom은 자체 텔레메트리를 ClickHouse에 저장합니다([텔레메트리](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md)).
Langfuse는 요청마다 중첩된 모델 호출이 포함된 트레이스, 생성별 비용, 필터링 가능한 점수, 프롬프트 버전을 추가합니다. Loom은 Langfuse와 관련해 네 가지 역할을 합니다.

| 역할            | Loom이 하는 일                                                                                    | 필요한 것                     |
| --------------- | ------------------------------------------------------------------------------------------------- | ----------------------------- |
| 트레이스 기록자 | 각 요청의 OTel 스팬 트리를 생성(generation)과 함께 OTLP로 전달                                    | `pk:sk` 토큰, OTel 활성화     |
| 점수 기록자     | 각 검색과 질문(ask)의 트레이스에 검색 품질 점수를 기록                                            | `pk:sk` 토큰                  |
| 프롬프트 연결자 | 각 생성에 사용된 Langfuse 프롬프트 이름과 버전을 커밋된 lockfile에서 가져와 태그로 지정           | lockfile, 런타임 호출 없음    |
| 삭제 백엔드     | 삭제된 사용자 또는 네임스페이스의 트레이스를 삭제                                                 | URL과 토큰(형식 무관)         |

Loom은 프롬프트를 가져오는 경로에 **포함되지 않습니다**. Langfuse에서 자체적으로 프롬프트를 관리하는 에이전트는 Langfuse SDK의 `get_prompt`로 프롬프트를 가져옵니다.

<a id="setup"></a>

## 설정

```bash
LOOM_LANGFUSE_URL=https://langfuse.example.com
LOOM_LANGFUSE_ADMIN_TOKEN=pk-lf-...:sk-lf-...
```

토큰은 `public_key:secret_key` 쌍이며 HTTP Basic으로 전송됩니다. 단일 bearer 토큰(`:` 없음)도 삭제(erasure)에는 계속 사용할 수 있지만, 트레이스와 점수 전송은 비활성화됩니다. 이 경우 서버는 이를 알리는 경고를 한 번 로그에 남깁니다.

`GET /v1/health?include=langfuse`로 결과를 확인하세요. 이 요청은 로컬 exporter 상태를 보고하며, `scores`와 `retrieval_judge` 아래에 점수가 몇 개나 게시되었는지, 실패했는지, 버려졌는지(큐 가득 참, 브레이커 열림)를 보여줍니다. 원격 Langfuse의 준비 상태를 확인하는 프로브는 아닙니다. 두 설정이 모두 있으면 부팅 로그의 `mode` 토큰에 `langfuse`가 포함됩니다.

<a id="traces"></a>

## 트레이스

Loom은 ClickHouse 익스포터 옆에 두 번째 span 프로세서를 연결하여 OpenTelemetry span 트리를 Langfuse의 OTLP 엔드포인트로 전달합니다. 트레이스에는 실제 중첩 구조(HTTP span, 핸들러, 각 generation), 세션 id, generation별 비용이 담깁니다.
채팅 호출은 `GENERATION` 관찰이고, 프로바이더에 도달한 임베딩 호출은 `EMBEDDING`입니다. Loom 자체 캐시에서 처리된 임베딩은 일반 span입니다. 호출자가 확인된 이후 요청이 여는 모든 span에는 요청의 사용자 id, 세션 id, `org:` 및 `namespace:` 태그, 트레이스 이름이 포함됩니다. Langfuse 4는 이 값들을 트레이스 루트가 아니라 관찰마다 읽기 때문입니다. 이 키는 ClickStack의 span에도 전달됩니다.
각 검색 단계(`search.<stage>`)는 `retriever` 관찰이며, Langfuse의 에이전트 그래프 보기를 구성하는 유형 중 하나입니다. OTel을 비활성화하면(`LOOM_OTEL_ENABLED=false`) Langfuse로 트레이스가 전달되지 않습니다.

| 설정                                                                          | 기본값                                              | 효과                                                                                                     |
| ----------------------------------------------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `LOOM_LANGFUSE_SAMPLE_RATE`                                                   | `1.0`                                               | 전달되는 트레이스의 비율이며, 트레이스가 온전히 도착하도록 트레이스 id 기준으로 샘플링합니다            |
| `LOOM_LANGFUSE_SPAN_DENYLIST`                                                 | `ch.query,ch.insert,GET /v1/health,POST /v1/health` | 전달하지 않는 span 이름 접두사(인프라 span은 ClickStack에 두는 것이 맞습니다)                            |
| `LOOM_LANGFUSE_CIRCUIT_FAILURE_THRESHOLD`, `LOOM_LANGFUSE_CIRCUIT_COOLDOWN_S` | `5`, `30`                                           | 차단기: 점수 POST가 N번 연속 실패하면 쿨다운 동안 점수를 버리고, 이후 한 번 시험 요청을 보냅니다         |

Langfuse는 Loom의 요청 경로에 놓이지 않습니다. 차단기는 실패한 점수 POST가 재시도 부담으로 번지는 것을 막습니다. OTLP 배치 프로세서는 자체 전달 큐를 가지므로 span 내보내기 실패는 차단기를 열지 **않습니다**. 따라서 `loom.langfuse.breaker` 게이지(0 닫힘, 1 반열림, 2 열림)는 원격 서비스의 가용성이 아니라 점수 전송 상태를 나타냅니다. 워커 비용 트레이스의 이름은 `worker.<name>`이며 워커의 org와 네임스페이스가 함께 기록됩니다. 트레이스 루트인 임베딩 span(임베딩 백필)과 워커 패스 루트는 전달하지 않습니다. 요청 경로의 임베딩은 핸들러 span 아래에 중첩되므로 유지됩니다.

> **팁:** span 양은 워커 임베딩이 대부분을 차지합니다. Langfuse를 설정하기 전에 프로덕션에서 `LOOM_LANGFUSE_SAMPLE_RATE`를 낮추세요.

<a id="what-text-leaves-loom"></a>

## Loom에서 나가는 텍스트

Langfuse에 전달되는 사용자 콘텐츠의 범위는 다음과 같습니다.

- **요청 및 응답 페이로드.** 전송되지 않습니다. 스팬에는 요청 본문이나 응답 본문이 포함되지 않습니다.
- **Loom 자체 모델 호출의 프롬프트 및 컴플리션 텍스트.** 프롬프트에 담긴 내용에 따라 달라집니다.

  | `LOOM_LANGFUSE_CAPTURE_IO_LEVEL` | 기록 대상                                                                                                             |
  | -------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
  | `off` (기본값)                   | 없음                                                                                                                  |
  | `query`                          | 프롬프트가 사용자의 검색 텍스트 그대로인 호출만 (HyDE, 쿼리 재작성, 보강 등)                                          |
  | `all`                            | 검색된 메모리나 대화 턴이 포함된 호출도 포함 (rerank, iterative judge, extraction, session summary 등)                |

  네임스페이스의 `capture_generation_io`(`namespace.register`로 설정)는 이 값을 재정의합니다. `false`는 서버 설정과 관계없이 off, `true`는 `all`을 의미하며, 설정하지 않으면 서버 값을 상속합니다. 각 필드는 `LOOM_LANGFUSE_GENERATION_IO_MAX_CHARS`(2000)로 길이가 제한되며, 잘린 경우 명시적인 잘림 표시가 붙습니다. 같은 수준이 `loom.search_trace_log.query`에도 적용됩니다.

토큰 수, 비용, 지연 시간, 품질 점수, 프롬프트 버전 링크에는 사용자 콘텐츠가 없으므로 제한을 받지 않습니다.

<a id="scores"></a>

## 점수

`server/observability/langfuse_scores.py`는 각 검색의 트레이스에 검색 품질 점수를 게시합니다: `retrieval_confidence`, `top_similarity`, `abstained`, `abstain_reason`.
`code.ask`는 `code_ask_cache_hit`과 `code_ask_coverage`를 추가합니다. `LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`가 0보다 크면(기본값은 0이며, 샘플링된 검색마다 모델 호출 비용이 듭니다), 검색과 상태를 공유하지 않는 독립적인 LLM 판정기가 `retrieval_relevance` 점수를 기록합니다.

점수 게시는 요청 경로 밖에서 이루어집니다. 크기가 제한된 큐를 백그라운드에서 비우며, Langfuse가 느려지면 가장 오래된 항목부터 버리고 버려진 수를 집계합니다. 점수는 해당 트레이스가 전달된 경우에만(같은 샘플링 게이트) 게시되며, 같은 브레이커를 거칩니다.

<a id="prompt-versions"></a>

## 프롬프트 버전

Loom의 시스템 프롬프트는 Python 상수입니다. `scripts/codegen/push_langfuse_prompts.py`는 이 프롬프트를 Langfuse에 등록하고, `sha256(prompt)[:12]`를 키로 하는 `server/observability/langfuse_prompts.lock.json`을 작성합니다. 런타임에 `server/observability/genai.py`는 전송한 프롬프트를 이 락파일에서 찾아, Langfuse를 호출하지 않고 해당 generation에 Langfuse 프롬프트 이름과 정수 버전을 태그로 붙입니다. 수정한 뒤 푸시하지 않은 프롬프트는 잘못된 버전에 연결되지 않고 연결 없이 그대로 남습니다.

```bash
python scripts/codegen/push_langfuse_prompts.py          # plan against the lockfile, offline
python scripts/codegen/push_langfuse_prompts.py --write  # push, then write the lockfile
```

`--write`에는 두 가지 Langfuse 설정이 모두 필요합니다. 텍스트가 이미 최신 버전과 같은 프롬프트는 다시 푸시되지 않습니다.

`POST /v1/admin/prompts.evaluate`(플랫폼 관리자용)는 프롬프트 버전 하나에 대해, 해당 트레이스의 루브릭 점수를 최소 개수 및 임계값과 비교하여 승격 여부 판정을 반환합니다. 프롬프트 버전에 연결된 각 generation은 자신의 트레이스에 `prompt:<name>:<version>` 태그를 붙이며, 평가는 Langfuse 3과 4 모두에서 이 태그가 달린 트레이스를 최대 200개까지 읽고 그 점수를 가져옵니다. Langfuse에는 아무것도 기록하지 않으며, Langfuse가 설정되지 않았으면 501 `INTEGRATION_NOT_CONFIGURED`를, Langfuse를 읽을 수 없으면 502 `BACKEND_UNAVAILABLE`을 응답합니다.

<a id="erasure"></a>

## 삭제

`erasure.request`는 saga의 한 단계로 Langfuse 백엔드(`server/erasure/app/backends.py`의 `LangfuseBackend`)를 실행합니다.

- **사용자 범위**는 `userId`와 `org:<org>` 태그로 필터링해 사용자의 트레이스를 찾은 다음, `DELETE /api/public/traces`로 배치 단위로 삭제합니다. 트레이스를 삭제하면 해당 트레이스의 observation과 score도 함께 제거됩니다. 모든 org가 키 쌍이 가리키는 하나의 Langfuse 프로젝트에 기록하고 `user_id`는 org 안에서만 고유하므로, org 필터가 중요합니다.
- **네임스페이스 범위**는 `namespace:<ns>`와 `org:<org>`를 모두 가진 트레이스를 삭제합니다.

조회 방식은 `GET /api/public/health`로 읽은 서버의 메이저 버전에 따라 달라집니다. Langfuse 3은 트레이스 목록(`GET /api/public/traces`)을 조회합니다. Langfuse 4는 해당 목록을 더 이상 제공하지 않으므로 observation 목록(`GET /api/public/v2/observations`)을 조회해 트레이스 id를 수집합니다. Langfuse 4는 삭제를 비동기로 처리하므로, 삭제된 트레이스가 몇 분 동안 계속 보일 수 있습니다. 서버 버전은 삭제 단계마다 한 번 읽습니다.

Langfuse 3에서 업그레이드한 배포에서는 Langfuse 4 조회가 observation 목록에 있는 항목만 찾으며, 삭제 결과에는 그 외의 항목이 보고되지 않습니다. Langfuse 3.227.0이 저장한 트레이스는 목록에 있습니다(4.76.0으로 in-place 업그레이드하여 테스트했습니다). Loom이 `x-langfuse-ingestion-version: 4` 헤더를 보내고, 해당 릴리스가 Langfuse 4가 읽는 위치에 트레이스를 기록하기 때문입니다. 그 이전 Langfuse 3 릴리스가 저장한 트레이스는 업그레이드 후 삭제 기능에 의존하기 전에 Langfuse의 historic backfill을 실행하세요.

이 단계는 fail closed로 동작합니다. 버전이 없는 health 응답, 5xx, 타임아웃, 또는 삭제 엔드포인트의 404(오래된 self-hosted 빌드이거나 GET과 POST만 전달하는 프록시)가 발생하면 예외가 발생하고, saga는 요청을 `failed`로 기록하며, `erasure_recovery` 워커가 재시도합니다. 이미 삭제된 트레이스를 다시 삭제해도 성공하므로 재실행은 멱등합니다. Langfuse 4에서는 아직 삭제 대기 중인 트레이스를 다시 찾아 삭제할 수 있습니다. Langfuse가 설정되지 않은 경우 스텁 백엔드가 의도를 로그로 남기고 0을 보고합니다. `LOOM_EXTERNAL_ERASURE_TIMEOUT_S`(30)가 각 호출의 제한 시간을 정합니다. [보존 및 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)를 참고하세요.

<a id="joining-a-request"></a>

## 요청 연결하기

Loom이 `X-Request-Id`로 반환하는 요청 id는 Langfuse 트레이스 메타데이터, `loom.agent_events.request_id`, 그리고 `loom.request_id` 스팬 속성에 들어 있으므로, Langfuse에서 Loom의 원장까지 요청을 따라갈 수 있습니다. 자세한 내용은 [시스템 간 요청 연결하기](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/index.md#joining-a-request-across-systems)를 참고하세요.

<a id="related"></a>

## 관련 문서

- [텔레메트리](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md): 동일한 스팬을 ClickHouse 측에서 다룹니다.
- [ClickStack](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md).
- 모든 `LOOM_LANGFUSE_*` 설정은 [구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#observability)를 참고하세요.
