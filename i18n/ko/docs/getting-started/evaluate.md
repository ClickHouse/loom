<!-- i18n: source=docs/getting-started/evaluate.md blob=882e7d370aa195573ce2e4029995fa1f32beadc9 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md), which is authoritative.

<a id="evaluate-loom"></a>

# Loom 평가하기

저장소에 있는 모든 하니스를 평가하는 것이 아니라, 내려야 하는 결정을 평가하세요. 이 가이드는
비용이 들지 않는 제품 스모크 테스트로 시작해 워크로드별 품질 테스트로 넘어갑니다.
공개 벤치마크 스위트는 게시된 주장을 감사하거나 재현해야 할 때 거치는 마지막 단계입니다.

<a id="choose-an-evaluation"></a>

## 평가 방식 선택

| 결정 사항                                       | 데이터                                                         | 프로바이더                                             | 일반적인 출력                                                                         |
| ----------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Loom이 올바르게 설치되고 연결되는가?            | 테스트용 사실 4개와 문서 1개                                   | 기본 제공 가짜 프로바이더로 충분함                     | 사실이 새 세션에서도 유지되고, 대시보드에 소스, 이력, 감사 행이 나타남                |
| 메모리가 우리 에이전트에 유용한가?              | 기대하는 메모리 키가 있는 실제 질문 25–50개                    | 실제 임베더, 추출 또는 `memory.ask`에는 실제 LLM       | recall@k, rank@1, 응답 보류, 답변 정확도, 지연 시간, 비용                             |
| 코드 인텔리전스가 우리 저장소에 유용한가?       | 대표 심볼, 호출자 집합, 이슈 형태의 질문                       | 실제 임베더, 인덱싱된 저장소                           | 정확한 정의, 호출자 F1, 파일 recall, 응답 보류, 토큰, 지연 시간                       |
| 공개된 결과를 재현할 수 있는가?                 | 이름이 명시된 공개 데이터셋과 커밋된 결과 아티팩트             | 고정된 모델 또는 문서화된 대체 모델                    | 같은 슬라이스, 리더, 판정기, 검색 모드를 사용한 비교 가능한 실행                      |

가짜 임베더(체크아웃의 `.env`)나 가짜 LLM으로 품질을 판단하지 마세요. 이들은 자격 증명 없이 스택을 부팅하고 통합 테스트를 결정적으로 실행하기 위해 존재합니다. 릴리스된 스택의 로컬 임베더는 실제로 검색을 수행하므로, 이를 사용한 검색의 첫 확인은 의미가 있습니다. 답변과 추출은 실제 LLM으로 판단하세요.

<a id="before-you-run"></a>

## 실행 전에

먼저 다음 다섯 가지를 적어 두세요.

1. **워크로드.** 이 결정이 다루는 사용자, 리포지터리, 질문 유형.
2. **기준선.** Loom 없이 같은 사용자나 에이전트가 하는 방식: 전체 컨텍스트, grep, BM25, 다른 메모리 시스템, 또는 검색 없음.
3. **정밀도 기준.** 허용할 최소 재현율, 답변 정확도 또는 작업 성공률. 품질이 비용보다 우선합니다.
4. **경제성 기준.** 최대 p95 지연 시간, 토큰, 모델 비용, 인덱싱 시간, 저장 공간.
5. **측정 환경.** Loom 커밋, ClickHouse 배포, 제공자 모델, 모델 파라미터, 하드웨어, 데이터셋 버전, 랜덤 시드.

기준을 미리 등록해 두면 실행 후에 편리한 지표나 구간이 결과로 둔갑하는 일을 막을 수 있습니다. 실패와 응답 보류(abstention)는 그대로 드러내 두세요. 요청 오류는 검색 실패로 조용히 집계하거나 버리지 말고 따로 보고하세요.

<a id="1-product-smoke-test"></a>

## 1. 제품 스모크 테스트

약 15분이 걸리며, 연동, 영속성, 가시성을 확인하는 테스트입니다. 의미 기반 품질은 검증하지 않습니다.

