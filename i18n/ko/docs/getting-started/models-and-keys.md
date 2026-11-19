<!-- i18n: source=docs/getting-started/models-and-keys.md blob=52104cd19c972224d162911f3102d1fff28bd3b8 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md), which is authoritative.

<a id="configure-models-and-api-keys"></a>

# 모델과 API 키 설정

코딩 에이전트, Loom의 내부 LLM, Loom의 임베딩 모델은 서로 별개의 선택 사항입니다.
OpenAI 모델을 사용하는 Loom 서버에 Claude Code를 연결할 수도 있고, Claude를 사용하는
서버에 Codex를 연결할 수도 있습니다. 에이전트가 Loom과 같은 제공자를 사용할 필요는 없습니다.

| 선택 사항                                        | 역할                                                                   | 설정 위치                                                  |
| ------------------------------------------------ | ---------------------------------------------------------------------- | ---------------------------------------------------------- |
| 에이전트: Claude Code, Codex 또는 다른 클라이언트 | 코딩 또는 어시스턴트 워크플로를 실행하고 Loom의 도구를 호출합니다.     | 해당 클라이언트에서 설정하며, Loom 토큰으로 연결합니다.    |
| Loom의 LLM                                       | Loom 내부에서 사실을 추출하고, 세션을 요약하고, 답변을 생성합니다.     | 서버의 제공자, 모델, API 키 설정.                          |
| Loom의 임베딩 모델                               | 메모리, 문서, 코드를 의미 기반으로 검색할 수 있게 만듭니다.            | 서버의 임베딩 모델, 차원, 제공자 키 설정.                  |

