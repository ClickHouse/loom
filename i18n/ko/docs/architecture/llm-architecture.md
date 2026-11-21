<!-- i18n: source=docs/architecture/llm-architecture.md blob=e4be5f935957cae12d8a7a0579f2dbb45846a398 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md), which is authoritative.

<a id="llm-and-embedding-architecture"></a>

# LLM 및 임베딩 아키텍처

Loom에는 교체 가능한 두 가지 제공자가 있습니다. 텍스트를 벡터로 변환하는 임베더(`server/embedding/`)와 프롬프트를 완성하는 LLM 클라이언트(`server/llm/`)입니다. 각각 설정에 따라 프로세스당 한 번 선택됩니다. 키가 없으면 각각 결정적인 가짜 구현으로 대체되므로, 키가 없는 설치에서도 서버가 부팅되고 응답합니다. 운영자 설정은 [배포](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md)에 있으며, 모든 설정은 [구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)에 있습니다.

<a id="goals-and-non-goals"></a>

## 목표와 비목표

| 목표                                                                                    | 비목표                                                                                  |
| --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| 쓰기 경로의 모든 정확성 게이트가 LLM 없이 동작한다.                                     | 범용 LLM 게이트웨이. Loom은 자체 기능을 위해서만 모델을 호출한다.                       |
| 키 없는 설치도 부팅되며, 가짜(fake) 구현으로 실행 중임을 분명하게 알려 준다.            | 프로바이더에 구애받지 않는 프롬프트. 프롬프트는 특정 모델에 맞춰 조정되며 변경 시 다시 벤치마크한다. |
| 임베더를 교체해도 recall 보정이 조용히 어긋나지 않는다.                                 | 벡터 공간 혼합. 서로 다른 모델의 벡터는 절대 비교하지 않는다.                           |
| 모든 프로바이더 호출은 해당 호출을 일으킨 op 또는 워커 패스와 그 비용에 귀속된다.       | 런타임 프롬프트 관리. 프롬프트는 코드다.                                                |

<a id="embedders"></a>

## 임베더

<a id="selection"></a>

### 선택

`server/embedding/embedder.py`(`provider_for`, `get_embedder`)는 프로세스마다 한 번 제공할 임베더를 선택합니다.

1. 레이블을 가장 먼저 해석합니다. `prefix:<dim>:<model>`과 `ctx[<variant>]:<model>`은 제공 중인 임베더에 대한 뷰이며, 제공 모델이 될 수 없습니다.
2. 설정되어 있으면 `LOOM_EMBEDDING_PROVIDER`(`fake`, `local`, `openai`, `gemini`)를 사용합니다. 그 외의 값은 오류를 발생시킵니다.
3. 설정되어 있지 않으면 `LOOM_EMBEDDING_MODEL`에서 추론합니다. `fake-ngram*`은 fake, `local` 또는 `local:*`은 local, `text-embedding-*`은 OpenAI, `gemini*`는 Gemini입니다. 그 외의 이름은 `unknown embedding model` 오류를 발생시킵니다.

| 제공자 | 모듈 | 모델 이름 | 필요 사항 | 비고 |
| -------- | ---------------------------- | -------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `fake`   | `server/embedding/fake.py`   | `fake-ngram-<dim>`                                                         | 없음                                  | 해시된 문자 트라이그램이며 L2 정규화됩니다. 결정적이며, 개발용 기본값입니다. |
| `local`  | `server/embedding/local.py`  | `local` (`bge-small`), `local:bge-small`, `local:bge-base`, `local:minilm` | `local-embedder` extra (`fastembed`) | CPU ONNX이며 차원은 각각 384, 768, 384입니다. 네트워크를 사용하지 않습니다. |
| `openai` | `server/embedding/openai.py` | `text-embedding-3-small`, `text-embedding-3-large`                         | `OPENAI_API_KEY`                         | `dimensions=LOOM_EMBEDDING_DIM`을 요청합니다.                                 |
| `gemini` | `server/embedding/gemini.py` | `gemini-embedding-001`                                                     | `GEMINI_API_KEY` 또는 `GOOGLE_API_KEY`     | `outputDimensionality=LOOM_EMBEDDING_DIM`을 요청합니다(최대 3072).          |

**차원.** 제공되는 차원은 `LOOM_EMBEDDING_DIM`(기본값 3072)입니다. OpenAI와 Gemini에는 해당 차원을 요청하므로, 이를 지원하는 모델을 선택하세요. 로컬 모델은 자체 기본 차원으로 출력합니다. 부팅 시 실제 차원을 확인하며(`probe_embedding_dim`), 불일치하면 `/v1/health`에 `embedding_dim_mismatch`로 표시됩니다.

**이미지 빌드.** Docker 이미지는 로컬 임베더를 설치하고 `bge-small`의 파일을 포함합니다. 파일은 리비전과 sha256으로 고정되어 있으며(`server/embedding/local_models.py`), 스택의 `env.example`은 이를 사용해 임베딩합니다.

<a id="protocol-and-paths"></a>

### 프로토콜과 경로

`Embedder` 프로토콜은 `model_name`, `dim`, `async embed(text)`, `async embed_many(texts)`로 구성됩니다.

| 경로 | 전송 내용 | 사용처 |
| -------------------- | -------------------------- | ------------------------------------------------------------------------ |
| `embed` (쿼리)      | 공백을 정규화한 텍스트 | 검색. 캐시됩니다.                                                          |
| `embed_many` (쓰기) | 원본 텍스트 | 저장되는 모든 벡터: `memory.set`, 추출, 수집(ingest), 복구 워커. |

- **캐시** (`server/embedding/_cache.py`). 쿼리는 프로세스별 LRU(`LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`)에서 먼저 제공되고, 그다음 같은 텍스트에 대해 진행 중인 호출, 그다음 `loom.embedding_cache`(최대 `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS`까지의 텍스트이며 `dim`이 일치하는 경우만), 마지막으로 제공자 순으로 처리됩니다. 키는 요청의 조직, 모델, 정규화된 텍스트를 해시하므로, 한 조직이 다른 조직의 벡터를 받거나 다른 조직과 진행 중인 호출을 공유하는 일은 없습니다. 응답이 빠르면 다른 조직이 같은 텍스트를 임베딩했다는 사실이 드러나기 때문입니다. 요청 밖의 호출(워커, CLI, 시작 시 프로브)에는 범위로 삼을 조직이 없으므로 캐시를 건너뜁니다. 워커의 ID는 마지막으로 처리한 행의 조직을 가지고 있어 다음 텍스트의 범위가 될 수 없기 때문입니다. 배치 항목은 원본 텍스트에 대한 별도의 키를 사용하므로, 두 경로가 서로의 결과를 제공하지 않습니다. 캐시 실패 시 제공자로 넘어갑니다.
- **배치.** OpenAI와 Gemini는 요청당 최대 100개의 입력을 받습니다. `memory.set`은 값과 키를 한 번의 요청으로 임베딩합니다.
- **보조 벡터 경로.** `prefix:<dim>:<model>`(Matryoshka 프리픽스, `server/embedding/prefix.py`)과 `ctx[<variant>]:<model>`(다시 쓴 텍스트에 대해 제공 모델을 적용, `server/embedding/alias.py`)은 `LOOM_CODE_SEARCH_VECTOR_MODELS`에 나열된 경우에만 만들어집니다. 보조 임베더는 fake로 대체되지 않으며, 키가 없으면 오류를 발생시킵니다.

<a id="provider-calls"></a>

### 제공자 호출

OpenAI와 Gemini는 `server/embedding/_provider.py`를 공유합니다.

