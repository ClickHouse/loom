<!-- i18n: source=docs/features/integrations/agent-substrate.md blob=3054a7336ba22e483dd7ab222be1f25e1bd0deee model=claude-sonnet-5-5 -->
> このページは[英語の原文](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md)の機械翻訳です。内容は英語の原文が優先されます。
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md), which is authoritative.

<a id="agent-substrate"></a>

# エージェント Substrate

[エージェント Substrate](https://ate.dev) v1.0.0 と Loom の Helm チャートを同じ Kubernetes クラスターに配置する必要があります。Substrate のエグレスゲートウェイが、各エージェントの Loom トークンを MCP 呼び出しに付与します。

トークンがアクターのテンプレートやスナップショットに含まれることはありません。Substrate v1.0.0 には Kubernetes 1.37 以降と、ノード上の gVisor またはマイクロ VM ランタイムが必要です。以下の手順では、Substrate の Kubernetes 認証情報プロバイダーも必要です。これは `ate-setup deploy ... --credential-provider='{"name":"k8s.io"}'` でデプロイされます。

以下の Substrate の手順は、Substrate v1.0.0 の公式ドキュメントに従っています。このページの内容は実際のクラスターでエンドツーエンドでは実行していないため、お使いの Substrate のバージョンに照らして確認してください。

<a id="how-the-pieces-fit"></a>

## 構成要素の連携

| 構成要素 | 実行場所 | 役割 |
| ------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Loom | namespace `loom` での `helm install loom` | メモリサーバー。その Service は `loom.loom.svc.cluster.local:7777` |
| エージェント | atespace 内の Substrate アクター | プレースホルダーの `Authorization` ヘッダーを付けて、Loom の MCP エンドポイント `/mcp` を呼び出す |
| Egress ポリシー | Substrate のコントロールプレーン (`kubectl ate`)。`atenet-egress` が適用する | アクターが Loom に到達することを許可する。ルールを追加しない限り、それ以外は許可しない |
| Loom トークン | Kubernetes Secret | エージェントごとに 1 つ。egress ゲートウェイがこれを `Authorization` ヘッダーに設定する |

すべてのリクエストは、Substrate の egress ゲートウェイ（`ate-system` 内の `atenet-egress`）を経由してアクターから送出されます。ゲートウェイは、アクターのポリシーで許可されていない egress をすべて拒否します。そのため、Loom に届く接続は、アクターからではなくゲートウェイの Pod からのものに見えます。各行にはトークンのサブジェクトが記録され、これによってアクターを区別できます。行の `agent` は MCP クライアントの名前であり、同じクライアントを実行しているアクター同士で共通になります。

<a id="1-install-loom-in-the-cluster"></a>

## 1. クラスタに Loom をインストールする

[Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md) の説明に従って、名前空間 `loom`、リリース名 `loom` でチャートをインストールします。これにより Service の名前は `loom` になります。エージェントに Ingress は不要です。エージェントはクラスタ内の DNS 名で Service に到達します。

チャートの NetworkPolicy は、デフォルトの `networkPolicy.ingressFrom` が空である間、すべての送信元を許可します。ここにピア（たとえばロードバランサのサブネット）を列挙する場合は、egress ゲートウェイもリストに加えてください。設定したリストはデフォルトに追加されるのではなく、デフォルトを置き換えるためです。

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

## 2. エージェントごとのトークン

各エージェントに専用のトークンを発行します。サービスアカウントのサブジェクトには、atespace と Substrate のアクターの名前を付けます。Loom はエージェントが書き込む各行にこのサブジェクトを記録し、トークンの名前空間とロールによって、読み取りと書き込みの範囲が決まります（[トークンと権限付与](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)）。

署名鍵を保持する Loom の Pod でトークンを発行し、その atespace のトークンだけを置く名前空間の Secret に保存します。

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl create namespace loom-tokens-research
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN"
```

Substrate の Kubernetes credential provider は、atespace に許可された名前空間からのみ Secret を読み取ります。管理者は、プロバイダーの名前空間ポリシーでその atespace にトークン用の名前空間を許可し、プロバイダーを再起動します。プロバイダーはポリシーを起動時に読み込みます。

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

プロバイダーが atespace に許可する単位は個々の Secret ではなく名前空間全体です。そのため、ある名前空間を許可された atespace は、その中のすべてのトークンを使えます。atespace ごとに 1 つの名前空間を用意すれば、各 atespace は自分のエージェントのトークンだけを扱えます。

トークンは `--ttl-days` の経過後に失効します。失効する前に新しいトークンを発行し、Secret を置き換えてください。credential provider はリクエストのたびに Secret を読み取るため、次の呼び出しから新しいトークンが使われます。

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN" --dry-run=client -o yaml | kubectl apply -f -
```

<a id="3-configure-the-agent-and-create-its-actor"></a>

## 3. エージェントを設定してそのアクターを作成する

Substrate のドキュメントには、アクター向けの MCP 設定は記載されていません。エージェントの MCP クライアントは、そのイメージ内で設定するか、アクターテンプレート内のリテラルな環境変数の値として設定します。サーバーエントリには Loom のエンドポイントとプレースホルダーのヘッダーを指定し、このヘッダーはゲートウェイが置き換えます。

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

キー名はクライアントに従います（各クライアントのエントリは [MCP クライアント](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md)を参照してください）。トークンはテンプレートに含めないでください。Substrate はテンプレートとスナップショットを保存し、そのコンテナ環境にはリテラルな値しか指定できません。

次に、atespace、エージェントのイメージを持つアクターテンプレート、そのテンプレートから作成するアクターを作成します。テンプレートのフィールドについては Substrate の [API ガイド](https://ate.dev/docs/reference/api-guide/)で説明されています。ここでは、テンプレートの `metadata` で、名前を `research-agent`、atespace を `research` としています。テンプレートの `workerSelector` はクラスター内の WorkerPool に一致している必要があり、イメージはダイジェストで固定する必要があります。次の 3 つを作成します。

```bash
kubectl ate create atespace research
kubectl ate create actor-template -f template.yaml
kubectl ate create actor research-agent --template=research-agent -a research
```

<a id="4-allow-the-actor-to-reach-loom-with-its-token"></a>

## 4. トークンを使ってアクターが Loom に到達できるようにする

アクターには 1 つの egress ポリシーがあります。ここで示すポリシーは、上で作成したアクターに対して Loom の Service への HTTP を許可し、エージェントが送る `Authorization` ヘッダーを Secret の `Bearer <token>` に置き換えます。

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

- ルールはリクエストのホストに対してマッチするため、MCP エントリの URL のホストである `loom.loom.svc.cluster.local` を指定します。
- ゲートウェイは、すでに存在するヘッダーを置き換えます。`Authorization` ヘッダーのないリクエストはそのまま送出され、Loom に拒否されます。
- Secret が存在しない場合や、atespace に権限が付与されていない場合、ゲートウェイはリクエストを拒否します。
- このガイドの `http://` の URL とルールを使うには、`tls.secretName` を設定していないチャートが必要です（設定すると、ポートは HTTPS のみで応答します）。この場合、チャートはクラスター内で HTTP を提供するため、このルールではトークンがゲートウェイから Loom まで暗号化されずに流れます。この区間を暗号化するには、公的に信頼された証明書を持つ、Loom の前段の TLS エンドポイント（チャートの Ingress ホストなど）をエージェントの接続先にし、そのホストを `https` ルールで許可します。この場合、ゲートウェイがエージェントの TLS を中継処理するため、アクターは Substrate の[egress トラストバンドル](https://ate.dev/docs/guides/egress-trust-bundle/)をマウントし、MCP クライアントにそれを指定します（クライアントのランタイムに応じて `SSL_CERT_FILE`、`NODE_EXTRA_CA_CERTS`、`REQUESTS_CA_BUNDLE` を使います）。また、Loom に見える接続元は `atenet-egress` ではなく Ingress コントローラーになります。

モデルプロバイダーなど、エージェントが必要とするその他の通信のルールも、同じポリシーに記述します。`kubectl ate update egress-policy` はすべてのルールを置き換え、メタデータが最新でないポリシーは拒否します。そのため、`kubectl ate get egress-policy research-agent -a research -o yaml` でポリシーを取得し、Loom のルールの隣にルールを追加して、そのファイルを `kubectl ate update egress-policy ... -f` で送り返してください。

ポリシーを設定したら、アクターを起動します。

```bash
kubectl ate resume actor research-agent -a research
```

<a id="several-agents"></a>

## 複数のエージェント

- 同じ Loom 名前空間内のエージェントは、書き込んだものをすべて共有します。チームの名前空間を読み取れるが、書き込みは自分の名前空間だけにしたいサブエージェントには、名前空間ごとにロールを指定したトークンを発行します（`--namespace-role team=viewer --namespace-role research=writer`、[トークンの発行](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#mint-a-token)）。
- 各アクターは、それぞれ専用のトークン、Secret、egress ポリシーを持ちます。別の atespace のアクターには、その atespace 専用のトークン名前空間とグラントが割り当てられます（[ステップ 2](#2-a-token-per-agent)）。
- エージェント間で作業を引き継ぐには、[タスクインボックス](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox)を使います。

<a id="related"></a>

## 関連情報

- [エージェントフレームワークとオーケストレーター](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md)：共有 MCP マウントについて説明しています。
- Substrate のガイド：[egress トラフィック](https://ate.dev/docs/guides/egress-traffic/)と [egress 認証情報の注入](https://ate.dev/docs/guides/egress-credential-injection/)。