`LOOM_API_KEY`는 **Loom**에 접근하기 위한 토큰입니다. 서버의 모델 호출 비용을 지불하는
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`와는 다릅니다. 제공자 키는
서버에만 보관하세요. 에이전트를 연결해도 에이전트의 구독 자격 증명이나 API 키가 Loom으로
전달되지 않습니다.

<a id="what-works-without-provider-keys"></a>

## 프로바이더 키 없이 무엇이 동작하나요?

릴리스된 Docker 설치본은 `local`(bge-small, 384차원, 이미지에 포함된 파일을 사용해 CPU에서 실행)로 임베딩하고, 설치 프로그램이 로컬 모델 서버를 설정하지 않았다면 `fake-echo-1` LLM을 선택합니다([내 머신의 모델 서버](#a-model-server-on-your-machine-no-api-key) 참고). 프로바이더 키나 네트워크 없이도 부팅되며, 스토리지, 대시보드, 구조적 코드 인덱싱, 시맨틱 검색이 동작합니다. 다만 표현을 바꾼 질문이 많은 경우에는 호스팅 임베더가 더 잘 검색합니다. fake LLM은 의미 있는 추출 사실, 요약, 생성된 답변을 만들지 못합니다. 체크아웃의 `.env`는 문자 겹침으로 매칭하는 `fake-ngram-3072` 임베딩을 선택합니다. 업그레이드된 설치본은 `~/.loom/.env`에 지정된 임베더를 그대로 유지합니다. 이를 `local`로 바꾸는 것은 다른 모델 변경과 마찬가지로 모델 변경이며, 저장된 내용을 다시 임베딩합니다.

표현을 바꾼 내용이 많은 콘텐츠에서 검색 품질을 높이려면 호스팅 임베딩 프로바이더를 설정하세요. 사실 자동 추출, 요약, 생성된 답변이 필요하다면 LLM도 설정하세요. LLM만으로는 임베딩 모델을 대체할 수 없고, 임베딩 모델만으로는 답변을 생성할 수 없습니다. 임베딩을 설정하지 않으면 체크아웃의 서버는 fake 임베더를 사용하며, 임베딩을 건너뛰지는 않습니다.

일부 호스팅 프로바이더도 키가 없으면 fake로 대체됩니다. `/v1/health`의 `fake_embedding`, `fake_llm`, `providers_degraded`, `problems`를 확인한 다음, 테스트용 사실을 저장하고 검색해 보고 답변도 시도해 보세요. 잘못된 키나 접근할 수 없는 모델 때문에 프로바이더 호출이 여전히 실패할 수 있으므로, 서버가 실행 중이라고 해서 모델 접근이 정상이라는 증거는 아닙니다. 환경 변수 기반 배포에서 `LOOM_REQUIRE_REAL_PROVIDERS=true`를 사용하면 fake 또는 키 없는 프로바이더를 degraded 상태 조건으로 처리합니다.

<a id="choose-a-setup"></a>

## 설정 선택하기

| 조직이 가진 것                                 | Loom LLM                  | 임베딩                                                 |
| ---------------------------------------------- | ------------------------- | ------------------------------------------------------ |
| OpenAI API 접근 권한                           | 사용 가능한 OpenAI 모델   | OpenAI. API 키 하나로 두 역할을 모두 처리할 수 있습니다. |
| Anthropic과 승인된 임베딩 제공자               | 사용 가능한 Claude 모델   | OpenAI, Gemini 또는 호환 엔드포인트와 해당 키.         |
| Anthropic API 접근 권한만 있음                 | 사용 가능한 Claude 모델   | 임베딩 제공자와 해당 API 키를 추가합니다.              |
| 제공자 키 없음, 머신에서 모델 서버 실행 중     | 로컬 모델 (`local`)       | 이미지에 포함된 `local` 임베더.                        |

**OpenAI API 키 하나로 두 역할을 모두 처리할 수 있으며, Anthropic 키는 필요하지 않습니다.**
Models & keys에서 메인 LLM을 하나 선택하고 작업별 재정의는 비워 두십시오. 검증과 판정을 포함해 활성화된
모든 LLM 작업이 해당 모델을 사용합니다. 고급 재정의는 선택 사항이며, 다른 제공자를 명시적으로 선택하는 경우에만
해당 제공자의 키가 필요합니다.

OpenAI, Google Gemini 또는 **OpenAI 호환 임베딩 엔드포인트**를 선택하십시오. OpenAI 임베딩에는
GPT 채팅 모델이 아니라 `text-embedding-3-large` 같은 임베딩 모델을 사용합니다.
Claude는 Loom의 LLM 작업을 처리할 수 있지만, 별도의 임베딩 제공자와 키가 필요합니다.
Anthropic은 자체 임베딩 모델을 제공하지 않으며, Voyage는 Loom에 내장된 제공자가 아닙니다.
[Anthropic의 임베딩 가이드](https://platform.claude.com/docs/en/build-with-claude/embeddings)

Loom의 내부 모델 작업은 코딩 에이전트 CLI 로그인이나 구독 자격 증명이 아니라 제공자 API 키를 사용합니다.
외부 에이전트가 어떤 제공자를 선택하는지는 이와 무관합니다.

<a id="set-the-server-configuration"></a>

## 서버 구성 설정

<a id="recommended-use-the-dashboard"></a>

### 권장: 대시보드 사용

플랫폼 관리자로서 **Settings → Models & keys**를 엽니다(Setup 중에도 사용할 수 있습니다).
LLM 및 임베딩 제공자를 선택하고, 모델 ID와 API 키를 입력한 다음
**Test & apply**를 클릭합니다. 활성화된 모든 작업에 하나의 LLM을 사용하려면 작업별 재정의를 비워 둡니다.
저장된 대시보드 설정은 환경 변수보다 우선하며 ClickHouse에 암호화되어 저장됩니다.
**Docker 재시작이나 Compose 명령은 필요하지 않습니다.** 키와 LLM 변경은 즉시 적용되며,
임베딩 변경은 아래에 설명된 재구축을 시작합니다. **Use environment
configuration**은 이러한 재정의를 제거하고 이 서버의 시작 설정을 복원합니다.
편집한 `.env` 파일을 다시 로드하지는 않습니다. 임베딩 공간을 변경하려면 재구축이 필요합니다.

대시보드의 임베딩 선택지는 호스팅 제공자입니다. 배포된 스택의
`local` 임베더를 유지하면서 실제 LLM을 추가하려면, `~/.loom/.env`에 LLM 관련 줄만 설정하고
([LLM에 Claude 사용](#claude-for-the-llm)에 나와 있습니다) 컨테이너를 다시 만드십시오.
임베딩 공간은 그대로이므로 다시 임베딩되는 항목은 없습니다.

대시보드에서 임베딩을 변경하면 Loom이 해당 재구축을 자동으로 큐에 넣고,
검증, 게시, 정리가 끝날 때까지 데이터 플레인 요청을 일시 중지한 상태로 유지합니다.
Models & keys에 단계와 진행 상황이 표시되며, 실패한 작업은 일시 중지된 채로 남아
저장된 커서부터 재시도할 수 있습니다. 컨테이너를 다시 만들 때는 ClickHouse 볼륨과
저장된 제공자 키를 봉인하는 원래의 `LOOM_JWT_SECRET`을 유지하십시오. 재구축은 제공자를 호출하므로
시간이 걸릴 수 있으며, 색인된 콘텐츠에 비례하여 요금이 발생할 수 있습니다. 재구축은 로컬 MergeTree 테이블에만
게시됩니다. 저장된 벡터가 없는 설치(예: 아직 아무것도 저장하지 않은 새 설치)에서의 첫 임베딩 선택은
재구축 없이 바로 전환됩니다. 복제 또는 공유 스토리지(예: ClickHouse Cloud)에 벡터가 존재하게 되면
Models & keys는 임베딩 필드를 잠그고, 서버는 아무것도 일시 중지하기 전에 임베딩 변경을 거부합니다.
그 경우 환경 설정에서 임베딩 모델을 변경하고 위에 설명된 대로 각 색인을 다시 임베딩하십시오. 원격 프로세스 손실 후 복구는 여전히 문서화된 제한 사항이며,
[제공자 재구성](../architecture/provider-reconfiguration.md)에서 다룹니다.

토큰 설정은 별개입니다. 이는 모델 제공자 자격 증명이 아니라 에이전트가 Loom에 접근하는 방식을 구성합니다.

<a id="alternative-configure-the-environment"></a>

### 대안: 환경 구성

자동화된 배포에서는 `~/.loom/.env`(또는 `LOOM_INSTALL_DIR`로 선택한 디렉터리)를 편집합니다.
소스 체크아웃에서는 저장소의 `.env`를 편집합니다.
키를 중복해서 추가하지 말고 기존 항목을 교체하십시오. 이 설정은 시작 기준값이며,
저장된 대시보드 재정의가 우선합니다. 환경 변수를 편집한 후에는 아래에 설명된 대로
컨테이너를 다시 만들거나 네이티브 서버를 재시작해야 합니다.

<a id="openai-for-both-roles"></a>

### 두 역할 모두에 OpenAI 사용

```dotenv
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=<your OpenAI API key>
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_REQUIRE_REAL_PROVIDERS=true
```

예시는 Loom에 포함된 기본 모델을 사용합니다. API 계정에서 활성화된 모델을 선택하십시오.
키만 설정해서는 기본 설치 프로그램 구성이 바뀌지 않습니다. 이 구성은
제공자를 명시적으로 지정합니다(로컬 임베더와 가짜 LLM).

<a id="claude-for-the-llm"></a>

### LLM에 Claude 사용

```dotenv
LOOM_LLM_PROVIDER=anthropic
LOOM_LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=<your Anthropic API key>
LOOM_REQUIRE_REAL_PROVIDERS=true
```

그런 다음 임베딩 구성을 선택합니다.

```dotenv
# OpenAI embeddings
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
OPENAI_API_KEY=<your OpenAI API key>
```

```dotenv
# Or Gemini embeddings
LOOM_EMBEDDING_PROVIDER=gemini
LOOM_EMBEDDING_MODEL=gemini-embedding-001
LOOM_EMBEDDING_DIM=3072
GEMINI_API_KEY=<your Gemini API key>
```

OpenAI 임베딩 API를 구현한 다른 서비스를 사용하려면 API 루트, 모델 ID,
기본 출력 차원을 구성합니다.

```dotenv
LOOM_EMBEDDING_PROVIDER=compatible
LOOM_EMBEDDING_BASE_URL=https://embedding-provider.example/v1
LOOM_EMBEDDING_MODEL=your-embedding-model
LOOM_EMBEDDING_DIM=768
LOOM_EMBEDDING_API_KEY=<your embedding provider API key>
# Enable only if the endpoint accepts the optional dimensions parameter:
LOOM_EMBEDDING_SEND_DIMENSIONS=false
```

기본 URL은 API 루트이며, Loom이 `/embeddings`를 덧붙입니다. 원격 서비스에는 HTTPS를 사용하십시오.
비공개 자체 호스팅 엔드포인트에는 HTTP를 사용할 수 있지만, 키와 콘텐츠가 암호화되지 않은 채 전송됩니다.
호환되려면 구성된 차원의 인덱스된 float 배열 응답이 필요하며, 다른 API를 쓰는 제공자는
별도의 어댑터가 필요합니다. 엔드포인트, 모델 또는 차원을 변경하면 기존 임베딩을 다시 구축해야 합니다.

조직에 현재 Anthropic API 접근 권한만 있다면, 시맨틱 검색을 활성화하기 전에
임베딩 제공자에 대한 접근 권한을 확보하십시오.

<a id="keep-every-llm-job-on-your-chosen-provider"></a>

### 모든 LLM 작업을 선택한 제공자에서 유지

Loom은 작업마다 다른 모델을 지원하지만, 여러 LLM 제공자의 계정이 필요하지는 않습니다.
보통 제공자별로 지정되는 작업이 선택한 모델을 사용하게 하려면, `LOOM_LLM_MODEL` **뒤에** 다음 설정을 추가하십시오.

```dotenv
LOOM_MEMORY_EXTRACT_VERIFY_MODELS=${LOOM_LLM_MODEL}
LOOM_SESSION_BRIEF_LLM_MODEL=
LOOM_CODE_BISECT_LLM_RERANK_MODEL=
LOOM_TRACKER_DUPCHECK_RERANK_MODEL=
LOOM_RETRIEVAL_JUDGE_MODEL=
```

Compose와 Loom의 dotenv 로더는 앞선 항목에서 검증기의 모델을 확장합니다.
위의 빈 값은 메인 LLM을 상속합니다. 이렇게 하면 캡처 검증기가 활성화된 상태로 유지됩니다.
검증기 목록이 비어 있으면 비활성화됩니다. 이전에 추가한 다른 교차 제공자 모델 재정의가 있다면
폴백, 합성, 리랭킹 모델을 포함해 모두 제거하십시오.

이 설정은 중요합니다. 일부 기본 고급 설정은 Claude 또는 OpenAI 모델을 명시적으로 지정하며,
제공자 키가 없는 재정의는 가짜 클라이언트로 폴백될 수 있습니다.
작업 전반에 하나의 모델을 사용하는 것은 편의를 위한 구성이며, 공개된 모든 벤치마크의 기반 구성이 아닙니다.
이후 개별 작업은 [작업별 모델](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#models-per-job)을 참고하여 조정하십시오.

<a id="a-model-server-on-your-machine-no-api-key"></a>

### 내 머신의 모델 서버, API 키 없음

`LOOM_LLM_PROVIDER=local`은 사용자가 실행하는 chat-completions 서버에서 Loom의 LLM 작업을 실행합니다.
[올라마(Ollama)](https://ollama.com), llama.cpp의 `llama-server` 또는 OpenAI chat-completions 형식을 따르는
모든 서버를 사용할 수 있습니다. Loom은 키를 보내지 않습니다. 배포된 스택의 `local`
임베더와 함께 쓰면 어떤 데이터도 머신 밖으로 나가지 않습니다.

터미널에서 실행한 새 설치는 Ollama가 `localhost:11434`에서 응답하면 이 옵션을 제안합니다.
`sh loom-install.sh` 앞에 `LOOM_LOCAL_MODELS=ollama`를 붙이면 새 설치든 기존 설치든 묻지 않고 설정하며,
`LOOM_LOCAL_MODELS=off`는 제안하지 않습니다. 다음 내용이 기록됩니다.

```dotenv
LOOM_LLM_PROVIDER=local
LOOM_LLM_MODEL=qwen2.5:3b
LOOM_LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
LOOM_ASSISTANT_PROVIDER=local
LOOM_ASSISTANT_MODEL=qwen2.5:3b
LOOM_ASSISTANT_ANSWER_CEILING_USD=0
LOOM_MEMORY_EXTRACT_VERIFY_MODELS=
LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=false
LOOM_LLM_REQUEST_TIMEOUT_S=900
LOOM_LLM_RETRY_ATTEMPTS=1
LOOM_EXTRACTION_QUEUE_MAX_BATCHES_PER_PASS=1
LOOM_EXTRACTION_QUEUE_MAX_PASS_SECONDS=3600
LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS=2
LOOM_ENTITY_EXTRACT_ENABLED=false
LOOM_SESSION_BRIEF_LLM_MODEL=
LOOM_CODE_BISECT_LLM_RERANK_MODEL=
LOOM_TRACKER_DUPCHECK_RERANK_MODEL=
LOOM_RETRIEVAL_JUDGE_MODEL=
```

이 프리셋은 모델의 판단에 따라 사실을 제거하는 두 작업을 끕니다. 두 작업의 임계값은 호스팅 모델 기준으로 설정되어 있습니다.
하나는 모든 검증기 모델이 지속성이 없다고 판단한 사실을 버리는 캡처 검증기이고, 다른 하나는
더 오래된 행이 대체되었다고 판단하면 그 행을 폐기하는 백그라운드
[충돌 해결기](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#the-conflict-resolver)입니다.
작은 모델이 여기서 잘못 판단하면 사실이 손실됩니다. 쓰기 시 충돌은 여전히 Review 큐에 표시되며,
이를 판단하게 하려면 `LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=true`로 설정하고
`LOOM_CONFLICT_RESOLVER_MODEL`(`gpt-6.1-sol`, OpenAI)의 키를 설정하십시오. 기존 설치에서는 Models & keys에 저장된
구성이 `.env`보다 우선합니다. 재시작 후 프리셋을 적용하려면 거기서
**Use environment configuration**을 선택하십시오.

먼저 모델을 받고(`ollama pull qwen2.5:3b`, 약 2 GB), Loom의 프롬프트를 담을 수 있는 컨텍스트
윈도우를 Ollama에 지정하십시오. 추출은 약 15,000 토큰을 전송하며, Ollama는 더 긴 프롬프트를 오류 없이
윈도우 크기로 잘라냅니다. 따라서 `OLLAMA_CONTEXT_LENGTH=32768`(`qwen2.5`가 지원하는 윈도우)로 실행하십시오.
CPU는 그만큼 긴 프롬프트를 읽는 데 몇 분이 걸릴 수 있으므로, 프리셋은 호스팅 기본값인 300초 대신
`LOOM_LLM_REQUEST_TIMEOUT_S=900`초 동안 응답을 기다리며 한 번만 시도합니다
(`LOOM_LLM_RETRY_ATTEMPTS=1`). 시간 초과된 서버는 두 번째에도 더 빨리 응답하지 않기 때문입니다.

코딩 플러그인이 보내는 캡처를 재생하는 워커는 각 패스에 마감 시간을 둡니다. 그래서 프리셋은 지연된 추출 패스가
한 배치에 한 시간까지 실행되도록 합니다(`LOOM_EXTRACTION_QUEUE_MAX_PASS_SECONDS`, 기본값은 간격의 네 배이며
최소 60초, 최대 900초이고, 배치의 임대는 최소 1분 더 길게 유지됩니다). `local` 제공자에서는 (그 마감 시간이나
Loom 종료로) 중간에 끊긴 캡처, 또는 호출이 시간 제한을 다 소진한 캡처가
`LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS=2` 시도 중 하나를 소모합니다. 따라서 머신에 비해 너무 긴 세션은
큐를 붙잡고 있지 않고 `failed`로 끝납니다(`memory.extraction_status`에 `cancelled` 또는 시간 초과가 표시됩니다).
긴 세션은 연속으로 여러 번의 호출이 필요할 수 있으며, 각 호출은 최대 시간 제한까지 걸립니다. 한 번에 하나의 요청만
처리하는 서버에서는 시간 제한에 다른 호출 뒤에서 기다리는 시간도 포함되며, 어시스턴트와 `memory.ask`가 서버를 공유하므로
캡처 중에 이들을 많이 사용하면 캡처가 실패할 수도 있습니다. 그런 경우 `LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS`를 올리십시오.
엔티티 태깅(`LOOM_ENTITY_EXTRACT_ENABLED`)은 꺼져 있습니다. 한 번에 하나의 요청만 처리하는 서버에서는 추출 뒤에 대기하게 되고,
마감 시간에 끊긴 패스는 아무것도 쓰지 않으므로 검색은 엔티티 일치 없이 동작합니다. 모델을 호출하는 다른 워커는
로컬 모델에 맞게 조정되어 있지 않습니다. 기본적으로 꺼져 있는 observe 추출은 최대 10개 세션의 패스가 900초에 끊기고
가장 최근 세션부터 다시 시작하며, 대화 제목과 구조화된 프로필은 대화 로깅이나 프로필 스키마를 설정하면 실행됩니다.

llama.cpp를 사용하려면 `LOOM_LOCAL_LLM_BASE_URL`을 해당 `/v1` 루트
(`http://host.docker.internal:8080/v1`)로 지정하고 `LOOM_LLM_MODEL`을 서버가 제공하는 이름으로 설정하십시오.
로컬 어시스턴트를 쓰려면 `LIBRECHAT_LOCAL_LLM_BASE_URL`도 그 루트로, `LOOM_ASSISTANT_MODEL`도 그 이름으로 설정하십시오.
체크아웃에서 실행한 서버는 설정의 기본값인 `localhost`(`http://localhost:11434/v1`)에서 모델에 접근합니다.

