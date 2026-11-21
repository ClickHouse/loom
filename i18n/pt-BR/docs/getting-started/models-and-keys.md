<!-- i18n: source=docs/getting-started/models-and-keys.md blob=52104cd19c972224d162911f3102d1fff28bd3b8 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md), which is authoritative.

<a id="configure-models-and-api-keys"></a>

# Configurar modelos e chaves de API

Seu agente de código, o LLM interno do Loom e o modelo de embeddings do Loom são escolhas separadas.
Você pode conectar o Claude Code a um servidor Loom que usa modelos da OpenAI, ou o Codex a um servidor
que usa Claude. O agente não precisa usar o mesmo provedor que o Loom.

| Escolha                                          | O que faz                                                              | Onde configurar                                            |
| ------------------------------------------------ | ---------------------------------------------------------------------- | ---------------------------------------------------------- |
| Seu agente: Claude Code, Codex ou outro cliente  | Executa seu fluxo de trabalho de código ou de assistente e chama as ferramentas do Loom. | No próprio cliente; conecte-o com um token do Loom.        |
| LLM do Loom                                      | Extrai fatos, resume sessões e gera respostas dentro do Loom.          | As configurações de provedor, modelo e chave de API do servidor. |
| Modelo de embeddings do Loom                     | Torna memórias, documentos e código pesquisáveis por significado.      | O modelo de embeddings, as dimensões e a chave do provedor no servidor. |

