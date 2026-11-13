<!-- i18n: source=docs/use-cases/autonomous-agents.md blob=775d2cb2e37c6672608876e9345bb6db7fdddc15 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/use-cases/autonomous-agents.md), which is authoritative.

<a id="memory-recall-audit-and-erasure-for-autonomous-agents"></a>

# 자율 에이전트를 위한 메모리, 회상, 감사, 삭제

Loom은 자율 에이전트에게 나중에 회상할 수 있는 메모리, 누가 무엇을 썼는지에 대한 감사 기록, 그리고 Loom이 보관하는 모든 위치에서 특정 범위를 삭제하는 기능을 제공합니다. 이들은 서버 작업이며, ClickHouse(라이브 메모리와 지식 상태의 경우 Postgres)에 저장됩니다.

<a id="what-you-get"></a>

## 제공 기능

- **메모리와 recall.** 출처(provenance)가 있는 버전 관리형 사실, 충분히 가까운 결과가 없으면 응답을 보류하는 검색, 성공한 실행이 활용한 메모리에 대한 랭킹 가산점을 제공합니다. [Memory](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md) · [Runs](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md)
- **거부(Refusal).** 고정(pinned)된 사실은 이와 모순되는 쓰기를 거부하며, `blacklist.add`는 어떤 쓰기도 저장할 수 없는 패턴을 등록합니다. [Conflicts and refusal](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md)
- **감사(Audit).** 메모리를 변경하는 각 작업은 내용의 SHA-256을 담은 감사 행을 남기며, 워커가 1시간 단위 구간을 해시 체인으로 봉인하므로 수정이나 삭제를 탐지할 수 있습니다. 전송 계층의 감사 쓰기는 fail-open 방식이므로(insert가 실패하면 해당 행이 유실됩니다), Postgres authority에서는 메모리 리비전의 감사 행이 리비전과 함께 저장됩니다. [Audit log](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/audit.md#limits-and-failure-modes)
- **검증 가능한 삭제.** `erasure.request`는 메모리 내용을 보관하는 모든 테이블에서 특정 범위(scope)를 삭제하며(기본값은 tombstone, `purge=true`는 `user_id` 또는 `namespace_list` 범위의 행을 삭제), 잔존 데이터 조사(residue census)와 영수증(receipt)으로 마무리합니다. [Retention and erasure](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)
- **롤백.** `namespace.rollback`은 네임스페이스의 모든 메모리를 특정 시점의 버전으로 되돌립니다. [History and provenance](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md)
- **헤드리스 운영.** 봇은 자체 신원을 부여받고(자가 가입이 열려 있는 곳에서는 직접 가입합니다), 첫 실행 전에 연결을 확인하며, 응답이 없을 때 진단할 수 있습니다. [Headless bots](https://github.com/ClickHouse/loom/blob/main/docs/guides/headless-bots.md)

감사와 삭제는 엔지니어링 기본 요소이며, 컴플라이언스 인증이 아닙니다. [Security](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md)

이 페이지의 일부 op는 [상태 페이지](https://github.com/ClickHouse/loom/blob/main/docs/reference/status.md)에서 experimental로 표시되어 있습니다.
