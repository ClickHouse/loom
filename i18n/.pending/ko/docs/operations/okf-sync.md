<!-- i18n: source=docs/operations/okf-sync.md blob=d6e5ec3b4a727da4861241c4b6c6b3efe03f98f2 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/okf-sync.md), which is authoritative.

<a id="copying-a-namespace-to-another-loom"></a>

# 네임스페이스를 다른 Loom으로 복사하기

프라이빗 네트워크 안의 작업이 일정에 따라 프라이빗 Loom의 네임스페이스 하나를 퍼블릭 Loom으로 복사합니다. `loom export-okf`가 프라이빗 Loom에서 네임스페이스를 읽고, `loom import-okf --prune`이 이를 퍼블릭 Loom에 씁니다. 퍼블릭 쪽에는 프라이빗 Loom으로 가는 경로나 토큰이 전혀 주어지지 않습니다. `deploy/okf-sync/cronjob.yaml`은 Kubernetes 예시로, 15분마다 실행되는 CronJob과 해당 ServiceAccount, NetworkPolicy를 포함합니다.

```bash
LOOM_API_KEY="$PRIVATE_VIEWER" loom export-okf --server-url "$PRIVATE_URL" \
  --namespace shared-lessons --no-evidence --out /tmp/bundle
LOOM_API_KEY="$PUBLIC_WRITER" loom import-okf /tmp/bundle --server-url "$PUBLIC_URL" \
  --namespace shared-lessons --agent okf-sync --prune
```

<a id="what-a-run-writes"></a>

## 실행이 기록하는 내용

- **변경이 없으면 아무것도 기록하지 않습니다.** 가져오기는 먼저 자신의 신원으로 저장된 활성 행을 읽고, 행이 이미 같은 값, 타입, 태그, confidence, `valid_at`, `valid_to`, `source_trace`, `source_class`(선언된 클래스, 없으면 `imported`)를 가지고 있으면 해당 concept를 건너뜁니다. 번들에 엔티티가 있으면 엔티티도 같은 방식으로 비교합니다. `memory.set`이 직접 추가하는 태그(`sensitivity:`, `attribute:`)는 차이로 보지 않습니다. 이미 활성 상태인 edge도 건너뜁니다. 번들의 여러 concept가 같은 키를 공유하면(서로 다른 작성자의 행), 가장 최근에 갱신된 것이 기록됩니다. 동일한 행을 다시 보내면 강화(reinforcement)로 집계되어, 실행할 때마다 버전 행 하나, 감사 행 하나, 그리고 더 높은 랭킹 가중치가 생깁니다. `sources:`가 있는 concept는 예외로, 가져오기가 수행하는 읽기가 연결된 source를 반환하지 않기 때문에 매 실행마다 기록됩니다. 대상이 다른 키의 행으로 병합하는 concept도 마찬가지입니다. 매 실행마다 다시 전송되어 해당 행을 강화하며, 마지막 줄에는 병합된 것으로 집계됩니다.
- **새로 생겼거나 변경된 concept마다 `memory.set`이 한 번씩 실행됩니다.** 다른 쓰기와 똑같이 충돌, 블랙리스트, 감사 검사를 거치며, 토큰이 작성자(actor)로 기록됩니다. 소스가 `derived` 또는 `external`로 표시한 행은 그 클래스를 유지하고, 나머지 행은 모두 `imported`로 저장됩니다. 이 값은 서버가 `okf:` trace에서 도출합니다. 양쪽의 org와 네임스페이스가 같으면 복사본에는 `origin:` 태그가 붙지 않습니다.
- **정리(prune)된 행마다 `memory.delete_version`이 한 번씩 실행됩니다.** `--prune`은 가져오기의 `--agent` 아래에 있고, 해당 토큰으로 작성되었으며, `source_trace`가 `okf:`로 시작하고, 키가 더 이상 번들에 없는 행만 삭제합니다. 네임스페이스에서 다른 사람이 작성한 행은 그대로 남습니다. 네임스페이스로 가져오는 번들마다 고유한 `--agent`를 지정하세요(`--prune`은 지정하지 않으면 거부됩니다). 삭제는 tombstone이며, `memory.restore`로 14일 이내에 행을 되살릴 수 있습니다.