`LOOM_API_KEY` é um token para acessar o **Loom**. Ele é diferente de
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` e `GEMINI_API_KEY`, que pagam pelas chamadas de modelo do
servidor. Mantenha as chaves dos provedores no servidor; conectar um agente não transfere as credenciais
de assinatura nem as chaves de API dele para o Loom.

<a id="what-works-without-provider-keys"></a>

## O que funciona sem chaves de provedor?

A instalação Docker lançada faz embeddings com `local` (bge-small, 384 dimensões, em CPU, a partir de arquivos que a imagem já traz) e seleciona o LLM `fake-echo-1`, a menos que o instalador tenha configurado um servidor de modelo local ([um servidor de modelo na sua máquina](#a-model-server-on-your-machine-no-api-key)). Ela inicia sem chaves de provedor e sem rede: o armazenamento, o painel, a indexação estrutural de código e a busca semântica funcionam, embora um embedder hospedado recupere melhor em perguntas com muita paráfrase. O LLM falso não produz fatos extraídos, resumos nem respostas geradas que façam sentido. O `.env` de um checkout seleciona os embeddings `fake-ngram-3072`, que correspondem por sobreposição de caracteres. Uma instalação atualizada mantém o embedder indicado no seu `~/.loom/.env`; trocá-lo para `local` é uma mudança de modelo como qualquer outra e gera novamente os embeddings do que está armazenado.

Para uma recuperação mais forte em conteúdo com muita paráfrase, configure um provedor de embeddings hospedado. Para extração automática de fatos, resumos e respostas geradas, configure também um LLM. Um LLM sozinho não substitui um modelo de embeddings; um modelo de embeddings sozinho não gera respostas. Sem nenhum embedding configurado, o servidor de um checkout usa seu embedder falso; ele não deixa de gerar embeddings.

Alguns provedores hospedados também recorrem a versões falsas quando a chave está ausente. Verifique `fake_embedding`, `fake_llm`, `providers_degraded` e `problems` em `/v1/health`, depois salve e recupere um fato de teste e experimente uma resposta. Uma chave inválida ou um modelo inacessível ainda pode fazer as chamadas ao provedor falharem; um servidor em execução não é prova de que o acesso ao modelo funciona. Use `LOOM_REQUIRE_REAL_PROVIDERS=true` em implantações baseadas em variáveis de ambiente para que provedores falsos ou sem chave passem a ser uma condição de saúde degradada.

<a id="choose-a-setup"></a>

## Escolha uma configuração

| O que sua organização tem                              | LLM do Loom                      | Embeddings                                                           |
| ------------------------------------------------------ | -------------------------------- | -------------------------------------------------------------------- |
| Acesso à API da OpenAI                                 | Um modelo da OpenAI disponível   | OpenAI; uma única chave de API pode atender às duas funções.         |
| Anthropic mais um provedor de embeddings aprovado      | Um modelo Claude disponível      | OpenAI, Gemini ou um endpoint compatível, com a respectiva chave.    |
| Acesso apenas à API da Anthropic                       | Um modelo Claude disponível      | Adicione um provedor de embeddings e a respectiva chave de API.      |
| Nenhuma chave de provedor; um servidor de modelos na máquina | Um modelo local (`local`)  | O embedder `local` que a imagem inclui.                              |

**Uma única chave de API da OpenAI é suficiente para as duas funções; uma chave da Anthropic não é necessária.**
Em Models & keys, escolha um LLM principal e deixe as substituições por tarefa em branco. Todas as
tarefas de LLM habilitadas usam esse modelo, incluindo verificação e avaliação. As substituições avançadas são
opcionais e exigem a chave de outro provedor somente quando você seleciona um explicitamente.

Escolha OpenAI, Google Gemini ou um **endpoint de embeddings compatível com a OpenAI**. Os embeddings da OpenAI usam
um modelo de embeddings, como `text-embedding-3-large`, e não um modelo de chat GPT.
O Claude pode executar as tarefas de LLM do Loom, mas exige um provedor de embeddings e uma chave separados.
A Anthropic não oferece um modelo de embeddings próprio; a Voyage não é um provedor integrado do Loom.
[Orientações de embeddings da Anthropic](https://platform.claude.com/docs/en/build-with-claude/embeddings)

As tarefas internas de modelo do Loom usam chaves de API de provedores, não logins de CLI de agentes de código
nem credenciais de assinatura. A escolha de provedor do seu agente externo é independente.

<a id="set-the-server-configuration"></a>

## Definir a configuração do servidor

<a id="recommended-use-the-dashboard"></a>

### Recomendado: usar o painel

Como administrador da plataforma, abra **Settings → Models & keys** (também disponível durante o Setup).
Escolha seu provedor de LLM e de embeddings, informe os IDs dos modelos e as chaves de API e, em seguida, clique em
**Test & apply**. Deixe os overrides por job em branco para usar um único LLM em todos os jobs habilitados.
As configurações salvas no painel substituem o ambiente e são criptografadas no ClickHouse.
**Não é necessário reiniciar o Docker nem executar um comando do Compose.** As atualizações de chave e de LLM são aplicadas em tempo real;
as mudanças de embedding iniciam a reconstrução descrita abaixo. **Use environment
configuration** remove esses overrides e restaura as configurações de inicialização deste servidor;
isso não recarrega um arquivo `.env` editado. Alterar o espaço de embeddings exige uma reconstrução.

As opções de embedding do painel são provedores hospedados. Para manter o embedder
`local` da stack lançada e adicionar um LLM real, defina apenas as linhas do LLM em `~/.loom/.env`
([Claude para o LLM](#claude-for-the-llm) mostra quais são) e recrie o contêiner: o
espaço de embeddings não muda, então nada é reindexado.

Nas mudanças de embedding feitas pelo painel, o Loom enfileira a reconstrução automaticamente e mantém
as requisições do plano de dados pausadas até que a verificação, a publicação e a limpeza terminem.
Models & keys mostra a fase e o progresso; um job que falhou permanece pausado e oferece
nova tentativa a partir do cursor salvo. Mantenha o volume do ClickHouse e o
`LOOM_JWT_SECRET` original, que protege as chaves de provedor armazenadas, ao recriar os contêineres. As reconstruções fazem chamadas ao provedor e podem
levar tempo e gerar custos proporcionais ao conteúdo indexado. A reconstrução publica
apenas em tabelas MergeTree locais. A primeira escolha de embedding em uma instalação sem
vetores armazenados, como uma instalação nova que ainda não armazenou nada, muda
diretamente, sem reconstrução. Quando já existem vetores em armazenamento replicado ou compartilhado,
como o ClickHouse Cloud, Models & keys bloqueia
os campos de embedding e o servidor recusa uma mudança de embedding antes de pausar
qualquer coisa: altere o modelo de embedding no ambiente e reindexe cada
índice conforme descrito acima. A recuperação de processos remotos perdidos continua sendo uma limitação documentada em
[reconfiguração de provedores](../architecture/provider-reconfiguration.md).

As configurações de token são separadas: elas configuram o acesso dos agentes ao Loom, não as credenciais do provedor de modelos.

<a id="alternative-configure-the-environment"></a>

### Alternativa: configurar o ambiente

Em implantações automatizadas, edite `~/.loom/.env` (ou o diretório selecionado com
`LOOM_INSTALL_DIR`). Em um checkout do código-fonte, edite o `.env` do repositório.
Substitua as entradas existentes em vez de acrescentar chaves duplicadas. Essas configurações são a
base de inicialização; os overrides salvos no painel têm precedência. As edições no ambiente exigem
a recriação do contêiner ou a reinicialização do servidor nativo, conforme descrito abaixo.

<a id="openai-for-both-roles"></a>

### OpenAI para as duas funções

```dotenv
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=<your OpenAI API key>
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_REQUIRE_REAL_PROVIDERS=true
```

Os exemplos usam os modelos padrão que acompanham o Loom. Escolha um modelo habilitado para a sua conta
de API. Definir apenas uma chave não altera a configuração padrão do instalador, que
nomeia seus provedores explicitamente (o embedder local e o LLM fake).

<a id="claude-for-the-llm"></a>

### Claude para o LLM

```dotenv
LOOM_LLM_PROVIDER=anthropic
LOOM_LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=<your Anthropic API key>
LOOM_REQUIRE_REAL_PROVIDERS=true
```

Em seguida, escolha uma configuração de embeddings:

```dotenv
# OpenAI embeddings
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
OPENAI_API_KEY=<your OpenAI API key>
```

```dotenv
# Or Gemini embeddings
LOOM_EMBEDDING_PROVIDER=gemini
LOOM_EMBEDDING_MODEL=gemini-embedding-001
LOOM_EMBEDDING_DIM=3072
GEMINI_API_KEY=<your Gemini API key>
```

Para outro serviço que implemente a API de embeddings da OpenAI, configure a raiz da API,
o ID do modelo e a largura nativa de saída:

```dotenv
LOOM_EMBEDDING_PROVIDER=compatible
LOOM_EMBEDDING_BASE_URL=https://embedding-provider.example/v1
LOOM_EMBEDDING_MODEL=your-embedding-model
LOOM_EMBEDDING_DIM=768
LOOM_EMBEDDING_API_KEY=<your embedding provider API key>
# Enable only if the endpoint accepts the optional dimensions parameter:
LOOM_EMBEDDING_SEND_DIMENSIONS=false
```

A URL base é a raiz da API; o Loom acrescenta `/embeddings`. Use HTTPS para serviços
remotos. HTTP é aceito para endpoints privados autohospedados, mas transmite a
chave e o conteúdo sem criptografia. A compatibilidade exige respostas indexadas com arrays de floats
da largura configurada; um provedor que use outra API precisa de um adaptador próprio.
Alterar o endpoint, o modelo ou a largura exige reconstruir os embeddings existentes.

Se a sua organização tem hoje apenas acesso à API da Anthropic, obtenha acesso a um
provedor de embeddings antes de habilitar a busca semântica.

<a id="keep-every-llm-job-on-your-chosen-provider"></a>

### Manter todos os jobs de LLM no provedor escolhido

O Loom permite usar modelos diferentes para jobs diferentes, mas você não precisa de contas em
vários provedores de LLM. Adicione estas configurações **depois** de `LOOM_LLM_MODEL` para que os jobs
normalmente específicos de um provedor usem o modelo que você selecionou:

```dotenv
LOOM_MEMORY_EXTRACT_VERIFY_MODELS=${LOOM_LLM_MODEL}
LOOM_SESSION_BRIEF_LLM_MODEL=
LOOM_CODE_BISECT_LLM_RERANK_MODEL=
LOOM_TRACKER_DUPCHECK_RERANK_MODEL=
LOOM_RETRIEVAL_JUDGE_MODEL=
```

O Compose e o carregador de dotenv do Loom expandem o modelo do verificador a partir da entrada anterior.
Os valores vazios acima herdam o LLM principal. Isso mantém o verificador de captura habilitado;
uma lista de verificadores vazia o desabilitaria. Remova quaisquer outros overrides de modelo entre provedores
que você tenha adicionado antes, incluindo modelos de fallback, de síntese e de reranking.

Essas configurações importam: alguns padrões avançados incluídos nomeiam explicitamente modelos da Claude ou da OpenAI,
e um override sem chave de provedor pode recorrer ao cliente fake.
Usar um único modelo em todos os jobs é uma configuração de conveniência, não a configuração por trás de
cada benchmark publicado. Ajuste jobs individuais depois usando
[modelos por job](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#models-per-job).

<a id="a-model-server-on-your-machine-no-api-key"></a>

### Um servidor de modelos na sua máquina, sem chave de API

`LOOM_LLM_PROVIDER=local` executa os jobs de LLM do Loom em um servidor de chat-completions que você mantém:
[página do Ollama](https://ollama.com), o `llama-server` do llama.cpp ou qualquer servidor que fale o
formato de chat-completions da OpenAI. O Loom não envia nenhuma chave a ele. Com o embedder `local` da
stack lançada, nada sai da máquina.

Uma instalação nova executada em um terminal oferece isso quando o Ollama responde em `localhost:11434`;
`LOOM_LOCAL_MODELS=ollama` antes de `sh loom-install.sh` define isso sem perguntar, em
uma instalação nova ou em uma existente, e `LOOM_LOCAL_MODELS=off` nunca oferece. Ele grava:

```dotenv
LOOM_LLM_PROVIDER=local
LOOM_LLM_MODEL=qwen2.5:3b
LOOM_LOCAL_LLM_BASE_URL=http://host.docker.internal:11434/v1
LOOM_ASSISTANT_PROVIDER=local
LOOM_ASSISTANT_MODEL=qwen2.5:3b
LOOM_ASSISTANT_ANSWER_CEILING_USD=0
LOOM_MEMORY_EXTRACT_VERIFY_MODELS=
LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=false
LOOM_LLM_REQUEST_TIMEOUT_S=900
LOOM_LLM_RETRY_ATTEMPTS=1
LOOM_EXTRACTION_QUEUE_MAX_BATCHES_PER_PASS=1
LOOM_EXTRACTION_QUEUE_MAX_PASS_SECONDS=3600
LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS=2
LOOM_ENTITY_EXTRACT_ENABLED=false
LOOM_SESSION_BRIEF_LLM_MODEL=
LOOM_CODE_BISECT_LLM_RERANK_MODEL=
LOOM_TRACKER_DUPCHECK_RERANK_MODEL=
LOOM_RETRIEVAL_JUDGE_MODEL=
```

O preset desativa os dois jobs que removem fatos com base no julgamento de um modelo, cujos
limites foram definidos em modelos hospedados: o verificador de captura, que descarta um fato que todos
os modelos verificadores consideram não durável, e o
[resolvedor de conflitos](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#the-conflict-resolver) em segundo plano,
que aposenta uma linha mais antiga que ele julga ter sido substituída. Um julgamento errado de um modelo pequeno nesses jobs
faria perder fatos. As gravações continuam sinalizando conflitos para a fila de Review; defina
`LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED=true` com a chave de
`LOOM_CONFLICT_RESOLVER_MODEL` (`gpt-6.1-sol`, OpenAI) para julgá-los. Em uma instalação
existente, uma configuração salva em Models & keys prevalece sobre o `.env`: após a reinicialização,
escolha **Use environment configuration** ali para que o preset seja aplicado.

Baixe o modelo primeiro (`ollama pull qwen2.5:3b`, cerca de 2 GB) e dê ao Ollama uma janela de contexto
que comporte os prompts do Loom: a extração envia cerca de 15,000 tokens, e o Ollama corta um
prompt mais longo para caber na janela sem emitir erro, então execute-o com
`OLLAMA_CONTEXT_LENGTH=32768` (a janela que o `qwen2.5` aceita). Uma CPU pode levar minutos para
ler um prompt tão longo, por isso o preset espera `LOOM_LLM_REQUEST_TIMEOUT_S=900` segundos por
uma resposta, em vez dos 300 padrão dos modelos hospedados, e tenta uma única vez
(`LOOM_LLM_RETRY_ATTEMPTS=1`): um servidor que estourou o tempo limite não responde mais rápido na segunda vez.

O worker que reprocessa as capturas enviadas pelos plugins de codificação dá um prazo a cada passada, então
o preset permite que uma passada de extração adiada dure uma hora em um único lote
(`LOOM_EXTRACTION_QUEUE_MAX_PASS_SECONDS`, por padrão quatro dos seus intervalos, no mínimo 60 s
e no máximo 900; o lease de um lote é mantido pelo menos um minuto a mais). Em um provedor `local`,
uma captura interrompida (nesse prazo ou pelo desligamento do Loom) ou cuja chamada esgotou
o tempo limite consome uma de suas `LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS=2` tentativas, de modo que uma sessão
longa demais para a máquina termina como `failed` (`memory.extraction_status` mostra `cancelled` ou
o timeout) em vez de travar a fila: uma sessão longa pode precisar de várias chamadas
seguidas, cada uma com até o tempo limite. Em um servidor que responde a uma requisição por vez, o tempo limite
também conta a espera atrás de outras chamadas, e o assistente e o `memory.ask` compartilham o
servidor, então o uso intenso deles durante uma captura também pode fazê-la falhar; aumente
`LOOM_EXTRACTION_QUEUE_MAX_ATTEMPTS` se isso acontecer. A marcação de entidades
(`LOOM_ENTITY_EXTRACT_ENABLED`) fica desativada: ela entraria na fila atrás da extração em um servidor que
responde a uma requisição por vez, e sua passada, cortada no prazo, não grava nada, então a
busca fica sem correspondências de entidades. Os outros workers que
chamam um modelo não são ajustados para um modelo local: a extração de observe (desativada por padrão), cuja
passada de até 10 sessões é cortada em 900 s e recomeça pela sessão mais recente, e
os títulos de conversas e os perfis estruturados, que rodam quando o registro de conversas ou um
esquema de perfil é configurado.

Para o llama.cpp, aponte `LOOM_LOCAL_LLM_BASE_URL` para a raiz `/v1` dele
(`http://host.docker.internal:8080/v1`) e defina `LOOM_LLM_MODEL` com o nome que ele serve;
para um assistente local, defina também `LIBRECHAT_LOCAL_LLM_BASE_URL` com essa raiz e
`LOOM_ASSISTANT_MODEL` com esse nome. Um servidor executado a partir de um checkout alcança o modelo em
`localhost`, o padrão da configuração (`http://localhost:11434/v1`).

