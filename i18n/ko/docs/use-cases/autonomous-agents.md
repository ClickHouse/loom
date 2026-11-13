<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# 자율 에이전트를 위한 메모리, 리콜, 감사, 삭제

Loom은 자율 에이전트에 나중에 리콜할 수 있는 메모리, 누가 무엇을 썼는지 보여 주는 감사 기록, 그리고 Loom이 보관하는 모든 위치에서 특정 스코프를 삭제하는 기능을 제공합니다. 이 기능은 서버 작업이며 ClickHouse에 저장됩니다(라이브 메모리와 지식 상태는 Postgres에 저장할 수도 있습니다).

<a id="what-you-get"></a>

## 제공 기능

- **메모리와 리콜.** 출처 정보가 있는 버전 관리 사실, 충분히 가까운 결과가 없으면 응답을 보류하는 검색, 성공한 실행이 활용한 메모리에 대한 랭킹 가중치를 제공합니다. [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **거부.** 고정(pinned)된 사실은 모순되는 쓰기를 거부하며, `blacklist.add`는 어떤 쓰기도 저장할 수 없는 패턴을 등록합니다. [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **감사.** 메모리를 변경하는 각 작업은 콘텐츠의 SHA-256을 담은 감사 행을 남기며, 워커가 한 시간 단위 구간을 해시 체인으로 봉인하므로 수정이나 삭제를 감지할 수 있습니다. 트랜스포트의 감사 쓰기는 fail-open 방식이어서 삽입이 실패하면 해당 행이 유실됩니다. Postgres authority에서는 메모리 리비전의 감사 행이 리비전과 함께 저장됩니다. [Audit log](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **검증 가능한 삭제.** `erasure.request`는 메모리 콘텐츠를 보관하는 모든 테이블에서 스코프를 삭제하고(기본값은 툼스톤, `purge=true`는 `user_id` 또는 `namespace_list` 스코프의 행을 삭제), 잔여 데이터 조사와 영수증으로 마무리합니다. [Retention and erasure](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **롤백.** `namespace.rollback`은 네임스페이스의 모든 메모리를 특정 시점의 버전으로 되돌립니다. [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **헤드리스 운영.** 봇은 자체 ID를 받고(자체 가입이 열려 있으면 직접 가입), 첫 실행 전에 연결을 확인하며, 응답이 없을 때 원인을 진단할 수 있습니다. [Headless bots](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

감사와 삭제는 엔지니어링 기본 기능이며 컴플라이언스 인증이 아닙니다. [Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 실험적(experimental)으로 표시되어 있습니다.
