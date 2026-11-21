<!-- i18n: source=docs/architecture/google-adk-adapter.md blob=6c709e61102ee7413e9b4764bfd5c80f4511830e model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/google-adk-adapter.md), which is authoritative.

<a id="google-adk-memory-service"></a>

# Serviço de memória do Google ADK

O adaptador do Google ADK implementa o serviço de memória do framework por meio dos
SDKs de Python e TypeScript do Loom. Os pacotes do framework são dependências opcionais. Cada
aplicação usa um namespace do Loom separado, porque seus documentos de conhecimento são
compartilhados com todos os leitores desse namespace.

As gravações de sessão extraem o texto dos eventos ativos do usuário e do modelo, excluindo payloads de
ferramentas e pensamentos, e então chamam `memory.set_from_messages` em lotes limitados.
Os dois SDKs recusam ids de usuário vazios, sentinela ou com espaços nas bordas antes de qualquer chamada.
Cada chamada carrega o usuário final e uma sessão derivada do usuário e do id de sessão
nativo. O Python também implementa os métodos de eventos incrementais, memória direta
e exclusão do framework instalado. Os ids de exclusão do Python carregam a chave, a sessão,
o escritor e a versão exata. Eles chamam `memory.delete_version` diretamente. Uma versão ausente ou
reescrita é uma operação nula (no-op), e
a linha de outro escritor nunca é selecionada. O campo de resposta aditivo `agent` nos
resultados de busca de memória vem da identidade da linha armazenada, incluindo páginas estacionadas.
Um resultado sem escritor ou versão conhecidos não tem id de exclusão. Clientes mais antigos ignoram
o campo extra; a exclusão exata do ADK exige um servidor que o forneça. Isso não adiciona
nenhuma coluna persistida nem migração. O TypeScript retorna ids informativos com escopo de escritor;
a interface do framework instalada não tem método de exclusão.

A busca primeiro chama `memory.search` para o usuário vinculado e depois preenche os espaços de resultado
disponíveis com `knowledge.search` no escopo do namespace. O conhecimento é contexto compartilhado da aplicação,
não memória privada do usuário. Os dois SDKs usam a mesma codificação de digest; a
representação nativa de timestamp do framework é convertida na fronteira. Uma instância do serviço
é vinculada a uma aplicação. As concessões de namespace isolam aplicações que não confiam entre si.

<a id="source-entry-points"></a>

## Pontos de entrada no código-fonte

- `sdk/python/loom/integrations/google_adk.py`: `LoomMemoryService`.
- `sdk/typescript/src/integrations/google-adk.ts`: `LoomMemoryService`.

<a id="invariants"></a>

## Invariantes

O servidor fornece a proveniência de ator e de origem por meio de seu caminho de gravação comum.
O adaptador não adiciona banco de dados, esquema nem provedor. As regras existentes de exclusão de memória,
apagamento de conhecimento e atualização contínua (rolling upgrade) se aplicam. A extração semântica pode revisar
fatos; este serviço não é um armazenamento exato de transcrições. Os resultados de conhecimento não têm id de
memória de usuário e não podem ser excluídos por meio do serviço de memória.