Um modelo por job cujo nome comece com `gpt`, `o1`, `o3`, `o4`, `chatgpt` ou `claude` vai para
esse provedor hospedado, seja qual for o valor de `LOOM_LLM_PROVIDER`; um modelo local com esse tipo de nome
(`gpt-oss`) roda localmente como `LOOM_LLM_MODEL` e, em uma configuração por job, só quando
essa configuração nomeia o mesmo modelo.

- **Acesso ao host.** Os contêineres alcançam a máquina como `host.docker.internal`. O Docker
  Desktop encaminha isso para serviços em loopback; com o Docker Engine no Linux, o Ollama precisa
  escutar além do loopback (`OLLAMA_HOST=0.0.0.0`, com a porta 11434 fechada para a rede).
  Com o Helm chart e `networkPolicy.enabled`, adicione a porta do servidor a
  `networkPolicy.extraEgress`.
- **O assistente.** `LOOM_ASSISTANT_PROVIDER=local` move o agente do assistente para o endpoint
  `local` do LibreChat, que chama `LIBRECHAT_LOCAL_LLM_BASE_URL` (padrão
  `http://host.docker.internal:11434/v1`), e exige `LOOM_ASSISTANT_MODEL`. O assistente
  responde chamando as ferramentas do Loom, então escolha um modelo que o servidor execute com chamadas de ferramenta (o Ollama
  as indica). Um modelo local não tem preço, então cada resposta conta
  `LOOM_ASSISTANT_ANSWER_CEILING_USD` ($0.10 por padrão) contra a parcela do assistente no
  limite diário de custo de modelo do namespace, como qualquer modelo sem preço: no limite
  e na parcela padrão, cinco respostas por dia. O preset define o teto como `0`, então as respostas locais
  não custam nada; esse também é o custo de uma resposta cortada antes de o uso chegar, então mantenha o valor
  padrão quando um modelo hospedado atender o assistente. Com o Helm chart, adicione
  `LOCAL_LLM_BASE_URL` ao Secret do assistente; a política do chart permite que o LibreChat alcance
  apenas DNS, seu banco de dados, o Loom e TCP 443, então o servidor de modelos precisa de uma NetworkPolicy
  própria que selecione o pod do LibreChat.
