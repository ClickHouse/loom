<!-- i18n: source=docs/operations/runtime-settings.md blob=b519ead8aee373492c371073baaa1fe49501d971 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/runtime-settings.md), which is authoritative.

<a id="runtime-settings"></a>

# 런타임 설정

모든 설정은 시작할 때 읽는 환경 변수 `LOOM_<FIELD>`이며, YAML 설정 파일에 넣을 수도 있습니다([구성 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md)). 레퍼런스에서 **live**로 표시된 설정은 Loom이 실행되는 동안에도 배포 전체에 적용되는 오버라이드를 받을 수 있습니다(검색과 랭킹 `memory_search_*`, 답변 합성 `memory_ask_*`, 추출·컨텍스트·지식 튜닝, ClickHouse 삽입 배치 `ch_batched_insert*` 등). 그 외의 모든 설정은 시작할 때 한 번만 읽으며, 변경하려면 재시작해야 합니다. 보정(calibration)이 대체하는 유사도 게이트(`memory_search_min_similarity`, `memory_search_abstain_threshold`, `memory_search_hyde_fallback_threshold`, `memory_search_query_rewrite_threshold`)는 live가 아닙니다. `LOOM_ADAPTIVE_THRESHOLDS`가 켜져 있으면 보정이 없을 때만 적용됩니다.

<a id="see-what-is-in-force"></a>

## 적용 중인 설정 확인

```bash
loom settings list --changed              # off its default, or overridden
loom settings list --prefix memory_ask_   # one area
loom settings list --json
```

각 줄에는 값, 출처, live 여부가 표시됩니다.

```text
memory_ask_recency_weight = 0.35  (override, live)
ch_host = "clickhouse"  (env)
```

| 출처            | 의미                                                                                     |
| --------------- | ---------------------------------------------------------------------------------------- |
| `override`      | `loom settings set` 또는 `/v1/admin/settings.set`으로 설정함                             |
| `dashboard`     | 설정 → Models & keys에 저장된 모델 구성                                                  |
| `env`           | 프로세스 환경                                                                            |
| `env_file`      | dotenv 파일(`LOOM_ENV_FILE`, 기본값 `.env`). 환경으로 복사된 경우도 포함                 |
| `settings_file` | YAML 설정 파일(`LOOM_SETTINGS_FILE`). `scripts/serve.sh`가 내보낸 경우도 포함            |
| `inferred`      | 시작할 때 다른 설정에서 채워짐(LLM 제공자, 로컬 모델의 너비)                             |
| `default`       | 내장 기본값                                                                              |

시크릿은 길이만 표시됩니다.

<a id="override-a-live-setting"></a>

## live 설정 오버라이드

```bash
loom settings set memory_ask_recency_weight 0.35
loom settings reset memory_ask_recency_weight
```

값은 해당 환경 변수로 지정했을 때와 같은 방식으로 기록되고 검증됩니다. 범위를 벗어난 값이나 live가 아닌 설정은 거부되며 아무것도 저장되지 않습니다. 오버라이드는 `loom.runtime_overrides`의 한 행입니다. 이를 저장한 프로세스는 즉시 적용하고, 워커를 포함한 다른 모든 프로세스는 `LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS`(기본값 10) 안에 적용합니다. 오버라이드는 재시작 후에도 유지되며, 리셋하기 전까지 환경과 설정 파일보다 우선합니다. 리셋하면 각 프로세스 자체의 값이나 기본값으로 돌아갑니다. `reset`은 저장된 오버라이드가 없는 설정은 거부하며, 이후 빌드에 더 이상 없는 설정의 오버라이드는 제거합니다. 변경할 때마다 변경 전후 값이 담긴 `setting.changed` 감사 행이 기록됩니다.

`--expect <value>`(HTTP에서는 `expected`)는 저장된 오버라이드를 새로 읽은 뒤 적용 중인 값이 지정한 값일 때만 변경하며, 그렇지 않으면 찾은 값과 함께 `409 PRECONDITION_FAILED`로 응답합니다. 그 사이 다른 레플리카나 CLI로 이루어진 변경을 모르는 채 덮어쓰지 않습니다. 같은 순간에 보낸 두 변경은 여전히 둘 다 통과할 수 있습니다.

<a id="see-and-undo-past-changes"></a>

## 과거 변경 확인 및 되돌리기

```bash
loom settings history memory_ask_recency_weight
loom settings history --limit 20      # every setting
```

각 줄은 기록된 변경 하나이며 최신순입니다. 언제, 누가(`cli:<user>` 또는 토큰 subject), 변경 전후 값이 표시됩니다. 변경 전 값이 오버라이드였다면 그것은 배포의 값이므로, `loom settings set`으로 다시 설정해 되돌립니다. 그렇지 않은 경우는 변경을 수행한 프로세스의 값(환경 또는 기본값)이었으며 `(undo: reset)`으로 표시됩니다. 이때는 `loom settings reset`으로 되돌리며, 각 레플리카 자체의 값이 복원됩니다. 변경 전 값이 없는 변경(이 빌드에 더 이상 없는 설정의 리셋)은 되돌릴 것이 없습니다.

오버라이드는 배포의 기본값이므로 네임스페이스나 요청이 설정한 값이 여전히 우선합니다. 네임스페이스의 `retrieval_defaults`(`top_k`, `min_similarity`, `reinforcement_weight`와 `namespace.register`가 받는 그 밖의 검색 옵션)와 요청 자체의 필드가 그렇습니다.

같은 작업을 플랫폼 관리자 토큰으로 HTTP를 통해 할 수 있습니다. 인증이 꺼져 있으면 루프백 주소(`localhost`)에서만 변경이 허용됩니다. 그렇지 않으면 웹 페이지가 DNS 리바인딩을 통해 접근할 수 있기 때문입니다.

```bash
curl -s "$LOOM_BASE_URL/v1/admin/settings.list" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"changed": true}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "value": 0.35}'
curl -s "$LOOM_BASE_URL/v1/admin/settings.set" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name": "memory_ask_recency_weight", "clear": true}'
```

`POST /v1/admin/settings.history`는 `{"name": "...", "limit": 50}`를 받습니다. Python SDK에는 `agent.deployment.settings()`, `set_setting(name, value, expected=...)`, `reset_setting(name, expected=...)`, `setting_history(name)`이 있습니다.

<a id="during-a-rolling-upgrade"></a>

## 롤링 업그레이드 중

해당 설정이 없거나, live로 읽지 않거나, 저장된 값을 필드가 거부하는 빌드는 저장된 오버라이드를 한 번 로그에 남기고 무시하며, 다른 프로세스는 계속 적용합니다. 런타임 설정이 생기기 전의 빌드는 저장된 오버라이드를 아무 표시 없이 무시하므로, 그런 빌드가 아직 서비스 중이면 오버라이드는 더 새로운 레플리카에만 적용됩니다. 랭킹 설정은 `memory.search`가 반환하는 결과를 바꾸므로, 변경을 유지하기 전에 벤치마크로 측정하세요([벤치마크 실행](https://github.com/ClickHouse/loom/blob/main/docs/benchmarks/running.md)).
