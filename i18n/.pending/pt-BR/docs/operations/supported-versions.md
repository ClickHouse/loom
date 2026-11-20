<!-- i18n: source=docs/operations/supported-versions.md blob=32631f7ef1ce0afc4871e89ec501bcad37732e46 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/operations/supported-versions.md), which is authoritative.

<a id="supported-versions"></a>

# Versões suportadas

As versões N e N−1 recebem correções, e um SDK em `X.Y` funciona com um servidor em `X.Y` ou `X.(Y−1)`. O Loom
publica uma única versão para o servidor, as duas imagens, o aplicativo web, os dois SDKs, as skills da raiz e o `appVersion` do
chart Helm ([processo de release](../development/release-process.md)). Esta página também trata dos
plugins de agentes de código e de como se anuncia uma mudança que remove algo.

<a id="release-lines"></a>

## Linhas de release

| Linha | O que recebe                                                                                      |
| ----- | ------------------------------------------------------------------------------------------------- |
| N     | a versão minor mais recente (`X.Y`): todas as correções, como release de patch (`X.Y.(Z+1)`) quando não podem esperar |
| N−1   | a versão minor anterior: correções de segurança e correções de perda de dados ou de resposta errada, como patches |
| N−2   | nenhuma: atualize para N ou N−1                                                                   |

Uma versão minor sai da `main` aproximadamente a cada duas semanas; um patch sai da branch
`release/X.Y` da linha, com a correção integrada primeiro à `main`. Uma atualização segue
[atualizações](https://github.com/ClickHouse/loom/blob/main/docs/operations/upgrades.md); duas versões adjacentes atendem lado a lado enquanto uma substitui a outra
([atualizações graduais](../architecture/rolling-upgrades.md)).

<a id="sdks-and-servers"></a>

## SDKs e servidores

Os SDKs (`clickhouse-loom` no PyPI, `@clickhouse/loom` no npm) são publicados a cada release, na versão do
servidor; o `latest` do npm aponta para a final mais alta, e um patch de uma linha mais antiga recebe a dist-tag
`release-X.Y`. Com o servidor mais antigo:

- um campo que o SDK envia e que o servidor mais antigo não conhece é atendido e citado em `ignored_args` na resposta, exceto em uma op que altera estado, em que uma chave que o servidor interpreta como erro de grafia
  de um de seus campos (um nome próximo, um sinônimo, uma palavra do nome de um campo) é recusada com
  `422 UNKNOWN_ARGUMENT` e nada é gravado
  ([campos desconhecidos e removidos](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#unknown-and-removed-fields));
- uma operação que o servidor mais antigo não atende responde `404`, que os SDKs lançam como `NotFound`
  (Python `loom.errors.NotFound`, TypeScript `NotFound`): uma chamada nunca tem sucesso em silêncio contra um
  servidor que não consegue executá-la. O mesmo erro indica uma linha ausente, então um cliente que precise distinguir os dois
  lê `ops.capabilities` na inicialização
  ([como descobrir o que um deployment atende](https://github.com/ClickHouse/loom/blob/main/docs/reference/http-api.md#discovering-what-a-deployment-serves)).

Uma versão major (`X.0`) pode quebrar a regra em relação ao último servidor `(X−1).Y`; suas notas de release
dizem do que um SDK em `X.0` precisa.

Um SDK mais antigo continua funcionando com um servidor mais novo enquanto o servidor atender ao que ele chama;
uma remoção segue a regra abaixo.

<a id="coding-agent-plugins"></a>

## Plugins de agentes de código

Os plugins do Claude Code e do Codex têm sua própria versão e indicam o servidor mais antigo que seus
hooks suportam (`MIN_SERVER_VERSION` em `lib/server-version.mjs`). Uma sessão com um servidor mais antigo
começa com uma linha `loom:` informando isso, e o plugin continua funcionando onde for possível
([Claude Code](../features/integrations/claude-code.md)). O plugin do Claude Code exige o Claude Code
2.1.139 ou mais recente, a primeira versão que executa os `args` de um hook sem shell.

<a id="removing-something"></a>

## Remoção de algo

Uma remoção é anunciada, manualmente no pull request de release, nas notas de release da versão minor
anterior à que a remove, em "What changed for clients". Até a remoção, o
servidor atende o campo ou a op antiga; depois dela, um campo de requisição removido ainda é aceito e citado
em `ignored_args`, de modo que um cliente que o envia fica sabendo da mudança sem falhar, e uma op removida
responde `404` (`NotFound`), que `ops.capabilities` distingue de uma linha ausente.

<a id="release-artifacts"></a>

## Artefatos de release

Toda imagem de release é construída na arquitetura em que roda, verificada quanto a vulnerabilidades críticas
com correção disponível e publicada com sua proveniência e um SBOM. O índice multi-arch de cada imagem traz uma
assinatura keyless do workflow de release; [imagens de contêiner](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#container-images)
traz o comando `cosign verify`. Cada arquivo no GitHub Release é assinado da mesma forma, em um
`<file>.sigstore.json` ao lado dele; [arquivos de release](https://github.com/ClickHouse/loom/blob/main/docs/operations/deployment.md#release-files) traz o
comando `cosign verify-blob`.