- **Qualidade e velocidade.** Os prompts e limites do Loom são ajustados em modelos hospedados, e nenhum
  benchmark publicado cobre um modelo local, então trate um modelo pequeno como uma forma de experimentar o Loom
  sem chave ou de manter tudo na máquina, não como um equivalente: espere que a extração,
  as respostas do `memory.ask` e o uso de ferramentas pelo assistente sejam mais fracos, em uma medida que ninguém
  mediu. Um modelo pequeno em uma CPU é lento com prompts longos, como a extração, então uma
  fila de captura ocupada acumula atraso; uma GPU, ou um modelo maior em uma máquina com memória suficiente,
  ajuda. Para um servidor de equipe, um modelo hospedado responde melhor.

<a id="through-an-inference-gateway"></a>

### Por meio de um gateway de inferência

Um gateway que fale a API da OpenAI ou da Anthropic na frente dos provedores
(para orçamentos, atribuição de custos ou uma única chave para vários serviços) recebe as
chamadas do Loom assim que as raízes de API apontam para ele; os nomes dos modelos continuam sendo os que o gateway
serve:

```bash
LOOM_OPENAI_BASE_URL=https://gateway.example/v1   # chat and embeddings
LOOM_ANTHROPIC_BASE_URL=https://gateway.example   # Messages: <root>/v1/messages
OPENAI_API_KEY=<the key the gateway issued for Loom>
```

Fora do padrão, a chave da Anthropic também é enviada como `Authorization: Bearer`, o
esquema que os gateways aceitam. Com uma chave de gateway, exija o gateway, para que uma raiz errada nunca
possa enviar prompts e a chave para outro lugar. Uma troca de token já o exige por si só.

```bash
LOOM_INFERENCE_GATEWAY_REQUIRED=true
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # comma-separated; exact names, any case
```

Enquanto o gateway é exigido, as chamadas de modelo e o conteúdo de memória que o Loom envia
a classificadores e rerankers vão apenas para os hosts que `LOOM_INFERENCE_GATEWAY_HOSTS`
lista. Um host corresponde exatamente: sem curinga, e `gateway.example` não
admite `api.gateway.example` nem `gateway.example.attacker.test`; um endereço IP
só corresponde se estiver listado.