| 항목 | 동작 |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 재시도 | 전송 오류와 408, 409, 425, 429, 500, 502, 503, 504를 재시도합니다. `LOOM_EMBEDDING_RETRY_ATTEMPTS`(3)회 시도하며, 지수 백오프(기본 0.5 s, 상한 `LOOM_EMBEDDING_RETRY_MAX_WAIT_S`, 10 s)를 사용합니다. |
| 입력 검사 | 입력의 토큰 수를 먼저 계산합니다(OpenAI 8191 토큰, Gemini 2048 토큰). 크기를 초과한 입력은 잘리지 않고 오류를 발생시킵니다. |
| 출력 검사 | 빈 벡터, 차원 불일치, 유한하지 않은 값은 `MalformedEmbedding`을 발생시킵니다. |
| 타임아웃 | 공유 HTTP 클라이언트의 `LOOM_HTTP_CLIENT_TIMEOUT_S`(20 s, 연결에는 `LOOM_HTTP_CLIENT_CONNECT_TIMEOUT_S`, 5 s)를 사용합니다. 쿼리 경로는 시도당 `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`(2.5 s)와 `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`(1)를 사용하며, 그 후 503 `EMBED_TIMEOUT`을 반환합니다. |
| 상태 | 호출 결과는 `/v1/health`에 보고됩니다. 취소는 제공자 실패로 간주하지 않습니다. |

<a id="changing-the-embedder"></a>

### 임베더 변경

`LOOM_EMBEDDING_MODEL` 또는 `LOOM_EMBEDDING_DIM`을 변경하면 벡터 공간이 바뀝니다. 저장된 벡터는 자신의 `embedding_model`을 가지고 있고 검색은 모델 간 비교를 하지 않으므로, 기존 행은 다시 임베딩될 때까지 일치하지 않습니다.

`loom reembed`(`server/memory/app/reembed.py`, `reembed_all`)는 `memory_embeddings`, `entity_vectors`, `conversation_embeddings`에서 `embedding_model`이 제공 중인 모델과 다른 모든 행을 다시 씁니다. 저장된 콘텐츠로 벡터를 다시 생성하며 다른 모든 컬럼은 유지합니다. `memory_embeddings`와 `conversation_embeddings`는 모델이 정렬 키에 포함되어 있으므로, `--delete-old`를 사용하기 전까지 기존 행과 새 행이 공존합니다. `entity_vectors`(`ReplacingMergeTree(created_at)`)는 그 자리에서 교체됩니다. 플래그는 `--org`, `--namespace`, `--batch-size`, `--dry-run`, `--key-only`, `--resume`, 그리고 `--in-database <named
collection>`이며, 마지막 플래그는 ClickHouse의 `aiEmbed`를 통해 테이블마다 `INSERT … SELECT`를 한 번씩 실행합니다(이를 제공하는 ClickHouse 릴리스가 필요하며, 고객 또는 ID 헤더가 설정되어 있거나 게이트웨이가 필수인 경우에는 거부됩니다. 이러한 호출은 누구의 것인지 명시하지 않아 게이트웨이의 검사를 건너뛰기 때문입니다). [CLI 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/cli.md)를 참고하세요.

나머지는 `embedding_worker`가 처리합니다. `embedding_dim = 0`인 스텁을 최신순으로 임베딩하며, 이전 모델로 레이블된 스텁은 제공 중인 모델로 임베딩하고 기존 스텁은 삭제 표시(tombstone)합니다. 임베딩 행이 없는 메모리는 먼저 제공 중인 모델 아래에 스텁을 받으며, 이때 `status`, `invalid_at`, 증명(attestation)을 복사합니다. 트래커 패스는 다른 모델이 쓴 행을 다시 임베딩합니다.

<a id="adaptive-thresholds"></a>

## 적응형 임계값

코사인 0.4가 의미하는 바는 임베더마다 다릅니다. 검색 게이트(유사도 하한, abstain, HyDE 폴백, 쿼리 재작성, 리랭크 건너뛰기)와 쓰기 게이트(충돌, 중복)는 모두 코사인 임계값이므로, 모델을 교체하면 이 값들이 모두 달라집니다.

`LOOM_ADAPTIVE_THRESHOLDS`가 켜져 있으면(기본값) `server/memory/app/embedder_calibration.py`의 `calibrate_embedder`가 부팅 시점에 임계값을 도출합니다. 내장된 세 가지 코퍼스(paraphrase 쌍, 질문-사실 쌍, distractor 쌍)를 임베딩하고 각 코사인 분포를 읽습니다(`derive_thresholds`).

| 임계값        | 도출 방식                                  |
| ------------- | ------------------------------------------ |
| 노이즈 하한   | 방해 쌍 p90 + 0.02                         |
| abstain       | 노이즈 하한 + 0.05                         |
| 쿼리 재작성   | max(질문-사실 p10, 노이즈 하한 + 0.01)     |
| HyDE 폴백     | max(질문-사실 p25, 쿼리 재작성)            |
| 리랭크 건너뛰기 | max(질문-사실 p75, HyDE 폴백)            |
| 충돌          | 바꿔 쓰기 쌍 p50                           |
| 중복          | 바꿔 쓰기 쌍 p75                           |

각 검색은 세 단계로 임계값을 결정합니다.

1. 네임스페이스별 캘리브레이션(`loom.namespace_calibration_result`, `code.calibrate`가 기록)
2. 클러스터 캘리브레이션(`loom.embedder_calibration`, 임베더 모델당 한 행)
3. 스칼라 설정(`LOOM_MEMORY_SEARCH_MIN_SIMILARITY`,
   `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`, `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`,
   `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD`, `LOOM_LLM_RERANK_SKIP_ABOVE`,
   `LOOM_CONFLICT_THRESHOLD`)

`calibration.get`과 `/v1/health`(`adaptive_thresholds`)에서 현재 적용 중인 값을 확인할 수 있습니다. 같은 게이트를 제공하는 모든 레플리카에서 `adaptive_thresholds.calibration_hash`는 동일합니다.

- 모든 레플리카는 하나의 캘리브레이션을 사용합니다. 부팅 시 레플리카는 자신의 임베더에 해당하는 `loom.embedder_calibration` 행을 가져와 사용하고, 행이 없을 때만 프로브를 실행합니다(가장 먼저 부팅한 레플리카가 프로브 결과를 게시합니다). 각 레플리카는 `LOOM_CALIBRATION_REFRESH_SECONDS`(60)마다 이 행을 다시 읽고 변경된 행을 반영합니다. 부팅 시 프로브가 실패한 레플리카는 이 주기에 다시 프로브합니다. 네임스페이스별 행은 60초 동안 캐시되므로, 한 레플리카에서 실행한 `code.calibrate`는 1분 이내에 모든 레플리카에 반영됩니다.
- Fake 임베더는 캘리브레이션을 건너뛰고 스칼라 게이트를 사용합니다. 프로브가 실패한 경우도 마찬가지이며, 부팅이 실패하지는 않습니다.
- 적응형 기능이 꺼져 있고 `text-embedding-3-small`(스칼라 게이트를 튜닝한 모델)이 아닌 실제 임베더를 사용하면 부팅 시 경고를 로그에 남깁니다. 벤치마크 재수행은 사용자가 직접 책임져야 합니다.
- `recalibration` 워커(매주 실행, `LOOM_RECALIBRATION_ENABLED`, 리더에서 실행)는 프로브를 다시 실행하여 그 결과를 클러스터 캘리브레이션으로 게시하고, 0.05 이상 드리프트가 발생하면 경고하며, 유사 쌍이 5개 이상이고 방해 쌍이 3개 이상인 네임스페이스 코퍼스를 다시 프로브합니다.

**이유.** 모델 자체의 분포에서 임계값을 도출하면 임베더 교체가 설정 변경으로 끝납니다. 대신 임계값은 통계적인 값이라는 단점이 있습니다. "유사" 쌍이 바꿔 쓴 문장이 아니라 수정본인 네임스페이스 코퍼스에서는 충돌 및 중복 게이트가 너무 낮게 설정됩니다.

