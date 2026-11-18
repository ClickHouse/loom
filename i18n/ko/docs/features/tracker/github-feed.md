<!-- i18n: source=docs/features/tracker/github-feed.md blob=81fe53135dda14b5a303c6f1f10b89c97e768376 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/github-feed.md), which is authoritative.

<a id="maintain-a-github-tracker-record"></a>

# GitHub 트래커 레코드 유지 관리

`loom tracker-feed`는 저장소의 이슈, 풀 리퀘스트, 이슈 댓글, 인라인 리뷰 댓글을 팀 전용 ClickHouse 데이터베이스로 복사합니다. Loom은 이렇게 만들어진 뷰를 [읽기 전용 트래커 소스](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md)를 통해 읽습니다. 복사된 콘텐츠, 백업, 삭제는 팀이 책임집니다. 이 작업은 모델이나 임베딩을 호출하지 않습니다.

<a id="prepare-the-record"></a>

## 레코드 준비

Loom 배포 데이터베이스와는 별도로, 안정적인 데이터베이스 UUID를 가진 빈 데이터베이스를 사용합니다. 전용 writer에게 해당 데이터베이스에서 테이블과 뷰를 만들고 행을 읽고 삽입할 권한을 부여합니다. writer의 자격 증명은 트래커 reader의 자격 증명과 분리해 두십시오.

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

writer 비밀번호는 스케줄러의 시크릿 기능을 통해 `LOOM_TRACKER_FEED_WRITER_PASSWORD`로 제공합니다. CLI는 서빙 프로세스와 동일한 Loom 배포 구성(ClickHouse 연결과 시크릿 루트 포함)으로 실행합니다.
`LOOM_TRACKER_ORG`는 초기화할 조직으로 설정합니다. CLI는 사용 전에 writer에 직접 부여된 권한을 확인하며, 이 세 개의 system 메타데이터 테이블을 제외하고 롤, grant option, 이 데이터베이스 밖의 권한이 있으면 거부합니다.

```bash
loom tracker-feed init --org acme --host record.example --database team_feed \
  --writer-user feed_writer
```

초기화하면 `github_issues`, `github_prs`, `edges`가 definer 뷰로 만들어집니다. reader에게는 이 뷰에 대한 접근 권한만 부여합니다.

```sql
GRANT SELECT ON team_feed.github_issues TO feed_reader;
GRANT SELECT ON team_feed.github_prs TO feed_reader;
GRANT SELECT ON team_feed.edges TO feed_reader;
```

