<!-- i18n: source=docs/features/integrations/agent-substrate.md blob=3054a7336ba22e483dd7ab222be1f25e1bd0deee model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md), which is authoritative.

<a id="agent-substrate"></a>

# Substrato para agentes

Requer o [Substrato para agentes](https://ate.dev) v1.0.0 e o Helm chart do Loom no mesmo cluster Kubernetes: o gateway de saída do Substrate adiciona o token do Loom de cada agente às suas chamadas MCP.

O token nunca faz parte do template do ator nem dos snapshots. O Substrate v1.0.0 exige Kubernetes 1.37 ou posterior, com um runtime gVisor ou microVM nos nós. Os passos abaixo também exigem o provedor de credenciais Kubernetes do Substrate, que o comando
`ate-setup deploy ... --credential-provider='{"name":"k8s.io"}'` implanta.

Os passos do Substrate abaixo seguem a documentação do próprio Substrate para a v1.0.0; esta página não foi executada de ponta a ponta em um cluster real, portanto confira os passos com a sua versão do Substrate.

<a id="how-the-pieces-fit"></a>

## Como as peças se encaixam

| Peça                | Onde roda                                                              | O que faz                                                                                  |
| ------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Loom                | `helm install loom` no namespace `loom`                                | o servidor de memória; seu Service é `loom.loom.svc.cluster.local:7777`                    |
| Agente              | um ator do Substrate em um atespace                                    | chama o endpoint MCP do Loom, `/mcp`, com um cabeçalho `Authorization` de espaço reservado |
| Política de egress  | plano de controle do Substrate (`kubectl ate`), aplicada pelo `atenet-egress` | permite que o ator acesse o Loom e nada além disso, a menos que você adicione regras |
| Token do Loom       | um Secret do Kubernetes                                                | um por agente; o gateway de egress o coloca no cabeçalho `Authorization`                   |

Toda requisição sai do ator pelo gateway de egress do Substrate (`atenet-egress` em
`ate-system`), que nega todo egress que a política de um ator não permita. Por isso, o Loom vê
conexões dos pods do gateway, não dos atores. Cada linha registra o subject do token, o que
distingue os atores entre si; o `agent` da linha é o nome do cliente MCP, que
atores que executam o mesmo cliente compartilham.

<a id="1-install-loom-in-the-cluster"></a>

## 1. Instale o Loom no cluster

Instale o chart conforme descrito em [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md), no namespace
`loom` com o nome de release `loom`, que dá ao Service o nome `loom`. Os agentes não precisam de
Ingress: eles acessam o Service pelo nome DNS dele dentro do cluster.

A NetworkPolicy do chart admite qualquer origem enquanto `networkPolicy.ingressFrom` estiver vazio, que é o
valor padrão. Se você listar peers ali (as sub-redes do balanceador de carga, por exemplo), adicione o gateway de egress
à lista, pois uma lista que você define substitui o padrão em vez de acrescentar a ele:

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

## 2. Um token por agente

Dê a cada agente o seu próprio token, com um subject de service account nomeado a partir do atespace e
do ator do Substrate. O Loom registra esse subject em cada linha que o agente grava, e os namespaces e as roles
do token determinam o que ele lê e grava
([tokens e grants](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)).

Gere o token em um pod do Loom, que guarda a chave de assinatura, e armazene-o em um Secret em um
namespace que contenha apenas os tokens desse atespace:

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl create namespace loom-tokens-research
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN"
```

O provedor de credenciais Kubernetes do Substrate lê um Secret apenas de namespaces que
o atespace tenha permissão para acessar. Um administrador concede ao atespace o namespace de tokens na
política de namespaces do provedor e reinicia o provedor, que lê a política na inicialização:

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

O provedor concede a um atespace namespaces inteiros, não Secrets individuais, então um atespace
com acesso a um namespace pode usar todos os tokens que há nele: um namespace por atespace mantém cada
atespace restrito aos tokens dos seus próprios agentes.

O token expira após `--ttl-days`: antes disso, gere um novo e substitua o Secret.
O provedor de credenciais lê o Secret a cada requisição, então a próxima chamada leva o novo
token:

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN" --dry-run=client -o yaml | kubectl apply -f -
```

<a id="3-configure-the-agent-and-create-its-actor"></a>

## 3. Configure o agente e crie seu actor

A documentação do Substrate não descreve nenhuma configuração de MCP para actors: configure o
cliente MCP do agente na imagem dele ou como valores literais de variável de ambiente no template do actor. A entrada
do servidor é o endpoint do Loom com um header de espaço reservado, que o gateway substitui:

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

Os nomes das chaves seguem o cliente ([clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) traz a entrada de cada um).
Mantenha o token fora do template: o Substrate armazena templates e snapshots, e o ambiente
do contêiner aceita apenas valores literais.

Em seguida, crie o atespace, o template de actor que carrega a imagem do agente e o actor
a partir desse template. O [guia da API](https://ate.dev/docs/reference/api-guide/) do Substrate
descreve os campos do template; aqui, seu `metadata` o nomeia como `research-agent` no atespace
`research`. O `workerSelector` do template deve corresponder a um WorkerPool no cluster, e a imagem
dele deve ser fixada por digest. Crie os três:

```bash
kubectl ate create atespace research
kubectl ate create actor-template -f template.yaml
kubectl ate create actor research-agent --template=research-agent -a research
```

<a id="4-allow-the-actor-to-reach-loom-with-its-token"></a>

## 4. Permita que o actor acesse o Loom, com o seu token

Um actor tem uma política de egress. Esta, para o actor criado acima, permite HTTP para o Service do Loom e substitui o cabeçalho `Authorization` que o agente envia por `Bearer <token>`, usando o valor do Secret:

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

- A regra confere com o host da requisição, então ela usa o host da URL da entrada MCP: `loom.loom.svc.cluster.local`.
- O gateway substitui um cabeçalho que já existe. Uma requisição sem o cabeçalho `Authorization` sai sem alteração e o Loom a recusa.
- Um Secret ausente, ou um atespace sem a permissão (grant), faz o gateway recusar a requisição.
- A URL `http://` e a regra deste guia exigem o chart sem `tls.secretName`, que faz a porta responder somente em HTTPS. Nesse caso, o chart serve HTTP dentro do cluster, então, nesta regra, o token trafega sem criptografia do gateway até o Loom. Para criptografar esse trecho, aponte o agente para um endpoint TLS na frente do Loom cujo certificado seja publicamente confiável, como o host do Ingress do chart, e permita esse host com uma regra `https`. O gateway então intercepta o TLS do agente, de modo que o actor monta o [pacote de confiança de egress](https://ate.dev/docs/guides/egress-trust-bundle/) do Substrate e aponta o cliente MCP para ele (`SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS` ou `REQUESTS_CA_BUNDLE`, conforme o runtime do cliente), e o Loom vê conexões vindas do ingress controller, e não de `atenet-egress`.

As regras para tudo o mais de que o agente precisa, como o provedor de modelo, ficam na mesma política. `kubectl ate update egress-policy` substitui todas as regras e recusa uma política cujos metadados não sejam os atuais. Por isso, leia a política com `kubectl ate get egress-policy research-agent -a research -o yaml`, adicione as regras ao lado da regra do Loom e envie esse arquivo de volta com `kubectl ate update egress-policy ... -f`.

Com a política aplicada, inicie o actor:

```bash
kubectl ate resume actor research-agent -a research
```

<a id="several-agents"></a>

## Vários agentes

- Agentes em um mesmo namespace do Loom compartilham tudo o que escrevem. Um subagente que deve ler
  o namespace de uma equipe, mas escrever apenas no seu próprio, recebe um token com um papel por namespace
  (`--namespace-role team=viewer --namespace-role research=writer`;
  [gerar um token](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#mint-a-token)).
- Cada ator recebe seu próprio token, Secret e política de egress; um ator em outro atespace recebe
  o token namespace e a concessão próprios desse atespace ([passo 2](#2-a-token-per-agent)).
- Repasse o trabalho entre agentes com a [caixa de entrada de tarefas](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox).

<a id="related"></a>

## Relacionados

- [Frameworks de agentes e orquestradores](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) para o mount MCP compartilhado.
- Guias do Substrate: [tráfego de saída (egress)](https://ate.dev/docs/guides/egress-traffic/) e
  [injeção de credenciais de saída (egress)](https://ate.dev/docs/guides/egress-credential-injection/).
