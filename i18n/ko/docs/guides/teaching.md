<!-- i18n: source=docs/guides/teaching.md blob=5ff57fa6e6aa684faaf742b9ce4b6c698eda6682 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/guides/teaching.md), which is authoritative.

<a id="teaching-with-loom"></a>

# Loom으로 가르치기

Loom은 AI 에이전트, 정보 검색, 데이터베이스 또는 소프트웨어 공학 강의의 실습 환경으로 사용할 수 있습니다. 각 학생은 Docker가 설치된 노트북이나, 로컬 컴퓨터에 아무것도 설치하지 않아도 되는 GitHub codespace에서 자신만의 단일 사용자 스택을 실행합니다. 각 실습에는 해당 작업을 다루는 페이지가 링크되어 있습니다.

<a id="setup"></a>

## 설정

| 학생의 환경                    | 시작할 문서                                                                                 |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| GitHub 계정과 브라우저         | [GitHub Codespaces에서 사용해 보기](../getting-started/quickstart.md#try-it-in-github-codespaces) |
| 본인 컴퓨터에 설치된 Docker    | [빠른 시작: 빠른 경로](../getting-started/quickstart.md#fast-path-released-image-no-clone) |

codespace는 실행되는 동안과 디스크가 보관되는 동안 학생의 Codespaces 할당량을 사용하며, 보관 기간 동안 사용하지 않고 두면 GitHub가 데이터와 함께 삭제합니다
([요금](https://docs.github.com/en/billing/concepts/product-billing/github-codespaces),
[자동 삭제](https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces)).
dev container의 `hostRequirements`는 실행에 필요한 최소 사양의 머신을 지정합니다
([`.devcontainer/devcontainer.json`](https://github.com/ClickHouse/loom/blob/main/.devcontainer/devcontainer.json)).

기본 설치에는 API 키가 필요하지 않습니다. 번들로 제공되는 로컬 모델로 임베딩하므로 검색이 오프라인에서도 동작하며, 쓰기 판정 중에는 LLM을 호출하지 않습니다. 추출, 요약, 생성된 답변(`memory.ask`, `knowledge.synthesize`)에는 실제 LLM이 필요합니다
([모델과 API 키](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/models-and-keys.md)). 아래 실습에서는 쓰기, 읽기, 검색만 사용합니다.

실습 1과 실습 2의 SDK 방식에는 가상 환경에 설치한 Python SDK가 필요하며, 노트북이나 codespace의 터미널에서 실행합니다. Debian 또는 Ubuntu(codespace와 WSL 포함)에서는 아래 명령을 실행하기 전에 `sudo apt-get update && sudo apt-get install -y python3-venv`를 실행하세요. SDK에는 Python 3.11 이상이 필요하며, 설치된 버전은 `python3 --version`으로 확인할 수 있습니다. Python 3.11 이상이 없는 학생은 codespace를 사용할 수 있으며, 이 단계에서 충분히 새로운 Python이 설치됩니다.

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install clickhouse-loom
```

실습의 `docker compose` 명령은 같은 터미널에서 실행하며, 스택의 컨테이너에 작용하고, 설치 디렉터리를 `<dir>`로 지칭합니다. 노트북에서는 `~/.loom`, codespace에서는 `/workspaces/.loom`입니다.

<a id="hand-out-the-labs-as-an-assignment"></a>

## 실습을 과제로 배포하기

[과정 스타터](https://github.com/ClickHouse/loom/blob/main/examples/course-starter/README.md)는 학생마다 하나씩 쓰는 저장소입니다. 실습 1과 2의 코드, 그리고 실습 4의 쿼리가 들어 있으며, 채워야 할 부분은 `TODO`로 표시되어 있습니다. 서술형 문제를 위한 답안 페이지, 최신 릴리스를 Python SDK 및 Claude Code와 함께 codespace에 설치하는 dev container, 채점 워크플로도 포함됩니다. 이를 본인 계정이나 학교 조직의 새 저장소에 복사한 다음, 그 저장소를 템플릿으로 지정하세요. `curl`, `tar`, `git`, 그리고 로그인된 [GitHub CLI](https://cli.github.com/)가 필요합니다.

```bash
mkdir loom-labs && cd loom-labs
curl -fsSL https://github.com/ClickHouse/loom/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=3 loom-main/examples/course-starter
git init -b main && git add . && git commit -m "Loom labs"
gh repo create <owner>/loom-labs --public --source . --push
gh repo edit <owner>/loom-labs --template
```

push하면 저장소가 템플릿이 되기 전에 채점 워크플로가 한 번 실행되며, 이 실행은 의도적으로 실패합니다. 각 검사 항목의 줄에 아직 작성해야 할 스타터 부분이 표시됩니다. 복사본은 `main`의 스타터이고 `main`을 기준으로 테스트되지만, 모든 실행은 최신 릴리스를 설치합니다. 따라서 다른 이유로 실패했다면 둘이 서로 다르다는 뜻입니다. 이 경우 다음 릴리스 이후에 스타터를 다시 복사하세요. 템플릿에 대한 이후의 push는 채점되지 않습니다.

그런 다음 학생은 각자 그 템플릿으로 만든 저장소에서 작업합니다. 저장소 페이지의 **Use this template**을 쓰거나 과제를 배포하는 도구를 사용하면 되고, 만든 저장소는 codespace에서 열거나 노트북에 clone합니다. 템플릿을 사용하려면 학생에게 템플릿에 대한 읽기 권한이 필요합니다. 공개 템플릿에는 정답이 들어 있지 않고, 비공개 템플릿은 해당 조직의 구성원이나 직접 추가한 협업자에게 사용할 수 있습니다.

`python grading/grade.py`는 학생이 실행 중인 스택을 대상으로 실습 코드를 실행하고, 코드가 어떤 과정을 거쳤는지가 아니라 무엇을 기록하고 무엇을 반환했는지를 검사합니다. 그러므로 코드도 직접 읽어 보세요. `Grade` 워크플로는 push할 때마다 같은 검사를 GitHub Actions 러너의 새 스택에서 실행하고, 검사 결과 표를 실행 요약에 기록합니다. 검사가 하나라도 실패하면 실행도 실패합니다. 각 검사는 수정하지 않은 스타터에서 실패하며 API 키가 필요하지 않습니다. 실습 3과 답안 페이지는 설명을 요구하는 문제이므로 직접 채점합니다.

채점 실행에는 약 2분이 걸리고, 대부분은 스택을 시작하는 데 쓰입니다. 비공개 저장소에서는 이 시간이 해당 저장소를 소유한 계정이나 조직의 Actions 할당량에서 차감됩니다([Actions 결제](https://docs.github.com/en/billing/concepts/product-billing/github-actions)). codespace 비용은 [설정](#setup)에 나온 대로입니다.

<a id="lab-1-an-agent-that-remembers"></a>

## Lab 1: 기억하는 에이전트

Python SDK로 작은 에이전트를 만듭니다. 에이전트는 신원(identity), 저장하고 다시 읽어 오는 사실, 세션, 실행(run)을 갖습니다. [첫 번째 에이전트](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/first-agent.md)를 따라 진행하세요.

실습:

- 저장된 어떤 사실로도 답할 수 없는 내용을 `memory.search`로 검색하고, 응답에 담긴 `abstain_code`가 무엇을 의미하는지 설명해 보세요
  ([검색과 질문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md)).
- 사실을 하나 저장한 뒤, 다른 키에 비슷하지만 다른 값을 저장하고 쓰기 판정 결과를 읽어 보세요
  ([쓰기가 판정되는 방식](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/conflicts-and-refusal.md#how-a-write-is-judged)).
- 사실을 수정하고 `memory.history`로 해당 사실의 버전을 읽어 보세요
  ([이력과 출처](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/history-and-provenance.md#versions-memoryhistory)).

<a id="lab-2-document-search-with-citations"></a>

## Lab 2: 인용이 포함된 문서 검색

`loom demo`로 문서 몇 개를 수집합니다. 기본 스택(가짜 LLM)에서 이 명령은 답변 대신 질문에 대한 상위 `knowledge.search` 결과를 출력합니다([문서에 대해 질문하기](../getting-started/quickstart.md#optional-ask-a-question-about-a-document)). `--file` 경로는 컨테이너 내부 경로이며, 컨테이너에는 이 문서들이 `/app/docs/` 아래에 들어 있습니다. 먼저 직접 만든 파일을 복사해 넣으세요(여기서는 현재 디렉터리의 `notes.md`). 파일을 식별할 수 있는 이름으로 복사해야 합니다. `loom demo`는 문서의 경로를 문서 이름으로 사용하므로, 같은 경로로 두 번째 파일을 복사하면 첫 번째 파일이 그 다음 리비전으로 대체됩니다.

```bash
docker compose --project-directory <dir> exec loom loom demo --file /app/docs/getting-started/quickstart.md --question "How do I get a token?"
docker compose --project-directory <dir> exec -T loom sh -c 'cat > /tmp/notes.md' < ./notes.md
docker compose --project-directory <dir> exec loom loom demo --file /tmp/notes.md --question "<a question the file answers>"
```

그런 다음 HTTP(단일 사용자 스택에서는 `Authorization` 헤더 없이) 또는 SDK로 `knowledge.search`를 사용해 네임스페이스의 문서를 검색합니다([knowledge 문서](https://github.com/ClickHouse/loom/blob/main/docs/features/knowledge.md)). SDK의 `knowledge.ingest_file`은 로컬 파일을 컨테이너에 복사하지 않고 게시합니다. 과정 스타터의 `labs/lab2.py`가 이 기능을 사용합니다.

실습:

- 질문 열 개에 대해 각각 답이 되는 문서와 구절에 레이블을 지정하고, `knowledge.search`의 recall@k를 측정합니다([워크로드 평가하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md#memory-and-rag)).
- 문서에 답이 없는 질문을 추가합니다. `knowledge.search`는 이런 질문에도 결과를 반환하므로, 답이 있는 질문과 없는 질문에서 결과 중 가장 높은 `similarity`(1에서 코사인 거리를 뺀 값)를 비교하고, 둘을 구분하는 기준값을 정합니다. 기본 하이브리드 모드에서 결과의 `score`는 융합된 순위 값이라 둘을 구분하지 못합니다.

<a id="lab-3-a-code-index"></a>

## Lab 3: 코드 인덱스

스택에서 리포지토리를 인덱싱합니다
([리포지토리 인덱싱하기](../getting-started/quickstart.md#optional-index-a-repository-on-the-released-stack)).
그런 다음 HTTP로 `POST /v1/code.<op>`를 호출해 심볼이 어디에 정의되어 있는지, 누가 호출하는지, 그 심볼을 변경하면 무엇이 영향을 받는지 질문합니다([코드 인덱스가 답하는 것](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#what-it-answers)). 단일 사용자 스택은 `code.*` 호출의 값을 자동으로 채워 주지 않으므로, 각 요청 본문에 `"org": "dev"`와 리포지토리의 `"namespace"`(퀵스타트 항목에서는 `code-myrepo`)를 명시해야 합니다.

코딩 에이전트도 연결하면 같은 질문을 할 수 있습니다
([코딩 에이전트 연결하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/connect-a-coding-agent.md)). 에이전트는 해당 제공자의 모델에서 실행되므로 그쪽의 로그인 또는 API 키가 필요하며, 스택은 이를 제공하지 않습니다.

실습:

- 함수 다섯 개를 골라 `code.callers`의 결과와 `grep`으로 찾은 결과를 비교하고, 각 차이를 설명해 보세요([답변 읽는 법](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#reading-an-answer)).
- 인덱싱한 리포지토리의 클론에서 함수를 변경한 뒤, diff를 `diff`로 `code.impact`에 보내 보세요
  ([에이전트가 사용하는 방법](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md#how-an-agent-uses-it)). 이 명령에는 `jq`가 필요합니다(Debian 또는 Ubuntu에서는 `sudo apt-get install -y jq`).

  ```bash
  git diff | jq -Rs '{org: "dev", namespace: "code-myrepo", diff: .}' \
    | curl -s -X POST http://localhost:7777/v1/code.impact -H 'Content-Type: application/json' -d @-
  ```

<a id="lab-4-memory-as-a-database"></a>

## 실습 4: 데이터베이스로서의 메모리

Loom은 데이터를 ClickHouse 테이블의 행으로 보관하며, 읽기 전용 ClickHouse 사용자는 뷰를 통해 네임스페이스를 읽습니다([SQL 인터페이스](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)). 스택의 네임스페이스인 `local`용 사용자를 만든 다음(첫 번째 명령이 비밀번호를 출력합니다), 그 사용자로 쿼리합니다.

```bash
docker compose --project-directory <dir> exec loom loom sql-access grant --user student1 --org dev --namespaces local
docker compose --project-directory <dir> exec clickhouse clickhouse-client --user student1 --password <password>
```

연습 문제:

- `loom.sql_v1_memories`에 대해 지난 한 시간 동안 각 에이전트가 만든 팩트의 수를 세는 쿼리를 작성하세요(리비전이 유지하는 `created_at` 기준).
- `memory.search` 결과를 같은 행에 대한 SQL 쿼리와 비교하고, 랭킹이 무엇을 더해 주는지 설명하세요.

<a id="which-labs-fit-which-course"></a>

## 어떤 실습이 어떤 과정에 맞는가

| 과정            | 실습    | 다음에 읽을 문서                                                                                          |
| --------------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| AI 에이전트             | 1, 2, 3 | [에이전트에 Loom 통합하기](https://github.com/ClickHouse/loom/blob/main/docs/guides/agent-integration.md), [메모리](https://github.com/ClickHouse/loom/blob/main/docs/concepts/memory.md)                     |
| 정보 검색 | 1, 2    | [검색과 질문](https://github.com/ClickHouse/loom/blob/main/docs/features/memory/search-and-ask.md), [Loom 평가하기](https://github.com/ClickHouse/loom/blob/main/docs/getting-started/evaluate.md)   |
| 데이터베이스             | 1, 4    | [스토리지 권한](https://github.com/ClickHouse/loom/blob/main/docs/architecture/storage-authority.md), [SQL 인터페이스](https://github.com/ClickHouse/loom/blob/main/docs/reference/sql-surface.md)     |
| 소프트웨어 공학  | 3       | [코드 인덱스](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/overview.md), [PR 이력](https://github.com/ClickHouse/loom/blob/main/docs/features/code-index/history-and-prs.md) |
