<!-- i18n: source=docs/architecture/dify-adapter.md blob=34e577e662d3664872b0d8163d92fc018b884cd1 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/dify-adapter.md), which is authoritative.

<a id="dify-tools-adapter"></a>

# Adaptador de ferramentas do Dify

O plugin do Dify chama o Loom por meio do SDK Python e não armazena nenhum conteúdo localmente.
Suas três ferramentas nativas correspondem a `memory.set`, `memory.search` e `knowledge.search`.
O pacote independente opcional exige a série secundária 0.11 do SDK de plugins e o
SDK do Loom 1.x; o servidor e o SDK principal não dependem do Dify.

<a id="invariants"></a>

## Invariantes

As credenciais do provedor vinculam o endpoint, a organização, o namespace, o token e o agente.
Um parâmetro de formulário obrigatório, User ID, vincula o usuário final fora dos argumentos do modelo.
A recuperação pode ler as memórias desse usuário entre agentes do Loom; os documentos são compartilhados pelo
namespace. Ambas as operações de busca fixam `namespace_scope=this`. Namespaces separados
e concessões de token restringem aplicações que não confiam umas nas outras. O Loom aplica a essas chamadas as regras habituais de
ator, proveniência, tratamento de conflitos, esquema progressivo e
apagamento.

O administrador controla o destino HTTP. O plugin verifica o formato da URL
e rejeita credenciais, consultas e fragmentos incorporados; a política de rede da implantação
restringe quais hosts o executor do plugin pode alcançar. As requisições têm timeouts finitos
e uma única tentativa. Os erros das ferramentas não expõem credenciais nem detalhes da resposta remota.
O plugin não cria nenhum banco de dados, cache, provedor de modelo ou caminho de telemetria adicional.

O Dify altera o threading do Python com greenlets do gevent. Cada requisição constrói, chama
e fecha seu cliente do SDK dentro do pool de threads nativas do hub atual, de modo que clientes sobrepostos
não executem loops de asyncio na mesma thread nativa. As credenciais permanecem locais a
essa requisição. A chamada do SDK espera no máximo 30 segundos; a admissão no pool, a configuração do cliente e
o fechamento têm esperas separadas, em vez de um prazo para a invocação inteira.

O Dify instala o pacote a partir de um `.difypkg` local. A CLI oficial empacota o código-fonte,
os metadados, os requisitos de execução, os ícones, a licença, o README e a declaração de privacidade. Seu
arquivo de ignore exclui testes, ambientes, credenciais e estado de build. A CI invoca
as ferramentas nativas contra um transporte HTTP falso do SDK, verifica chamadas nativas sobrepostas
via HTTP em loopback, valida os esquemas das ferramentas nativas e gera o pacote.
Os testes ao vivo só são executados com uma URL privada explícita do Loom e um
token; a execução na plataforma é documentada separadamente desses testes do SDK.

<a id="source-entry-points"></a>

## Pontos de entrada do código-fonte

- `integrations/dify/client.py`: chamadas do SDK com escopo e falhas de requisição.
- `integrations/dify/provider/loom.py`: validação de credenciais.
- `integrations/dify/tools/`: implementações e esquemas das ferramentas nativas.
- `.github/workflows/dify-plugin.yml`: testes e empacotamento.
