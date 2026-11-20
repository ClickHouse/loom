<!-- i18n: source=docs/features/integrations/agent-substrate.md blob=3054a7336ba22e483dd7ab222be1f25e1bd0deee model=claude-sonnet-5-5 -->
> Esta página es una traducción automática de la [página en inglés](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/agent-substrate.md), which is authoritative.

<a id="agent-substrate"></a>

# Agente Substrate

Requiere [Agente Substrate](https://ate.dev) v1.0.0 y el chart de Helm de Loom en un mismo clúster de Kubernetes: el gateway de salida de Substrate agrega el token de Loom de cada agente a sus llamadas MCP.

El token nunca forma parte de la plantilla ni de los snapshots del actor. Substrate v1.0.0 requiere Kubernetes 1.37 o posterior, con un runtime gVisor o microVM en los nodos. Los pasos siguientes también requieren el proveedor de credenciales de Kubernetes de Substrate, que se despliega con `ate-setup deploy ... --credential-provider='{"name":"k8s.io"}'`.

Los pasos de Substrate que se describen a continuación siguen la documentación propia de Substrate para v1.0.0; esta página no se ha ejecutado de principio a fin en un clúster real, por lo que conviene verificarlos con tu versión de Substrate.

<a id="how-the-pieces-fit"></a>

## Cómo encajan las piezas

| Pieza                  | Dónde se ejecuta                                                       | Qué hace                                                                                     |
| ---------------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Loom                   | `helm install loom` en el espacio de nombres `loom`                    | el servidor de memoria; su Service es `loom.loom.svc.cluster.local:7777`                     |
| Agente                 | un actor de Substrate en un atespace                                   | llama al endpoint MCP de Loom, `/mcp`, con un encabezado `Authorization` de marcador de posición |
| Política de egress     | el plano de control de Substrate (`kubectl ate`), aplicada por `atenet-egress` | permite que el actor llegue a Loom, y a nada más a menos que añadas reglas                   |
| Token de Loom          | un Secret de Kubernetes                                                | uno por agente; el gateway de egress lo coloca en el encabezado `Authorization`              |

Cada solicitud sale del actor a través del gateway de egress de Substrate (`atenet-egress` en
`ate-system`), que deniega todo el egress que la política de un actor no permita. Por lo tanto, Loom ve
conexiones de los pods del gateway, no de los actores. Cada fila registra el subject del token, que
distingue a los actores entre sí; el `agent` de la fila es el nombre del cliente MCP, que
comparten los actores que ejecutan el mismo cliente.

<a id="1-install-loom-in-the-cluster"></a>

## 1. Instalar Loom en el clúster

Instala el chart como describe [Kubernetes](https://github.com/ClickHouse/loom/blob/main/docs/operations/kubernetes.md), en el espacio de nombres
`loom` con el nombre de release `loom`, que da nombre al Service `loom`. Los agentes no necesitan
Ingress: llegan al Service por su nombre DNS dentro del clúster.

La NetworkPolicy del chart admite cualquier origen mientras `networkPolicy.ingressFrom` esté vacío, que es su
valor por defecto. Si listas pares ahí (por ejemplo, las subredes del balanceador de carga), añade el gateway de salida
a la lista, ya que una lista que defines reemplaza el valor por defecto en lugar de añadirse a él:

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

## 2. Un token por agente

Da a cada agente su propio token, con un subject de cuenta de servicio con el nombre del atespace y
el actor de Substrate. Loom registra ese subject en cada fila que escribe el agente, y los espacios de nombres y los roles del token deciden qué lee y qué escribe
([tokens y permisos](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md)).

Genera el token en un pod de Loom, que guarda la clave de firma, y almacénalo en un Secret en un
espacio de nombres que contenga solo los tokens de ese atespace:

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl create namespace loom-tokens-research
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN"
```

El proveedor de credenciales de Kubernetes de Substrate lee un Secret solo de los espacios de nombres que se han concedido al
atespace. Un administrador concede al atespace el espacio de nombres de tokens en la política de espacios de nombres del
proveedor y reinicia el proveedor, que lee la política al arrancar:

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

El proveedor concede a un atespace espacios de nombres completos, no Secrets individuales, de modo que un atespace
con acceso a un espacio de nombres puede usar todos los tokens que contiene: un espacio de nombres por atespace mantiene cada
atespace limitado a los tokens de sus propios agentes.

El token caduca tras `--ttl-days`: antes de que eso ocurra, genera uno nuevo y reemplaza el Secret.
El proveedor de credenciales lee el Secret en cada solicitud, así que la siguiente llamada lleva el nuevo
token:

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl -n loom-tokens-research create secret generic research-agent \
  --from-literal=token="$TOKEN" --dry-run=client -o yaml | kubectl apply -f -
```

<a id="3-configure-the-agent-and-create-its-actor"></a>

## 3. Configurar el agente y crear su actor

La documentación de Substrate no describe ninguna configuración de MCP para actores: configura el cliente MCP del agente en su imagen o como valores literales de variables de entorno en la plantilla del actor. La entrada del servidor es el endpoint de Loom con un encabezado de marcador de posición, que el gateway reemplaza:

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

Los nombres de las claves dependen del cliente ([clientes MCP](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/mcp-clients.md) incluye la entrada para cada uno).
Mantén el token fuera de la plantilla: Substrate almacena las plantillas y los snapshots, y el entorno de su contenedor admite únicamente valores literales.

Después crea el atespace, la plantilla de actor que lleva la imagen del agente y el actor a partir de esa plantilla. La [guía de la API](https://ate.dev/docs/reference/api-guide/) de Substrate describe los campos de la plantilla; aquí su `metadata` la nombra `research-agent` en el atespace `research`. El `workerSelector` de la plantilla debe coincidir con un WorkerPool del clúster, y su imagen debe estar fijada por digest. Crea los tres:

```bash
kubectl ate create atespace research
kubectl ate create actor-template -f template.yaml
kubectl ate create actor research-agent --template=research-agent -a research
```

<a id="4-allow-the-actor-to-reach-loom-with-its-token"></a>

## 4. Permitir que el actor llegue a Loom, con su token

Un actor tiene una política de egress. Esta, para el actor creado arriba, permite HTTP hacia el Service de Loom y reemplaza el encabezado `Authorization` que envía el agente por `Bearer <token>` tomado del Secret:

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

- La regla coincide con el host de la solicitud, por lo que nombra el host de la URL de la entrada MCP: `loom.loom.svc.cluster.local`.
- El gateway reemplaza un encabezado que ya existe. Una solicitud sin encabezado `Authorization` sale sin cambios y Loom la rechaza.
- Si falta el Secret, o si un atespace no tiene la concesión, el gateway rechaza la solicitud.
- La URL `http://` y la regla de esta guía requieren el chart sin `tls.secretName`, que hace que el puerto responda solo por HTTPS. El chart entonces sirve HTTP dentro del clúster, así que con esta regla el token viaja sin cifrar desde el gateway hasta Loom. Para cifrar ese tramo, apunta el agente a un endpoint TLS delante de Loom cuyo certificado sea de confianza pública, como el host del Ingress del chart, y permite ese host con una regla `https`. El gateway entonces intercepta el TLS del agente, por lo que el actor monta el [paquete de confianza de egress](https://ate.dev/docs/guides/egress-trust-bundle/) de Substrate y apunta su cliente MCP a él (`SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS` o `REQUESTS_CA_BUNDLE`, según el runtime del cliente), y Loom ve conexiones del ingress controller en lugar de `atenet-egress`.

Las reglas para cualquier otra cosa que el agente necesite, como su proveedor de modelos, van en la misma política. `kubectl ate update egress-policy` reemplaza todas las reglas y rechaza una política cuyos metadatos no sean los actuales, así que lee la política con
`kubectl ate get egress-policy research-agent -a research -o yaml`, añade las reglas junto a la regla de Loom y envía ese archivo de vuelta con `kubectl ate update egress-policy ... -f`.

Con la política en su lugar, inicia el actor:

```bash
kubectl ate resume actor research-agent -a research
```

<a id="several-agents"></a>

## Varios agentes

- Los agentes de un mismo espacio de nombres de Loom comparten todo lo que escriben. Un subagente que deba leer
  el espacio de nombres de un equipo pero escribir solo en el suyo recibe un token con un rol por espacio de nombres
  (`--namespace-role team=viewer --namespace-role research=writer`;
  [crear un token](https://github.com/ClickHouse/loom/blob/main/docs/guides/tokens-and-grants.md#mint-a-token)).
- Cada actor recibe su propio token, Secret y política de salida; un actor de otro atespace recibe
  el espacio de nombres de token y la concesión propios de ese atespace ([paso 2](#2-a-token-per-agent)).
- Traspasa el trabajo entre agentes con la [bandeja de tareas](https://github.com/ClickHouse/loom/blob/main/docs/features/tasks-and-decisions.md#task-inbox).

<a id="related"></a>

## Relacionado

- [Frameworks de agentes y orquestadores](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/frameworks.md) para el montaje MCP compartido.
- Guías de Substrate: [tráfico de salida](https://ate.dev/docs/guides/egress-traffic/) e
  [inyección de credenciales de salida](https://ate.dev/docs/guides/egress-credential-injection/).
