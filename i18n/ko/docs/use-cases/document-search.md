<!-- i18n: source=docs/use-cases/document-search.md blob=998687ee94e711f04bb8143f28cec818b63e994f model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/document-search.md), which is authoritative.

<a id="document-search-and-rag-for-agents"></a>

# 에이전트를 위한 문서 검색과 RAG

Loom은 문서를 인덱싱하고(Markdown과 텍스트, 그리고 extractor 서비스가 변환하는 PDF, `.docx`, `.pptx` 파일) 에이전트가 MCP, HTTP 또는 SDK를 통해 인용과 함께 문서를 검색할 수 있게 합니다. 이것은 검색 증강 생성(RAG)에서 검색을 맡는 부분입니다. 답변은 Loom이 반환한 구절을 바탕으로 에이전트나 모델이 작성합니다.

<a id="what-you-get"></a>

## 제공 기능

- **문서 구조를 따르는 청크.** `knowledge.add`는 텍스트와, 선택적으로 원본 파일을 저장합니다. Loom은 제목을 기준으로 텍스트를 나누고, 각 섹션에 윈도우를 적용하고, 모든 청크를 임베딩합니다. [Knowledge documents](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)
- **인용.** 검색 결과에는 해당 문서의 `source_uri`와 구절이 표시되므로, 에이전트가 출처를 인용하고 링크할 수 있습니다.
- **메모리, 문서, 대화를 아우르는 단일 검색.** `loom_search`는 호출자의 메모리 네임스페이스 전체에 걸쳐 메모리, knowledge, 프로필, 대화 검색을 한 번의 호출로 실행합니다. [Unified search](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md)
- **문서 소스.** 네임스페이스의 admin은 Loom이 문서를 가져올 소스를 등록할 수 있습니다. 동기화 워커를 사용하려면 `LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`가 필요합니다.
  [Document sources](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#document-sources)
- **유사도 하한.** `loom_search`는 유사도 하한보다 낮은 knowledge 결과를 제외하므로, 관련성이 낮은 구절이 모델에 전달되지 않습니다. [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md#loom_search)
- **모든 구절에 대한 접근 제어.** 문서는 viewer, writer, admin 역할이 있는 네임스페이스에 저장됩니다. [Namespaces and sharing](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)

<a id="measured"></a>

## 측정 결과

커밋된 PDF, DOCX, PPTX 파일 코퍼스(제외된 27개 파일)에서, OCR을 켠 상태(기본값)의 Loom extractor는 측정 실행에서 텍스트 재현율 0.999, 표 셀 F1 0.991을 기록했습니다([document parsing](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/results.md#document-parsing)). 문서에 대한 검색 품질은 일부만 측정되었습니다. 아직 빠져 있는 부분은 [coverage and known gaps](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/coverage.md)에서 확인할 수 있습니다.

<a id="start"></a>

## 시작하기

[빠른 시작](../getting-started/quickstart.md)에서는 extractor 서비스와 함께 스택을 실행합니다. 대시보드의 Library 페이지에서 문서를 추가하거나, 텍스트는 `knowledge.add`로, extractor가 변환하는 파일은 `knowledge.upload` 후 `knowledge.ingest`로 추가합니다([ingesting files](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md#ingesting-files)).

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 experimental로 표시되어 있습니다.
