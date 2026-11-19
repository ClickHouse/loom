<!-- i18n: source=docs/architecture/source-push.md blob=3114dcae551415671efdfa6f880845f601eb7342 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/architecture/source-push.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/architecture/source-push.md), which is authoritative.

<a id="document-source-push-notifications"></a>

# Notificações push de fontes de documentos

Exige `LOOM_KNOWLEDGE_SOURCE_PUSH_ENABLED=true` para solicitar sincronizações de fontes de documentos a partir de notificações do provedor. As notificações nunca gravam o conteúdo dos documentos nem alteram a política de acesso da fonte. A consulta periódica normal de 15 minutos continua habilitada e é o caminho de recuperação para entregas perdidas. Desabilitar o push pausa a configuração, as entregas e a renovação, sem impedir o novo selamento de segredos nem a desativação da configuração.

<a id="source-entry-points"></a>

## Pontos de entrada no código

- `server/knowledge/api/ops_source_push.py` (`OPERATIONS`) declara as operações de administrador para configuração, confirmação e status.
- `server/knowledge/app/source_push_routes.py` (`notification`) limita o tamanho dos corpos de callback e despacha as entregas dos provedores.
- `server/knowledge/app/source_push.py` (`receive`, `_fresh`, `_put`, `renew_drive`, `reseal_registry`) autentica os avisos e coordena as gravações do ciclo de vida do registro.
- `server/knowledge/domain/source_push.py` (`fingerprint`, `verify_drive`, `verify_notion`, `verify_linear`) vincula a configuração e verifica a prova do provedor; `server/knowledge/store/source_push.py` (`get`, `put`) persiste as versões do registro.
- `server/knowledge/app/sources.py` (`sources_lease`, `deactivate_namespace_sources`) coordena as mudanças no ciclo de vida das fontes; `server/erasure/store/steps_ledgers.py` (`delete_source_push_by_namespace`) conclui a exclusão do registro.

<a id="invariants"></a>

## Invariantes

- Entregas autenticadas solicitam a sincronização pelo caminho existente da fonte. Os payloads do provedor não podem gravar conteúdo nem ampliar o acesso à fonte.
- Uma entrega deve corresponder ao ID da fonte atual, à geração da configuração e à credencial, ao item e ao fingerprint da configuração. O lease da fonte e as leituras sequenciais impedem a substituição e a desativação da fonte e respostas tardias do provedor.
- Os segredos de push usam selamento vinculado a um propósito. Apenas um administrador pode revelar um desafio pendente; leituras comuns, sujeitos de auditoria e reexecução de idempotência o omitem.
- As gravações no registro e a exclusão do namespace compartilham um lease de escritor com fencing. A exclusão remove todas as versões armazenadas, e a publicação da configuração exige suporte verificado da frota para essa etapa de exclusão.

<a id="registration-and-authentication"></a>

## Registro e autenticação

O registro de push é separado do registro de fontes e dos cursores de sincronização. Uma linha vincula uma organização, um namespace, o ID da fonte, o provedor e uma geração de configuração aleatória a um fingerprint da credencial e dos itens da fonte. A configuração é uma operação de administrador. É necessária uma URL base pública HTTPS configurada explicitamente.

A URL de callback coloca o namespace, o ID da fonte e a geração da configuração no caminho, e a organização exata no parâmetro de consulta obrigatório `org`. A codificação da consulta preserva nomes de organização que contêm barras ou caracteres reservados de URL. Um callback relê o registro e a fonte atual sob o lease de fontes do namespace antes de registrar uma solicitação de sincronização. Uma fonte removida ou substituída, uma credencial alterada, um conjunto de itens alterado, um namespace inativo ou uma geração desativada fazem a entrega ser recusada. Os payloads e cabeçalhos do callback não são armazenados nem registrados em log.

- Os canais do Drive usam IDs de canal aleatórios independentes e tokens de canal selados. Uma entrega deve corresponder ao ID do canal ativo, ao token e ao ID do recurso antes da expiração. Isso é verificação por token de canal: o Drive não assina essas entregas. Notificações `sync` iniciais são confirmadas sem solicitar trabalho. As respostas de watch só são ativadas depois de verificar novamente a geração da configuração.
- A configuração do Notion captura um desafio de verificação limitado e selado para uma geração nova. Apenas um administrador pode revelá-lo enquanto a configuração está pendente. Depois de verificar a assinatura na interface do provedor, o administrador confirma essa geração exata e o fingerprint do desafio. Um desafio sem assinatura não pode ativar uma assinatura nem solicitar sincronização. As entregas ativas exigem o HMAC do corpo bruto e a identidade do workspace configurada.
- A configuração do Linear primeiro expõe um endereço de notificação pendente. O administrador cria o webhook nesse endereço e depois confirma a mesma geração com o segredo de assinatura. As entregas exigem o HMAC do corpo bruto, o timestamp em milissegundos do corpo assinado dentro de um minuto e a identidade da organização configurada.