이름이 `gpt`, `o1`, `o3`, `o4`, `chatgpt` 또는 `claude`로 시작하는 작업별 모델은
`LOOM_LLM_PROVIDER`의 값과 관계없이 해당 호스팅 제공자로 전달됩니다. 그렇게 명명된 로컬 모델(`gpt-oss`)은
`LOOM_LLM_MODEL`로는 로컬에서 실행되고, 작업별 설정에서는 그 설정이 같은 모델을 지정할 때만 로컬에서 실행됩니다.

- **호스트에 접근하기.** 컨테이너는 머신을 `host.docker.internal`로 접근합니다. Docker
  Desktop은 이를 루프백의 서비스로 전달합니다. Linux의 Docker Engine에서는 Ollama가 루프백 밖에서도
  수신 대기해야 합니다(`OLLAMA_HOST=0.0.0.0`, 포트 11434는 네트워크에 닫아 둡니다).
  `networkPolicy.enabled`가 설정된 Helm 차트에서는 서버의 포트를 `networkPolicy.extraEgress`에 추가하십시오.
- **어시스턴트.** `LOOM_ASSISTANT_PROVIDER=local`은 어시스턴트의 에이전트를 LibreChat의 `local` 엔드포인트로
  옮기며, 이 엔드포인트는 `LIBRECHAT_LOCAL_LLM_BASE_URL`(기본값
  `http://host.docker.internal:11434/v1`)을 호출하고 `LOOM_ASSISTANT_MODEL`이 필요합니다. 어시스턴트는
  Loom의 도구를 호출해 답하므로, 서버가 도구 호출과 함께 실행하는 모델을 고르십시오(Ollama가 표시해 줍니다).
  로컬 모델에는 가격이 없으므로, 가격이 없는 모든 모델과 마찬가지로 각 답변은 네임스페이스의 일일 모델 비용 상한 중
  어시스턴트 몫에서 `LOOM_ASSISTANT_ANSWER_CEILING_USD`(기본값 $0.10)만큼으로 계산됩니다. 기본 상한과 몫에서는
  하루 다섯 번의 답변입니다. 프리셋은 이 상한을 `0`으로 설정하므로 로컬 답변은 비용이 들지 않습니다. 이 값은
  사용량이 도착하기 전에 끊긴 답변의 비용이기도 하므로, 호스팅 모델이 어시스턴트를 담당할 때는 기본값을 유지하십시오.
  Helm 차트에서는 어시스턴트의 Secret에 `LOCAL_LLM_BASE_URL`을 추가하십시오. 차트의 정책은 LibreChat이
  DNS, 자체 데이터베이스, Loom, TCP 443에만 접근하게 하므로, 모델 서버에는 LibreChat 파드를 선택하는
  별도의 NetworkPolicy가 필요합니다.