- **Na inicialização**, o Loom se recusa a carregar sem `LOOM_INFERENCE_GATEWAY_HOSTS`,
  ou enquanto `LOOM_OPENAI_BASE_URL`, `LOOM_ANTHROPIC_BASE_URL`,
  `LOOM_EMBEDDING_BASE_URL`, `LOOM_INFERENCE_TOKEN_URL` (e `LOOM_LOCAL_LLM_BASE_URL`
  enquanto `LOOM_LLM_PROVIDER=local`) ou uma entrada de
  `LOOM_RERANK_ENDPOINTS_JSON` nomear outro host, enquanto o embedder for
  o do Gemini, ou enquanto `LOOM_CODE_SEARCH_RERANK_DEFAULT` ou
  `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT` for `builtin:cohere`.
  As duas raízes de API contam, inclusive a de um provedor que você não usa (os padrões
  delas são as APIs dos próprios provedores): um modelo nomeado por requisição pode ser
  de qualquer um dos provedores.
- **No momento da chamada**, uma chamada de modelo para qualquer outro host é recusada antes de
  sair, também em cada salto de redirecionamento, seja qual for a origem do nome: um modelo nomeado por
  requisição, uma entrada de `LOOM_CODE_SEARCH_VECTOR_MODELS`. Uma chamada recusada não é
  repetida, e `/v1/health` informa que o provedor está falhando.
- **Namespaces**: `namespace.register` recusa um `classify_endpoint` que não seja
  https na porta padrão de um host listado (422), e um armazenado antes
  nunca é chamado: memory.set mantém o tipo do chamador, com um aviso no log.
  `builtin:cohere` é recusado como escolha de rerank (422), e um namespace que
  o tenha armazenado faz rerank com nada, com um aviso no log.
- **Painel**: salvar uma configuração de modelo com um endpoint em outro host
  (ou o embedder do Gemini) responde 422, e a verificação do modelo responde
  `gateway_required`. Uma configuração armazenada que a verificação recusa não é aplicada,
  incluindo suas definições de vetor em uso, e `/v1/health` informa
  `model_configuration_refused`. A verificação lê apenas o embedding, então uma
  configuração recusada sempre gera embeddings diferentes das definições em vigor, e as
  tabelas de vetores podem conter esse outro espaço: toda requisição do plano de dados
  nesse servidor é recusada (503 `GATEWAY_REQUIRED`, que nenhum dos dois SDKs tenta de novo, sem
  `Retry-After`) e o servidor não fica pronto, até que uma configuração que a verificação
  aceite a substitua ou ela seja redefinida para a do ambiente. Uma reinicialização
  gradual então para nesse servidor: substitua a configuração antes de fazer o rolling.
- **Implantação gradual:** uma build anterior ignora as duas definições, então ative-as
  somente depois que todas as réplicas executarem esta. Uma implantação que já troca um token
  se recusa a iniciar nesta build até que `LOOM_INFERENCE_GATEWAY_HOSTS` liste o seu
  gateway, e enquanto ela gera embeddings com Gemini, indica um endpoint de rerank em outro
  host ou define `builtin:cohere` como padrão da busca de código; requisições que indicam
  `builtin:cohere` recebem 422, e namespaces que armazenaram esse valor ou um
  `classify_endpoint` fora do gateway deixam de fazer rerank ou classificação até que ele seja alterado. Defina
  os hosts e mova o embedder para trás do gateway (`LOOM_EMBEDDING_PROVIDER=compatible`,
  `LOOM_EMBEDDING_BASE_URL` apontando para o gateway) primeiro, ou a implantação para na
  primeira réplica atualizada.

Um gateway que aceita identidade de workload (troca de token RFC 8693)
não precisa de chave alguma: o Loom troca ali o token OIDC do seu workload,
renova a resposta cinco minutos antes de expirar (na metade do prazo, para um token que vive
menos de dez minutos) e a envia em cada chamada. Sem uma chave da qual inferi-los,
indique o provedor e os modelos:

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token         # https
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # every root's host
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # rotated by the platform
LOOM_INFERENCE_TOKEN_SCOPE="chat embeddings"                         # if the gateway asks for one
LOOM_OPENAI_BASE_URL=https://gateway.example/v1                      # both roots required:
LOOM_ANTHROPIC_BASE_URL=https://gateway.example                      # the token is the gateway's
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

O Loom se recusa a iniciar enquanto a raiz de qualquer um dos provedores, ou o endpoint de token,
estiver em um host que `LOOM_INFERENCE_GATEWAY_HOSTS` não lista: um modelo indicado por
requisição pode ser de qualquer um dos provedores, e o token do gateway iria para lá. No
painel, as alterações de modelo também são verificadas por meio do gateway.

No Kubernetes, o arquivo de token é um token de service account projetado, com a audiência
do gateway; o gateway deve confiar no emissor OIDC do cluster e corresponder à service account
do Loom (`system:serviceaccount:<namespace>:<name>`), e não apenas ao emissor,
já que todo pod do cluster pode obter um token dele.

Um gateway que verifica o próprio token do workload o recebe como bearer, sem
troca: defina o modo bearer em vez de um endpoint de token. O Loom lê o arquivo no máximo
uma vez por minuto, e de novo imediatamente quando o gateway responde 401, de modo que um token rotacionado
seja captado sem reiniciar:

```bash
LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # sent as it is
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # required, as with the exchange
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_EMBEDDING_PROVIDER=compatible                                   # embeddings through the gateway too
LOOM_EMBEDDING_BASE_URL=https://gateway.example/v1
```

Indique o provedor e os modelos como no exemplo de troca. O token vai somente para os
hosts do gateway: o modo bearer exige o gateway, assim como a troca. Um endpoint de
embedding compatível que receba sua própria `LOOM_EMBEDDING_API_KEY` envia essa chave no lugar.

