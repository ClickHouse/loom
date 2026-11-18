<!-- i18n: source=docs/architecture/claude-memory-adapter.md blob=b72a628092f2f86d148e87a01ed0f9e99469b83f model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/claude-memory-adapter.md), which is authoritative.

<a id="claude-api-memory-file-tree"></a>

# Claude API 메모리 파일 트리

Claude API 메모리 어댑터는 프레임워크의 Python 메모리 도구 베이스와 TypeScript 메모리 핸들러를 Loom 지식 문서를 통해 구현합니다. 프레임워크 SDK는 선택적 의존성입니다. 두 구현은 동일한 문서 URI와 스토어 태그를 사용하므로, 한 언어로 만든 파일을 다른 언어에서도 읽을 수 있습니다.

파일은 `loom-memory://<store>/memories/<path>` 형식입니다. 디렉터리는 URI 접두사이며, 별도의 디렉터리 행은 없습니다. 경로는 정규화되며, `/memories` 아래에 남지 않는 경로는 거부됩니다. 디렉터리 읽기는 스토어 태그, URI 접두사, 투영된 URI 필드와 함께 `knowledge.list`를 사용하고, 파일 읽기는 `knowledge.get`을 사용합니다. 편집에는 `knowledge.upsert`를, 삭제에는 크기가 제한된 `knowledge.bulk_delete` 호출을 사용합니다. 이름 변경은 각 파일을 복사한 뒤 이전 URI를 삭제합니다. 이름 변경이 실패하면 복사본이 남을 수 있으며, 동시에 수행되는 읽기-수정-쓰기 편집에서는 나중의 쓰기가 유지됩니다.

<a id="source-entry-points"></a>

## 소스 진입점

- `sdk/python/loom/integrations/claude_memory.py`: `AsyncLoomMemoryTool` 및
  `LoomMemoryTool`.
- `sdk/typescript/src/integrations/claude-memory.ts`: `loomMemoryTool` 및
  `loomMemoryHandlers`.

<a id="invariants"></a>

## 불변 조건

스토어는 파일을 그룹화하며, 접근은 네임스페이스 권한 부여로 제어됩니다. 비공개 사용자 트리에는 별도의 네임스페이스가 필요합니다. 어댑터는 스토어 이름을 접근 권한 부여로 해석하지 않습니다. 행위자, 소스 출처, 문서 리비전, blob 정리 및 삭제는 기존 지식 작업을 따릅니다. 로컬 영구 상태, 데이터베이스 또는 스키마는 추가되지 않습니다. 디렉터리 작업은 원자적 스냅샷이 아닙니다.
