<!-- i18n: source=docs/guides/teaching.md blob=5ff57fa6e6aa684faaf742b9ce4b6c698eda6682 model=claude-sonnet-5-5 -->
> Esta página é uma tradução automática da [página em inglês](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md), que prevalece.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md), which is authoritative.

<a id="teaching-with-loom"></a>

# Ensinando com o Loom

O Loom pode servir como ambiente de laboratório para um curso sobre agentes de IA, recuperação de informação, bancos de dados ou engenharia de software. Cada aluno executa sua própria stack de usuário único, em um laptop com Docker ou em um codespace do GitHub, sem nada instalado na máquina. Cada laboratório aponta para a página que faz o trabalho.

<a id="setup"></a>

## Configuração

| Os alunos têm                       | Comece por                                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------------------------- |
| Uma conta do GitHub e um navegador  | [Experimente no GitHub Codespaces](../getting-started/quickstart.md#try-it-in-github-codespaces) |
| Docker na própria máquina           | [Quickstart: caminho rápido](../getting-started/quickstart.md#fast-path-released-image-no-clone) |

Um codespace consome a cota de Codespaces do aluno enquanto está em execução e enquanto seu disco é mantido, e
o GitHub exclui, junto com seus dados, um codespace que fique sem uso durante o período de retenção
([cobrança](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces),
[exclusão automática](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces)).
O `hostRequirements` do dev container define a menor máquina em que ele pode rodar
([`.devcontainer/devcontainer.json`](https://github.com/ClickHouse/loom/blob/main/.devcontainer/devcontainer.json)).

A instalação padrão não precisa de chave de API: ela gera embeddings com um modelo local incluído, então a busca funciona
offline, e nenhum veredito de escrita chama um LLM. A extração, os resumos e as respostas geradas
(`memory.ask`, `knowledge.synthesize`) precisam de um LLM de verdade
([modelos e chaves de API](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)); os exercícios abaixo usam
apenas escritas, leituras e busca.

O Lab 1 e a rota do SDK no Lab 2 precisam do SDK Python em um ambiente virtual, em um
terminal no notebook ou no codespace. No Debian ou no Ubuntu, incluindo um codespace e o WSL,
execute `sudo apt-get update && sudo apt-get install -y python3-venv` antes dos
comandos abaixo. O SDK exige Python 3.11 ou mais recente; `python3 --version` mostra qual versão está
instalada. Um aluno sem Python 3.11 ou mais recente pode usar o codespace, onde este passo
instala um Python novo o bastante.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install clickhouse-loom
```

Os comandos `docker compose` dos labs rodam nesse mesmo terminal, atuam sobre os contêineres
da stack e indicam o diretório de instalação como `<dir>`: `~/.loom` em um notebook e
`/workspaces/.loom` em um codespace.

<a id="hand-out-the-labs-as-an-assignment"></a>

## Distribua os laboratórios como uma tarefa

O [starter do curso](https://github.com/ClickHouse/loom/blob/main/examples/course-starter/README.md) é um repositório para cada
aluno: o código dos Labs 1 e 2 e a consulta do Lab 4, com as lacunas marcadas como `TODO`, uma
página de respostas para os exercícios escritos, um dev container que instala a versão mais recente
em um codespace com o Python SDK e o Claude Code, e um workflow de correção. Copie-o para um
novo repositório na sua conta ou na organização da sua escola e marque esse
repositório como template (é necessário ter `curl`, `tar`, `git` e o
[GitHub CLI](https://cli.github.com/), com login feito):

```bash
mkdir loom-labs && cd loom-labs
curl -fsSL https://github.com/ClickHouse/loom/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=3 loom-main/examples/course-starter
git init -b main && git add . && git commit -m "Loom labs"
gh repo create <owner>/loom-labs --public --source . --push
gh repo edit <owner>/loom-labs --template
```

O push executa o workflow de correção uma vez, antes de o repositório ser um template, e essa execução
falha de propósito: a linha de cada verificação indica a parte do starter que ainda precisa ser escrita. A
cópia é o starter em `main`, testado contra `main`, enquanto cada execução instala a versão
mais recente, então uma falha por qualquer outro motivo significa que os dois divergem: copie o starter novamente
após a próxima versão. Os pushes seguintes no template não são corrigidos.

Cada aluno trabalha então em um repositório criado a partir desse template, com **Use this
template** na página dele ou com a ferramenta que distribui suas tarefas, e o abre em
um codespace ou o clona em um laptop. O aluno precisa de acesso de leitura ao template para usá-lo:
um template público não contém respostas, e um privado funciona para os membros da
organização ou para os colaboradores que você adicionar.

`python grading/grade.py` executa o código do laboratório contra a stack em execução do aluno e verifica
o que ele escreveu e retornou, não como chegou a isso, então leia o código também. O workflow `Grade`
executa as mesmas verificações a cada push, contra uma stack nova em um runner do GitHub Actions, e
escreve a tabela de verificações no resumo da execução; a execução falha enquanto qualquer verificação falhar. Cada
verificação falha no starter intocado e não precisa de chave de API. O Lab 3 e a página de respostas são
corrigidos manualmente: seus exercícios pedem explicações.

Uma execução de correção leva cerca de dois minutos, a maior parte para iniciar a stack. Em um repositório
privado, esses minutos contam contra a cota do Actions da conta ou organização
que é dona dele ([cobrança do Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions));
os codespaces custam o que [Configuração](#setup) informa.

<a id="lab-1-an-agent-that-remembers"></a>

## Laboratório 1: um agente que lembra

Crie um agente pequeno com o SDK Python: uma identidade, fatos que ele salva e lê de volta, uma sessão e uma execução. Siga [Seu primeiro agente](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md).

Exercícios:

- Busque com `memory.search` algo que nenhum fato salvo responda e explique o
  `abstain_code` que a resposta traz
  ([busca e pergunta](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)).
- Salve um fato e depois um valor próximo, mas diferente, sob outra chave, e leia o
  veredito da escrita ([como uma escrita é julgada](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#how-a-write-is-judged)).
- Revise um fato e leia suas versões com `memory.history`
  ([histórico e proveniência](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory)).

<a id="lab-2-document-search-with-citations"></a>

## Lab 2: busca em documentos com citações

Ingira alguns documentos com `loom demo`, que na stack padrão (LLM falso) imprime os
principais hits de `knowledge.search` para uma pergunta, em vez de uma resposta ([faça uma pergunta sobre um
documento](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)). O caminho de
`--file` fica dentro do contêiner, que contém esta documentação em `/app/docs/`. Antes, copie um
arquivo seu (aqui `notes.md`, do diretório atual), com um nome que o
identifique, já que `loom demo` nomeia o documento pelo caminho: um segundo arquivo copiado para
o mesmo caminho substitui o primeiro como sua próxima revisão.

```bash
docker compose --project-directory <dir> exec loom loom demo --file /app/docs/getting-started/quickstart.md --question "How do I get a token?"
docker compose --project-directory <dir> exec -T loom sh -c 'cat > /tmp/notes.md' < ./notes.md
docker compose --project-directory <dir> exec loom loom demo --file /tmp/notes.md --question "<a question the file answers>"
```

Em seguida, busque nos documentos do namespace com `knowledge.search` via HTTP (sem cabeçalho
`Authorization` na stack de usuário único) ou pelo SDK
([documentos de conhecimento](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)). O `knowledge.ingest_file` do SDK
publica um arquivo local sem copiá-lo para o contêiner; o `labs/lab2.py` do starter do curso
o utiliza.

Exercícios:

- Para dez perguntas, indique o documento e a passagem que responde a cada uma e meça
  o recall@k em `knowledge.search` ([avalie sua carga de trabalho](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md#memory-and-rag)).
- Adicione perguntas que os documentos não respondem. O `knowledge.search` ainda retorna hits
  para elas, então compare o maior `similarity` entre os hits (1 menos a distância de cosseno)
  em perguntas com e sem resposta e escolha um corte que as separe. O `score` de um hit
  é um ranking fundido no modo híbrido padrão e não as separa.

<a id="lab-3-a-code-index"></a>

## Lab 3: um índice de código

Indexe um repositório na stack
([indexar um repositório](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)).
Depois pergunte onde um símbolo é definido, quem o chama e o que uma mudança nele afeta ([o que
o índice de código responde](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#what-it-answers)) por HTTP,
com `POST /v1/code.<op>`. A stack de usuário único não preenche as chamadas `code.*`, então cada
corpo informa `"org": "dev"` e o `"namespace"` do repositório (`code-myrepo` na
entrada do quickstart).

Um agente de código pode fazer as mesmas perguntas depois de conectado
([conectar um agente de código](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)). Ele roda no modelo do seu próprio
provedor, então precisa de um login ou de uma chave de API lá; a stack não fornece nenhum dos dois.

Exercícios:

- Escolha cinco funções e compare `code.callers` com o que o `grep` encontra; explique cada
  diferença ([como ler uma resposta](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#reading-an-answer)).
- Em um clone do repositório indexado, altere uma função e envie o diff para
  `code.impact` como `diff`
  ([como um agente o usa](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#how-an-agent-uses-it)). O
  comando precisa do `jq` (`sudo apt-get install -y jq` no Debian ou Ubuntu):

  ```bash
  git diff | jq -Rs '{org: "dev", namespace: "code-myrepo", diff: .}' \
    | curl -s -X POST http://localhost:7777/v1/code.impact -H 'Content-Type: application/json' -d @-
  ```

<a id="lab-4-memory-as-a-database"></a>

## Lab 4: memória como banco de dados

O Loom guarda seus dados como linhas em tabelas do ClickHouse, e um usuário somente leitura do ClickHouse lê um namespace por meio de suas views ([superfície SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)). Crie um para o namespace da stack, `local` (o primeiro comando imprime a senha dele) e depois faça consultas com esse usuário:

```bash
docker compose --project-directory <dir> exec loom loom sql-access grant --user student1 --org dev --namespaces local
docker compose --project-directory <dir> exec clickhouse clickhouse-client --user student1 --password <password>
```

Exercícios:

- Escreva uma consulta sobre `loom.sql_v1_memories` que conte os fatos que cada agente criou
  na última hora (por `created_at`, que uma revisão preserva).
- Compare o resultado de um `memory.search` com uma consulta SQL sobre as mesmas linhas e explique o que
  o ranqueamento acrescenta.

<a id="which-labs-fit-which-course"></a>

## Quais laboratórios se encaixam em cada curso

| Curso                 | Laboratórios | Leia a seguir                                                                                                 |
| --------------------- | ------------ | ------------------------------------------------------------------------------------------------------------- |
| Agentes de IA         | 1, 2, 3      | [Integrar o Loom a um agente](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), [memória](../concepts/memory.md)                     |
| Recuperação de informação | 1, 2     | [Buscar e perguntar](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md), [avaliar o Loom](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)   |
| Bancos de dados       | 1, 4         | [Autoridade de armazenamento](../architecture/storage-authority.md), [Superfície SQL](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)     |
| Engenharia de software | 3           | [Índice de código](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md), [Histórico de PRs](../features/code-index/history-and-prs.md) |