<a id="llm-clients"></a>

## LLM 클라이언트

<a id="selection-1"></a>

### 선택

`server/core/llm_provider.py`의 `resolve_llm_provider`가 프로바이더를 고릅니다.

1. 명시적으로 지정한 `LOOM_LLM_PROVIDER`는 절대 덮어쓰지 않습니다.
2. 아니면 `LOOM_LLM_MODEL`에서 정합니다. `claude*`는 `anthropic`, `gpt*`, `o1*`, `o3*`, `o4*`,
   `chatgpt*`는 `openai`, `fake*`는 `fake`입니다.
3. 아니면 존재하는 키에서 정합니다. `OPENAI_API_KEY`가 있으면 `gpt-6-luna`와 함께 `openai`, 그다음 `ANTHROPIC_API_KEY`가 있으면 `claude-haiku-4-5`와 함께 `anthropic`입니다.
4. 아니면 `fake`(`fake-echo-1`)입니다.

`Settings`는 로드할 때 한 번 결정하며, `settings.llm_provider`는 항상 구체적인 값입니다. 어떻게 정해졌는지는 `llm_provider_inferred()`와 `llm_provider_source()`가 알려 줍니다. 프로바이더가 추론된 경우 부팅 시 INFO 로그를 한 줄 남깁니다. `loom doctor`의 `llm_provider` 줄은 키가 없으면 경고합니다.
기본 `.env`는 `LOOM_LLM_PROVIDER=fake`로 고정되어 있습니다.

| 프로바이더    | 모듈                      | 키                                                   |
| ----------- | ------------------------- | ---------------------------------------------------- |
| `openai`    | `server/llm/openai.py`    | `OPENAI_API_KEY`                                     |
| `anthropic` | `server/llm/anthropic.py` | `ANTHROPIC_API_KEY`                                  |
| `local`     | `server/llm/local.py`     | 없음: `LOOM_LOCAL_LLM_BASE_URL`의 OpenAI 클라이언트 |
| `fake`      | `server/llm/fake.py`      | 없음                                                 |

단계별 모델(`LOOM_MEMORY_ASK_SYNTH_MODEL`, `LOOM_CODE_SUMMARY_MODEL` 및 그 밖의
`*_model` 설정)은 `get_llm_client_for_model`로 자체 클라이언트를 만들며, 이름에서 프로바이더를 추론합니다. 배포 환경의 `LOOM_LLM_MODEL`을 다시 지정하면 결정된 프로바이더에서 실행되므로(`Settings.provider_of`), 호스팅 모델처럼 보이는 이름의 로컬 모델도 로컬로 유지됩니다.

<a id="protocol"></a>

### 프로토콜

`LLMClient`에는 `provider`, `model`, 그리고 메서드 하나가 있습니다.

```python
async def complete(system, user, max_tokens=1024, response_format=None) -> LLMResult
```

`LLMResult`는 텍스트, 입력 및 출력 토큰, 캐시된 입력 토큰과 캐시 쓰기 토큰, 응답한 프로바이더와 모델, 그리고 `from_cache`를 담습니다. 도구 사용 경로는 없습니다.

**구조화된 출력.** 호출자는 `response_format={"type": "json_object"}`를 전달합니다. OpenAI 클라이언트는 이를 그대로 넘깁니다. Anthropic 클라이언트에는 JSON 모드가 없으므로 시스템 프롬프트에 JSON 지시문을 덧붙이고, 코드 펜스를 제거하며, 시스템 블록을 캐시 가능으로 표시합니다. 파싱은 호출자가 합니다(`server/llm/json_text.py`).

**모델별 요청 형태는 학습으로 정해집니다.** 요청에 무엇을 담을지는 모델 이름으로 결정하지 않습니다.
각 클라이언트는 원하는 형태의 요청(temperature, 가장 낮은 reasoning effort, 출력 상한)을 만들고, `server/llm/traits.py`가 프로세스당 모델별로 한 번 프로바이더의 응답에서 학습합니다.

- 모델이 받지 않는 필드(`temperature`, `reasoning_effort`, `output_config`, 다른 이름의 상한 필드)를 지목하는 400이 오면 그 필드를 제외하고 호출을 다시 보내며, 이후 호출에서도 해당 필드를 뺍니다.
- reasoning effort를 거부하는 400이 오면 응답에 나열된 값 중 가장 낮은 것으로 정합니다(`LOOM_LLM_OPENAI_REASONING_EFFORT`로 하나를 고정할 수도 있습니다).
- 모델의 출력 한도를 지목하는 400이 오면 상한을 그 값으로 제한합니다.
- reasoning 토큰과 thinking 토큰은 출력 상한에 포함되므로, 응답이 보고한 토큰 수가 모델의 예비분이 됩니다(관측된 최대치의 두 배, 최대 `LOOM_LLM_HIDDEN_REASONING_RESERVE_MAX`). 이 값이 모든 호출자의 상한에 더해집니다. 상한에 도달했는데 보이는 텍스트가 없는 응답은 최대치를 예비분으로 삼아 한 번 다시 보냅니다.

따라서 새 모델에는 코드 변경이 필요 없습니다. 첫 호출이나 두 번째 호출로 프로세스가 그 모델이 필요로 하는 바를 익힙니다. `LOOM_LLM_ANTHROPIC_EFFORT`는 Claude 모델에 thinking effort를 보냅니다.

<a id="retries-timeouts-fallback"></a>

### 재시도, 타임아웃, 폴백

- **재시도**(`server/llm/_retry.py`): 전송 오류와 429, 500, 502, 503, 504, 529에 적용됩니다.
  `LOOM_LLM_RETRY_ATTEMPTS`(4)회 시도하며, 지수 백오프(기본 0.5초, 상한 `LOOM_LLM_RETRY_MAX_WAIT_S`, 20초)를 사용합니다. 요청 타임아웃
  `LOOM_LLM_REQUEST_TIMEOUT_S`는 300초입니다. 멈춘 연결만 잡아내므로 느린 모델이나 긴 출력은 여기에 걸리지 않습니다. 더 짧은 시간이 필요한 호출자는 호출을 직접 감쌉니다(`memory.search`의 HyDE는 `LOOM_MEMORY_SEARCH_HYDE_TIMEOUT_S`, 6초만큼 기다립니다).
