<!-- i18n: source=docs/features/conversations-and-sessions.md blob=b2079f3bf2ad65d5e3c485f2587db24b35257342 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/conversations-and-sessions.md), which is authoritative.

<a id="conversations-and-sessions"></a>

# Conversas e sessões

O Loom mantém três tipos de estado de sessão, cada um em seu próprio plano:

| Plano              | Armazena                                                                   | Embedding e busca                                                           | Escrito por                                                                 |
| ------------------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Plano de conversa  | Os turnos literais de uma thread (`loom.conversations`)                    | Turnos do usuário e do assistente e resumos de compactação, por `conversation.search` | `conversation.append`, os hooks de captura dos plugins, `loom ingest-transcript` |
| Briefs de sessão   | O estado condensado e retomável de uma linha de trabalho (`loom.session_briefs`) | Não                                                                 | `session.brief_save`, os plugins de codificação na compactação e no fim da sessão |
| Memória            | Fatos duráveis extraídos do que foi dito                                   | Sim, por `memory.search`                                                    | `memory.set`, `memory.set_from_messages`                                    |

Armazenar um turno não cria uma memória; para transformar uma conversa em fatos, envie-a para
[`memory.set_from_messages`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md). Um proprietário pode desativar o registro de transcrições sem perder
a memória extraída, e um brief nunca compete com fatos na recuperação.

<a id="conversations"></a>

## Conversas

<a id="recording-is-the-owners-choice"></a>

### A gravação é escolha do proprietário

`conversation.append` armazena um turno somente quando o namespace tem o registro de conversas
ativado. A chave (`conversation_logging_enabled` em `loom.namespaces`) vem desativada por padrão
e falha de forma fechada: um namespace sem linha não é
gravado. O proprietário a ativa com `conversation.set_logging(enabled=true)` (admin) ou
com a chave **Record conversations** na página Conversas do painel.

Quando o registro está desativado, o append é aceito e nada é mantido: a resposta traz
`stored: false, reason: "logging_disabled"`. Um cliente não pode sobrescrever a escolha do proprietário.
Se o turno for removido enquanto seu embedding é calculado, o append descarta o vetor tardio e
responde `stored: false, reason: "deleted"`.

<a id="writing-turns"></a>

### Escrevendo turnos

`conversation.append` recebe `thread_id`, `role` (`user`, `assistant`, `system` ou
`tool`), `content` e, opcionalmente, `msg_id`, `reasoning` (o raciocínio do modelo mantido em
sua própria coluna), `user_id`, `run_id`, `metadata` e `tokens`. A thread é criada no
primeiro append; `conversation.create` apenas anexa antecipadamente um `title`, `metadata` ou `run_id`.

- **Idempotente em `msg_id`.** Um turno cujo `(thread_id, msg_id)` já foi gravado não é
  escrito de novo (`reason: "duplicate"`). Sem um `msg_id`, o servidor deriva um a partir de
  um hash da thread, do papel e do conteúdo, de modo que um cliente que registra um turno de novo não
  duplica a transcrição.
- **Turnos de user e assistant têm embedding**, até os primeiros 2,048 caracteres,
  que a linha de embedding mantém: um novo embedding parte do texto a partir do qual o vetor foi construído.
  Linhas gravadas por versões anteriores mantêm 240 caracteres, e um novo embedding trabalha a partir
  delas. Turnos `system` e `tool` ficam na transcrição sem indexação, exceto o resumo
  de uma compactação.
- **O embedding falha de forma aberta.** Um embedding com falha ainda armazena o turno, com o erro
  registrado na sua linha de embedding. Nada tenta de novo: o turno fica fora da
  busca vetorial e só é encontrado pelo fallback de substring.