- **품질과 속도.** Loom의 프롬프트와 임계값은 호스팅 모델 기준으로 조정되어 있고, 로컬 모델을 다룬 공개
  벤치마크는 없습니다. 따라서 작은 모델은 키 없이 Loom을 시험해 보거나 모든 것을 머신 안에 두기 위한 수단으로 보고,
  동등한 대안으로 여기지 마십시오. 추출, `memory.ask` 답변, 어시스턴트의 도구 사용은 더 약할 것으로 예상되며,
  그 정도는 측정된 바 없습니다. CPU에서 작은 모델은 추출 같은 긴 프롬프트에서 느리므로 바쁜 캡처 큐가
  밀립니다. GPU나, 메모리가 충분한 머신에서의 더 큰 모델이 도움이 됩니다. 팀 서버에는 호스팅 모델이 더 나은 답을 제공합니다.

<a id="through-an-inference-gateway"></a>

### 추론 게이트웨이를 통한 사용

제공자 앞에서 OpenAI 또는 Anthropic API를 제공하는 게이트웨이(예산, 비용 귀속, 여러 서비스에 대한 단일 키 용도)는
API 루트가 그쪽을 가리키면 Loom의 호출을 받습니다. 모델 이름은 게이트웨이가 제공하는 그대로 유지됩니다.

```bash
LOOM_OPENAI_BASE_URL=https://gateway.example/v1   # chat and embeddings
LOOM_ANTHROPIC_BASE_URL=https://gateway.example   # Messages: <root>/v1/messages
OPENAI_API_KEY=<the key the gateway issued for Loom>
```

기본값이 아닌 경우 Anthropic 키는 게이트웨이가 받는 방식인 `Authorization: Bearer`로도 전송됩니다.
게이트웨이 키를 사용할 때는 게이트웨이를 필수로 지정하여, 잘못된 루트가 프롬프트와 키를 다른 곳으로
보내지 못하게 하십시오. 토큰 교환은 그 자체로 이를 요구합니다.

```bash
LOOM_INFERENCE_GATEWAY_REQUIRED=true
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # comma-separated; exact names, any case
```

게이트웨이가 필수인 동안, 모델 호출과 Loom이 분류기 및 리랭커에 게시하는 메모리 콘텐츠는
`LOOM_INFERENCE_GATEWAY_HOSTS`에 나열된 호스트로만 전송됩니다. 호스트는 정확히 일치해야 합니다. 와일드카드는 없으며,
`gateway.example`은 `api.gateway.example`이나 `gateway.example.attacker.test`를 허용하지 않습니다.
IP 주소는 나열된 경우에만 일치합니다.