- **폴백 모델.** `LOOM_LLM_FALLBACK_MODEL`을 설정하면 클라이언트를 `FallbackLLMClient`(`server/llm/fallback.py`)로 감쌉니다. 장애(전송 오류, 재시도 가능한 상태 코드, 타임아웃)나 거부된 요청(400, 413, 422)이 발생하면 호출을 두 번째 모델에 넘기며, 401, 403, 404는 그대로 전파합니다. 넘길 때마다 로그를 남기고 집계하며(`loom.llm.fallback`), 결과에는 응답한 모델의 이름이 담깁니다. 폴백은 연쇄되지 않습니다.
- **키 누락.** 클라이언트는 fake가 되며(`_build_llm_client`), WARNING으로 기록됩니다.
- **한 ID의 거부.** ID 헤더가 설정된 경우, 게이트웨이 호스트의 응답 중 `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON`에 나열된 응답 패턴과 일치하는 것은 게이트웨이가 해당 호출의 ID를 거부하거나 기다리라고 요청하는 것입니다([ID 헤더](#identity-headers)). 이런 응답은 재시도하지 않고 폴백 모델에도 넘기지 않습니다.

<a id="response-cache"></a>

### 응답 캐시

`CachedLLMClient`(`server/llm/cache.py`)는 완성 결과를 `loom.llm_response_cache`에 캐시합니다(TTL 7일). 키는 모델별이며 클라이언트가 서비스하는 조직, 시스템 프롬프트, 사용자 프롬프트, `max_tokens`, `response_format`, 추가 인자를 기준으로 하므로 한 조직이 다른 조직의 응답을 받는 일은 없습니다. 조직 없이 만든 클라이언트는 프로바이더를 직접 호출합니다. 요청한 모델의 비어 있지 않은 응답만 저장하므로 폴백 응답은 캐시되지 않습니다. 각 행에는 프롬프트에 텍스트가 담긴 메모리 키(`window_keys`)가 기록되어, 삭제와 편집 시 키로 행을 지울 수 있습니다. 사용자 삭제 시에는 해당 사용자의 메모리 요약이 담긴 행(`consolidated` 및 `src:<key>` 태그)도 함께 삭제합니다. 이 요약은 다른 ID의 키로 사용자의 메모리를 인용하고 있기 때문입니다.

| 사용처       | 스위치                                                                                                    |
| ------------ | --------------------------------------------------------------------------------------------------------- |
| `memory.ask` | `LOOM_MEMORY_ASK_LLM_CACHE_DEFAULT`(켜짐), `LOOM_MEMORY_ASK_LLM_CACHE_NAMESPACES`, 요청별 `llm_cache` |
| `code.ask`   | `LOOM_CODE_ASK_LLM_CACHE`(켜짐), 0이 아닌 temperature는 캐시를 우회합니다                                 |
| 추출         | 꺼짐. 단, `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS`가 켜는 경우는 예외입니다(기본값으로는 `bench` 클래스)         |

<a id="the-customer-of-a-model-call"></a>

## 모델 호출의 고객

`LOOM_INFERENCE_CUSTOMER_HEADER`를 설정하면 모델로 가는 모든 요청(chat, embedding, rerank, classify)에 해당 데이터를 보유한 org가 함께 전달되고, `LOOM_INFERENCE_FEATURE_HEADER`를 설정하면 요청을 만든 기능이 함께 전달됩니다(`server/llm/customer.py`). 헤더는 전송 코드가 직접 추가합니다. 공유 HTTP 클라이언트는 고객을 지정하지 않는 비모델 호출도 처리하기 때문입니다.

- **org의 출처.** 요청의 경우: 토큰을 검증한 뒤 전송 계층이 기록한 신원(`observability.genai`)이며, 이는 op가 대상으로 하는 org입니다. 워커 또는 CLI의 경우: 호출을 `customer.for_org(org)`로 감싸며, org는 작업 대상 행에서 가져옵니다. `--org`를 지정한 CLI 명령은 해당 org로 범위가 한정됩니다. 워커 자신의 신원은 고려하지 않습니다. 한 번의 pass는 마지막으로 처리한 행의 org를 기록하므로, 다음 행의 호출이 이를 물려받아서는 안 되기 때문입니다. 배포 자체의 호출(시작 시 embedder 프로브, 재보정 워커의 고정 쌍, 프로바이더 점검)은 `customer.system()` 아래에서 실행되며 `LOOM_INFERENCE_SYSTEM_CUSTOMER`를 지정합니다. 샘플링된 검색 judge는 판정 대상 org의 쿼리와 메모리 텍스트를 보내므로 그 org를 지정합니다. `code.refresh`는 poller의 ingest pass를 실행하며, 이때 호출은 poller의 경우 네임스페이스에 귀속되는 것과 달리 호출자(토큰의 subject)에 귀속됩니다.
- **호출당 고객 하나.** 여러 org의 행을 묶어 처리하는 작업(embedding 워커, `loom reembed`, 모델 재구축)은 호출 전에 배치를 org별로 나눕니다.
- **게이트웨이의 이름.** `LOOM_INFERENCE_CUSTOMER_FORMAT`은 org id를 게이트웨이의 고객 id로 변환하고, `LOOM_INFERENCE_FEATURE_MAP_JSON`은 Loom의 기능 이름을 게이트웨이의 이름으로 매핑합니다. 여러 고객을 서비스하는 셀은 `LOOM_INFERENCE_CUSTOMER_REQUIRED`를 설정하여, 헤더 없이는 시작되지 않게 합니다.
- **고객이 없으면 호출도 없습니다.** 고객을 지정하지 않은 호출은 아무것도 전송되기 전에 `Unattributed`(`MODEL_CALL_UNATTRIBUTED`)를 발생시킵니다. 누락된 호출 경로는 아무에게도 청구되지 않은 채 넘어가지 않고 즉시 오류로 드러납니다. embedding 워커(메모리와 코드)는 먼저 확인하고, org를 지정하지 않은 행은 즉시 포기한 뒤 모델 호출 없이 해당 오류를 기록합니다. 재시도해도 도움이 되지 않고, 이 거부는 프로바이더 장애가 아니기 때문입니다.

- **꺼진 org는 호출하지 않습니다.** `org.set_model_calls`는 `loom.org_model_calls`에 기록합니다. 모든 전송 코드에서 가장 먼저 실행되는 `customer.require()`(게이트웨이 토큰, embedding 캐시, 프로바이더 상태 확인보다 앞섭니다)는 호출의 org를 각 프로세스가 가진 꺼진 org 사본(`customer.switched_off_orgs`)과 대조합니다. 캐시된 벡터나 응답도 거부됩니다. 이 스위치는 킬 스위치이므로 모델이 만든 어떤 것도 해당 org에 전달되지 않습니다. 사본은 최대 `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`마다 읽고, `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS`를 넘겨서까지 신뢰하지는 않습니다. 이를 넘으면 모든 org에 대한 모델 호출이 거부됩니다(`customer.SwitchUnread`). 작업은 SwitchUnread 거부를 스위치 꺼짐과 동일하게 처리합니다(`customer.HOLDS`). 행은 시도 횟수를 소모하지 않고 그대로 남으며, 재구축 단계는 다시 실행되고, fail-open 방식의 reranker, classifier, judge는 통과시킵니다. 사본이 너무 오래되었거나 이 프로세스에서 이루어진 변경이 아직 읽히지 않은 경우, 읽기 중에 이루어진 호출은 그 읽기를 기다립니다. 그렇지 않으면 가지고 있는 사본으로 처리됩니다. 모든 작업은 작업을 선택할 때 꺼진 org를 제외합니다(쿼리 안에서 제외하므로 해당 행은 시도 횟수를 소모하지 않고 다른 org의 배치 자리를 차지하지도 않습니다). 코드 ingest는 해당 org의 리포지토리를 건너뛰어 코드 인덱스 전체를 동결합니다. org가 다시 켜질 때 sweep의 한 시간보다 오래된 충돌 표시는 sweep 범위를 벗어난 것으로, 장애 중에 유실된 judge와 마찬가지로 기록된 쓰기가 그대로 유지됩니다. 이 확인은 프로바이더 상태 기록보다 앞선 `require()`에서 실행되므로, 거부가 프로바이더 장애로 기록되는 일은 없습니다. 고객 헤더나 신원 헤더가 없을 때 여러 org의 작업 배치는 하나의 호출이지만, 그 안에 꺼진 org가 있으면 예외입니다. 이 경우 `customer.groups`가 org별로 나누어, 스위치가 해당 org의 호출만 거부하고 나머지는 계속 진행됩니다.

**헤더를 쓰고 고객별 토큰을 쓰지 않는 이유.** 게이트웨이는 이미 Loom을 하나의 서비스(하나의 토큰)로 알고 있습니다. org별 자격 증명은 수천 개를 발급, 교체, 폐기해야 하지만 게이트웨이가 검증할 수 있는 내용은 늘지 않습니다. 헤더의 org는 요청이 대상으로 하는 org이며, 해당 토큰에 대해 인가된 것입니다.

<a id="identity-headers"></a>

### 신원 헤더

`LOOM_INFERENCE_IDENTITY_HEADERS_JSON`은 에이전트나 사람별로 과금하거나 제한하는 게이트웨이를 위해, 호출이 누구를 위한 것인지를 더 자세히 지정합니다(`server/llm/identity.py`). 헤더 이름을 템플릿에 매핑하며, `customer.require()`가 호출의 귀속 정보로 이를 렌더링하고, 전송 코드는 게이트웨이로 가는 모든 chat, embedding, rerank, classify 요청에서 고객 헤더 옆에 이를 추가합니다.

| 플레이스홀더   | 요청                                                                            | 작업 (`customer.for_org`)                                     |
| ------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `{org}`       | 요청이 대상으로 하는 org                                                          | 행의 org                                                       |
| `{namespace}` | 전송 계층이 기록한 네임스페이스                                                    | 행의 네임스페이스                                                |
| `{subject}`   | 토큰의 subject                                                                  | 행의 작성자, 없으면 해당 행이 속한 네임스페이스를 자신의 것으로 가진 에이전트    |
| `{agent}`     | 에이전트 subject `sa:<trust>:<agent>`의 `<agent>`                                | 동일하게, 해당 subject의 값                                      |
| `{tenant}`    | `LOOM_INFERENCE_TENANT_CLAIM`이 지정한 클레임에 대해 해당 trust가 요구하는 단일 값 | 동일                                                            |
| `{email}`     | 사람의 주소: subject 또는 검증된 디렉터리 표기                                     | 동일                                                            |
| `{user_id}`   | 요청의 `user_id`, 없으면 subject                                                 | 행의 `user_id`, 없으면 subject                                   |

- **빈 값은 제외됩니다.** 템플릿이 빈 값으로 렌더링되는 헤더는 전송되지 않으므로, 값을 알 수 없을 때 플레이스홀더만 있는 헤더는 제외됩니다. 플레이스홀더 주변의 텍스트는 작성된 그대로 전송됩니다. 값은 헤더가 담을 수 없는 문자(공백, `%`, 출력 가능한 ASCII 밖의 모든 문자)에 한해서만 퍼센트 인코딩됩니다. 접두사 클레임(`tenant_id=acme-*`)은 테넌트를 지정하지 않습니다.
- **에이전트는 통째로 전달됩니다.** 템플릿에 `{agent}` 또는 `{tenant}`가 있는 헤더들은 함께 전송되거나 아예 전송되지 않습니다. 이들의 플레이스홀더 중 하나라도 빈 값으로 렌더링되면(정확한 테넌트를 요구하지 않는 trust의 에이전트, trust 자체의 토큰) 어느 것도 전송되지 않으며, 게이트웨이는 불완전한 신원을 거부하는 대신 기본 신원을 적용합니다. 다른 헤더들은 독립적으로 처리됩니다.
- **org가 없으면 호출도 없습니다.** 신원 헤더가 설정된 경우, org를 지정하지 않은 호출은 고객 헤더의 경우와 마찬가지로 거부되고(`MODEL_CALL_UNATTRIBUTED`, 로그 기록), 배포 자체의 호출(`customer.system()`)만 예외입니다. `--org` 없이 실행한 `loom` 명령이나 신원이 기록되지 않은 요청이 게이트웨이의 기본값으로 나가는 일은 없습니다. `loom` 명령은 `--namespace`도 지정하므로, 그 호출은 해당 네임스페이스를 자신의 것으로 가진 에이전트를 위한 것이 됩니다. 헤더가 에이전트를 지정하는 동안에는, `--org`만 있고 네임스페이스가 없는 명령의 호출은 게이트웨이의 기본 에이전트에게 청구되는 대신 같은 방식으로 거부됩니다.
- **작성자가 우선합니다.** 작업의 호출은 해당 행을 작성한 주체를 위한 것입니다. 이는 `actor`의 subject(`human:` 또는 `process:`와 토큰의 subject)입니다. 파이프라인 자체의 actor(`process:extraction-worker`, `process:cli:<command>`, `provenance.PIPELINE_ACTORS`)는 주체가 아닙니다. 지연된 `memory.set_from_messages`는 큐 행에 enqueue한 주체를 저장하며, 엔티티 추출기, embedding 워커, projection repair, 코드 링크 judge는 작업 대상 메모리의 작성자를 읽습니다. Observe-extract, 프로필 fold, 구조화된 프로필은 프롬프트에 포함될 수 있는 observation 또는 fact 중 가장 최근의 주체 작성자를 사용합니다. 이 쓰기들의 순서는 버전이 정하므로, 업데이트는 새로운 쓰기로 간주됩니다. reflection과 기간 digest는 세션 메모리 중 가장 최근 작성자(`memory_repo.session_writers`)를 사용하고, 충돌 resolver는 현재 행의 작성자를 사용합니다. 주체를 기록하지 않는 행(대화 턴, 공유 범위 벡터)만 해당 행이 속한 네임스페이스를 자신의 것으로 가진 에이전트(`sa:<trust>:<agent>`가 소유하며, 에이전트의 첫 토큰 교환 때 해당 trust가 만든 홈)를 사용합니다. 배포 자체의 호출(`customer.system()`)만이 누구도 지정하지 않습니다.
- **호출당 신원 하나.** 행을 배치로 묶어 처리하는 작업(embedding 워커, `loom reembed`, 모델 재구축)은 행이 렌더링하는 신원별로 묶으므로(`customer.groups`) 한 호출에 두 에이전트의 텍스트가 함께 담기지 않으며, 여러 네임스페이스에 걸친 한 에이전트의 행은 한 호출을 공유합니다.
- **읽기.** trust의 클레임(`loom.machine_identities`), 네임스페이스의 소유자, 계정의 이메일은 프로세스당 최대 분당 한 번 읽습니다. 읽기에 실패하면 가지고 있는 사본을 사용하고, 사본이 없으면 호출은 보류됩니다(`identity.IdentityUnread`, `customer.HOLDS` 중 하나). 다른 사람의 신원으로 나가는 대신 대기하며, 작업은 시도 횟수를 소모하지 않고 해당 작업을 30초 동안 남겨 둡니다.
- **게이트웨이에만 전달됩니다.** 신원 헤더는 `LOOM_INFERENCE_GATEWAY_HOSTS`에 나열된 호스트에만 전달되므로, 신원 헤더를 설정하려면 이 값이 필요합니다. 모델 클라이언트의 요청 훅(`server/clients/_http.py`)은 리다이렉트 hop을 포함한 다른 모든 요청에서 이 헤더를 제거합니다. 운영자의 reranker, 네임스페이스의 classifier, Cohere 또는 다른 곳의 프로바이더는 호출이 누구를 위한 것인지 알 수 없습니다. reranker와 classifier도 모델 클라이언트를 통해 전송합니다.
- **신원 하나의 거부.** 게이트웨이 호스트의 응답 중 `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON`에 나열된 응답 패턴과 일치하는 것은 프로바이더 장애가 아니라 게이트웨이가 해당 신원을 거부한 것입니다. 각 항목은 상태와, 오류 본문(`error` 아래)의 `code`, `type`, `message` 중 하나 이상을 지정하며 각각 정확히 일치해야 합니다. 메시지 안의 단어는 일치 대상이 아닙니다. 기본값은 예산을 초과한 에이전트(`budget_exceeded`), 거부된 에이전트(`agent_denied`), 불완전하게 지정된 에이전트(`identity_incomplete`, `identity_malformed`)를 code로 매칭하고, `agent_request_limit`에서는 대기합니다. 미터링의 Anthropic 형식 예산 응답에는 code가 없으므로 type과 전체 메시지로 매칭합니다. 게이트웨이가 자체 장애에도 내는 응답(`token_unavailable`, `identity_missing`)은 제외되므로, 상태 확인은 여전히 장애나 잘못된 설정을 감지합니다. 모델 클라이언트의 응답 훅(`gateway.answered`)은 `refused` 아래의 항목에 대해 `ModelCallRefused`를 발생시켜, 요청이 게이트웨이의 `gateway_status` 및 `gateway_code`와 함께 403 `MODEL_CALL_REFUSED`로 응답하게 하고, `wait` 아래의 항목에 대해서는 `ModelCallDeferred`를 발생시켜 게이트웨이의 `Retry-After`와 함께 503 `MODEL_CALL_DEFERRED`로 응답하게 합니다. 요청의 호출이 2초 이하의 대기를 요청받은 경우 한 번 기다리고, 공유 모델 클라이언트가 리다이렉트 이후를 포함해 다시 전송한 뒤에야 지연 처리됩니다. 어느 응답도 더 이상 재시도하지 않으며 fallback 모델로 넘기지도 않고, `/v1/health`는 둘 중 어느 것에서도 아무것도 읽지 않으며, org의 스위치는 건드리지 않습니다. 둘 다 `customer.HOLDS`이므로 작업은 시도 횟수를 소모하지 않고 작업 내용을 보존합니다.
- **작업의 보류.** 해당 응답 이후, 같은 origin, 경로, 모델에 대한 해당 신원의 작업 호출(`customer.for_org`)은 보류됩니다. 요청 훅(`gateway.sending`)이 아무것도 전송하지 않고 거부하며, 거부 후에는 `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS` 동안, 대기 후에는 응답의 `Retry-After`(최대 5분) 동안 유지됩니다. 다른 모델이나 embedding으로 가는 호출은 계속 진행되며, 다른 신원의 호출도 마찬가지입니다. 고객 헤더는 신원의 일부입니다. 해당 경로와 모델에서의 성공은 요청이든 작업이든 보류를 끝냅니다. 포그라운드 요청은 항상 게이트웨이에 문의하는데, 게이트웨이가 이전에 거부하거나 지연시킨 신원을 언제든 다시 받아들일 수 있기 때문입니다. 그 밖의 응답은 이전과 같이 프로바이더의 응답으로 취급됩니다. 다른 403은 재시도되지 않는 프로바이더 장애이고, 다른 429는 재시도되는 속도 제한입니다.
- **정체가 아니라 건너뜁니다.** 작업은 보류가 지속되는 동안 보류된 작업을 다음 선택에서 제외하므로(`server/workers/holds.py`), 배치는 보류될 수밖에 없는 작업으로 다시 채워지는 대신 다른 신원의 작업으로 채워집니다. 추출 큐는 보류가 끝날 때까지 요청을 이유와 함께 `held`로 표시하고, 롤백 후 이전 워커가 회수할 수 있는 `running` lease를 저장합니다. 그동안 해당 enqueue 주체의 네임스페이스 내 다른 요청은(`enqueued_by` 컬럼 기준) 하나도 가져가지 않습니다. 프로필 fold는 슬롯의 네 배를 읽고, 보류된 신원의 슬롯을 다음에 읽은 신원에게 넘기며, 해당 신원을 스캔에서 제외합니다. 구조화된 프로필 워커는 보류가 끝날 때까지 보류된 사용자를 제외합니다. embedding 워커는 보류된 각 stub의 `embed_retry_at`을 시도 횟수를 소모하지 않고 기록합니다. Projection repair는 limit을 적용하기 전에 보류된 실패 ID를 제외합니다. embedding 선택은 정상 작성자가 같은 네임스페이스와 에이전트 레이블을 사용하는 경우를 포함해 어느 limit보다 먼저 보류된 행을 제외합니다. 배치 embedder(embedding 워커, `loom reembed`, 모델 재구축)는 한 번에 한 그룹씩 보류하고(`customer.GROUP_HOLDS`: 꺼진 org, 거부된 신원, 대기 요청을 받은 신원, 읽지 못한 신원), 다른 그룹의 호출로 얻은 벡터는 기록합니다. 모델 재구축은 이렇게 정상적으로 얻은 벡터를 스테이징하되, 보류된 모든 행을 재구축할 수 있을 때까지 페이지의 커서와 카운터를 유지합니다. 저장된 재시도 기한은 워커가 바뀌어도 유지되며, 보류가 끝나기 전의 프로바이더 호출을 막습니다. 검증은 게시 전에 유실된 소스 벡터를 여전히 거부합니다.

**네임스페이스의 소유자를 쓰는 이유.** 작업 대상 행 중에는 주체를 기록하지 않는 것(대화 턴, 공유 범위 벡터)이 있으며, 에이전트의 토큰은 자신의 네임스페이스를 홈으로 가지므로 네임스페이스가 작업 대상 에이전트를 알려 줍니다. 작성자를 기록하지 않은 공유 네임스페이스의 행은 렌더링된 헤더만 가지고 나가며 에이전트는 지정되지 않습니다.

**응답 표를 쓰는 이유.** 게이트웨이가 한 신원을 거부하는 경우와 자체 장애는 같은 상태를 공유할 수 있으며(허용되지 않은 에이전트에 대한 403, 에이전트를 대리할 권한이 없는 Loom의 자격 증명에 대한 403), 메시지 안의 단어는 계약이 아닙니다. 표에는 게이트웨이가 한 신원에 대해 내는 정확한 응답이 안정적인 code가 있는 경우 그 code로 나열되며, 오직 이들만 프로바이더 다운으로 읽히지 않고 해당 신원을 보류시킵니다.

<a id="fake-providers"></a>

## 가짜 프로바이더

가짜 프로바이더 덕분에 키가 없는 설치도 부팅할 수 있고, 테스트는 오프라인으로 실행되며, CI는 결정적으로 유지됩니다. 실제 서비스에는 사용하지 마세요. 모든 폴백은 눈에 보입니다:

- 로그에 남습니다(임베더는 ERROR, LLM은 WARNING).
- `/v1/health`와 대시보드에 `problems` 항목이 표시됩니다: `fake_embedder`,
  `embedder_key_missing`, `fake_llm`, `llm_key_missing`, 또는 실제 프로바이더의 마지막 호출이 실패했을 때의 `provider_failing:<name>`
  (`server/observability/problems.py`).
- `LOOM_REQUIRE_REAL_PROVIDERS=true`로 설정하면 가짜 프로바이더 항목은 오류가 되고 health는
  `degraded`로 표시됩니다. HTTP 상태는 계속 200이며, ClickHouse 장애일 때만 503입니다.

가짜 LLM을 사용하는 LLM 기능은 결정적인 에코로 응답하며, 일부 기능(대화 제목 생성기)은 스스로 건너뜁니다.

<a id="llm-driven-and-deterministic-parts"></a>

## LLM 기반 부분과 결정적 부분

무엇을 저장할지, 누가 읽을 수 있는지를 결정하는 모든 게이트는 결정적입니다:
권한 부여, 블랙리스트, 민감도 탐지, 충돌 및 중복 판정, 순위 지정, 응답 보류 기준선(abstain floor)이 여기에 해당합니다. LLM 단계는 콘텐츠(사실, 답변,
요약, 제목)를 생성하거나 보강합니다. 각 단계는 선택 사항이고, 범위가 제한되며, 실패 시 결정적 경로로 넘어갑니다(fail open).

**이유.** LLM이 결정하는 게이트는 재현, 감사, 단위 테스트가 불가능하고, 모든 쓰기에 비용과 지연 시간을 더합니다. 결정적 게이트는 CI에서 가짜(fake) 프로바이더로 프로덕션 코드 경로 그대로 실행됩니다. 대가는 코사인 임계값만으로는 수정과 모순을 구분할 수 없다는 점이며, 그래서 충돌 리졸버가 존재합니다. 리졸버는 쓰기가 끝난 뒤 리더에서 판단하므로, 쓰기 자체의 판정은 결정적으로 유지됩니다.

| 기능                                          | LLM 사용      | 스위치 (기본값)                                                                                                                                                                                                                                                                                              |
| --------------------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `memory.set`, `memory.search` 코어            | 아니요        | —                                                                                                                                                                                                                                                                                                            |
| 충돌 및 중복 판정                             | 아니요, 코사인 | `LOOM_CONFLICT_THRESHOLD`                                                                                                                                                                                                                                                                                    |
| 충돌 리졸버 (쓰기 이후)                       | 예            | `conflict_resolver` 워커, `LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED` (LLM 프로바이더 또는 리졸버 모델의 프로바이더가 fake가 아니면 켜짐); `LOOM_CONFLICT_RESOLVER_MODEL` (`gpt-6.1-sol`)                                                                                                                      |
| `memory.set_from_messages` 추출               | 예            | 항상 사용; 지연 실행은 `extraction_queue` 워커를 통해 처리                                                                                                                                                                                                                                                   |
| 추출 검증기 (코딩 캡처)                       | 예            | `LOOM_MEMORY_EXTRACT_VERIFY_MODELS` (`claude-opus-5-5`; `""`이면 꺼짐); fail open                                                                                                                                                                                                                            |
| `memory.ask` 합성                             | 예            | `LOOM_MEMORY_ASK_SYNTH_MODEL`, `LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP`, `LOOM_MEMORY_ASK_MID_MODEL`                                                                                                                                                                                                              |
| `memory.search` HyDE 및 폴백 도우미           | 예, 제한됨    | 요청별 `hyde`; `LOOM_MEMORY_SEARCH_FALLBACK_MAX_LLM_CALLS` (2); `retrieval_budget: "fast"`, `recipe: "fast"` 또는 `context.pack`에서는 [사용 안 함](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md#modes-and-recipes)                                                      |
| `memory.search` 리랭크                        | 예, 옵트인    | 네임스페이스의 `rerank_endpoint` 또는 요청별 `rerank` (`builtin:openai[:model]`, `builtin:cohere[:model]`, `LOOM_RERANK_ENDPOINTS_JSON`에 있는 이름); `retrieval_budget: "fast"`, `recipe: "fast"` 또는 `context.pack`에서는 `builtin:openai`가 [사용 안 함](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md#modes-and-recipes)으로 처리됨 |
| `knowledge.search` 리랭크                     | 예, 옵트인    | 요청별 `rerank` 또는 `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT`; `memory.ask`, `context.pack`, `loom_search`의 저장소 문서 구간에서는 사용 안 함                                                                                                                                                                  |
| 엔티티 추출                                   | 예            | `entity_extractor` 워커, `LOOM_ENTITY_EXTRACT_ENABLED` (켜짐)                                                                                                                                                                                                                                                |
| 대화 제목                                     | 예            | `conversation_titler` 워커 (켜짐; fake LLM에서는 건너뜀)                                                                                                                                                                                                                                                     |
| 관찰 마이닝, 리플렉션, 프로필 폴드            | 예            | `observe_extract`, `reflection_distill`, `profile_consolidation` 워커 (꺼짐)                                                                                                                                                                                                                                 |
| 구조화된 프로필                               | 예            | `structured_profile` 워커 (켜짐; 프로필 스키마가 있는 네임스페이스에만 해당), `LOOM_STRUCTURED_PROFILE_MODEL`                                                                                                                                                                                                |
| 코드 요약                                     | 예            | `code_summary` 워커 (꺼짐), `LOOM_CODE_SUMMARY_MODEL`                                                                                                                                                                                                                                                        |
| `code.ask`                                    | 예            | `LOOM_CODE_ASK_LLM_MODEL`                                                                                                                                                                                                                                                                                    |
| 세션 브리프 (`session.brief_save`)            | 예, 선택 사항 | `LOOM_SESSION_BRIEF_LLM` (켜짐), `LOOM_SESSION_BRIEF_LLM_MODEL`                                                                                                                                                                                                                                              |
| 검색 품질 판정기                              | 예, 샘플링    | `LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE` (0)                                                                                                                                                                                                                                                                       |
| 임베딩 복구, 보정                             | 아니요        | —                                                                                                                                                                                                                                                                                                            |

에이전트가 `memory.set`을 호출할지 여부는 전적으로 클라이언트 모델이 결정합니다. MCP 도구
설명(`server/transport/mcp_descriptions.py`)은 문법이 아니라 의도를 설명합니다.

<a id="llm-free-reads"></a>

### LLM 없는 읽기

`LOOM_READS_LLM_FREE=true`를 설정하면 모든 읽기 op(`Operation.read_only`)가 LLM 호출 없이 실행되므로, 배포 환경에서 읽기가 모델에 도달하지 않는다고 보장할 수 있습니다. HTTP 및 MCP 전송 계층은 핸들러가 실행되는 동안 요청에 표시를 남깁니다(`server/core/llm_free.py`). 메모리 검색은 빠른 자세(fast posture) 및 `context.pack`과 동일한 게이트(`_is_llm_free`)를 통해 이 표시를 읽고, 모델을 호출할 수 있는 다른 모든 읽기 단계는 `reads_llm_free()`를 읽습니다.

- `memory.search`, `search.query`(MCP `loom_search` 도구), `context.pack`, 그리고 이들을 기반으로 하는 모든 읽기: HyDE, 그래프 트리플, 재시도 판정기, LLM 카운트 의도, 동사 동의어를 사용하지 않습니다. 철자 교정 및 집합 확장 임베딩은 건너뛰므로, 일반 검색이나 팩은 쿼리를 한 번만 임베딩합니다.
- `code.search`: HyDE, 모델이 이름 붙인 개념 확장, 쿼리 확장을 사용하지 않습니다. 메모는 LLM 없는 답변을 따로 보관합니다.
- 리랭킹: 리랭커는 모델 호출입니다. LLM 없는 읽기는 요청에서 명시한 리랭커만 실행합니다. 네임스페이스의 `rerank_endpoint`와 코드 및 지식 검색 기본값(`LOOM_CODE_SEARCH_RERANK_DEFAULT`, `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT`)은 실행되지 않습니다. 요청이 명시한 리랭커는 실행되지만, `builtin:openai`는 예외로 융합된 순서를 그대로 유지합니다.
- `tracker.duplicate_check`와 `tracker.answer`: 리랭크나 불확실성 판정기를 사용하지 않습니다. `code.bisect_candidates`: 리랭크를 사용하지 않습니다. 각각 이미 폴백으로 쓰던 결정적 답변을 유지합니다.
- `memory.ask`와 `code.ask`는 요청에서 `allow_synthesis=false` / `synthesize=false`를 설정하지 않으면, 결정적 경로로 답할 수 있는 경우에도 해당 설정을 명시한 409 `READS_LLM_FREE`로 응답합니다. 이 플래그를 설정하면 LLM 호출 없이 회상된 근거를 반환합니다.
  이 호출의 목적은 모델의 답변이므로, 설정은 모델 없이 답하는 대신 요청을 거부합니다. `memory.brief`는 근거만 요청하며, 답변 없이 근거와 알 수 없는 항목(unknowns)을 반환합니다.
- 샘플링되는 검색 품질 판정기는 아무것도 큐에 넣지 않습니다. 두 전송 계층 모두 검색의 품질을 동일한 표시 안에서 기록합니다.

쓰기(`memory.set_from_messages` 추출, 충돌 판정기, `knowledge.synthesize`, 세션 브리프)와
백그라운드 워커는 여전히 모델을 호출합니다.

<a id="prompts"></a>

## 프롬프트

프롬프트는 코드와 함께 버전이 관리되는 Python 상수입니다.

| 프롬프트 계열                                                                  | 위치                                                                                                                          |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| 팩트 추출, 판정(judge), 재시도 보충 지침                                       | `server/memory/domain/extraction_prompts.py`; 조립은 `server/memory/app/extract/prompts.py`                                   |
| 검색 보조(HyDE, 반복 판정, 파생 의도, 트리플, 동사 동의어)                     | `server/memory/domain/search_prompts.py`                                                                                      |
| 재순위화                                                                       | `server/memory/app/rerank.py`                                                                                                 |
| `memory.ask` 종합                                                              | `server/memory/app/ask/prompts.py`                                                                                            |
| 워커 프롬프트                                                                  | 워커 모듈(`code_summary`, `conversation_titler`, `profile_consolidation`, `structured_profile`, `entity_extractor`)           |

각 작업을 어떤 모델이 실행할지는 `server/settings/catalog.py`의 `Models` 섹션 한 곳에서 설정합니다
([작업별 모델](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#models-per-job)). 프롬프트 안에 들어가는 회수된 메모리는
데이터로 구분되어 감싸집니다([보안](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#prompt-safety)).

**추출 출력 복구** (`server/memory/app/extract/extract.py`의 `extract_from_messages`):

- 잘림으로 보이는 파싱 실패는 토큰 예산을 두 배(최대 8192)로 늘려 재시도한 다음, 완전한 팩트 객체만 유지합니다.
- 그 외의 파싱 실패는 더 엄격한 보충 지침을 붙여 한 번 재시도합니다.
- 사소하지 않은 입력에서 팩트가 하나도 나오지 않으면 한 번 더 재시도합니다.

**Langfuse.** `scripts/codegen/push_langfuse_prompts.py --write`(`LOOM_LANGFUSE_URL` 및
`LOOM_LANGFUSE_ADMIN_TOKEN` 사용)는 프롬프트 상수를 고정된 이름으로 게시하고, 각 프롬프트의 텍스트 해시와 버전을
`server/observability/langfuse_prompts.lock.json`에 기록합니다. 실행 중에 프롬프트를 가져오는 일은 없습니다.
이 락파일 덕분에 생성 스팬에 프롬프트의 이름, 버전, 해시를 담을 수 있습니다
(`server/observability/genai.py`).

**프롬프트가 코드인 이유.** 프롬프트 변경은 벤치마크로 측정되는 동작 변경입니다. diff에서 검토하고, 벤치마크를 돌리고,
그 출력을 파싱하는 코드와 함께 롤백합니다. 프롬프트를 수정하려면 배포가 필요합니다.

<a id="cost-accounting"></a>

## 비용 집계

모든 provider 호출은 사용량을 op별 누적기(`server/observability/cost_accumulator.py`)에 보고합니다. 두 전송 방식 모두 요청마다 누적기를 바인딩하며, 워커 supervisor는 패스마다 바인딩합니다. 가격은 같은 모듈의 `PRICE_PER_1K_TOKENS`에서 가져오며, 캐시 읽기와 캐시 쓰기 배수가 적용됩니다. 목록에 없는 모델은 $0으로 청구되고 경고를 한 번 로그에 남깁니다. Gemini 임베딩은 사용량을 보고하지 않으며 $0으로 청구됩니다.

op의 `loom.agent_events` 행에는 `upstream_cost_usd`, `embedding_tokens`, `input_tokens`, `output_tokens`, `llm_calls`, 그리고 가장 많이 사용된 모델이 담깁니다(`server/observability/ledger.py`의 `emit_agent_event`). 캐시로 절감된 비용은 답변마다(`memory.ask`의 `llm_cache_saved_usd`) 그리고 이벤트의 `args`(`out_llm_cache_saved_usd`)에 나타납니다. [비용 및 사용량](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/cost-and-usage.md)을 참고하세요.

<a id="changing-a-default"></a>

## 기본값 변경하기

개발용 기본값은 가짜(fake) 구현입니다. 프로덕션에서는 `LOOM_EMBEDDING_MODEL`과
`LOOM_LLM_MODEL`을 명시적으로 설정합니다. `.env.example`의 주석 처리된 프로바이더 블록은
1536 차원의 `text-embedding-3-small`과 `gpt-6-luna`를 사용합니다.

**두 기본값 중 하나를 변경하거나** Anthropic 프로바이더를 사용하도록 전환하기 전에 **`make bench-quick`을 실행하세요.** 이 명령은 실행 중인 서버를 대상으로, 답변 판정기(answer judge)와 함께 장기 메모리 벤치마크(`bench/longmemeval.py`)의 30개 질문 샘플을 실행하고 `/tmp` 아래에 CSV를 작성합니다. 실제 프로바이더가 필요합니다. [벤치마크 실행하기](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)를 참고하세요.

<a id="invariants"></a>

## 불변 조건

- 하나의 프로세스는 시작 시점에 선택된 임베더 하나와 기본 LLM 클라이언트 하나만 사용합니다.
- `embedding_model` 값이 서로 다른 벡터는 절대 비교하지 않습니다.
- 사용 중인 임베더는 키가 없을 때에만 fake 임베더로 대체되며, 이 사실은 `/v1/health`에 표시됩니다. 보조 임베더는 절대 대체되지 않습니다.
- 검색에는 항상 임계값이 있습니다. 네임스페이스 보정값이 있으면 그 값을, 없으면 부팅 시 보정값을, 그것도 없으면 스칼라 설정값을 사용합니다.
- 저장이나 접근 여부를 결정하는 게이트는 LLM을 호출하지 않습니다.
- 대체 모델의 응답은 캐시되지 않습니다.
- 모든 provider 호출의 사용량은 해당 호출을 수행한 op 또는 pass의 `agent_events` 행에 기록됩니다.

<a id="source-entry-points"></a>

## 소스 진입점

| 항목                      | 위치                                                                                                                          |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 임베더 선택               | `server/embedding/embedder.py` (`provider_for`, `get_embedder`, `probe_embedding_dim`)                                        |
| 임베딩 캐시               | `server/embedding/_cache.py`                                                                                                  |
| 프로바이더 재시도 및 보호 장치 | `server/embedding/_provider.py`, `server/llm/_retry.py`                                                                  |
| LLM 선택                  | `server/core/llm_provider.py` (`resolve_llm_provider`), `server/llm/client.py` (`get_llm_client`, `get_llm_client_for_model`) |
| 대체 모델                 | `server/llm/fallback.py` (`FallbackLLMClient`)                                                                                |
| 응답 캐시                 | `server/llm/cache.py` (`CachedLLMClient`)                                                                                     |
| 보정                      | `server/memory/app/embedder_calibration.py`, `server/memory/workers/recalibration.py`                                         |
| 재임베딩                  | `server/memory/app/reembed.py` (`reembed_all`)                                                                                |
| 프롬프트                  | `server/memory/domain/extraction_prompts.py`, `server/memory/domain/search_prompts.py`                                        |
| 프롬프트 게시             | `scripts/codegen/push_langfuse_prompts.py`                                                                                    |
| 비용                      | `server/observability/cost_accumulator.py`, `server/observability/ledger.py`                                                  |
| 프로바이더 문제           | `server/observability/problems.py`                                                                                            |

<a id="related"></a>

## 관련 문서

- [추출](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md): 추출기가 생성하는 결과물입니다.
- [검색과 순위 지정](https://github.com/ClickHouse/loom/blob/main/docs/concepts/retrieval-and-ranking.md): 임계값이 적용되는 위치입니다.
- [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md): 트레이스와 프롬프트 링크입니다.
- [벤치마크](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md): 변경 사항을 측정하는 방법입니다.
