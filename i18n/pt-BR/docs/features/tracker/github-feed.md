<!-- i18n: source=docs/features/tracker/github-feed.md blob=81fe53135dda14b5a303c6f1f10b89c97e768376 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md), which is authoritative.

<a id="maintain-a-github-tracker-record"></a>

# Manter um registro de tracker do GitHub

`loom tracker-feed` copia issues, pull requests, comentários de issues e comentários
de revisão em linha de um repositório para um banco de dados ClickHouse dedicado da equipe. O Loom lê as views resultantes por meio de uma [fonte de tracker somente leitura](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md). A equipe é responsável pelo
conteúdo copiado, pelos backups e pela exclusão. O job não faz chamadas a modelos nem a embeddings.

<a id="prepare-the-record"></a>

## Preparar o registro

Use um banco de dados vazio com um UUID de banco de dados estável, separado do banco de dados da implantação do Loom. Dê a um gravador dedicado permissão para criar tabelas e views e para ler e
inserir linhas nesse banco de dados. Mantenha as credenciais dele separadas das do leitor do tracker.

```sql
CREATE DATABASE team_feed;
CREATE USER feed_writer IDENTIFIED BY '…';
GRANT CREATE TABLE, CREATE VIEW, SELECT, INSERT ON team_feed.* TO feed_writer;
GRANT SELECT ON system.columns TO feed_writer;
GRANT SELECT ON system.databases TO feed_writer;
GRANT SELECT ON system.tables TO feed_writer;
CREATE USER feed_reader IDENTIFIED BY '…' SETTINGS readonly = 2;
GRANT SELECT ON system.columns TO feed_reader;
```

Forneça a senha do gravador como `LOOM_TRACKER_FEED_WRITER_PASSWORD` por meio do recurso de segredos do agendador. Execute a CLI com a mesma configuração de implantação do Loom usada pelo processo de serviço, incluindo a conexão com o ClickHouse e a raiz de segredos.
Defina `LOOM_TRACKER_ORG` com a organização que está sendo inicializada. A CLI verifica as permissões diretas do gravador antes do uso e recusa funções (roles), grant options e autoridade fora desse banco de dados, exceto por essas três tabelas de metadados do sistema.

```bash
loom tracker-feed init --org acme --host record.example --database team_feed \
  --writer-user feed_writer
```

A inicialização cria `github_issues`, `github_prs` e `edges` como views com definer.
Conceda ao leitor acesso somente a essas views:

```sql
GRANT SELECT ON team_feed.github_issues TO feed_reader;
GRANT SELECT ON team_feed.github_prs TO feed_reader;
GRANT SELECT ON team_feed.edges TO feed_reader;
```

[Registre a fonte de tracker](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#adding-a-tracker-source) no modo views
usando `feed_reader`. Inclua cada repositório na lista de namespaces permitidos da fonte e
use namespaces restritos para registros privados. Defina `LOOM_TRACKER_ORG` da implantação
com a mesma organização. O registro da fonte mantém as verificações existentes de host, credencial e contrato. Endereços HTTP e privados exigem a configuração de host privado de desenvolvimento; conexões de produção usam HTTPS.

<a id="configure-and-schedule-a-repository"></a>

## Configurar e agendar um repositório

Crie um token de granularidade fina somente leitura ou um token de instalação de App restrito aos
repositórios que serão copiados. Ele precisa das permissões de leitura de Metadata, Issues e Pull requests.
`--read-only-token` é a atestação de permissões feita pelo operador; a CLI não consegue certificar a
ausência de permissões de escrita a partir de uma requisição bem-sucedida ou do prefixo do token.

Forneça esse token como `LOOM_TRACKER_FEED_GITHUB_TOKEN` por meio do recurso de segredos e, em seguida, configure um repositório da lista de permitidos:

```bash
loom tracker-feed configure --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer --read-only-token --max-pages 5
loom tracker-feed sync --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer
```

A configuração sela o token no banco de dados de feed da equipe. O sync precisa da senha do gravador e da raiz de segredos da implantação; ele não precisa da variável de ambiente do token.
As identidades dos repositórios copiados mantêm a grafia da lista de permitidos da fonte registrada,
de modo que os filtros de namespace correspondem mesmo que a capitalização da API seja diferente.
Tokens de instalação de App [expiram após uma hora](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).
Um processo de segredos externo deve renovar o token de instalação e executar `configure` novamente antes
da expiração. O feed não emite nem renova tokens de instalação.
Use `--writer-password-env` ou `--token-env` para selecionar outra variável de credencial.
Durante uma rotação da raiz de segredos, gravações de metadados bem-sucedidas resselam as credenciais abertas por
`LOOM_JWT_SECRET_PREVIOUS`. Conclua um ciclo bem-sucedido antes de aposentar a raiz anterior.

Agende o `sync` a cada cinco minutos ou com mais frequência. As observações de visibilidade expiram
após 15 minutos, ocultando o repositório se as verificações deixarem de ter sucesso. Cada job tem um limite de 120 segundos e lê no máximo `--max-pages` páginas por fluxo, com 100 itens por
página e um orçamento de 2 a 100 páginas. O limite de timestamp da última página é reprocessado;
o mínimo deixa uma página anterior a partir da qual progredir.
Um erro de capacidade deixa o cursor inalterado; aumente
o orçamento limitado de páginas ou reduza o backlog do repositório antes de tentar novamente. As contagens
na saída padrão informam as linhas processadas, incluindo as reprocessadas.

<a id="visibility-and-erasure"></a>

## Visibilidade e exclusão

Cada fluxo verifica a identidade imutável e a visibilidade atual do repositório.
Verificações com falha ocultam suas views. Cada item e comentário mantém a classificação de privacidade da observação junto com o conteúdo. As views usam a classificação mais restritiva
entre o repositório, o item e os comentários armazenados, inclusive quando essas tabelas chegam a uma
réplica em momentos diferentes. Trata-se de uma observação por polling com expiração limitada,
e não de um espelho instantâneo de permissões. As verificações normais de privacidade do tracker e de permissões diretas continuam valendo.

Quando um repositório se torna público, a configuração ou o sync redefine seus cursores e
os ciclos agendados reprocessam todos os fluxos dentro do mesmo orçamento de páginas. Um item se torna público
depois que sua linha e seus comentários armazenados são atualizados sob observações públicas.
Linhas excluídas no upstream exigem a exclusão das cópias retidas; o reprocessamento não consegue atualizá-las.

Os corpos incluem até 32,768 caracteres do item e os 64 comentários atualizados mais recentemente,
com até 2,048 caracteres por comentário. Solicitações de revisão e evidências de testes permanecem
desconhecidas quando a fonte não fornece essa evidência.
O `updated_at` das views acompanha a publicação de itens e comentários no registro, de modo que um comentário
importado depois do seu item permanece visível para leituras incrementais do tracker.

Listagens incrementais não informam exclusões. Para apagar o conteúdo copiado, pare todos os gravadores agendados, apague as linhas brutas, os comentários e a configuração ou os cursores do repositório afetados,
e exclua o repositório da lista antes de reiniciar. Remover um namespace do Loom apaga os dados derivados do Loom; isso não apaga o banco de dados da equipe nem os backups. A inicialização vincula um
registro a uma implantação e a uma organização. Use um registro separado para outra implantação.

Consulte a [arquitetura do feed](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md) para ver limites de cursor,
concorrência e propriedade.