- **시작 시** Loom은 `LOOM_INFERENCE_GATEWAY_HOSTS`가 없거나,
  `LOOM_OPENAI_BASE_URL`, `LOOM_ANTHROPIC_BASE_URL`,
  `LOOM_EMBEDDING_BASE_URL`, `LOOM_INFERENCE_TOKEN_URL`(그리고 `LOOM_LLM_PROVIDER=local`일 때의
  `LOOM_LOCAL_LLM_BASE_URL`) 또는 `LOOM_RERANK_ENDPOINTS_JSON`의 항목이 다른 호스트를 가리키거나,
  임베더가 Gemini이거나, `LOOM_CODE_SEARCH_RERANK_DEFAULT` 또는
  `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT`가 `builtin:cohere`이면 로드를 거부합니다.
  사용하지 않는 제공자의 API 루트를 포함해 두 API 루트가 모두 해당됩니다(기본값은 제공자 자체의 API입니다).
  요청마다 지정한 모델은 어느 쪽 제공자의 것일 수도 있기 때문입니다.
- **호출 시** 다른 호스트로 향하는 모델 호출은 무엇이 지정했든(요청마다 지정한 모델,
  `LOOM_CODE_SEARCH_VECTOR_MODELS` 항목 등) 나가기 전에 거부되며, 모든 리디렉션 홉에서도 마찬가지입니다.
  거부된 호출은 재시도되지 않고, `/v1/health`는 해당 제공자를 실패 중으로 보고합니다.
- **네임스페이스**: `namespace.register`는 목록에 있는 호스트의 기본 포트에서 https가 아닌
  `classify_endpoint`를 거부하며(422), 이전에 저장된 것은 호출되지 않습니다. memory.set은 호출자의 유형을 유지하고
  로그에 경고를 남깁니다. `builtin:cohere`는 리랭크 선택지로 거부되며(422), 이를 저장한 네임스페이스는
  아무것도 사용하지 않고 리랭킹하며 로그에 경고를 남깁니다.
- **대시보드**: 다른 호스트의 엔드포인트(또는 Gemini 임베더)를 사용하는 모델 구성을 저장하면 422로 응답하고, 모델 검사는 `gateway_required`로 응답합니다. 검사가 거부한 저장된 구성은 서빙 벡터 설정을 포함해 적용되지 않으며, `/v1/health`는 `model_configuration_refused`를 보고합니다. 검사는 임베딩만 읽기 때문에, 거부된 구성은 항상 현재 적용 중인 설정과 다르게 임베딩하며, 벡터 테이블에는 그 다른 공간의 벡터가 들어 있을 수 있습니다. 그래서 해당 서버의 모든 데이터 플레인 요청이 거부되고(503 `GATEWAY_REQUIRED`, 두 SDK 모두 재시도하지 않으며 `Retry-After`도 없음) 서버는 준비 상태가 되지 않습니다. 이 상태는 검사를 통과하는 구성으로 교체하거나 환경의 구성으로 초기화할 때까지 계속됩니다. 이후 롤링 재시작은 그 서버에서 멈춥니다. 롤링 전에 구성을 교체하세요.
- **롤아웃:** 이전 빌드는 두 설정을 모두 무시하므로, 모든 레플리카가 이 빌드를 실행한 뒤에 설정을 켜세요. 이미 토큰을 교환하는 배포는 `LOOM_INFERENCE_GATEWAY_HOSTS`에 게이트웨이가 나열될 때까지 이 빌드에서 시작을 거부합니다. 또한 Gemini로 임베딩하거나, 다른 호스트의 rerank 엔드포인트를 지정하거나, 코드 검색의 기본값을 `builtin:cohere`로 둔 경우에도 시작을 거부합니다. `builtin:cohere`를 지정한 요청은 422를 받고, 이를 저장했거나 게이트웨이 밖의 `classify_endpoint`를 저장한 네임스페이스는 변경하기 전까지 rerank 또는 분류를 멈춥니다. 호스트를 설정하고 임베더를 게이트웨이 뒤로 옮기는 작업(`LOOM_EMBEDDING_PROVIDER=compatible`, 게이트웨이를 가리키는 `LOOM_EMBEDDING_BASE_URL`)을 먼저 하세요. 그렇지 않으면 롤아웃이 처음 업그레이드된 레플리카에서 멈춥니다.

워크로드 아이덴티티(RFC 8693 토큰 교환)를 받는 게이트웨이는 키가 전혀 필요 없습니다. Loom은 워크로드의 OIDC 토큰을 그곳에서 교환하고, 응답이 만료되기 5분 전에(수명이 10분 미만이면 절반 시점에) 갱신하며, 모든 호출에 함께 보냅니다. 키로부터 추론할 수 없으므로 제공자와 모델을 직접 지정합니다.

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token         # https
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # every root's host
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # rotated by the platform
LOOM_INFERENCE_TOKEN_SCOPE="chat embeddings"                         # if the gateway asks for one
LOOM_OPENAI_BASE_URL=https://gateway.example/v1                      # both roots required:
LOOM_ANTHROPIC_BASE_URL=https://gateway.example                      # the token is the gateway's
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

두 제공자의 루트나 토큰 엔드포인트 중 하나라도 `LOOM_INFERENCE_GATEWAY_HOSTS`에 나열되지 않은 호스트에 있으면 Loom은 시작을 거부합니다. 요청마다 지정되는 모델은 어느 제공자의 것이든 될 수 있고, 그러면 게이트웨이의 토큰이 그곳으로 전송되기 때문입니다. 대시보드에서도 모델 변경은 게이트웨이를 통해 검사됩니다.

Kubernetes에서 토큰 파일은 게이트웨이의 audience를 가진 projected 서비스 어카운트 토큰입니다. 게이트웨이는 클러스터의 OIDC 발급자를 신뢰해야 하며, 발급자만이 아니라 Loom의 서비스 어카운트(`system:serviceaccount:<namespace>:<name>`)와도 일치시켜야 합니다. 클러스터의 모든 파드가 그 발급자로부터 토큰을 받을 수 있기 때문입니다.

워크로드의 토큰을 게이트웨이가 직접 검증하는 경우에는 교환 없이 그 토큰을 bearer로 받습니다. 토큰 엔드포인트 대신 bearer 모드를 설정하세요. Loom은 파일을 최대 1분에 한 번 읽고, 게이트웨이가 401로 응답하면 즉시 다시 읽으므로, 교체된 토큰은 재시작 없이 반영됩니다.

```bash
LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # sent as it is
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # required, as with the exchange
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_EMBEDDING_PROVIDER=compatible                                   # embeddings through the gateway too
LOOM_EMBEDDING_BASE_URL=https://gateway.example/v1
```