<a id="naming-the-customer-on-every-call"></a>

#### Indicando o cliente em cada chamada

Um gateway que mede ou define orçamento por cliente lê o cliente de um cabeçalho.
Indique os cabeçalhos, e toda requisição de chat, embedding, rerank e classify carrega
a org cujos dados ela contém e a funcionalidade que a originou:

```bash
LOOM_INFERENCE_CUSTOMER_HEADER=X-Customer-Id   # the org
LOOM_INFERENCE_FEATURE_HEADER=X-Feature        # optional, with the customer header: hyde, memory.search, worker.embedding
LOOM_INFERENCE_SYSTEM_CUSTOMER=loom            # calls that belong to no org
LOOM_INFERENCE_CUSTOMER_REQUIRED=true          # a cell serving several customers: refuse to start without the header
LOOM_INFERENCE_CUSTOMER_FORMAT='cloud:{org}'   # the gateway's customer id for an org; default the org id
LOOM_INFERENCE_FEATURE_MAP_JSON='{"hyde": "search", "*": "memory"}'  # Loom's feature names to the gateway's
```

- Uma requisição indica a org que ela endereça, conforme autorizado para o seu token; um chamador
  não pode indicar uma org que o seu token não alcança.
- Um job em segundo plano indica a org das linhas em que trabalha e faz uma chamada
  por org: o worker de embedding, `loom reembed` e uma reconstrução de modelo dividem seus
  lotes por org.
- As chamadas da própria implantação (a verificação do embedder na inicialização, os pares fixos
  do worker de recalibração, a verificação de provedor do painel) indicam
  `LOOM_INFERENCE_SYSTEM_CUSTOMER`.
- O juiz de recuperação por amostragem (`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`) envia a consulta
  e as memórias da org julgada, então cada busca julgada adiciona uma chamada que
  indica essa org.
- Com o cabeçalho de cliente definido, uma chamada de modelo que não indica org é recusada
  (`MODEL_CALL_UNATTRIBUTED`) em vez de ser enviada: é um caminho de chamada que
  esqueceu sua org, e o gateway não a cobraria de ninguém.
- Um id de org é enviado com codificação percentual onde for necessário.

Respostas em cache não fazem chamada: um embedding ou uma resposta de modelo servida do
cache do Loom não é medida de novo. O cliente ainda é verificado antes do cache, então um
caminho que não indica nenhum é recusado mesmo em um acerto.

<a id="naming-the-agent-on-every-call"></a>

#### Indicando o agente em cada chamada

Um gateway que cobra ou limita cada agente lê para quem uma chamada se destina a partir de cabeçalhos. Mapeie cada
cabeçalho para um template, e toda requisição de chat, embedding, rerank e classify ao gateway
o carrega:

```bash
LOOM_INFERENCE_IDENTITY_HEADERS_JSON='{"X-Tenant": "{tenant}", "X-Agent": "{agent}", "X-Actor": "{email}"}'
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # required: identity headers go only to these hosts
LOOM_INFERENCE_TENANT_CLAIM=tenant_id   # the workload trust claim {tenant} names (the default)
```

- Os placeholders são `{org}`, `{namespace}`, `{subject}`, `{user_id}`, `{agent}` (o agente
  do token de uma workload trust, `sa:<trust>:<agent>`), `{tenant}` (o valor que essa trust exige
  em sua claim `LOOM_INFERENCE_TENANT_CLAIM`) e `{email}` (o endereço de uma pessoa autenticada).
- Um cabeçalho cujo template resulta vazio não é enviado: com placeholders simples, uma chamada sem
  agente por trás (a de uma pessoa, ou a da própria implantação) não carrega cabeçalho de agente. Os cabeçalhos
  que citam `{agent}` ou `{tenant}` vão juntos ou não vão, de modo que um agente nunca vai sem
  seu tenant.
- Uma chamada que não indica org é recusada (`MODEL_CALL_UNATTRIBUTED`), a menos que seja da própria
  implantação: execute os comandos `loom` que chamam modelos com `--org`, e com `--namespace` enquanto os
  cabeçalhos indicarem o agente (uma chamada sem namespace é recusada em vez de ser cobrada do
  agente padrão do gateway).
- Jobs em segundo plano indicam a identidade das linhas em que trabalham: o autor de uma memória ou de uma
  extração adiada, ou então o agente em cujo namespace próprio a linha está. Um job que processa linhas em lote
  faz uma chamada por identidade.
- Nenhum outro host recebe os cabeçalhos de identidade: um reranker, um classificador ou um provedor fora de
  `LOOM_INFERENCE_GATEWAY_HOSTS` nunca descobre para quem uma chamada se destina.
- `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` lista as respostas que o seu gateway dá para uma
  identidade, cada uma por seu status e pelo `code`, `type` ou `message` exato do seu corpo de erro
  (em `error`). Compare por `code` quando o gateway fornecer um. O padrão corresponde aos códigos por agente de um
  proxy de inferência e à recusa de orçamento da medição por trás dele:

  ```json
  {
    "refused": [
      { "status": 429, "code": "budget_exceeded" },
      { "status": 429, "type": "rate_limit_error", "message": "budget exceeded" },
      { "status": 403, "code": "agent_denied" },
      { "status": 400, "code": "identity_incomplete" },
      { "status": 400, "code": "identity_malformed" }
    ],
    "wait": [{ "status": 429, "code": "agent_request_limit" }]
  }
  ```

  A segunda entrada é correspondida por type e message porque a resposta da medição no formato
  Anthropic em `/v1/messages` não traz code.