마지막 줄에는 집계가 표시됩니다.
`500 rows from the bundle into acme/shared-lessons: 0 written, 500 unchanged, 0 failed …,
0 pruned`.
종료 코드 1은 대상을 읽을 수 없었거나 쓰기, 링크, 삭제, evidence 배치 또는 `--verify`가 실패했음을 뜻합니다. 종료 코드 2는 가져오기가 어떤 요청도 보내기 전에 거부되었음을 뜻하며, lint 결과, prune 가드, 디렉터리가 아닌 경로, 또는 누락된 `--agent`, 토큰, org가 원인입니다.

<a id="guards"></a>

## 보호 장치

- `--prune`은 개념이 없는 번들과, 개념 파일의 해시가 내보내기가 `index.md`에 기록한 `loom.corpus_digest`와 일치하지 않는 번들을 거부합니다. 수정된 번들이나 이전 번들이 있던 디렉터리로 내보낸 번들은 삭제를 일으키지 않습니다. `index.md`에 `loom.complete: true`가 없는 번들은 기록되지만 정리되지 않으며, 마지막 줄에 그 사실이 표시됩니다. 내보내기는 `--limit`에서 멈췄거나, `--include-tombstoned`로 실행되었거나(이런 번들에는 삭제된 행이 남습니다), 읽는 동안 소스가 바뀐 경우(페이지를 읽는 중의 쓰기, 또는 전체를 읽기 전에 삭제된 행) 이 표시를 남기지 않고 그 이유를 출력합니다. 이전 릴리스가 쓴 번들에는 표시가 없습니다.
- 내보내기는 소스에서 대체되었거나 중복된 행을 `status: deprecated`로 함께 담으므로, 소스가 다른 키로 합친 키도 정리되지 않고 사본에 남습니다.
- 내보내기는 소스를 오프셋으로 페이지 단위로 읽기 때문에, 목록을 읽는 도중 소스에 쓰기가 발생하면 페이지가 밀려 살아 있는 행을 건너뛸 수 있습니다. 페이지를 읽은 뒤에는 `memory.keys`를 읽는데, 이는 한 문장으로 모든 살아 있는 행의 식별자와 키를 가져오는 호출입니다(네임스페이스의 한 시점 기준). 그 행들이 모두 목록에 있었을 때만 목록이 완전하다고 봅니다. 그 시점 이후에 쓴 행은 이후의 변경이며 다음 실행이 반영합니다. 목록 조회는 최대 세 번 시도하며, 그래도 완전하지 않으면 `loom.complete`를 남기지 않습니다. 그러면 그 실행은 정리 없이 복사만 하고, 이후 더 조용한 실행이 정리합니다. `memory.keys`에 응답하지 못하는 오래된 소스나 살아 있는 행이 100,000개를 넘는 소스는 완전하다고 표시되지 않습니다.
- 각 삭제 전에 그 행을 자체 식별자로 다시 읽고, 다음 조건을 모두 만족할 때만 진행합니다. 행이 해당 `--agent` 아래에 있고, 자체 토큰으로 기록되었으며(에이전트 이름만으로는 호출자가 고른 값일 뿐입니다), 가져오기 목록이 `okf:` 추적과 함께 본 버전 그대로이며, `memory.lock`이 없어야 합니다. 이 중 하나라도 어긋나면 유지한 것으로 집계하고, 그 사이에 사라졌거나 다른 에이전트 아래로 옮겨진 행은 집계하지 않습니다. 삭제는 `memory.delete_version`으로 하며, 이 호출은 정확히 그 식별자 아래의 그 버전만 지우고 다른 에이전트의 행은 지우지 않습니다. 삭제가 실행되는 시점에 진행 중인 해당 키의 `memory.set` 또는 `memory.bulk_set`은 자신의 행을 유지합니다. 예외는 [메모리 아키텍처](../architecture/memory-architecture.md#lifecycle)에 있습니다.
- `memory.delete_version`을 모르는 오래된 대상 레플리카는 404로 응답하고 아무것도 삭제하지 않습니다. 정리는 이 실패를 집계하고 가져오기는 1로 종료됩니다. 어느 쪽 Loom이든 롤링 업그레이드 중에 중단할 필요는 없습니다.
- 가져오기는 내보내기가 0으로 종료되었을 때만(`sh -e`) 실행되며, 각 실행은 번들을 새로운 `emptyDir`에 쓰므로 이전 실행이 남긴 번들은 가져오지 않습니다.
- `--dry-run`은 같은 검사를 거친 뒤 기록하거나 삭제할 항목을 나열하고, 아무것도 쓰지 않습니다.
- `concurrencyPolicy: Forbid`는 두 실행이 겹치지 않게 합니다.

한 번의 실행에서 정리할 수 있는 행 수에는 상한이 없습니다. 소스 네임스페이스가 의도적으로 대부분의 행을 잃으면 운영자 개입 없이 다음 실행에서 그대로 전파됩니다. 모든 행을 잃은 경우는 전파되지 않습니다. 가져오기가 빈 번들을 거부하므로 작업이 매 실행마다 실패합니다. CronJob을 일시 중지하고(`kubectl patch cronjob okf-sync -p '{"spec":{"suspend":true}}'`) 공개 Loom의 사본을 `memory.delete`로 삭제하세요. 내용 자체를 없애야 한다면 삭제 요청(erasure)을 통해 지우세요.

실수로 정리된 키는 그 키가 담긴 번들로 다음에 실행될 때 새 행으로 다시 기록되며, 이때 `memory_id`가 바뀝니다. 원래 행을 되살리려면 `memory.restore`를 사용하세요.

<a id="what-a-run-costs"></a>

## 실행 비용

변경할 내용이 없는 실행은 읽기 요청만 보냅니다. `sensitivity: high` 행을 키로 읽을 때마다 감사 기록(`memory.sensitive_read`)이 남습니다. 따라서 가져오기 ID로 접근하는 high 행이 있으면, 조용한 실행이라도 해당 행을 키로 읽는 쪽마다 high 행 하나당 감사 행이 하나씩 추가됩니다. 네임스페이스에 행이 N개 있을 때의 요청 수는 다음과 같습니다.

| 쪽                   | 요청                                                                                                                                                                                                                                                                                                                              |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 내보내기, 비공개 Loom | 100행 단위의 `memory.list` 페이지(`LOOM_MEMORY_LIST_VIEWER_LIMIT`가 뷰어 토큰의 페이지 크기를 제한함), `memory.keys` 1회, 빌드 커밋을 확인하기 위한 `GET /v1/health` 1회, (에이전트, 사용자, 세션) 각각에 대해 500행마다 `memory.bulk_get` 1회, 유형이 있는 엣지 2,000개마다 `memory.export_evidence` 페이지 1개                      |
| 가져오기, 공개 Loom   | 1,000행 단위의 `memory.list` 페이지, 500행마다 `memory.bulk_get` 1회, 그리고 엣지 페이지를 다시 읽음. 이어서 변경된 행마다 `memory.set` 1회, 새 엣지마다 `memory.link` 1회, 삭제 후보마다 `memory.bulk_get`(번들에서 키가 사라진 `okf:` 추적이 있는 행, 유지 여부와 무관), 삭제된 행마다 `memory.delete_version` 1회 |

작성자가 한 명인 501행 네임스페이스는 모두 합해 15회의 요청을 사용합니다. 내보내기 쪽은 `memory.list` 6회, `memory.keys` 1회, health 읽기 1회, `memory.bulk_get` 2회, 엣지 페이지 1개이고, 가져오기 쪽은 각각 1회, 2회, 1회입니다. 하나의 ID로 50,000행을 처리하면 실행 한 번에 약 750회의 요청이 발생하며, 이는 15분 주기 기준으로 초당 1회에도 미치지 않습니다. 훅이 기록하는 것처럼 네임스페이스가 여러 세션에 걸쳐 있으면 세션마다 `memory.bulk_get` 1회가 필요하며, 페이지를 읽는 동안 소스가 변경되면 내보내기는 소스를 최대 세 번까지 나열합니다.

<a id="tokens"></a>

## 토큰

작업은 토큰 두 개를 가지며, 각 토큰은 하나의 네임스페이스로만 범위가 제한됩니다.

```bash
# On the private Loom: read the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=viewer \
  --ttl-days 90 --save ./private-viewer.jwt
# On the public Loom: write the namespace, nothing else.
loom mint-token --subject okf-sync --org <org> --namespace-role shared-lessons=writer \
  --ttl-days 90 --save ./public-writer.jwt
```

CronJob이 읽는 Secret에 저장합니다.

```bash
kubectl create secret generic okf-sync \
  --from-file=private-viewer=./private-viewer.jwt \
  --from-file=public-writer=./public-writer.jwt
```

TTL이 끝나기 전에 같은 `--subject`와 같은 종류(기본값인 서비스 계정)로 토큰을 다시 발급하고 Secret을 교체합니다. Prune은 해당 토큰의 actor가 기록한 행만 삭제하므로(서비스 계정은 `process:<subject>`, 사용자 토큰은 `human:<subject>`), subject나 종류가 다른 토큰을 사용하면 이전 사본이 그대로 남습니다(마지막 줄은 이를 유지된 것으로 집계합니다). 유출된 토큰은 `loom revoke-token`으로 폐기합니다. 인증이 꺼진 서버에서는 어떤 행에도 actor가 없으므로 prune은 아무것도 삭제하지 않고 모든 후보를 유지된 것으로 집계합니다.

- 공개 Loom에서는 작업의 토큰이 해당 네임스페이스의 유일한 writer입니다. 이 네임스페이스의 reader(공개 CI의 봇)는 `viewer` 토큰을 가집니다.
- 비공개 Loom에서는 네임스페이스에 게시하는 사람과 에이전트가 이 네임스페이스의 writer입니다. 이를 읽는 다른 모든 주체는 `viewer`를 가지므로, writer가 게시하려 하지 않은 내용은 네임스페이스에 도달하지 않습니다.

<a id="the-copied-namespace-is-published"></a>

## 복사된 네임스페이스는 공개된다

네임스페이스의 모든 내용은 한 번의 스케줄 간격 안에 공개 Loom에 반영됩니다. 비공개 쪽에서 이 네임스페이스에 쓰는 것은 공개하는 것으로 간주하세요. 기밀 내용은 다른 네임스페이스에 보관하세요. `sensitivity: high`로 표시된 행은 비공개 쪽에 남습니다(`--include-high`를 지정하지 않으면 내보내기에서 제외됩니다). 행이 `high`가 되면 다음 실행 때 공개 쪽에서 정리됩니다. 단, 내보내기가 담을 마지막 행이었다면 예외입니다. 이 경우 번들에 개념이 없으므로 가져오기가 번들을 거부하고 공개 사본은 그대로 남습니다. 공개 Loom에서 `memory.delete`로 삭제하거나, 내용을 반드시 없애야 하면 삭제 요청(erasure)을 통해 지우세요. 내보내기는 `memory.bulk_get`으로 읽은 `sensitivity` 값, 또는 `memory.set`과 `memory.update`가 추가하는 `sensitivity:high` 태그로 `high` 행을 판별합니다. `memory.bulk_get`이 `sensitivity`를 반환하지 않는 비공개 Loom에 대해서는 모든 행이 비공개 쪽에 남으므로(내보내기는 이를 "no known sensitivity"로 집계합니다), 번들에 개념이 없어 `--prune`이 번들을 거부합니다. 정리(prune)는 툼스톤 처리입니다. 값은 14일 동안 복원할 수 있고 공개 Loom의 버전 기록에도 남습니다. 공개 Loom에서 반드시 제거해야 하는 내용은 [삭제(erasure)](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md)를 통해 처리하세요.

<a id="network"></a>

## 네트워크

NetworkPolicy는 인그레스를 허용하지 않습니다. 이그레스는 DNS, 7777 포트의 프라이빗 Loom 웹 파드, 443 포트의 퍼블릭 Loom 주소로만 제한됩니다. NetworkPolicy는 호스트 이름이 아니라 주소를 기준으로 매칭하므로, 퍼블릭 Loom의 주소를 `ipBlock`에 나열하세요. 주소가 자주 바뀐다면 CNI의 호스트 이름 정책이나 이그레스 프록시를 사용하세요.

프라이빗 릴리스에서 `networkPolicy.enabled`를 `ingressFrom`과 함께 설정한 경우, 작업의 파드(`app.kubernetes.io/name: okf-sync`) 또는 해당 네임스페이스를 `ingressFrom`에 추가하세요. 그러지 않으면 릴리스 자체의 정책이 내보내기를 거부합니다.

<a id="related"></a>

## 관련 문서

- [OKF 번들](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/okf.md): 번들 형식과 가져오기.
- [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md): CronJob이 파드 보안 설정을 그대로 가져오는 차트.
