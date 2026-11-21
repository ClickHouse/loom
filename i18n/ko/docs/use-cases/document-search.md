<!-- i18n: source=docs/use-cases/document-search.md blob=0848e72f516323271d09e930d51b4314d82ce625 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# 에이전트를 위한 문서 검색과 RAG

Loom은 문서(Markdown과 텍스트, 그리고 extractor 서비스가 변환하는 PDF, `.docx`, `.pptx` 파일)를 색인하고, 에이전트가 MCP, HTTP 또는 SDK를 통해 인용과 함께 문서를 검색할 수 있게 합니다. 검색 증강 생성(RAG)에서 검색을 맡는 부분이며, 답변은 Loom이 반환한 구절을 바탕으로 에이전트나 모델이 작성합니다.

<a id="what-you-get"></a>

## 제공 기능

- **문서 구조를 따르는 청크.** `knowledge.add`는 텍스트와, 선택적으로 원본 파일을 저장합니다. Loom은 제목을 기준으로 문서를 나누고, 각 섹션에 윈도우를 적용하고, 모든 청크를 임베딩합니다. [지식 문서](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **인용.** 검색 결과에는 해당 문서의 `source_uri`와 구절이 표시되므로, 에이전트가 출처를 인용하고 링크할 수 있습니다.
- **메모리, 문서, 대화를 아우르는 하나의 검색.** `loom_search`는 호출자의 메모리 네임스페이스 전체에 걸쳐 메모리, 지식, 프로필, 대화 검색을 한 번의 호출로 실행합니다. [통합 검색](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **문서 소스.** 네임스페이스의 관리자는 Loom이 문서를 가져올 소스를 등록할 수 있습니다. 동기화 워커를 사용하려면 `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`가 필요합니다.
  [문서 소스](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **코드 옆의 문서.** 저장소의 trunk 코드 네임스페이스에 있는 소스에서 가져온 문서에는 해당 저장소의 `repo:<id>` 태그가 붙으므로, 그 저장소를 대상으로 한 검색에서 코드와 함께 문서를 읽을 수 있습니다. [문서 소스](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources) ·
  [코드에 연결된 메모리](memory-linked-to-code.md)
- **유사도 하한.** `loom_search`는 약한 구절을 모델에 넘기는 대신, 유사도 하한보다 낮은 지식 검색 결과를 제외합니다. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **모든 구절에 적용되는 접근 제어.** 문서는 viewer, writer, admin 역할이 있는 네임스페이스에 저장됩니다. [네임스페이스와 공유](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 측정 결과

PDF, DOCX, PPTX 파일로 이루어진 커밋된 코퍼스(보류된 27개 파일)에서, OCR을 켠 상태(기본값)의 Loom extractor는 측정한 실행에서 텍스트 재현율 0.999, 표 셀 F1 0.991을 기록했습니다([문서 파싱](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)).
문서에 대한 검색 품질은 일부만 측정되었으며, 아직 부족한 부분은 [커버리지와 알려진 공백](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)에서 확인할 수 있습니다.

<a id="start"></a>

## 시작하기

[퀵스타트](../getting-started/quickstart.md)에서는 extractor 서비스와 함께 스택을 실행합니다. 대시보드의 Library 페이지에서 문서를 추가하거나, 텍스트는 `knowledge.add`로 추가합니다. extractor가 변환하는 파일은 `knowledge.upload` 다음에 `knowledge.ingest`를 사용합니다([파일 수집](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적(experimental)으로 표시되어 있습니다.