- **Um turno classificado como high não recebe embedding.** Um turno cujo texto contém um formato high (uma
  chave de API colada, uma atribuição `password=`, um número de cartão, um SSN ou um PIN informado)
  é armazenado como escrito, mas não recebe linha de embedding, e seu texto nunca é enviado ao modelo de embedding.
  `conversation.search` nunca o retorna, nem por vetor nem pelo fallback de substring,
  e `conversation.threads` lista um último turno assim com um trecho vazio (e um título
  que se classifica como high fica vazio); ele verifica os 240 caracteres que seu trecho mostra e
  1,024 além deles. O gerador de títulos nunca envia esse turno ao seu modelo. Uma thread cujos
  primeiros oito turnos de user e assistant são todos classificados como high, ou cujo título gerado
  é classificado como high, permanece sem título, marcada com `title_refused` em seus metadados para que o
  gerador de títulos não peça de novo; uma com menos desses turnos espera, sem marca, por um turno
  comum. Novos `metadata` vindos de `conversation.update` ou `conversation.create` substituem
  a marca, e o gerador de títulos então pergunta mais uma vez e marca a thread de novo.
  `conversation.get` e `conversation.list` ainda servem o turno em sua thread
  ([sensibilidade](https://github.com/ClickHouse/loom/blob/main/docs/architecture/security.md#sensitivity)).

<a id="saving-single-messages"></a>

### Salvando mensagens individuais

Um cliente de chat pode permitir que um usuário salve uma mensagem para busca posterior sem gravar as
transcrições do namespace. `conversation.save_message` (writer) é esse opt-in, e
`conversation.search_saved_messages` (viewer) é a sua busca. Uma mensagem salva é uma
linha de conversa, nunca um documento na coleção pessoal do usuário
([compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md#saved-messages)).

Os clientes salvam mensagens somente quando todas as réplicas preservam o estado salvo durante a
compactação. Veja as
[regras de rolling upgrade](https://github.com/ClickHouse/loom/blob/main/docs/architecture/rolling-upgrades.md#the-rules).

Elas diferem de `conversation.append` e `conversation.search` em seis aspectos:

- **A chave Record conversations não se aplica.** O salvamento é armazenado mesmo quando a
  chave (ou `conversation.set_logging`) desativou a gravação, e salvar não ativa
  a gravação.
- **A mensagem é sinalizada, não rotulada de novo.** A linha da transcrição e sua linha de embedding
  carregam `is_saved` e `saved_at`, o momento em que o usuário salvou a mensagem. Uma mensagem que a
  transcrição já contém é sinalizada no lugar: seu `run_id` e seu conteúdo permanecem
  como foram gravados.
- **A busca é por usuário.** `conversation.search_saved_messages` retorna apenas mensagens
  sinalizadas pertencentes a `user_id`, no único namespace indicado. `conversation.search` ranqueia
  todos os turnos gravados e, por padrão, se estende pelos namespaces do chamador.
- **A busca é híbrida.** A perna vetorial gera o embedding dos primeiros 2,048 caracteres de uma mensagem;
  uma perna BM25 lê o texto inteiro, de modo que um termo exato (um id de ticket, um nome de tabela) ou
  uma frase no fundo de uma resposta salva longa ainda é ranqueado. `search_mode="hybrid"` (padrão) funde
  as duas por rank recíproco, `semantic` e `lexical` executam uma só perna, e `lexical_weight`
  (0–1) escala a perna BM25. O trecho de um resultado BM25 é centrado nos termos encontrados. O
  corpus é o conjunto de mensagens salvas de um usuário, então suas estatísticas são calculadas a cada consulta.
- **Um token fixado em um usuário funciona aqui.** Uma integração que atende muitos usuários por
  uma única credencial a emite com `--read-scope own_user` (e `--read-user-id` para um
  usuário). As operações de mensagens salvas então agem somente como esse usuário: um corpo que nomeia outro
  `user_id` é recusado com 403, e toda instrução carrega o usuário do token. Um token de
  usuário final chama essas operações com `namespace: "@me"`
  ([compartilhamento](https://github.com/ClickHouse/loom/blob/main/docs/architecture/sharing.md#saved-messages)). As tabelas de conversa não têm
  `session_id`, então um token `own_session` é recusado.
  Toda outra operação de conversa (`append`, `get`, `list`, `search`, `threads`, ...) continua
  recusada com tal token, porque uma thread mistura usuários e não tem eixo de usuário para delimitar.
- **Propriedade, exclusão e papel são verificados a cada salvamento.** Salvar uma mensagem armazenada
  sob outro `user_id` responde `indexed: false, reason: "owner_mismatch"`. Uma mensagem com
  tombstone responde `reason: "deleted"`, e um turno system ou tool armazenado, que nunca é
  embutido, responde `reason: "unsupported_role"`. Uma mensagem cujo texto é classificado como high
  também nunca recebe embedding: ela responde `reason: "high_sensitivity"`, e uma nova não é
  armazenada. Uma que uma versão anterior salvou nunca é um resultado vetorial ou por palavra-chave de
  `conversation.search_saved_messages`, e `conversation.list_saved_messages` a lista
  com um trecho vazio (ele verifica os `conversation_search_excerpt_chars` caracteres que
  mostra e 1,024 além deles, como `conversation.threads` faz).
  As verificações também se aplicam a uma mensagem que já está salva.

Salvar e remover dos salvos compartilham uma lease de thread renovável com as escritas de embedding do append,
a retirada da compactação e a exclusão da thread. Append e compactação concluem o
trabalho do provedor antes de obter a lease.

A linha da transcrição decide quem é o dono de uma mensagem salva. Um salvamento reivindica apenas linhas que nenhum
usuário possui ou que o chamador possui, na própria mutação, de modo que, entre dois usuários salvando uma mesma
mensagem sem dono, o primeiro a reivindica e o segundo recebe `owner_mismatch`. A linha de
embedding só é sinalizada depois que a linha da transcrição é lida de volta como do chamador.

Um salvamento pode ser repetido com segurança. Dois salvamentos concorrentes de uma mensagem nova armazenam uma única linha de transcrição:
seu insert carrega um token de deduplicação derivado da identidade da mensagem. Cada
chamada relê a mensagem e repara o que uma escrita interrompida ou concorrente deixou
para trás: grava uma linha de embedding ausente, gera o embedding de novo quando o último falhou, e
sinaliza qualquer linha ainda sem sinalização. Um salvamento cujo embedding falha armazena a mensagem como salva e
responde `indexed: false, reason: "embed_failed"`; salvá-la de novo tenta o embedding outra vez. Um
append de transcrição cujo embedding chega depois de um salvamento concorrente sinaliza sua própria linha
de embedding.

`conversation.list_saved_messages` (viewer) percorre as mensagens salvas de um usuário, sem
consulta, das salvas mais recentemente para as mais antigas (`limit` até 200, um `cursor` opaco e um
`conversation_id` opcional). Seu `ts` é o momento em que o usuário salvou a mensagem, independente
do horário da mensagem na transcrição. Linhas gravadas sem timestamp de salvamento usam o timestamp
da mensagem. É a primeira visão de um painel "Saved"; a busca
a restringe quando o usuário digita. `conversation.saved_status` (viewer) recebe um
`conversation_id` e até 500 `message_ids` e responde quais deles esse usuário
salvou, de modo que um cliente desenha o estado salvo de cada mensagem ao abrir uma conversa
em uma única chamada.

`conversation.unsave_message` (writer) retira uma mensagem salva da busca do seu proprietário.
Onde o namespace grava transcrições, o turno permanece na transcrição com
a sinalização removida, porque o proprietário escolheu gravá-lo. Onde não grava, o turno
é removido com um delete leve, porque só existia por causa do salvamento
(`removed: true`). O embedding mantém um tombstone para que versões mais antigas não reapareçam.
Uma mensagem
que o usuário do chamador nunca salvou, ou que outro usuário salvou, responde `found: false`; nunca
diz qual dos casos. Remover dos salvos é idempotente, e salvar de novo restaura a mensagem. A exclusão
por usuário remove as mensagens salvas junto com o resto das linhas de conversa desse usuário.

<a id="reading-and-searching"></a>

### Leitura e busca

| Operação               | Papel  | Faz                                                                                                                        |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------- |
| `conversation.threads` | viewer | Threads recentes de um namespace, das mais novas às mais antigas (`window_days`, `limit`, `offset` até 100,000; `agent="*"` para todos os agentes). |
| `conversation.get`     | viewer | Metadados de uma thread; `include_messages=true` pagina seus turnos (`order="desc"` para o final).                         |
| `conversation.list`    | viewer | Os turnos de uma thread, paginados por cursor, opcionalmente por `run_id`.                                                 |
| `conversation.search`  | viewer | Busca vetorial sobre turnos com embedding, opcionalmente por thread, execução ou papel.                                    |
| `conversation.update`  | writer | Altera `title`, `metadata` ou `run_id` de uma thread.                                                                      |

`conversation.search` ranqueia os turnos por similaridade de cosseno, descarta os abaixo de
`LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY` (0.30) e, por padrão, se estende pelos namespaces do chamador
(`namespace_scope="all_mine"`). Quando o modelo de embedding está indisponível, recorre a uma
correspondência de substring para que a operação ainda responda. Os resultados mostram os primeiros
`LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS` caracteres de um turno (240 por padrão); leia o turno
com `conversation.get` ou `conversation.list`. A operação fica oculta do MCP;
os agentes recebem a seção de conversas de [`loom_search`](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md).

`memory.recall` é uma leitura diferente: ele busca os turnos que uma extração leu (mantidos
em `loom.memory_evidence`), que existem quer o plano de conversas grave ou não. Veja
[busca e pergunta](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md).

<a id="compacting-and-deleting"></a>

### Compactação e exclusão

- **`conversation.compact`** (writer, requer um provedor de LLM) resume os turnos mais antigos
  anteriores aos últimos `keep_recent` (padrão 10) que cabem no prompt do sumarizador, grava o
  resumo como um único turno `system` com embedding cujo `metadata.compacted_from` lista os
  `msg_id`s resumidos, e põe tombstone nos originais e em seus embeddings:
  `conversation.search` então encontra o resumo no lugar deles. Um turno que outro agente da
  thread ainda mantém ativo com o mesmo texto, como um turno que a busca indexa, mantém seu
  embedding (veja delete abaixo). Uma janela com menos de `min_to_compact` (padrão 4) turnos
  novos é deixada como está. Um resumo anterior não conta nem para `min_to_compact` nem para
  `messages_summarized`. Resumos no início da janela são incorporados ao prompt a partir de uma
  cota separada; um que não cabe no que resta dela permanece ativo, e um que uma execução com falha deixou
  (seus turnos todos ainda ativos e na janela, no `ts` do primeiro) é
  substituído sem ser lido. Um resumo mais adiante na janela consome o orçamento do prompt como qualquer turno e
  é substituído junto com ele. Um turno classificado como high fica fora da janela: ele
  permanece ativo e nunca é enviado ao sumarizador, de modo que o resumo, colocado no primeiro
  turno da janela, pode vir antes dele na thread. Um turno salvo com
  `conversation.save_message` também nunca é resumido nem recebe tombstone; ele permanece literal
  entre o resumo e os turnos recentes. Um resumo que não pode receber embedding não compacta
  nada (`reason: "embed-failed"`), e o mesmo vale para um que é classificado como high
  de qualquer forma (`reason: "summary-high-sensitivity"`), já que nunca recebe embedding. Ao contrário de
  `embed-failed`, essa recusa não vale uma nova tentativa: a janela continua a mesma enquanto seus
  turnos estão ativos, então uma nova tentativa paga pela mesma chamada ao sumarizador e muito provavelmente obtém o
  mesmo resumo. Uma janela que contém apenas turnos salvos ou turnos classificados como high
  responde `reason: "nothing-to-compact"`.
- **`conversation.delete`** (admin) põe tombstone nos turnos, na linha da thread e nos
  embeddings de uma thread; `confirm_thread_id` deve ser igual a `thread_id`. Um turno que outro agente também
  registrou na thread mantém seu embedding enquanto esse agente tiver uma cópia ativa com o mesmo
  texto que a busca indexa (um turno de user ou assistant, ou um resumo), já que as
  duas cópias compartilham uma linha de embedding; a linha assume o agente, o papel, a execução e o
  usuário dessa cópia, de modo que a busca do próprio agente que excluiu a descarta (a compactação faz o mesmo). Uma linha
  de exatamente 240 caracteres (um corte de uma versão anterior, ou um turno desse tamanho) é mantida
  apenas para uma cópia cujos primeiros 2,048 caracteres coincidem com o turno do seu agente, e recebe
  tombstone quando não resta linha de mensagem para verificar isso. Uma
  exclusão também conclui linhas da thread cujo próprio agente não mantém mais uma mensagem ativa
  para o turno: uma que ele excluiu ou compactou (versões anteriores deixaram algumas), ou uma que o
  TTL de retenção expirou. Para os
  dados de uma pessoa ou de um namespace em todos os lugares, use
  [`erasure.request`](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/retention-and-erasure.md).

Turnos com tombstone são filtrados de toda leitura, exceto `erasure.export`, que os
retorna a menos que seja chamado com `include_tombstoned: false`. Eles só são removidos do disco se o
operador definiu um TTL de tabela (`LOOM_RETENTION_CONVERSATIONS_DAYS`, 0 = manter para sempre).
Esse TTL cobre apenas `loom.conversations`: a linha de embedding de um turno, que mantém até 2,048
caracteres do seu texto, não tem TTL. Depois que sua mensagem expirou, uma exclusão
de thread inteira em sua thread a passa a uma cópia ativa do turno ou põe tombstone nela; uma solicitação de
erasure põe tombstone nela e só a remove em um erasure `namespace_list` com `purge`.
`erasure.export` não retorna linhas de embedding, então o trecho de um turno que o TTL
expirou é apagado junto com o titular, mas não está na exportação dele.

<a id="thread-titles"></a>

### Títulos de thread

O worker `conversation_titler` dá às threads sem título (criadas por appends sem
`conversation.create`) um título curto escrito por LLM, com o qual a página Conversas do painel
abre. Ele cobre namespaces que gravam conversas, threads ativas nas
últimas `LOOM_CONVERSATION_TITLER_WINDOW_HOURS` (168) horas com pelo menos
`LOOM_CONVERSATION_TITLER_MIN_TURNS` (4) turnos, no máximo
`LOOM_CONVERSATION_TITLER_MAX_THREADS_PER_PASS` (10) por passagem. Vem ativado por padrão
(`LOOM_CONVERSATION_TITLER_ENABLED`) e não faz nada com o LLM fake.

<a id="capturing-sessions-from-clients"></a>

## Capturando sessões a partir de clientes

| Cliente             | Como os turnos chegam ao Loom                                                                                                                                                                                                                                                    |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Plugin do Claude Code | O hook Stop (`hooks/loom-conversation.mjs`) adiciona cada novo turno a partir da transcrição da sessão, com `msg_id` = o uuid da transcrição; `LOOM_CAPTURE_CONVERSATIONS=false` o desativa. Ele mantém em cache o veredito de gravação do namespace e ignora as adições enquanto a gravação está desativada. |
| Plugin do Codex       | A mesma captura, veja [Codex](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/codex.md).                                                                                                                                                                                                                 |
| Cursor             | `integrations/cursor/cursor-session-hook.sh`, executado pelo hook de fim de conversa do Cursor, entrega a transcrição para `loom ingest-transcript`.                                                                                                                                |
| Outros agentes       | `integrations/generic/ingest.sh` sobre a transcrição exportada.                                                                                                                                                                                                            |
| Qualquer cliente MCP     | A listagem `core` padrão não inclui `conversation.append`, e nada nela leva o modelo a registrar turnos: faça a captura com um hook de transcrição, ou monte `?toolset=all`, que lista `loom_conversation_append`.                                                     |
| Seu próprio código      | `conversation.append`, ou o `Session` do SDK Python, abaixo.                                                                                                                                                                                                            |

`loom ingest-transcript --namespace NS --file session.md` lê um arquivo Markdown com
seções `# user` / `# assistant` alternadas, ou um array JSON de `{role, content}`,
e adiciona cada turno (`--dry-run` os conta; `--thread-id` define um id estável).

Os plugins também entregam os turnos da sessão a `memory.set_from_messages(defer=true)` ao
fim da sessão, de modo que os fatos são extraídos independentemente de a transcrição estar gravada, e
registram as ações de ferramentas como observações episódicas por meio de `observe.record` /
`observe.crystallize` (veja [extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md)). A configuração
hook a hook está em [Claude Code](https://github.com/ClickHouse/loom/blob/main/docs/features/integrations/claude-code.md).

<a id="session-briefs"></a>

## Briefs de sessão

Um brief de sessão é o estado retomável de uma linha de trabalho: objetivo, status, decisões, becos sem saída, o que foi feito, questões em aberto, próximos passos, arquivos principais, restrições e ponteiros. Ele é lido após uma compactação, no dia seguinte, em outro cliente ou por um colega de equipe. Nunca é gerado como embedding nem retornado por `memory.search`. Qualquer agente que consiga ler o namespace pode retomá-lo.

| Operação               | Papel  | Faz                                                                                                                                  |
| ---------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `session.brief_save`   | writer | Revisa (ou inicia) um brief a partir de `events` (o que aconteceu desde o último salvamento), de `sections` escritas pelo agente, ou de ambos. |
| `session.brief_get`    | viewer | Carrega um brief por `brief_key`; senão o mais recente que uma sessão escreveu (`from_session`); senão o mais recente de um `project`. |
| `session.brief_list`   | viewer | Os briefs do namespace, do mais recente ao mais antigo, uma linha cada (`mine_only` para os do chamador).                            |
| `session.brief_delete` | writer | Exclui um brief quando quem o escreveu pede que seja esquecido.                                                                      |

**Endereçamento.** Um `brief_key` vazio vira `auto/<project>/<branch>`, de modo que toda sessão
na mesma linha de trabalho revisa o mesmo brief, ou `session/<session_id>` quando nenhum
dos dois é conhecido. As chaves seguem `^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$`. Um token limitado a um
usuário (ou sessão) armazena seus briefs como `~<user>~<key>` (ou `~<user>~<session>~<key>`) e
os vê como `<key>`; um token válido para todo o namespace os lista com esse prefixo e pode obtê-los ou
excluí-los por ele. Um token limitado não pode enviar uma chave com `~`.

**Revisão.** Um salvamento com `events` (tipos `user`, `assistant`, `edit`, `command`,
`error`, `read`, `search`, `tool`, `note`, `summary`; até 4,000 por chamada) é condensado
no servidor. Com `LOOM_SESSION_BRIEF_LLM` ativado (padrão), o modelo indicado por
`LOOM_SESSION_BRIEF_LLM_MODEL` revisa o brief uma vez por janela de
`LOOM_SESSION_BRIEF_WINDOW_CHARS` caracteres, no máximo `LOOM_SESSION_BRIEF_MAX_WINDOWS`
janelas por salvamento; sem a chave desse modelo, o LLM do deployment o escreve, e sem nenhum
LLM o construtor determinístico escreve objetivo, status, arquivos, erros e ponteiros (sem
decisões nem becos sem saída). O campo `author` na resposta indica `llm`, `deterministic` ou
`model` (o agente que chamou escreveu as seções). Sob uma
[política de extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md#operator-extraction-policy) do operador, o construtor nunca
é executado e nenhum resumo do cliente é extraído dos eventos. Uma janela cuja chamada ao modelo falhou
é ignorada enquanto as demais são revisadas. Um salvamento somente de eventos sem modelo para chamar (nenhum
configurado, `use_llm: false`, ou as chamadas de modelo da organização desativadas antes de a primeira
janela ser revisada) não grava nada e responde `changed: false`, `reason: "not-revised"`;
uma organização desativada depois de algumas janelas terem sido revisadas mantém essas janelas e não revisa
mais nenhuma. Um salvamento somente de eventos em que toda chamada ao modelo falhou, ficou retida por uma chave não lida
ou não pôde ser interpretada é recusado com 503 para que o cliente envie novamente. Um salvamento que também
traz as `sections` do agente as grava sobre o brief como estava, nos dois casos, e
não aproveita nada dos eventos.
As seções de estado são substituídas a cada revisão; decisões, becos sem saída e itens concluídos
se acumulam. Toda revisão é mantida em `loom.session_brief_revisions`; `revision=N` em
`brief_get` lê uma delas. Um salvamento que repete a sessão e o
`event_cursor` do salvamento anterior é uma nova tentativa e não grava nada; `if_revision` faz de um salvamento
um compare-and-set (409 em caso de divergência).

**A restauração é escolha da pessoa.** Os plugins de código salvam briefs na compactação e
no fim da sessão sem gastar nenhum token de modelo no cliente, e o primer de início de sessão apenas
cita os briefs salvos. O carregamento acontece quando a pessoa pede, pela
skill `loom-session` ou pela seção de briefs da página Conversations do painel.
`brief_get(full=true)` anexa, literalmente, o último resumo de compactação do próprio cliente abaixo
do brief. Os briefs nunca são carregados sozinhos: só a pessoa sabe qual linha de trabalho está retomando.

<a id="example"></a>

## Exemplo

A `Session` do SDK Python vincula estas ops a um escopo `(user, session)`: `append`
grava os turnos na transcrição e, a cada `flush_every` turnos, em
`memory.set_from_messages`; `resume` é `context.pack` para o escopo mais o final da
transcrição.

```python
from loom import Agent

async with Agent("dev", "local", "support-bot", base_url="http://localhost:7777") as bot:
    s = bot.session("thread-42", user_id="u1")
    resumed = await s.resume(query="resume the billing migration")
    await s.append(
        {"role": "user", "content": "We moved invoices to the new ledger."},
        {"role": "assistant", "content": "Noted; I'll update the export job."},
    )
    print(s.transcript_stored)  # False when the namespace does not record
    await s.close()  # flushes the buffered turns to extraction

    await bot.briefs.save(
        project="billing",
        branch="ledger-move",
        sections={
            "goal": "Move invoices to the new ledger",
            "next_steps": ["Update the export job"],
        },
    )
```

A extração em `close()` precisa de um provedor de LLM real para encontrar fatos; com o
provedor falso padrão, os turnos ainda são gravados.

<a id="settings"></a>

## Configurações

| Configuração                                                                | Efeito                                                                                                                                                                    |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CONVERSATION_SEARCH_MIN_SIMILARITY`                                   | O limite mínimo de cosseno de `conversation.search` ([Conhecimento → Conversas](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)).                                                     |
| `LOOM_CONVERSATION_SEARCH_EXCERPT_CHARS`                                    | Quanto de um turno um resultado de busca ou uma listagem de mensagens salvas exibe, em tempo real ([Conhecimento → Conversas](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)).                        |
| `LOOM_CONVERSATION_TITLER_*`                                                | A chave de ativação, a cadência, a janela e o limite por passagem do worker de títulos ([Conhecimento → Conversas](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#conversations)).                                  |
| `LOOM_RETENTION_CONVERSATIONS_DAYS`, `LOOM_TIERING_CONVERSATIONS_WARM_DAYS` | TTL opcional e movimentação para o volume warm das transcrições ([Retenção](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#retention), [armazenamento em camadas](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#storage-tiering)). |
| `LOOM_SESSION_BRIEF_LLM`, `…_LLM_MODEL`, `…_WINDOW_CHARS`, `…_MAX_WINDOWS`  | Como os resumos são condensados ([Contexto](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#context)).                                                                                              |
| `LOOM_MCP_SESSION_RUN_IDLE_SECONDS`                                         | Encerra a execução implícita de uma sessão MCP após esse tempo sem nenhuma chamada ([MCP](https://github.com/ClickHouse/loom/blob/main/docs/reference/configuration.md#mcp)).                                                           |

<a id="limits"></a>

## Limites

- **"Fiz o append, mas não há nada lá."** A gravação está desativada para o namespace; o append
  respondeu `stored: false`. Ative-a com `conversation.set_logging`.
- **As transcrições não são mineradas.** Os turnos gravados nunca se tornam memórias por conta própria.
- **`conversation.search` prioriza vetores.** Uma redação exata que o embedding não
  captura pode ficar mal classificada; `memory.recall` faz leituras lexicais e por janela de tempo sobre os
  turnos que a extração leu.
- **Os briefs têm tamanho limitado.** Um brief renderizado fica abaixo de cerca de 12,000 caracteres; cada
  seção de lista tem um limite (por exemplo, 20 decisões, 12 caminhos sem saída, 8 próximos passos), então itens antigos
  são descartados.
- **Um brief não é um lock.** Duas sessões no mesmo branch revisam o mesmo brief;
  passe `if_revision` quando isso não puder se intercalar.

<a id="related"></a>

## Relacionados

- [Extração](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/extraction.md) — como transformar uma transcrição em memórias.
- [Memória de trabalho e perfis](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/working-memory-and-profiles.md) — o bloco de rascunho "onde
  eu estava" que os plugins também escrevem.
- [Execuções](https://github.com/ClickHouse/loom/blob/main/docs/features/runs.md) — o registro de execuções ao qual as gravações e observações de uma sessão se juntam.
- [Busca unificada](https://github.com/ClickHouse/loom/blob/main/docs/features/unified-search.md) — a seção de conversas do `loom_search`.
- [Rastreamento e retomada](https://github.com/ClickHouse/loom/blob/main/docs/guides/trace-and-resume.md) — como retomar uma execução longa.
- [Arquitetura de memória](https://github.com/ClickHouse/loom/blob/main/docs/architecture/memory-architecture.md) — as tabelas de conversas e
  resumos vistas por dentro.
