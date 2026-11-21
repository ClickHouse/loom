<!-- i18n: source=docs/features/integrations/agent-substrate.md blob=3054a7336ba22e483dd7ab222be1f25e1bd0deee model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md), which is authoritative.

<a id="agent-substrate"></a>

# 에이전트 서브스트레이트

[에이전트 서브스트레이트](https://ate.dev) v1.0.0과 Loom의 Helm 차트가 하나의 Kubernetes 클러스터에 있어야 합니다. Substrate의 egress 게이트웨이가 각 에이전트의 Loom 토큰을 해당 에이전트의 MCP 호출에 추가합니다.

토큰은 actor의 템플릿이나 스냅샷에 포함되지 않습니다. Substrate v1.0.0은 Kubernetes 1.37 이상이 필요하며, 노드에 gVisor 또는 microVM 런타임이 있어야 합니다. 아래 단계에는 Substrate의 Kubernetes credential provider도 필요하며, 이는 `ate-setup deploy ... --credential-provider='{"name":"k8s.io"}'` 명령으로 배포됩니다.

아래의 Substrate 단계는 v1.0.0 버전의 Substrate 공식 문서를 따릅니다. 이 페이지의 내용은 실제 클러스터에서 처음부터 끝까지 실행해 보지 않았으므로, 사용 중인 Substrate 버전에 맞는지 확인하십시오.

<a id="how-the-pieces-fit"></a>

## 구성 요소가 맞물리는 방식

| 구성 요소         | 실행 위치                                                              | 하는 일                                                                      |
| ----------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Loom              | 네임스페이스 `loom`에서 `helm install loom`                            | 메모리 서버입니다. Service는 `loom.loom.svc.cluster.local:7777`입니다        |
| 에이전트          | atespace 안의 Substrate actor                                          | 자리 표시자 `Authorization` 헤더와 함께 Loom의 MCP 엔드포인트 `/mcp`를 호출합니다 |
| Egress 정책       | Substrate의 컨트롤 플레인(`kubectl ate`), `atenet-egress`가 적용       | actor가 Loom에 접근하도록 허용하며, 규칙을 추가하지 않는 한 그 외에는 허용하지 않습니다 |
| Loom 토큰         | Kubernetes Secret                                                      | 에이전트마다 하나씩 있으며, egress 게이트웨이가 이를 `Authorization` 헤더에 넣습니다 |

모든 요청은 Substrate의 egress 게이트웨이(`ate-system`의 `atenet-egress`)를 통해 actor를 벗어나며, 게이트웨이는 actor의 정책이 허용하지 않는 모든 egress를 거부합니다. 따라서 Loom에는 actor가 아니라 게이트웨이 파드에서 온 연결로 보입니다. 각 행에는 토큰의 subject가 기록되며, 이를 통해 actor를 구분할 수 있습니다. 행의 `agent`는 MCP 클라이언트의 이름이며, 같은 클라이언트를 실행하는 actor끼리 공유합니다.

<a id="1-install-loom-in-the-cluster"></a>

## 1. 클러스터에 Loom 설치

[Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md)에서 설명하는 대로 차트를 네임스페이스 `loom`에 릴리스 이름 `loom`으로 설치합니다. 이렇게 하면 Service 이름이 `loom`이 됩니다. 에이전트에는 Ingress가 필요하지 않습니다. 에이전트는 클러스터 DNS 이름으로 Service에 접근합니다.

차트의 NetworkPolicy는 `networkPolicy.ingressFrom`이 비어 있는 기본값일 때 모든 소스를 허용합니다. 이 항목에 피어(예: 로드 밸런서의 서브넷)를 나열한다면, 지정한 목록이 기본값에 추가되는 것이 아니라 기본값을 대체하므로 egress 게이트웨이도 목록에 추가하세요.

```yaml
networkPolicy:
  enabled: true
  ingressFrom:
    # ...the peers you already allow...
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: ate-system
      podSelector:
        matchLabels:
          app: atenet-egress
```

<a id="2-a-token-per-agent"></a>

## 2. 에이전트별 토큰

각 에이전트에 자체 토큰을 발급하고, 서비스 계정 subject에는 atespace와 Substrate actor의 이름을 사용합니다. Loom은 에이전트가 쓰는 모든 행에 이 subject를 기록하며, 토큰의 네임스페이스와 역할이 에이전트가 읽고 쓸 수 있는 범위를 결정합니다
([토큰과 권한 부여](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)).

서명 키를 가진 Loom pod에서 토큰을 발급하고, 해당 atespace의 토큰만 담는 네임스페이스의 Secret에 저장합니다.

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl create namespace loom-tokens-research
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN"
```

Substrate의 Kubernetes credential provider는 atespace에 허용된 네임스페이스에서만 Secret을 읽습니다. 관리자는 provider의 네임스페이스 정책에서 atespace에 토큰 네임스페이스를 허용하고 provider를 재시작합니다. provider는 시작할 때 정책을 읽습니다.

```bash
kubectl -n ate-system edit configmap k8s-credential-provider-namespace-policy
```

```yaml
data:
  namespace-policy.yaml: |
    policies:
    - atespace: research
      allowedNamespaces:
      - loom-tokens-research
```

```bash
kubectl -n ate-system rollout restart deployment/k8s-credential-provider
```

provider는 개별 Secret이 아니라 네임스페이스 전체를 atespace에 허용합니다. 따라서 네임스페이스를 허용받은 atespace는 그 안의 모든 토큰을 사용할 수 있습니다. atespace마다 네임스페이스를 하나씩 두면 각 atespace가 자신의 에이전트 토큰만 사용하도록 제한됩니다.

토큰은 `--ttl-days`가 지나면 만료됩니다. 만료 전에 새 토큰을 발급하고 Secret을 교체하세요. credential provider는 요청마다 Secret을 읽으므로 다음 호출부터 새 토큰이 사용됩니다.

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN" --dry-run=client -o yaml | kubectl apply -f -
```

<a id="3-configure-the-agent-and-create-its-actor"></a>

## 3. 에이전트를 구성하고 액터 만들기

Substrate 문서에는 액터용 MCP 구성에 대한 설명이 없습니다. 에이전트의 MCP 클라이언트는 이미지 안에서 구성하거나, 액터 템플릿에 리터럴 환경 값으로 지정하세요. 서버 항목은 Loom의 엔드포인트와 자리표시자 헤더로 이루어지며, 이 헤더는 게이트웨이가 대체합니다.

```json
{
  "mcpServers": {
    "loom": {
      "type": "http",
      "url": "http://loom.loom.svc.cluster.local:7777/mcp",
      "headers": { "Authorization": "Bearer placeholder" }
    }
  }
}
```

키 이름은 클라이언트에 따라 다릅니다([MCP 클라이언트](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)에 각 클라이언트의 항목이 있습니다).
토큰은 템플릿에 넣지 마세요. Substrate는 템플릿과 스냅샷을 저장하며, 컨테이너 환경은 리터럴 값만 받습니다.

그다음 atespace, 에이전트의 이미지를 담은 액터 템플릿, 그리고 그 템플릿으로 액터를 만듭니다. 템플릿의 필드는 Substrate의 [API 가이드](https://ate.dev/docs/reference/api-guide/)에 설명되어 있으며, 여기서는 템플릿의 `metadata`가 atespace `research`에 `research-agent`라는 이름을 지정합니다. 템플릿의 `workerSelector`는 클러스터의 WorkerPool과 일치해야 하고, 이미지는 digest로 고정해야 합니다. 세 가지를 만듭니다.

```bash
kubectl ate create atespace research
kubectl ate create actor-template -f template.yaml
kubectl ate create actor research-agent --template=research-agent -a research
```

<a id="4-allow-the-actor-to-reach-loom-with-its-token"></a>

## 4. 액터가 토큰으로 Loom에 접근하도록 허용

액터에는 하나의 egress 정책이 있습니다. 아래 정책은 앞에서 만든 액터에 대해 Loom의 Service로 향하는 HTTP를 허용하고, 에이전트가 보내는 `Authorization` 헤더를 Secret의 `Bearer <token>`으로 바꿉니다.

```bash
kubectl ate create egress-policy research-agent -a research -f - <<'EOF'
rules:
- http:
    hostnames: ["loom.loom.svc.cluster.local"]
    ports:
      numbers: [7777]
    effects:
      replaceHeaders:
      - header: Authorization
        prefix: "Bearer "
        credentialUri: ate-secret://k8s.io/default/loom-tokens-research/research-agent/token
EOF
```

- 규칙은 요청의 호스트와 일치시키므로, MCP 항목 URL의 호스트인 `loom.loom.svc.cluster.local`을 지정합니다.
- 게이트웨이는 이미 있는 헤더를 교체합니다. `Authorization` 헤더가 없는 요청은 그대로 나가고 Loom이 이를 거부합니다.
- Secret이 없거나 atespace에 권한이 부여되지 않았다면 게이트웨이가 요청을 거부합니다.
- 이 가이드의 `http://` URL과 규칙은 `tls.secretName` 없이 설치한 차트를 전제로 하며, 이 값을 설정하면 해당 포트는 HTTPS로만 응답합니다. 이 차트는 클러스터 내부에서 HTTP로 서비스하므로, 이 규칙에서는 토큰이 게이트웨이에서 Loom까지 암호화되지 않은 채 전달됩니다. 이 구간을 암호화하려면 Loom 앞단에 있는, 인증서가 공개적으로 신뢰되는 TLS 엔드포인트(예: 차트의 Ingress 호스트)로 에이전트를 향하게 하고, 해당 호스트를 `https` 규칙으로 허용하세요. 그러면 게이트웨이가 에이전트의 TLS를 가로채므로, 액터는 Substrate의 [egress 신뢰 번들](https://ate.dev/docs/guides/egress-trust-bundle/)을 마운트하고 MCP 클라이언트가 이를 사용하도록 지정해야 합니다(클라이언트의 런타임에 따라 `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS` 또는 `REQUESTS_CA_BUNDLE`). 이 경우 Loom에는 `atenet-egress`가 아니라 인그레스 컨트롤러에서 온 연결로 보입니다.

에이전트가 필요로 하는 다른 대상(예: 모델 제공자)에 대한 규칙도 같은 정책에 넣습니다. `kubectl ate update egress-policy`는 모든 규칙을 교체하며, 메타데이터가 현재 버전이 아닌 정책은 거부합니다. 따라서 `kubectl ate get egress-policy research-agent -a research -o yaml`로 정책을 읽고 Loom 규칙 옆에 규칙을 추가한 다음, 그 파일을 `kubectl ate update egress-policy ... -f`로 다시 보내세요.

정책을 적용했으면 액터를 시작합니다.

```bash
kubectl ate resume actor research-agent -a research
```

<a id="several-agents"></a>

## 여러 에이전트

- 하나의 Loom 네임스페이스에 있는 에이전트는 각자 기록한 모든 내용을 공유합니다. 팀의 네임스페이스는 읽되 쓰기는 자신의 네임스페이스에만 해야 하는 하위 에이전트에는 네임스페이스별로 역할이 지정된 토큰을 발급합니다
  (`--namespace-role team=viewer --namespace-role research=writer`;
  [토큰 발급](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#mint-a-token)).
- 각 액터는 자신만의 토큰, Secret, 송신 정책을 가집니다. 다른 atespace에 있는 액터는 해당 atespace 고유의 토큰 네임스페이스와 권한 부여를 받습니다([2단계](#2-a-token-per-agent)).
- 에이전트 간 작업 전달에는 [작업 인박스](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox)를 사용합니다.

<a id="related"></a>

## 관련 문서

- 공유 MCP 마운트에 대해서는 [에이전트 프레임워크와 오케스트레이터](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)를 참고하세요.
- Substrate 가이드: [송신 트래픽](https://ate.dev/docs/guides/egress-traffic/) 및
  [송신 자격 증명 주입](https://ate.dev/docs/guides/egress-credential-injection/).