1. [로컬 스택을 부팅](quickstart.md#1-boot-the-stack)하고 `/v1/health`가
   `status: ok`를 보고하는지 확인합니다.
2. [사실을 기록하고 검색](quickstart.md#4-first-memory-over-http)합니다.
3. [코딩 에이전트를 연결](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)하고, 지속되어야 하는 선호 사항을 알려준 뒤 새 세션을 열어 그 선호 사항을 물어봅니다.
4. 사실을 수정한 다음, 대시보드에 출처와 버전 이력이 표시되는지 확인합니다.
5. 문서 하나로 `loom demo`를 실행하고 반환된 청크가 해당 문서를 인용하는지 확인합니다
   ([문서 스모크 테스트](quickstart.md#optional-ask-a-question-about-a-document)).

새 세션이 사실을 기억하고, 수정한 내용이 현재 답변을 대체하며, 관련 없는 질문에는 답변을 보류하고, 대시보드에 작성자와 이력이 표시될 때만 통과로 판단합니다. health가 `fake_embedding` 또는 `fake_llm`을 보고하는 동안에는 이 결과가 프로덕션 환경의 recall에 대해 아무것도 알려주지 않습니다.

<a id="2-evaluate-your-workload"></a>

## 2. 워크로드 평가

[실제 provider](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#providers)를 구성하고, 벡터 차원이 바뀌었다면 데이터를 초기화하거나 다시 임베딩한 다음, `/v1/health`에서 확인된 모델을 기록하세요. 격리된 네임스페이스를 사용하고, 짝지은 모든 비교군에 동일한 인덱싱 데이터를 유지하세요.

<a id="memory-and-rag"></a>

### 메모리와 RAG

실제 작업에서 뽑은 질문을 최소 25~50개 모아 세트를 만드세요. 다음을 포함하세요.

- 직접적인 사실, 바꿔 말한 질문, 멀티홉 질문, 시간에 민감한 업데이트
- 답이 이미 다른 내용으로 대체되었어야 하는 질문
- 그럴듯하지만 답할 수 없는 질문. 오답과 답변 보류(abstention)를 측정하기 위한 것입니다.
- 중요한 값만 다른, 거의 중복된 방해 항목

답할 수 있는 각 메모리 질문에는 실행 전에 기대하는 `memory_key`를 레이블로 지정하세요. 문서 RAG의 경우 기대하는 문서와 청크에 레이블을 지정하고 `knowledge.search`를 질의한 뒤, 해당 식별자를 동일한 검색 지표로 채점하세요. 검색과 합성은 따로 측정하세요.

- **검색:** recall@k, rank@1, MRR, 오탐률, 답변 보류 정밀도/재현율
- **답변:** 하나로 고정한 reader와 judge 기준의 정확도, 그리고 인용 정확도
- **비용 효율:** p50/p95 지연 시간, 입력/출력 토큰, 모델 비용, 구축 비용, 저장된 행 수

기존 메모리 코퍼스가 있다면 다음 형태의 JSON 데이터셋을 저장하세요. 제공된 하네스는 `memory.search`를 대상으로 하므로, `knowledge.search`에는 동등한 채점기를 사용하세요.

```json
{
  "org": "acme",
  "namespace": "support-eval",
  "questions": [{ "query": "When can we deploy?", "gold_keys": ["deploy_window"] }]
}
```

그런 다음 코퍼스를 바꾸지 않고 검색을 실행하세요.

```bash
export PYTHONPATH=$PWD LOOM_API_KEY=<token>
.venv/bin/python bench/retrieval_recall.py \
  --dataset /path/to/retrieval_qa.json --base-url http://localhost:7777 \
  --top-k 10 --out /tmp/loom-retrieval.csv
```

`--min-recall`과 `--min-mrr`은 사전에 등록한 기준으로만 설정하세요. 답변 정확도를 보려면 같은 질문에 `memory.ask` 또는 프로덕션 reader를 사용하세요. recall과 답변 정확도는 서로 다른 측정값입니다.

<a id="code-intelligence"></a>

### 코드 인텔리전스

프로덕션 저장소와 비슷한 저장소를 인덱싱하세요. Loom을 튜닝한 저장소만으로 시작하지 마세요. 다음에 레이블을 지정하세요.

- 기대하는 파일과 한정 이름(qualified symbol, 즉 해당 심볼을 둘러싼 네임스페이스/모듈/클래스로 한정된 심볼. 정규화하거나 표준화한 심볼이 아닙니다)이 있는 정의
- 기대하는 호출자 전체 집합이 있는 호출자
- 상위 5개와 상위 10개 안에 허용되는 파일이 있는 개념형 질문과 이슈 형태의 질문
- 답변을 보류해야 하는 저장소 밖 질문
- 영향 분석 질의에서 기대하는 테스트 또는 의존 대상

인덱싱된 저장소를 대상으로 code-agent 인수 보고서를 실행하세요.

```bash
export PYTHONPATH=$PWD
.venv/bin/python bench/acceptance/code_agent.py \
  --base-url http://localhost:7777 --token-file /path/to/token \
  --namespace <code-namespace>
```

내장 fixture는 ClickHouse를 대상으로 하며 배포를 점검하는 데 유용합니다. 제품 도입 결정에는 이를 레이블이 지정된 자체 질문으로 교체하거나 확장하고, 같은 에이전트 작업을 일반 저장소 도구만 사용한 경우와 비교하세요. 작업 성공률을 먼저 보고한 다음 턴 수, 토큰, 비용, 실제 소요 시간을 보고하세요. 도구 스키마 오버헤드는 클라이언트에 따라 다르므로, 클라이언트가 MCP 도구 정의를 지연 로드하는지 기록하세요.

<a id="keep-the-comparison-fair"></a>

### 공정한 비교 유지하기

- 짝지은 비교군에서는 동일한 모델, 프롬프트, 데이터 스냅샷, 질문 순서를 사용하세요.
- 추출/인덱스 구축 비용은 질문당 비용과 분리하고, 구축 비용을 상쇄하는 데 필요한 질문 수를 명시하세요.
- 평가용 읽기에서 학습되지 않도록 (`auto_touch: false`, `reinforce: false`) 설정하여, 앞선 질의가 이후 질의를 개선하지 못하게 하세요.
- 표본 크기, 범주별 결과, 불확실성을 보고하세요. 작은 스모크 슬라이스를 일반적인 주장으로 확대하지 마세요.
- 집계 점수만이 아니라 오류와 미스를 포함한 질문별 출력을 저장하세요.

<a id="3-audit-or-reproduce-published-results"></a>

## 3. 공개된 결과 감사 및 재현

README는 증거 장부가 아니라 성적표입니다. 공개된 모든 수치에 대해 다음을 수행하세요.

1. [벤치마크 결과](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)에서 해당하는 섹션을 엽니다.
2. [`bench/results/`](https://github.com/ClickHouse/loom/blob/main/bench/results)에 커밋된 아티팩트를 확인합니다.
3. 데이터셋 슬라이스, Loom 커밋, 모델, reader, judge, 검색 모드, 시드를 확인합니다.
4. 결과 섹션에 있는 재현 명령을 그대로 실행합니다.
5. [방법론](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)을 참고하여 차이를 해석합니다. 여기에는 judge 보정, 부트스트랩 신뢰구간, 알려진 편향이 포함됩니다.

서드파티 데이터셋은 모두 이 저장소에서 재배포하지는 않습니다. 각 데이터셋의 출처와 라이선스는 [데이터셋](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md#datasets)에 정리되어 있습니다. 전체 공개 실행은 몇 시간이 걸릴 수 있고 제공자 요금이 발생할 수 있습니다. 문서화된 스모크 슬라이스로 먼저 시작한 다음, 비교 결과를 공개하기 전에 전체 프로토콜을 실행하세요.

<a id="what-to-publish"></a>

## 게시할 내용

README에는 대표성이 있고, 한 행으로 이해할 수 있으며, 커밋된 산출물과 재현 명령으로 뒷받침되는 결과만 게시합니다. 전체 카테고리 표, 모델, 프롬프트, 불확실성, 비용, 실패 분석, 한계는 [결과 원장](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md)에 정리합니다. 안정적인 평가 규칙은 [방법론](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md)에, 명령과 데이터셋 확보 방법은 [실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)에 정리합니다.

공개하는 모든 주장에는 다음을 명시해야 합니다.

- 작업과 표본 크기
- 지표, 그리고 그것이 검색, 답변 품질, 종단 간 성공 중 무엇인지
- 기준선과 짝지은 차이(있는 경우)
- 모델과 심사자, 또는 LLM 심사자를 사용하지 않았다는 사실
- 불확실성, 또는 질문 하나가 바뀔 때의 변화 단위
- 구축 비용과 쿼리 비용을 구분한 값
- 커밋된 산출물과 정확한 재현 명령
- 중요한 부정적 발견과 결과가 일반화되지 않는 범위

사용 가능한 모든 수치를 README에 게시하지는 마세요. 긴 점수표는 제품 결정을 가리고, 벤치마크 간의 잘못된 비교를 부추기며, 오래된 주장을 찾아내기 어렵게 만듭니다. README는 독자를 근거로 안내하고, 근거 자체는 문서에 담아야 합니다.

<a id="next"></a>

## 다음 단계

- [벤치마크 범위](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md): 공개된 측정 결과가 있는 항목, 하네스만 있는 항목, 아직 공개된 근거가 없는 항목을 정리합니다.
- [벤치마크 개요](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/overview.md): 측정하려는 지표에 맞는 하네스를 선택합니다.
- [벤치마크 실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md): 프로바이더, 토큰, 명령, 데이터셋을 설명합니다.
- [벤치마크 방법론](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/methodology.md): 비교 규칙과 한계를 설명합니다.
- [벤치마크 결과](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md): 커밋된 측정 결과와 아티팩트를 제공합니다.