- Nunca liste uma resposta que o gateway também dá para uma falha própria: `token_unavailable` (ele
  não consegue obter uma credencial, o que uma queda do serviço de credenciais também responde) ou
  `identity_missing` (a requisição não indicou agente e o gateway não tem padrão, um problema
  de configuração). Se listadas, `/v1/health` deixaria de ver a queda ou a
  configuração incorreta. Respostas no nível da implantação também continuam sendo falhas do provedor: `caller_*`,
  `scope_not_granted` (uma rota que os escopos do token não cobrem), `route_not_found`,
  `upstream_*`, `misconfigured`, `no_identity`.
- Uma resposta em `refused` faz essa requisição responder 403 `MODEL_CALL_REFUSED`, e uma em
  `wait` responder 503 `MODEL_CALL_DEFERRED` com o `Retry-After` do gateway; uma espera de dois segundos ou
  menos é aguardada uma vez primeiro, e a chamada é feita de novo. Nada tenta de novo nenhuma das respostas,
  `/v1/health` não as lê como falha do provedor, e os jobs em segundo plano seguram as chamadas
  dessa identidade ao mesmo modelo, por `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS` (10
  minutos por padrão) após uma recusa e pelo `Retry-After` após uma espera, enquanto o outro trabalho
  continua. Qualquer resposta que a tabela não liste é do provedor, como sempre.
