<!-- i18n: source=docs/features/integrations/clickstack.md blob=008f7906fbb2d60f24b7f99be4e0ee44382e64f9 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/clickstack.md), which is authoritative.

<a id="clickstack-and-hyperdx"></a>

# ClickStack과 HyperDX

Loom은 항상 OpenTelemetry를 내보냅니다. HyperDX 소스, 대시보드, 삭제 단계는 별도로 설정해야 합니다.

Loom은 작업마다 스팬 하나를 내보내며, 로그와 메트릭도 함께 내보냅니다. 스팬에는 `loom.operation`, `loom.org`, `loom.namespace`, `loom.agent`, `loom.request_id`가 들어 있고, 값이 있으면 `loom.user_id` / `enduser.id`도 들어 있습니다. 기본적으로 Loom은 이 데이터를 자체 ClickHouse 서비스의 `default.otel_*` 테이블에 컬렉터 스키마 그대로 직접 삽입하므로, HyperDX는 실행할 컬렉터 없이 이 데이터를 읽습니다. `LOOM_CLICKSTACK_DATABASE`를 설정하면 여기와 아래 모든 쿼리에서 `default` 대신 해당 데이터베이스의 테이블을 사용합니다. 컬럼 구성은 `schemas/otel/clickstack_otel_tables.sql`에 있으며, ClickStack이 스키마를 변경하면 이 파일도 함께 변경해야 합니다.