제공자와 모델은 교환 예시와 같이 지정하세요. 토큰은 게이트웨이의 호스트로만 전송됩니다. 교환 방식과 마찬가지로 bearer 모드도 게이트웨이를 필수로 요구합니다. 자체 `LOOM_EMBEDDING_API_KEY`가 지정된 compatible 임베딩 엔드포인트에는 대신 그 키를 보냅니다.

<a id="naming-the-customer-on-every-call"></a>

#### 모든 호출에 고객 지정하기

고객별로 사용량을 측정하거나 예산을 관리하는 게이트웨이는 헤더에서 고객을 읽습니다. 헤더 이름을 지정하면 모든 chat, 임베딩, rerank, classify 요청에 해당 데이터를 가진 org와 그 요청을 일으킨 기능이 담깁니다.

```bash
LOOM_INFERENCE_CUSTOMER_HEADER=X-Customer-Id   # the org
LOOM_INFERENCE_FEATURE_HEADER=X-Feature        # optional, with the customer header: hyde, memory.search, worker.embedding
LOOM_INFERENCE_SYSTEM_CUSTOMER=loom            # calls that belong to no org
LOOM_INFERENCE_CUSTOMER_REQUIRED=true          # a cell serving several customers: refuse to start without the header
LOOM_INFERENCE_CUSTOMER_FORMAT='cloud:{org}'   # the gateway's customer id for an org; default the org id
LOOM_INFERENCE_FEATURE_MAP_JSON='{"hyde": "search", "*": "memory"}'  # Loom's feature names to the gateway's
```

- 요청은 토큰에 대해 인가된 범위에서 자신이 대상으로 하는 org를 지정합니다. 호출자는 자신의 토큰이 닿지 않는 org를 지정할 수 없습니다.
- 백그라운드 작업은 처리 중인 행이 속한 org를 지정하며, org당 한 번씩 호출합니다. 임베딩 워커, `loom reembed`, 모델 재빌드는 배치를 org별로 나눕니다.
- 배포 자체의 호출(임베더의 시작 검사, 재보정 워커의 고정 쌍, 대시보드의 제공자 검사)은 `LOOM_INFERENCE_SYSTEM_CUSTOMER`를 지정합니다.
- 샘플링되는 검색 판정기(`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`)는 판정 대상 org의 쿼리와 메모리를 보내므로, 판정된 검색마다 그 org를 지정하는 호출이 하나 추가됩니다.
- 고객 헤더가 설정되어 있으면 org를 지정하지 않는 모델 호출은 전송되지 않고 거부됩니다(`MODEL_CALL_UNATTRIBUTED`). org를 빠뜨린 호출 경로이며, 게이트웨이는 이를 누구에게도 청구하지 못하기 때문입니다.
- org id는 필요한 곳에서 퍼센트 인코딩되어 전송됩니다.

캐시된 응답은 호출을 만들지 않습니다. Loom의 캐시에서 제공된 임베딩이나 모델 응답은 다시 측정되지 않습니다. 고객은 캐시보다 먼저 검사되므로, 고객을 지정하지 않는 경로는 캐시 적중이어도 거부됩니다.

<a id="naming-the-agent-on-every-call"></a>

#### 모든 호출에 에이전트 지정하기

에이전트별로 청구하거나 제한하는 게이트웨이는 호출이 누구를 위한 것인지를 헤더에서 읽습니다. 각 헤더를 템플릿에 매핑하면 게이트웨이로 가는 모든 chat, 임베딩, rerank, classify 요청에 그 헤더가 담깁니다.

```bash
LOOM_INFERENCE_IDENTITY_HEADERS_JSON='{"X-Tenant": "{tenant}", "X-Agent": "{agent}", "X-Actor": "{email}"}'
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # required: identity headers go only to these hosts
LOOM_INFERENCE_TENANT_CLAIM=tenant_id   # the workload trust claim {tenant} names (the default)
```

- 플레이스홀더는 `{org}`, `{namespace}`, `{subject}`, `{user_id}`, `{agent}`(워크로드 트러스트 토큰의 에이전트, `sa:<trust>:<agent>`), `{tenant}`(해당 트러스트가 `LOOM_INFERENCE_TENANT_CLAIM` 클레임에 요구하는 값), `{email}`(로그인한 사람의 주소)입니다.
- 템플릿이 빈 값으로 렌더링되는 헤더는 전송되지 않습니다. 플레이스홀더만 있는 경우, 뒤에 에이전트가 없는 호출(사람의 호출이나 배포 자체의 호출)에는 에이전트 헤더가 붙지 않습니다. `{agent}` 또는 `{tenant}`를 사용하는 헤더는 함께 전송되거나 아예 전송되지 않으므로, 에이전트가 테넌트 없이 전송되는 일은 없습니다.
- org를 지정하지 않는 호출은 배포 자체의 호출이 아닌 한 거부됩니다(`MODEL_CALL_UNATTRIBUTED`). 모델을 호출하는 `loom` 명령은 `--org`와 함께 실행하고, 헤더가 에이전트를 지정하는 동안에는 `--namespace`도 함께 지정하세요(네임스페이스가 없는 호출은 게이트웨이의 기본 에이전트에 청구되지 않고 거부됩니다).
- 백그라운드 작업은 처리 중인 행의 아이덴티티를 지정합니다. 메모리나 지연된 추출의 작성자이거나, 그렇지 않으면 그 행이 속한 네임스페이스를 소유한 에이전트입니다. 행을 배치로 처리하는 작업은 아이덴티티마다 한 번씩 호출합니다.
- 다른 호스트에는 아이덴티티 헤더가 전달되지 않습니다. `LOOM_INFERENCE_GATEWAY_HOSTS` 밖에 있는 reranker, 분류기, 제공자는 호출이 누구를 위한 것인지 알지 못합니다.
- `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON`은 하나의 아이덴티티에 대해 게이트웨이가 주는 응답을 나열하며, 각 응답은 상태와 오류 본문(`error` 아래)의 정확한 `code`, `type` 또는 `message`로 식별합니다. 게이트웨이가 제공하는 경우 `code`로 매칭하세요. 기본값은 추론 프록시의 에이전트별 코드와 그 뒤에 있는 측정 시스템의 예산 거부에 맞춰져 있습니다.

  ```json
  {
    "refused": [
      { "status": 429, "code": "budget_exceeded" },
      { "status": 429, "type": "rate_limit_error", "message": "budget exceeded" },
      { "status": 403, "code": "agent_denied" },
      { "status": 400, "code": "identity_incomplete" },
      { "status": 400, "code": "identity_malformed" }
    ],
    "wait": [{ "status": 429, "code": "agent_request_limit" }]
  }
  ```

  두 번째 항목은 `/v1/messages`에서 측정 시스템이 주는 Anthropic 형식의 응답에 코드가 없기 때문에 type과 message로 매칭합니다.