A identidade do provedor é lida com a credencial já existente da fonte durante a configuração. Os corpos das notificações têm um limite fixo de tamanho. Notificações válidas duplicadas são combinadas com a solicitação de sincronização pendente existente; uma sincronização completa em espera é preservada. Callbacks bem-sucedidos respondem HTTP 200 dentro de um orçamento de quatro segundos do handler.

<a id="drive-coverage-and-renewal"></a>

## Cobertura e renovação do Drive

Um canal de mudanças do usuário no Drive não cobre todas as mudanças em drives compartilhados. Por isso, a configuração cria canais para o corpus do usuário e para cada drive compartilhado do qual a conta é membro, até 19 drives. Uma listagem de associação completa e limitada evita confundir o ID do drive de um arquivo compartilhado diretamente com associação: os itens compartilhados diretamente com um não membro usam o log do usuário. A configuração recusa um conjunto de corpora incompleto ou acima do limite, em vez de afirmar uma cobertura completa de push. Mover um arquivo para um novo corpus pode exigir nova configuração; a consulta periódica ainda descobre a mudança.

Um worker limitado, desabilitado por padrão, renova os canais antes da expiração. Ele cria um novo canal único, valida a resposta e a geração da fonte e depois o troca sob o lease da fonte. Em seguida, tenta parar o canal antigo, sem garantia. Uma renovação com falha mantém a consulta periódica disponível e exibe o canal expirado no status da configuração.

O catálogo de configuração controla os prazos de trabalho do provedor, de parada por canal, de limpeza total e de passagem do worker. Seus valores padrão são 40, dois, cinco e 120 segundos. Uma passagem deixa de iniciar renovações quando não há tempo suficiente para uma tentativa do provedor e duas janelas de limpeza. A configuração recusa um trabalho do provedor somado à limpeza que possa exceder a janela de trabalho de 48 segundos dentro do lease de renovação de 120 segundos, ou um orçamento de passagem que não deixe tempo para iniciar uma renovação. O prazo de parada do canal não pode exceder a janela total de limpeza.

<a id="lifecycle-and-storage"></a>

## Ciclo de vida e armazenamento

O registro é ordenado por organização, namespace e ID da fonte, de modo que as leituras por namespace podam o intervalo físico de chaves. As varreduras de manutenção avançam nessa mesma ordem. O registro armazena a tupla de identidade completa, o ator marcado pelo servidor e a proveniência derivada. Os segredos usam selamento vinculado a um propósito e participam do novo selamento. As leituras comuns de fontes e os sujeitos de auditoria nunca incluem tokens ou desafios. A leitura de configuração do administrador ignora o armazenamento de reexecução de idempotência, de modo que um desafio pendente revelado não possa ser reexecutado a partir de um cache comum depois da ativação.

A remoção da fonte, a rotação de credenciais, as atualizações de itens e a desativação do namespace desativam a configuração de push sob o mesmo lease da fonte. Respostas externas de watch tardias não conseguem reviver uma configuração desativada. A exclusão do namespace remove todas as versões do registro pelo mecanismo de exclusão já existente. As gravações de configuração exigem uma frota que entenda a etapa de exclusão do registro; uma frota ilegível leva à recusa. Antes de reverter para um build sem este registro, desabilite o push, apague as linhas do registro com o build atual e verifique se as exclusões dos namespaces foram concluídas. Os canais do provedor que não podem ser parados expiram; callbacks desativados os recusam.

<a id="alternatives"></a>

## Alternativas

Colocar o estado de push na linha da fonte faria cada entrega disputar com as gravações de credenciais e arriscaria propagar uma credencial antiga. Colocá-lo nos detalhes de sincronização misturaria segredos de configuração selados com a saída de inspeção comum. O registro separado evita ambos os problemas. Como as notificações são apenas avisos, uma fila de eventos durável, um arquivo de payloads do provedor e um segundo caminho de ingestão são desnecessários.

<a id="provider-contracts"></a>

## Contratos dos provedores

- [Notificações push do Drive](https://developers.google.com/workspace/drive/api/guides/push)
- [Logs de usuário e de drives compartilhados do Drive](https://developers.google.com/workspace/drive/api/guides/about-changes)
- [Verificação de webhook do Notion](https://developers.notion.com/reference/webhooks)
- [Entrega de eventos do Notion](https://developers.notion.com/reference/webhooks-events-delivery)
- [Verificação de webhook do Linear](https://linear.app/developers/webhooks)