`feed_reader`를 사용하여 views 모드로 [트래커 소스를 등록](https://github.com/ClickHouse/loom/blob/main/docs/features/tracker/record-contract.md#adding-a-tracker-source)합니다. 각 저장소를 소스의 네임스페이스 허용 목록에 포함하고, 비공개 레코드에는 제한된 네임스페이스를 사용합니다. 배포의 `LOOM_TRACKER_ORG`도 같은 조직으로 설정합니다. 소스 등록은 기존의 호스트, 자격 증명, 계약 검사를 그대로 유지합니다. HTTP와 사설 주소를 사용하려면 개발용 private-host 설정이 필요하며, 프로덕션 연결은 HTTPS를 사용합니다.

<a id="configure-and-schedule-a-repository"></a>

## 저장소 구성 및 스케줄링

복사할 저장소로만 제한된 읽기 전용 세분화된 토큰 또는 App 설치 토큰을 만듭니다. 이 토큰에는 Metadata, Issues, Pull requests 읽기 권한이 필요합니다. `--read-only-token`은 권한에 대한 운영자의 보증이며, CLI는 요청 성공 여부나 토큰 접두사만으로는 쓰기 권한이 없음을 증명할 수 없습니다.

이 토큰을 시크릿 기능을 통해 `LOOM_TRACKER_FEED_GITHUB_TOKEN`으로 제공한 다음, 허용 목록에 있는 저장소 하나를 구성합니다.

```bash
loom tracker-feed configure --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer --read-only-token --max-pages 5
loom tracker-feed sync --org acme --source widgets --repo acme/widgets \
  --writer-user feed_writer
```

구성 시 토큰은 팀의 피드 데이터베이스에 봉인되어 저장됩니다. sync에는 writer 비밀번호와 배포 시크릿 루트가 필요하며, 토큰 환경 변수는 필요하지 않습니다.
복사된 저장소 식별자는 등록된 소스의 허용 목록에 적힌 표기를 그대로 유지하므로, API의 대소문자가 다르더라도 네임스페이스 필터가 일치합니다.
App 설치 토큰은 [한 시간 후 만료](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)됩니다.
외부 시크릿 프로세스가 만료 전에 설치 토큰을 갱신하고 `configure`를 다시 실행해야 합니다. 피드는 설치 토큰을 발급하거나 갱신하지 않습니다.
다른 자격 증명 변수를 지정하려면 `--writer-password-env` 또는 `--token-env`를 사용합니다.
루트 시크릿을 교체하는 동안 메타데이터 쓰기가 성공하면 `LOOM_JWT_SECRET_PREVIOUS`로 열린 자격 증명이 다시 봉인됩니다. 이전 루트를 폐기하기 전에 턴을 한 번 성공적으로 완료하십시오.

`sync`는 5분마다 또는 더 자주 실행하도록 스케줄링합니다. 가시성 관찰 결과는 15분 후 만료되므로, 검사가 성공하지 못하면 저장소가 숨겨집니다. 각 작업의 제한 시간은 120초이며, 스트림마다 최대 `--max-pages` 페이지를 읽습니다. 페이지당 항목은 100개이고 페이지 예산은 2~100입니다. 마지막 페이지의 타임스탬프 경계는 다시 처리되며, 최소값은 진행의 기준이 될 이전 페이지를 남겨 둡니다.
용량 오류가 발생해도 커서는 바뀌지 않습니다. 재시도하기 전에 제한된 페이지 예산을 늘리거나 저장소의 백로그를 줄이십시오. 표준 출력의 개수는 다시 처리된 행을 포함하여 처리된 행 수를 보고합니다.

<a id="visibility-and-erasure"></a>

## 가시성과 삭제

모든 스트림은 저장소의 불변 식별자와 현재 가시성을 확인합니다. 검사에 실패하면 해당 뷰가 숨겨집니다. 각 항목과 댓글은 관찰 시점의 개인정보 분류를 콘텐츠와 함께 보관합니다. 뷰는 저장소, 항목, 저장된 댓글 중 가장 제한적인 분류를 사용하며, 이는 해당 테이블들이 서로 다른 시점에 레플리카에 도달하는 경우에도 마찬가지입니다. 이는 만료 기한이 있는 폴링 관찰이며, 권한을 즉시 반영하는 미러가 아닙니다. 일반적인 트래커 개인정보 보호 검사와 직접 권한 검사는 계속 적용됩니다.

저장소가 공개로 전환되면 구성 또는 sync가 커서를 초기화하고, 예약된 턴이 같은 페이지 예산 안에서 모든 스트림을 다시 처리합니다. 항목은 해당 행과 저장된 댓글이 공개 관찰 상태에서 갱신된 후에 공개됩니다. 업스트림에서 삭제된 행은 보관된 사본을 삭제해야 하며, 재처리로는 갱신할 수 없습니다.

본문은 항목당 최대 32,768자와 가장 최근에 갱신된 댓글 64개를 포함하며, 댓글당 최대 2,048자입니다. 소스가 리뷰 요청이나 테스트 증거를 제공하지 않으면 해당 정보는 알 수 없음으로 남습니다.
뷰의 `updated_at`은 레코드에서 항목과 댓글이 게시된 시점을 추적하므로, 항목보다 나중에 가져온 댓글도 증분 트래커 읽기에서 계속 보입니다.

증분 목록은 삭제를 보고하지 않습니다. 복사된 콘텐츠를 삭제하려면 예약된 모든 writer를 중지하고, 영향받는 원본 행, 댓글, 저장소 구성 또는 커서를 삭제한 다음, 다시 시작하기 전에 해당 저장소를 제외하십시오. Loom 네임스페이스를 삭제하면 Loom의 파생 데이터는 제거되지만 팀의 데이터베이스나 백업은 삭제되지 않습니다. 초기화는 레코드를 하나의 배포와 조직에 바인딩합니다. 다른 배포에는 별도의 레코드를 사용하십시오.

커서 경계, 동시성, 소유권에 대해서는 [피드 아키텍처](https://github.com/ClickHouse/loom/blob/main/docs/architecture/github-feed.md)를 참고하십시오.
