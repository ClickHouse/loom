<!-- i18n: source=docs/guides/without-the-dashboard.md blob=d950ac1fe252e9d585139b1da802185c47ab7aa9 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/without-the-dashboard.md), which is authoritative.

<a id="use-loom-without-the-dashboard"></a>

# 대시보드 없이 Loom 사용하기

`/me` 대시보드에서 하는 모든 작업은 터미널, 스크립트 또는 에이전트의 셸에서도 할 수 있습니다. `loom login`으로 한 번 로그인한 다음 `loom call` 또는 Python SDK를 사용하세요. 이 페이지는 각 대시보드 화면을 해당하는 명령과 SDK 호출에 대응시켜 보여 줍니다. 목록에 없는 화면도 모두 일반 operation으로 처리할 수 있습니다(`loom call --list`).

<a id="sign-in"></a>

## 로그인

```console
$ loom login --server https://loom.example.com   # approve on any browser: password or SSO
$ loom whoami
$ loom logout
```

`loom login`은 스스로 갱신되는 세션을 저장합니다. 라우터 뒤에 [셀](https://github.com/ClickHouse/loom/blob/main/docs/architecture/cells.md)이 있는 환경에서 `loom login --org <org>`는 해당 조직의 셀에 직접 로그인합니다. 이때 라우터의 `GET /cells/lookup`을 호출하며, 라우터나 셀이 아직 없으면 지정받은 서버를 그대로 사용합니다. 429 응답을 받으면 최대 `--wait`초(기본값 120) 동안 기다리고, 그 외의 경우에는 로그인을 시작하지 않고 거부합니다. 이후 세션은 해당 셀 호스트의 세션이 되므로, `--server` 또는 `LOOM_BASE_URL`이 출력된 호스트를 가리키도록 설정하세요. CI와 서비스에는 코드를 승인해 줄 사람이 없으므로, 봇으로 로그인하거나(아래 참고) `LOOM_API_KEY`에 범위가 지정된 토큰을 설정해 사용합니다. [토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#sign-in-from-a-terminal-or-a-notebook)를 참고하세요.
SDK만 설치되어 있다면 같은 명령을 `python -m loom login`과 같은 형태로 실행하세요.

<a id="bots-and-ci-jobs"></a>

## 봇과 CI 작업

봇은 `loom login`이 필요 없습니다. 봇을 한 번 등록한 뒤(`loom bot create` 또는 플랫폼 관리자의 `loom call /v1/admin/bots.create`), `LOOM_CLIENT_ID`와 개인 키(`LOOM_CLIENT_KEY_FILE`, `LOOM_CLIENT_KEY`, 또는 `LOOM_CLIENT_KEY_SECRET`에 지정한 AWS Secrets Manager 시크릿) 중 하나를 사용해 실행합니다. CI 작업에서는 `LOOM_WORKLOAD_AUDIENCE`를 사용할 수 있습니다.
SDK는 토큰을 직접 발급받고 갱신합니다. 토큰 유효 시간은 1시간이며, 봇의 최대 유효 시간이 더 짧으면 그보다 짧아집니다. [싱글 사인온](https://github.com/ClickHouse/loom/blob/main/docs/operations/sso.md#bots-and-ci)을 참고하세요.

<a id="loom-call-the-whole-api"></a>

## `loom call`: 전체 API

```console
$ loom call --list                          # every operation and the role it needs
$ loom call memory.search query="deploy window" top_k:=5
$ loom call grants.approve namespace=support subject=bob@example.com decision=approve
$ loom call /v1/me/service_accounts         # a route outside the operation registry
```

CI와 에이전트용 개인 토큰에는 전용 명령이 있습니다.

```console
$ loom token create --name ci --namespace-role support=writer --namespace docs --ttl-days 0
$ loom token list
$ loom token revoke --name ci
```

- **작업 이름**은 `/v1/<op>`로 POST됩니다. `org`와 `namespace`는 토큰에서 가져오며, `--org`와 `--namespace`로 덮어쓸 수 있습니다.
- **경로**(`/`로 시작하는 값)는 입력한 그대로 전송됩니다. 본문이 없으면 GET, 본문이 있으면 POST입니다.
- **필드**는 텍스트의 경우 `key=value` 형식이며 입력한 그대로 전송됩니다. 그 외의 값은 `key:=json` 형식을 사용합니다(`top_k:=5`, `pinned:=true`, `tags:='["a","b"]'`). `--data`는 JSON 객체 전체를 받으며, `@file` 또는 표준 입력을 뜻하는 `-`도 사용할 수 있습니다.
- **출력:** 응답은 JSON으로 출력됩니다. 서버에서 오류가 발생하면 오류 코드가 포함된 한 줄을 stderr에 출력하고 종료 코드 1로 끝납니다. 명령 자체의 실수(알 수 없는 작업, 잘못된 형식의 필드)는 종료 코드 2로 끝납니다.

각 작업의 필드는 [작업 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations.md)에 나와 있습니다.

Python에서는 `agent.call("<op>", {...})`로 모든 작업에 접근할 수 있으며, 모든 작업은 `agent.client`의 메서드로도 제공됩니다. 레지스트리 밖의 경로는 `agent.account`와 `agent.deployment`에 있습니다.

<a id="dashboard-view--command-and-sdk-call"></a>

## 대시보드 보기 → 명령과 SDK 호출

| 대시보드                          | `loom call`                                                                                                                    | SDK (`agent = loom.Agent(org, namespace, name)`)                                                                      |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| 메모리: 검색, 탐색, 편집          | `memory.search`, `memory.list`, `memory.set`, `memory.update_by_id`, `memory.delete`                                           | `agent.memory.search(...)`, `.list(...)`, `.set(...)`                                                                 |
| 지식: 업로드, 읽기, 다운로드      | `knowledge.add`, `knowledge.list`, `knowledge.search`, `/v1/knowledge.download?org=…&namespace=…&source_uri=…`                 | `agent.knowledge.add(...)`, `.list()`, `.download(source_uri)`                                                        |
| 네임스페이스: 생성, 설정          | `namespace.register`, `namespace.get`, `namespace.list_mine`, `policy.set`, `blacklist.add`                                    | `agent.namespaces.*`, `agent.policy.*`, `agent.blacklist.*`                                                           |
| 네임스페이스 참여                 | `namespace.browse`, `namespace.join`, `namespace.leave`                                                                        | `agent.client.namespace.join(...)`                                                                                    |
| 참여 요청 승인                    | `grants.pending_for_me`, 이어서 `grants.approve namespace=… subject=… decision=approve` (또는 `reject`)                        | `agent.client.grants.pending_for_me()`, `agent.client.grants.approve(...)`                                            |
| 멤버와 권한                       | `grants.list`, `grants.create`, `grants.revoke`                                                                                | `agent.grants.*`                                                                                                      |
| 검토 대기열 (충돌)                | `conflicts.list`, `conflicts.resolve`, `learnings.review`                                                                      | `agent.conflicts.list()`, `.resolve(...)`                                                                             |
| 활동, 감사 로그                   | `profile.activity`, `audit.list`, `agents.connected`, `mentor.brief`, `memory.exposure_summary`, `memory.owner_report`         | `agent.audit.list(...)`, `agent.profile.*`                                                                            |
| 비용과 예산                       | `cost.summary`, `cost.budget_status`                                                                                           | `agent.cost.summary(...)`                                                                                             |
| 실행                              | `run.list`, `run.get`                                                                                                          | `agent.run.*`                                                                                                         |
| 코드 인덱스                       | `code.index_status`, `code.repo.add`, …                                                                                        | `agent.code.*`                                                                                                        |
| 삭제, 잊기                        | `erasure.request`, `erasure.status`, `erasure.list`, `erasure.export`, `memory.clear`                                          | `agent.erasure.*`                                                                                                     |
| 작업과 받은 편지함                | `task.create`, `task.pull`, `task.updates`, `inbox.*`                                                                          | `agent.tasks.*`                                                                                                       |
| 트래커 소스                       | `tracker.source.add`, `tracker.source.list`, `tracker.item`, …                                                                 | `agent.tracker.*`                                                                                                     |
| 그래프 (엔터티와 사실)            | `facts.traverse`, `facts.*`                                                                                                    | `agent.facts.*`                                                                                                       |
| 대화와 세션 브리프                | `conversation.*`, `session.brief_*`                                                                                            | `agent.conversations.*`, `agent.briefs.*`                                                                             |
| 설정: 토큰                        | `/v1/me/service_accounts`, `/v1/me/service_accounts.create`, `/v1/me/service_accounts.revoke`, `loom token create`             | `agent.account.tokens()`, `.create_token(...)`, `.revoke_token(name)`                                                 |
| 기본 네임스페이스                 | `/v1/me/set-default-namespace namespace=…`                                                                                     | `agent.account.set_default_namespace(...)`                                                                            |
| 공유 요약                         | `/v1/me/sharing-summary`                                                                                                       | `agent.account.sharing_summary()`                                                                                     |
| 관리자: 상태                      | `/v1/health`, `ops.health`                                                                                                     | `agent.deployment.health()`                                                                                           |
| 관리자: 백그라운드 워커           | `/v1/admin/workers.status`                                                                                                     | `agent.deployment.workers_status()`                                                                                   |
| 관리자: 모델과 프로바이더         | `/v1/admin/models`, `/v1/admin/models/validate`, `/v1/admin/models` (POST), `/v1/admin/models/reset`, `/v1/admin/models/retry` | `agent.deployment.models()`, `.validate_models(...)`, `.save_models(...)`, `.reset_models(...)`, `.retry_models(...)` |
| 관리자: 사용자, 클레임 해제       | `admin.users`, `/v1/admin/claim_release`                                                                                       | `agent.client.admin.users()`, `agent.deployment.release_claim(...)`                                                   |

대시보드와 동일한 역할이 적용됩니다. 참여를 승인하려면 해당 네임스페이스의
관리자 권한이 필요합니다. 관리자 보기에는 조직 관리자 또는 플랫폼 관리자 권한이 필요합니다.