모델 호출 스팬에는 OpenTelemetry GenAI 속성이 들어 있습니다([텔레메트리](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#traces-and-logs)). 이 속성은 HyperDX의 LLM 보기가 제공자, 작업, 토큰 사용량, 프롬프트 캐시 비율, 비용을 읽을 때 사용하는 키로 기록됩니다.

<a id="choose-the-export-path"></a>

## 내보내기 경로 선택

| `LOOM_OTEL_EXPORTER` | 동작                                                                                                                                      |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `ch`                 | 같은 서비스의 `default.otel_traces`, `otel_logs`, `otel_metrics_sum`, `otel_metrics_histogram`, `otel_metrics_gauge`에 삽입합니다          |
| `otlp`               | `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT`로 전송합니다(헤더는 `LOOM_OTEL_EXPORTER_OTLP_HEADERS`에 지정). 콜렉터나 다른 백엔드에 사용합니다        |
| 비어 있음(기본값)    | 엔드포인트가 설정되어 있으면 `otlp`, 아니면 `ch`                                                                                          |

`ch` 모드에서 Loom은 먼저 `default.otel_traces`가 있는지 확인합니다. 테이블이 없으면
익스포터는 로그를 한 줄 남기고 스스로 비활성화됩니다. 테이블을 만들려면 다음을 실행합니다.

```bash
clickhouse-client --multiquery < schemas/otel/clickstack_otel_tables.sql
```

`GET /v1/health?include=otel`은 확정된 익스포터를 보여 줍니다. 나머지 OTel 설정(내보내기 간격, 템포럴리티, 게이지 하트비트, 서비스 이름)은
[텔레메트리](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#exporters)에서 확인할 수 있습니다.

<a id="point-hyperdx-at-the-tables"></a>

## HyperDX를 테이블에 연결하기

행은 이미 서비스에 들어 있으므로 collector 및 수집 키(ingestion key) 단계는 건너뜁니다.
HyperDX **소스(source)** 세 개를 만드세요. `otel_traces`에 대한 트레이스, `otel_logs`에 대한 로그, `otel_metrics_*`에 대한 메트릭입니다. `deploy/clickstack_sources.sh`는 HyperDX API를 통해 이 소스들을 만들고 소스 간 상관관계를 연결합니다(호스팅 ClickStack은 콘솔 세션 쿠키인 `HDX_COOKIE`로 인증합니다).
각 소스의 필드 매핑은 [`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md)에서 확인할 수 있습니다.

<a id="dashboards-and-alerts"></a>

## 대시보드와 알림

[`deploy/hyperdx_dashboards.md`](https://github.com/ClickHouse/loom/blob/main/deploy/hyperdx_dashboards.md)는 레시피 모음입니다.

- **Loom — Memory Health**: 검색 품질 추이(`loom.search.top_similarity`), 작업 및 전송 방식별 호출 수와 오류, 시스템 상태, 비용과 토큰, 검색 계층별 지연 시간, 쓰기 결과, 그리고 요청 패널(요청률, 지연 시간, 오류, 가장 느린 요청, 테넌트별 트래픽, 트레이스별 로그)로 구성됩니다.
- **Loom — Code Plane**: 인덱스 지연, 티어 지연 및 기타 `loom.code.*` 게이지를 보여 줍니다. JSON 정의는 `deploy/dashboards/code_plane.json`에 있습니다.
- 부트스트랩 순서와 저장된 검색.

직접 타일을 작성할 때 주의할 두 가지 함정이 있습니다.

- **카운터는 누적값입니다.** `LOOM_OTEL_METRICS_DELTA_TEMPORALITY=true`로 설정하지 않으면 카운터의 행은 누적 합계입니다. 비율을 구하려면 합계가 아니라 연속된 데이터 포인트 간의 차이가 필요합니다. 이 설정과 대시보드는 함께 변경하세요.
- **HyperDX에서는 빈 결과가 0으로 읽힙니다.** 알림의 최소 샘플 가드는 행이 없는 상태가 아니라 정상 범위 내의 값을 반환해야 합니다. 그렇지 않으면 데이터가 없을 때 알림이 발생합니다.

메트릭 이름과 권장 임계값은 [메트릭 카탈로그](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md#metrics)에서 확인할 수 있습니다.

<a id="erasure"></a>

## 삭제

`LOOM_CLICKSTACK_DATABASE`가 필요합니다. ClickStack에는 사용자 데이터를 삭제하는 관리자 API가 없으므로,
백엔드(`server/erasure/app/backends.py`의 `ClickStackBackend`)가 해당 데이터베이스의 OTel 테이블에 대해
경량 `DELETE FROM` 문을 실행합니다.

| 테이블                                                                           | 검색하는 속성 컬럼        |
| -------------------------------------------------------------------------------- | ------------------------- |
| `otel_logs`                                                                      | `LogAttributes`           |
| `otel_traces`                                                                    | `SpanAttributes`          |
| `otel_metrics_gauge`, `_sum`, `_histogram`, `_exponential_histogram`, `_summary` | `Attributes`              |
| `hyperdx_sessions`                                                               | `ResourceAttributes`      |

- **사용자 범위**는 `LOOM_CLICKSTACK_USER_ID_KEYS`(기본값
  `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id`)에 있는 키 중 하나라도 일치하면 대상으로 삼으며(OR 조건),
  SDK마다 키 이름이 다르기 때문입니다. Loom의 요청 span에는 `enduser.id`가 있고, 모델 호출 span에는
  `loom.user_id`와 `langfuse.user.id`가 있습니다. Langfuse를 설정하면 호출자가 확인된 이후 요청이 여는 다른 모든
  span에도 이 키들이 붙습니다. 이 span들은 모두 `loom.org`도 가지므로 아래의 조직 보호 규칙이 적용됩니다.
- **네임스페이스 범위**는 `loom.namespace` 또는 `service.namespace`와 일치하는 항목을 대상으로 삼습니다. Loom의 exporter는
  `service.namespace`를 상수 `loom`으로 설정하고 테넌트는 `loom.namespace`에 담습니다. `service.namespace`는 같은
  ClickStack에 기록하는 다른 emitter를 위한 것이며, 값 `loom`은 일치 대상이 되지 않습니다.
- **조직 보호.** _다른_ 조직의 `loom.org` 태그가 붙은 행은 절대 삭제하지 않습니다. `loom.namespace`로 일치한 행은
  삭제를 수행하는 조직의 `loom.org`를 가지고 있어야 합니다. Loom의 op 및 도메인 메트릭(카운터와 히스토그램)에는
  `loom.namespace`만 있고 조직 태그가 없으므로, 네임스페이스 삭제 시 이 메트릭은 남습니다. 메모리 내용은 담고 있지
  않지만 `loom.namespace` 레이블이 네임스페이스 이름이며, 개인 네임스페이스의 경우 이 이름이 사용자의 이메일에서
  파생됩니다. 이 메트릭은 collector 테이블 자체에 TTL이 있다면 그 TTL이 제거할 때까지 남아 있습니다.
  `loom.org` 태그가 없는 행이 사용자 키 또는 `service.namespace`(다른 emitter)로 일치하면, 해당 대상자의 데이터를
  담고 있을 수 있으므로 삭제됩니다.

경량 삭제는 행 존재 마스크만 기록하므로, 행은 즉시 모든 읽기에서 사라지고 실제 바이트는 다음 병합 때 제거됩니다.
`ALTER TABLE … DELETE`는 영향을 받는 모든 파트를 다시 작성합니다. 속성 맵 값에 블룸 필터가 없는 테이블(메트릭 테이블에는
없습니다)은 조건절이 전체를 스캔합니다. `loom doctor`가 해당 테이블을 알려 줍니다.

이 단계는 삭제하기 전에 행 수를 세고 그 수를 보고합니다. 테이블이나 데이터베이스가 없으면 삭제할 것이 없는 것으로
간주하며, 그 외의 실패는 이 단계를 실패 처리(fail closed)하고 `erasure_recovery` 워커가 재시도합니다.

기본값에 의존하기 전에 데이터에 실제로 어떤 사용자 ID 키가 있는지 확인하세요.

```sql
SELECT DISTINCT arrayJoin(mapKeys(LogAttributes))
FROM default.otel_logs WHERE TimestampTime > now() - INTERVAL 1 DAY
```

이 단계가 속한 saga에 대해서는 [보존 및 삭제](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)를 참고하세요.

<a id="settings"></a>

## 설정

| 설정                               | 기본값                                                     | 효과                                         |
| ---------------------------------- | ---------------------------------------------------------- | -------------------------------------------- |
| `LOOM_OTEL_EXPORTER`               | `""`                                                       | `ch`, `otlp` 또는 자동                       |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` | `""`                                                       | 콜렉터 기본 URL                              |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`  | `""`                                                       | 쉼표로 구분한 `K=V` 헤더                     |
| `LOOM_OTEL_ENABLED`                | `true`                                                     | 마스터 스위치                                |
| `LOOM_CLICKSTACK_DATABASE`         | `""`                                                       | OTel 테이블이 있는 데이터베이스이며, 삭제 기능을 활성화함 |
| `LOOM_CLICKSTACK_USER_ID_KEYS`     | `user_id,user.id,enduser.id,loom.user_id,langfuse.user.id` | 사용자 범위 삭제가 일치 대상으로 삼는 속성 키 |

<a id="related"></a>

## 관련 문서

- [텔레메트리](https://github.com/ClickHouse/loom/blob/main/docs/features/observability/telemetry.md): 내보내는 항목과 전체 메트릭 카탈로그.
- [모니터링](https://github.com/ClickHouse/loom/blob/main/docs/operations/monitoring.md).
- [Langfuse](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/langfuse.md): 동일한 스팬을 모델 호출 트레이스로 제공합니다.