- Como cada valor é encontrado: [cabeçalhos de identidade](https://github.com/ClickHouse/loom/blob/main/docs/architecture/llm-architecture.md#identity-headers).

<a id="switching-an-orgs-model-calls-off"></a>

#### Desligando as chamadas de modelo de uma org

Um admin da org pode interromper toda chamada de modelo para os dados da org, com ou sem
gateway, pela página Guardrails do painel ou pela API:

```bash
curl -s -X POST "$LOOM_BASE_URL/v1/org.set_model_calls" -H "Authorization: Bearer $ORG_ADMIN_TOKEN" \
  -d '{"org":"acme","enabled":false,"reason":"customer turned AI off"}'
```

- Enquanto desligado, nenhuma requisição de chat, embedding, rerank ou classify é enviada para a
  org, e nenhum vetor ou resposta em cache é servido a ela também: o desligamento é um
  kill switch. Uma requisição que precise de uma chamada é recusada (403 `MODEL_CALLS_DISABLED`):
  `memory.set` de uma memória que recebe embedding na escrita, ou em um namespace com um
  endpoint classify, `memory.search`, e `tracker.similar`,
  `tracker.duplicate_check` e `code.bisect_candidates` quando fazem embedding, julgamento
  ou rerank, entre elas. Uma leitura por chave (`memory.get`) e uma exclusão não fazem
  chamada de modelo e respondem normalmente.
  O embedder local roda no próprio processo e não envia nada, mas também é desligado.
  Uma chamada que indica a org é verificada mesmo quando o Loom a faz para si mesmo;
  apenas as chamadas próprias que não indicam org (a verificação de inicialização, a calibração) continuam.
- `"enabled": true` o liga de novo, e o trabalho em espera é executado.
- **O bloqueio de um admin da plataforma.** Um admin da plataforma pode adicionar `"locked": true`: os
  próprios admins da org ainda podem desligá-lo, mas religá-lo ou desbloqueá-lo é recusado
  (403, indicando quem o bloqueou) até que um admin da plataforma envie `"locked": false`. Uma alteração de admin da plataforma que não envie `locked`
  mantém o bloqueio. [`org.model_calls`](https://github.com/ClickHouse/loom/blob/main/docs/reference/operations/org.md#orgmodel_calls)
  lê a configuração, o bloqueio e quem os definiu.
- **O offboarding de uma org** é manual: um admin da plataforma envia
  `"enabled": false, "locked": true`. Os vetores de consulta em cache da org expiram
  após 30 dias. Nenhum caminho do Loom desliga uma org por conta própria.
- Todo servidor aplica uma alteração dentro de `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`
  (padrão 10; o processo que a recebeu, na sua próxima chamada). Um servidor que
  não consegue ler o desligamento por mais de
  `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS` (padrão 60, de 15 a 300) não faz nenhuma
  chamada de modelo para nenhuma org até conseguir.
- Desligue uma org somente depois que todas as réplicas executarem uma build que conheça o
  desligamento: uma mais antiga continua chamando modelos para a org.

O que espera, e por quanto tempo, enquanto uma org está desligada:

| Trabalho                                                                                                  | Enquanto a org está desligada                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Embeddings de memória (o worker de embedding)                                                             | Não são selecionados: as memórias permanecem, sem vetor, e recebem embedding quando a org voltar a ser ligada. Nunca são descartadas.                                                                                                                                                                                                                                                                                                                             |
| Requisições de extração (a fila de extração)                                                              | Ficam pendentes, sem serem reivindicadas. Quando uma requisição esperou `LOOM_MODEL_CALLS_OFF_MAX_WAIT_DAYS` (padrão 7, de 1 a 29) desde que entrou na fila e desde que a org foi desligada, ela é marcada como falha com um registro em `memory_drops`, motivo `model_calls_off`. O registro é escrito de forma síncrona antes: uma requisição cujo registro não possa ser escrito continua pendente. O limite fica abaixo de 30 dias porque o TTL de 30 dias da fila removeria a requisição antes, sem registro. |
| Marcação de entidades, consolidações de perfil, resumos de período, títulos de conversa, reflexões, destilação de observações | Não são selecionados; feitos quando a org voltar a ser ligada. Derivam de linhas que permanecem, então nada é descartado.                                                                                                                                                                                                                                                                                                                                         |
| Reparos de projeção                                                                                       | Não são varridos; tentados de novo quando a org voltar a ser ligada.                                                                                                                                                                                                                                                                                                                                                                                              |
| Julgamentos de conflito pendentes                                                                         | Não são julgados novamente. Um julgamento mais antigo que a hora da varredura quando a organização volta já ficou para trás da varredura, como um julgamento perdido em uma interrupção: a escrita permanece como foi feita.                                                                                                                                                                                                                                      |
| Ingestão de código, resumos de código, recuperação de embeddings de código                                | Os repositórios da organização são ignorados, então todo o seu índice de código, inclusive a busca lexical, permanece como estava. Um stub que a varredura de recuperação deixou para trás é encontrado pela sua varredura completa periódica.                                                                                                                                                                                                                     |
| Julgamentos de links de código                                                                            | Continuam pendentes.                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Embeddings do tracker, pontuação de links do tracker                                                      | O namespace é ignorado antes que o seu cursor avance.                                                                                                                                                                                                                                                                                                                                                                                                            |
| Amostras do juiz de recuperação                                                                           | Descartadas: uma amostra não é um trabalho devido.                                                                                                                                                                                                                                                                                                                                                                                                               |
| `loom reembed`                                                                                            | As linhas da organização são deixadas de fora, contadas (`skipped_switched_off`) e informadas; execute-o novamente quando a organização for religada. `--delete-old` mantém os vetores antigos de toda organização cujas linhas foram deixadas de fora.                                                                                                                                                                                                           |
| A reconstrução após uma troca de modelo                                                                   | É concluída; as linhas da organização são preparadas sem vetor e contadas no progresso da reconstrução (`rows_waiting`). Quando a organização for religada, os embeddings de memória, os chunks de código e os resumos de código são gerados novamente pelos seus workers de recuperação; uma linha de qualquer outra tabela fica sem vetor até ser escrita de novo ou até que uma reconstrução posterior a cubra.                                                  |

<a id="without-a-workload-token"></a>

#### Sem um token de workload

Onde a plataforma não fornece um token ao Loom (uma VM, um host de contêineres), o Loom assina o seu próprio: defina uma audiência no lugar do arquivo de token, e o gateway passa a confiar no Loom como emissor.

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example
LOOM_INFERENCE_TOKEN_AUDIENCE=loom-inference   # what the gateway's entry for Loom names
LOOM_PUBLIC_BASE_URL=https://loom.example      # the issuer is <this>/v1
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

Cada troca envia um novo token que vive cinco minutos: emissor `<LOOM_PUBLIC_BASE_URL>/v1`, subject `loom`, a audiência acima, assinado com RS256 usando a chave RS256 mais recente do keyring de assinatura. Crie uma antes com `loom
signing-key rotate --algorithm RS256`, a menos que `LOOM_JWT_SECRET` seja uma chave RSA, que já assina como RS256. A partir daí, essa chave também assina os próprios tokens do Loom, que todo Loom da implantação verifica; um segredo HS256 nunca é usado, pois o gateway não conseguiria verificá-lo. O gasto é atribuído a esse único subject.

A entrada do gateway para o Loom informa esse emissor, a audiência, o subject `loom`, RS256 e as chaves em `<LOOM_PUBLIC_BASE_URL>/v1/.well-known/jwks.json` (a descoberta OIDC está em `/v1/.well-known/openid-configuration`). O gateway precisa conseguir buscar essa URL por https; onde ele não consegue alcançar o Loom, publique uma cópia do conjunto de chaves em algum lugar que ele alcance. Cada Loom publica uma chave na próxima atualização do keyring depois que `rotate` a grava, e a chave só assina `--activate-in` depois (300 s por padrão): faça o rotate com um `--activate-in` maior do que o tempo que o gateway mantém a sua cópia das chaves, ou do que o tempo necessário para copiá-las. Uma chave desativada continua confiável no gateway até que a sua cópia seja atualizada. `loom signing-key retire` se recusa a desativar a última chave RS256 ativa quando `LOOM_INFERENCE_TOKEN_AUDIENCE` está definido no seu próprio ambiente, então execute-o com o ambiente dos servidores. O Loom (o servidor e todo comando `loom`) recusa essas configurações sem um `LOOM_PUBLIC_BASE_URL` https, com `LOOM_JWT_AUDIENCE` como audiência, ou com um emissor em que o seu próprio verificador confia, e o seu próprio verificador nunca aceita um token com a audiência do gateway.

<a id="apply-changes-and-check-the-result"></a>

## Aplicar as alterações e verificar o resultado

**Alterações feitas no painel:** aguarde até que Models & keys informe a configuração ativa. Se uma reconstrução estiver em andamento, acompanhe o progresso ali mesmo; tente novamente um job com falha a partir da mesma página. Não recrie os containers para aplicar uma alteração feita no painel.

**Somente alterações de ambiente:** para a stack publicada, recrie o container do servidor:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
curl -fsS http://localhost:7777/v1/health
```

Use o seu diretório de instalação e a sua porta, se forem diferentes. Recrie o container: um simples `docker compose restart` não recarrega o ambiente a partir de `.env`. Servidores nativos devem ser reiniciados com o ambiente atualizado. Para a stack do checkout, execute `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build --force-recreate loom`.

O health informa `fake_llm`, `fake_embedding`, `providers_degraded` e `problems` acionáveis. Com os dois providers reais configurados, as flags fake devem ser false. Isso verifica a configuração e a saúde do servidor; salve e recupere também um fato de teste e experimente uma resposta para confirmar que a sua conta consegue chamar os modelos escolhidos.

Alterar o LLM ou rotacionar uma chave de API não exige reconstruir os embeddings. Alterar o modelo de embedding exige, mesmo quando as dimensões continuam as mesmas. No caso de alterações de ambiente (em vez do fluxo pelo painel), os índices existentes de memória, documentos, tracker e código precisam dos respectivos procedimentos de re-embedding ou de nova ingestão; `loom reembed` sozinho não é uma reconstrução universal de todos os índices. Se as dimensões mudarem, verifique as restrições de vetor do schema existente antes de aplicar a nova configuração. Preserve os seus dados e siga [manutenção](https://github.com/ClickHouse/loom/blob/main/docs/operations/maintenance.md#re-embed-and-re-extract) e [operações do índice de código](https://github.com/ClickHouse/loom/blob/main/docs/operations/code-index-operations.md).