- 게이트웨이가 자체 장애에도 주는 응답은 절대 나열하지 마세요. `token_unavailable`(자격 증명을 가져오지 못한 경우로, 자격 증명 서비스 장애도 이렇게 응답함)과 `identity_missing`(요청이 에이전트를 지정하지 않았고 게이트웨이에 기본값이 없는 경우로, 구성 문제)가 그렇습니다. 나열하면 `/v1/health`가 장애나 잘못된 구성을 감지하지 못하게 됩니다. 배포 수준의 응답도 계속 제공자 장애로 취급됩니다: `caller_*`, `scope_not_granted`(토큰의 스코프가 다루지 않는 라우트), `route_not_found`, `upstream_*`, `misconfigured`, `no_identity`.
- `refused` 아래의 응답은 해당 요청이 403 `MODEL_CALL_REFUSED`로 응답하게 하고, `wait` 아래의 응답은 게이트웨이의 `Retry-After`와 함께 503 `MODEL_CALL_DEFERRED`로 응답하게 합니다. 대기 시간이 2초 이하이면 먼저 한 번 기다린 뒤 호출을 다시 시도합니다. 어느 응답도 재시도되지 않고, `/v1/health`는 이를 제공자 장애로 읽지 않으며, 백그라운드 작업은 거부 후 `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS`(기본 10분) 동안, 대기 후에는 `Retry-After` 동안 그 아이덴티티의 같은 모델 호출을 보류하고 다른 작업은 계속합니다. 표에 나열되지 않은 응답은 늘 그렇듯 제공자의 응답입니다.
- 각 값을 찾는 방법: [아이덴티티 헤더](../architecture/llm-architecture.md#identity-headers).

<a id="switching-an-orgs-model-calls-off"></a>

#### org의 모델 호출 끄기

org 관리자는 게이트웨이 유무와 관계없이 대시보드의 Guardrails 페이지나 API로 org 데이터에 대한 모든 모델 호출을 중지할 수 있습니다.

```bash
curl -s -X POST "$LOOM_BASE_URL/v1/org.set_model_calls" -H "Authorization: Bearer $ORG_ADMIN_TOKEN" \
  -d '{"org":"acme","enabled":false,"reason":"customer turned AI off"}'
```

- 꺼져 있는 동안에는 해당 org에 대한 chat, 임베딩, rerank, classify 요청이 전송되지 않으며, 캐시된 벡터나 응답도 제공되지 않습니다. 이 스위치는 킬 스위치입니다. 이런 호출이 필요한 요청은 거부됩니다(403 `MODEL_CALLS_DISABLED`). 쓰기 시 임베딩되는 메모리나 classify 엔드포인트가 있는 네임스페이스의 `memory.set`, `memory.search`, 그리고 임베딩, 판정 또는 rerank를 수행하는 경우의 `tracker.similar`, `tracker.duplicate_check`, `code.bisect_candidates`가 여기에 포함됩니다. 키로 읽는 `memory.get`과 삭제는 모델 호출을 하지 않으므로 응답합니다.
  로컬 임베더는 프로세스 안에서 실행되고 아무것도 전송하지 않지만, 이것도 꺼집니다. org를 지정하는 호출은 Loom이 스스로 만든 것이라도 검사되며, org를 지정하지 않는 Loom 자체의 호출(시작 검사, 보정)만 계속됩니다.
- `"enabled": true`로 다시 켜면 대기 중이던 작업이 실행됩니다.
- **플랫폼 관리자의 잠금.** 플랫폼 관리자는 `"locked": true`를 추가할 수 있습니다. 그러면 org 자체의 관리자는 여전히 끌 수 있지만, 다시 켜거나 잠금을 해제하는 것은 플랫폼 관리자가 `"locked": false`를 보낼 때까지 거부됩니다(403, 잠근 주체를 명시). `locked`를 보내지 않는 플랫폼 관리자의 변경은 잠금을 유지합니다. [`org.model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/org.md#orgmodel_calls)는 설정, 잠금, 설정한 주체를 읽습니다.
- **org 오프보딩**은 수동입니다. 플랫폼 관리자가 `"enabled": false, "locked": true`를 보냅니다. org의 캐시된 쿼리 벡터는 30일 후에 만료됩니다. Loom의 어떤 경로도 org를 스스로 끄지 않습니다.
- 모든 서버는 `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`(기본 10, 변경을 받은 프로세스는 다음 호출 시) 이내에 변경을 적용합니다. 서버가 `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS`(기본 60, 15~300)보다 오래 스위치를 읽지 못하면, 읽을 수 있을 때까지 어느 org에 대해서도 모델 호출을 하지 않습니다.
- org는 모든 레플리카가 이 스위치를 아는 빌드를 실행한 뒤에만 끄세요. 이전 빌드는 해당 org에 대해 계속 모델을 호출합니다.

org가 꺼져 있는 동안 무엇이 얼마나 대기하는지는 다음과 같습니다.

| 작업                                                                                                      | org가 꺼져 있는 동안                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 메모리 임베딩(임베딩 워커)                                                                                | 선택되지 않습니다. 메모리는 벡터 없이 남아 있다가 org가 다시 켜지면 임베딩됩니다. 삭제되지 않습니다.                                                                                                                                                                                                                                                                                                                                                             |
| 추출 요청(추출 큐)                                                                                        | 클레임되지 않은 채 대기 상태로 남습니다. 요청이 큐에 들어간 뒤, 그리고 org가 꺼진 뒤 `LOOM_MODEL_CALLS_OFF_MAX_WAIT_DAYS`(기본 7, 1~29)만큼 대기하면 `model_calls_off` 사유의 `memory_drops` 레코드와 함께 실패로 표시됩니다. 레코드는 먼저 동기적으로 기록되며, 레코드를 쓸 수 없는 요청은 대기 상태로 남습니다. 큐의 30일 TTL이 레코드 없이 요청을 먼저 제거하지 않도록 제한값은 30일 미만으로 유지됩니다. |
| 엔티티 태깅, 프로필 폴드, 기간 다이제스트, 대화 제목, 리플렉션, 관찰 증류                                 | 선택되지 않으며, org가 다시 켜지면 수행됩니다. 남아 있는 행에서 파생되므로 삭제되는 것은 없습니다.                                                                                                                                                                                                                                                                                                                                                                |
| 프로젝션 복구                                                                                             | 스캔되지 않으며, org가 다시 켜지면 재시도됩니다.                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 보류 중인 충돌 판정 | 다시 판정하지 않습니다. 조직이 복귀했을 때 스윕의 한 시간보다 오래된 판정은 스윕 대상에서 벗어나며, 장애 중에 유실된 판정과 마찬가지로 해당 쓰기는 이루어진 그대로 유지됩니다. |
| 코드 수집, 코드 요약, 코드 임베딩 복구 | 해당 조직의 저장소는 건너뛰므로, 어휘 검색을 포함한 조직의 전체 코드 인덱스는 기존 상태로 유지됩니다. 복구 스캔이 지나쳐 간 스텁은 주기적인 전체 스캔에서 찾아냅니다. |
| 코드 링크 판정 | 처리 대기 상태로 남습니다. |
| 트래커 임베딩, 트래커 링크 점수 계산 | 커서가 움직이기 전에 네임스페이스를 건너뜁니다. |
| 검색 판정 샘플 | 폐기됩니다. 샘플은 반드시 처리해야 하는 작업이 아닙니다. |
| `loom reembed` | 해당 조직의 행은 제외되고, 집계(`skipped_switched_off`)되어 보고됩니다. 조직이 다시 켜지면 한 번 더 실행하십시오. `--delete-old`는 행이 제외된 모든 조직의 기존 벡터를 유지합니다. |
| 모델 변경에 따른 재구축 | 완료됩니다. 해당 조직의 행은 벡터 없이 준비되며 재구축 진행 상황에 집계됩니다(`rows_waiting`). 조직이 다시 켜지면 메모리 임베딩, 코드 청크, 코드 요약은 각 복구 워커가 다시 임베딩합니다. 그 밖의 테이블의 행은 다시 기록되거나 이후 재구축에서 처리될 때까지 벡터가 없습니다. |

<a id="without-a-workload-token"></a>

#### 워크로드 토큰이 없는 경우

플랫폼이 Loom에 토큰을 제공하지 않는 환경(VM, 컨테이너 호스트)에서는 Loom이 직접 토큰에 서명합니다. 토큰 파일 대신 audience를 설정하면, 게이트웨이가 Loom을 발급자로 신뢰합니다.

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example
LOOM_INFERENCE_TOKEN_AUDIENCE=loom-inference   # what the gateway's entry for Loom names
LOOM_PUBLIC_BASE_URL=https://loom.example      # the issuer is <this>/v1
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

교환할 때마다 수명이 5분인 새 토큰을 보냅니다. 발급자는 `<LOOM_PUBLIC_BASE_URL>/v1`, 주체(subject)는 `loom`, audience는 위에서 설정한 값이며, 서명 키링에서 가장 최근의 RS256 키로 RS256 서명을 합니다. 먼저 `loom
signing-key rotate --algorithm RS256`으로 키를 생성하십시오. 다만 `LOOM_JWT_SECRET`이 RSA 키라면 이미 RS256으로 서명하므로 생성하지 않아도 됩니다. 그 이후부터는 이 키가 Loom 자체 토큰에도 서명하며, 배포에 속한 모든 Loom이 이를 검증합니다. 게이트웨이가 검증할 수 없으므로 HS256 시크릿은 사용하지 않습니다. 비용은 그 하나의 주체에 귀속됩니다.

게이트웨이의 Loom 항목에는 해당 발급자, audience, 주체 `loom`, RS256, 그리고 `<LOOM_PUBLIC_BASE_URL>/v1/.well-known/jwks.json`의 키를 지정합니다(OIDC 디스커버리는 `/v1/.well-known/openid-configuration`에 있습니다). 게이트웨이는 https로 해당 URL을 가져올 수 있어야 하며, Loom에 접근할 수 없는 경우에는 게이트웨이가 접근할 수 있는 곳에 키 집합의 사본을 게시하십시오. 각 Loom은 `rotate`가 키를 기록한 뒤 다음 키링 갱신 시점에 키를 게시하며, 이 키는 `--activate-in`만큼 지난 뒤에야 서명에 사용됩니다(기본값 300초). 게이트웨이가 키 사본을 유지하는 시간이나 키를 복사하는 데 걸리는 시간보다 긴 `--activate-in`으로 rotate하십시오. 폐기된 키는 게이트웨이의 사본이 갱신될 때까지 게이트웨이에서 계속 신뢰됩니다. `loom signing-key retire`는 자신의 환경에 `LOOM_INFERENCE_TOKEN_AUDIENCE`가 설정된 경우 마지막 활성 RS256 키의 폐기를 거부하므로, 서버와 동일한 환경에서 실행하십시오. Loom(서버와 모든 `loom` 명령)은 `LOOM_PUBLIC_BASE_URL`이 https가 아니거나, `LOOM_JWT_AUDIENCE`를 audience로 사용하거나, 자체 검증기가 신뢰하는 발급자인 경우 이 설정을 거부하며, 자체 검증기는 게이트웨이의 audience를 가진 토큰을 절대 수락하지 않습니다.

<a id="apply-changes-and-check-the-result"></a>

## 변경 사항 적용 및 결과 확인

**대시보드에서 변경한 경우:** Models & keys가 활성 구성을 표시할 때까지 기다립니다. 리빌드가 실행 중이면 같은 화면에서 진행 상황을 확인하고, 실패한 작업은 같은 페이지에서 다시 시도합니다. 대시보드 변경을 적용하기 위해 컨테이너를 다시 만들지 마세요.

**환경 변수만 변경한 경우:** 배포된 스택에서는 서버 컨테이너를 다시 만듭니다.

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
curl -fsS http://localhost:7777/v1/health
```

설치 디렉터리나 포트가 다르다면 해당 값을 사용하세요. 컨테이너는 반드시 다시 만들어야 합니다. 단순한 `docker compose restart`는 `.env`에서 환경 변수를 다시 불러오지 않습니다. 네이티브 서버는 업데이트된 환경 변수로 재시작해야 합니다. 체크아웃 스택에서는 `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build --force-recreate loom`을 실행합니다.

health 응답은 `fake_llm`, `fake_embedding`, `providers_degraded`, 그리고 조치가 필요한 `problems`를 보고합니다. 실제 프로바이더 두 개가 모두 구성되어 있다면 fake 플래그는 false여야 합니다. 이 확인은 구성과 상태만 점검하므로, 테스트용 사실을 저장하고 조회해 보고 답변도 시도하여 사용 중인 계정이 선택한 모델을 호출할 수 있는지 확인하세요.

LLM을 변경하거나 API 키를 교체할 때는 임베딩을 다시 빌드할 필요가 없습니다. 임베딩 모델을 변경하면, 차원이 같더라도 다시 빌드해야 합니다. 대시보드 워크플로가 아닌 환경 변수 변경의 경우, 기존 메모리, 문서, 트래커, 코드 인덱스는 각각에 해당하는 재임베딩 또는 재수집 절차가 필요합니다. `loom reembed`만으로 모든 인덱스를 다시 빌드할 수 있는 것은 아닙니다. 차원이 바뀌면 새 구성을 적용하기 전에 기존 스키마의 벡터 제약 조건을 확인하세요. 데이터를 보존하고 [유지보수](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#re-embed-and-re-extract) 및 [코드 인덱스 운영](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md) 문서를 따르세요.
