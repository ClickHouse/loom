<!-- i18n: source=docs/development/dashboard-design.md blob=c782f871de17e68b618eca3cedf06efa52cfd5f3 model=claude-sonnet-5-5 -->
> 이 페이지는 [영어 원문](https://github.com/ClickHouse/loom/blob/main/docs/development/dashboard-design.md)을 기계 번역한 것이며, 영어 원문이 기준입니다.
> Machine-translated from the [English page](https://github.com/ClickHouse/loom/blob/main/docs/development/dashboard-design.md), which is authoritative.

<a id="dashboard-design"></a>

# 대시보드 디자인

`/`의 대시보드는 ClickHouse의 디자인 시스템인 [클릭 UI](https://github.com/ClickHouse/click-ui)를 다크 테마로 적용하여 만들어졌습니다. 이 페이지에서는 외관과 동작이 어디에서 비롯되는지, 이를 어떻게 변경하는지, 그리고 대시보드 변경 사항을 검토할 때 적용하는 규칙을 설명합니다.

<a id="click-ui-is-the-component-library"></a>

## Click UI는 컴포넌트 라이브러리입니다

`@clickhouse/click-ui`(`frontend/package.json`에 정확한 버전으로 고정)는 런타임 의존성입니다. `src/main.tsx`는 `ClickUIProvider`를 마운트하는 대신 다크 토큰과 페이지 기본값을 직접 로드합니다(`vite.config.ts`의 `click-ui-styles` 별칭). 이 프로바이더는 배럴 import 때문에 모든 컴포넌트의 CSS와 두 테마를 함께 내보내기 때문입니다. Click UI 컴포넌트가 기대하는 Radix 툴팁 프로바이더는 같은 위치에서 직접 마운트하며, `index.html`은 `data-cui-theme="dark"`를 설정합니다. 컨트롤은 서브패스(`@clickhouse/click-ui/Button`)로 import한 Click UI 컴포넌트입니다. 따라서 대시보드가 import하는 컴포넌트만 번들에 포함되고, 지연 로딩 뷰에서만 쓰는 컴포넌트는 해당 뷰와 함께 로드됩니다.

| 필요한 것                              | 사용할 것                                                                                 |
| -------------------------------------- | ----------------------------------------------------------------------------------------- |
| 버튼                                   | `Button` (`type`: primary, secondary, empty, danger. `htmlType`은 항상 전달)              |
| 입력 필드                              | `TextField`, `SearchField`, `PasswordField`, `NumberField`, `TextAreaField`, `Select`     |
| 체크 또는 스위치                       | `Checkbox`, `Switch` (레이블은 일반 문자열)                                               |
| 탭, 메뉴, 팝오버                       | `Tabs` (`Tabs.TriggersList`에 이름 지정), `Dropdown`, `Popover` (아래 Loom의 트리거와 함께) |
| 테이블, 배지, 알림, 페이지네이션       | `Table`, `Badge`, `Alert`, `Pagination`, `ProgressBar`                                    |
| 텍스트 링크 또는 링크 모양의 동작      | `Link` (동작에는 `component="button"`, 새 탭에는 `ui/ExternalLink`)                       |
| 아이콘                                 | Click UI 이름을 쓴 `Icon`. 유일한 콘텐츠가 아니면 `aria-hidden`                           |

`src/components`의 일부 Loom 프리미티브는 Click UI 0.13이 부족한 부분을 보완하기 위해 Click UI를 감쌉니다. 아래에 있는 Click UI 컴포넌트가 아니라 이 프리미티브를 사용하세요.

| 프리미티브                                       | 감싸는 대상              | 이유                                                                                                                                                                                                                                               |
| ------------------------------------------------ | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ui/Modal`, `ModalFooter`                        | `Dialog`                 | Click UI의 닫기 버튼은 아이콘 이름("cross")으로 불리고, 오버레이에 z-index가 없으며, 포커스는 `Dialog.Trigger`에서만 연 요소로 돌아갑니다                                                                                                         |
| `IconButton`                                     | `IconButton` + `Tooltip` | 아이콘만 있는 컨트롤에는 이름과 툴팁이 필요하고, Radix 트리거의 `type="button"`이 Click UI의 variant prop에 전달됩니다                                                                                                                            |
| `ui/Menu`: `MenuTrigger`, `PopoverTrigger`       | Radix 트리거             | Click UI의 `Dropdown.Trigger`와 `Popover.Trigger`는 `aria-expanded`를 감싸는 `div`에 두고 포커스도 그 요소로 돌려줍니다                                                                                                                            |
| `ui/AnchoredPopover`                             | `Popover`                | 일반 클릭에는 팝오버를 열고 수정 키를 누른 클릭에는 이동하는 링크입니다. Radix의 트리거는 자식이 막지 않은 클릭에서만 토글됩니다                                                                                                                   |
| `ui/Unavailable`                                 | —                        | 밝힌 이유로 사용할 수 없는 컨트롤입니다: `aria-disabled`, 툴팁, 설명                                                                                                                                                                               |
| `ui/PageHeader`                                  | —                        | 모든 페이지의 제목 행입니다: 아이콘 없는 20 px h1(Click UI의 페이지 제목과 동일), 그 옆의 범위 배지, 끝에 놓이는 동작                                                                                                                             |
| `ui/ChoiceGroup`                                 | `RadioGroup`             | Click UI의 그룹 레이블은 그룹에 이름을 주지 않고, 텍스트가 아닌 항목 레이블은 "[object Object]"로 읽힙니다                                                                                                                                        |
| `ui/ExternalLink`                                | `Link`                   | Click UI의 `icon` prop은 아이콘 이름("popout")을 링크 텍스트 뒤에 읽습니다. 이 프리미티브는 아이콘을 숨기고 "(opens in a new tab)"을 읽어 줍니다                                                                                                  |
| `ui/Tooltip` (`Tip`)                             | `Tooltip`                | `main.tsx`가 마운트하는 프로바이더 위에서 하나의 툴팁 형태를 씁니다(스펙은 `src/test/rtl.tsx`에서 받습니다). Click UI의 `disabled`는 Radix를 제어/비제어 사이에서 전환시키고, 꺼진 툴팁도 열린 것으로 취급되어 다음 툴팁이 지연을 건너뜁니다 |
| `ui/TabTrigger`                                  | `Tabs.Trigger`           | Click UI의 Icon은 블록이라 래퍼가 없으면 아이콘이 레이블 위에 쌓입니다                                                                                                                                                                            |
| `Toast` (`toast()`)                              | —                        | Click UI의 Toast에는 키 기반 교체나 요청 ID 줄이 없고 닫기 버튼이 "cross"로 불립니다. Loom의 카드는 Click UI의 토스트 토큰을 사용하며 오른쪽 아래에 표시됩니다                                                                                    |
| `ConfirmDialog` (`confirmAction()`)              | `Modal` + `Button`       | Click UI의 ConfirmationDialog는 확인 버튼에 포커스를 두지만, Loom은 취소에 포커스를 둡니다                                                                                                                                                        |
| `ui/fieldError` (`{...fieldError(id, message)}`) | 필드의 `error`           | Click UI는 오류를 단순 `<div>`로 그리고 `aria-invalid`와 `aria-describedby`를 설정하지 않습니다. 이 프리미티브가 메시지를 입력에 연결합니다                                                                                                         |
| `ui/Alert`                                       | `Alert`                  | Click UI의 Alert는 루트에 `role`을 두지만 props 타입에는 선언이 없습니다. 라이브 알림은 래퍼 없이 `role="alert"` 또는 `role="status"`를 받습니다                                                                                                  |
| `ui/DraftSearch`                                 | `SearchField`            | 서버 읽기 위의 검색 상자로, 입력 중인 내용은 자체적으로 유지하고 확정된 텍스트(잠시 멈춘 뒤 또는 Enter)만 페이지에 넘기므로 키 입력은 페이지가 아닌 상자만 렌더링합니다. `key`가 바뀌면 비워집니다                                                  |
| `ui/Stat`                                        | `BigStat`                | 나란히 놓인 수치 중 하나입니다: 값 아래에 힌트가 있는 작은 BigStat이며, BigStat에는 이를 위한 슬롯이 없습니다                                                                                                                                     |

Loom은 Click UI 토큰을 사용하는 자체 요소도 몇 가지 유지합니다.

- `Chip`(Click UI에는 필터 칩이 없음), `EmptyState`, `Skeleton`(Click UI에 없음).
- `Panel`: 헤더 슬롯과 스크롤되는 본문을 갖춘 Click UI의 카드 모양입니다. Click UI의 `Panel` 컨테이너에는 이것이 없습니다.
- 메모리 인스펙터 드로어: Radix 다이얼로그 시트입니다. Click UI의 `Flyout`은 좁은 화면에서 호버 스트립으로 접히고 ref를 받지 않기 때문입니다.
- 펼침 토글: `aria-expanded`와 Click UI 쉐브론을 갖춘 네이티브 버튼 또는 Click UI `empty` `Button`입니다. Click UI의 `Collapsible`은 `aria-expanded`를 설정하지 않기 때문입니다.
- 아바타 또는 네임스페이스 이름을 보여 주는 트리거: 토큰을 사용하는 네이티브 버튼입니다. Click UI의 `empty` 버튼은 링크 텍스트입니다.
- 날짜 및 날짜-시간 필드(그래프의 "Valid as of", 작업의 마감 시각): Click UI의 필드 토큰으로 그린 네이티브 `date`, `datetime-local` 입력입니다. Click UI의 `DatePicker`는 레이블용 `name`이나 `id`를 받지 않고 시간도 없기 때문입니다.
- 트래커 소스의 저장소 목록: 각 항목을 제거 가능한 태그로 바꾸는, 필드 스타일 상자 안의 네이티브 입력입니다. Click UI에는 태그 입력이 없기 때문입니다.
- 파일 선택기: Click UI `Button`으로 여는 숨겨진 네이티브 `file` 입력입니다. 파일을 끌어다 놓을 수도 있는 곳(문서 다이얼로그)에서는 버튼이 `ui/DropZone` 안에 놓입니다. 이는 파일이 위에 있을 때 색조가 바뀌는 점선 드롭 대상이며(Click UI에는 없음), 거친 포인터에서는 힌트가 숨겨집니다.
- 알려진 ID를 제안하면서도 다른 ID를 받는 ID 필드(Assistant의 user 및 agent 필드): 네이티브 `datalist`를 가진 Click UI `TextField`입니다. Click UI의 `AutoComplete`는 목록에서만 고르고 자유 텍스트를 유지하지 않기 때문입니다.
- xl 미만의 Ask AI 버튼: 고스트 `IconButton`의 토큰으로 그린 Click UI의 프레임 없는 `Button`입니다(`styles.css`의 `.assistant-launcher`). `IconButton`을 쓰지 않는 이유는, 모든 너비에서 하나의 요소를 쓰면 크기를 조절해도 포커스가 유지되고, xl부터는 같은 버튼이 단어가 있는 secondary `Button`이 되는데 `IconButton`에는 이를 위한 슬롯이 없기 때문입니다. 패널이 열려 있는 동안에는 해당 유형의 `:active` 토큰(xl부터는 secondary `Button`, 그 미만은 고스트 `IconButton`)에서 눌린 모양을 가져옵니다. Click UI의 `Button`에는 선택 상태가 없기 때문입니다.
- "Go to Ask AI": 앞에 오는 건너뛰기 링크와 같은 스타일의 네이티브 버튼입니다. 앵커로 이동하지 않고 패널을 열기 때문입니다.
- History가 표시되는 동안 Ask AI 패널의 History 버튼: `aria-pressed`를 가진 Click UI의 고스트 `IconButton`이며 해당 버튼의 `:active` 토큰으로 그립니다(`styles.css`의 `.assistant-head`). Click UI의 `IconButton`에는 선택 상태가 없기 때문입니다.
- 검색의 Ask AI 옵션의 "Ask": primary `Button`의 토큰으로 그린 알약 모양입니다(`.search-ask`). 리스트박스 옵션 안에는 버튼을 둘 수 없기 때문입니다. 답변 카드의 **Continue in chat**은 옵션 역할을 부여한 Click UI `Button`이어서, 그 아래 결과에 닿는 것처럼 Enter와 화살표 키가 필드에서 이 버튼에도 닿습니다. 키로 이동한 옵션은 포커스 링을 받습니다(`.search-continue`).
- 사이드바 내비게이션 항목: Click UI의 `--click-sidebar-*` 토큰으로 그린 앵커입니다. Click UI의 `SidebarNavigationItem`은 레이블에 링크가 들어갈 수 있는 `div`여서, 선택 상태가 링크가 아닌 `div`에 놓이게 됩니다.
- Library의 위치 트리: Click UI의 사이드바 내비게이션 및 테이블 행 토큰으로 그린 행입니다. Click UI의 사이드바 내비게이션은 중첩을 지원하지 않기 때문입니다.
- Loom 고유 위젯(그래프 캔버스, 작업 그리드와 보드, 패턴 보드)과 그 안의 Click UI 컨트롤.
- 작업의 리스 바: 남은 시간이 10분 미만이면 경고 색상으로 바뀌는 56 × 4 px 막대입니다. Click UI의 작은 `ProgressBar`는 색이 하나이고 레이블 행이 있는데, 곧 만료될 리스는 경고이기 때문입니다.

<a id="component-or-design-values"></a>

### 컴포넌트냐 디자인 값이냐

요소가 Click UI 컴포넌트와 같을 때, 즉 구조, 역할, 동작(키, 포커스, ARIA, 포털, 위치 지정)이 같을 때는 항상 그 Click UI 컴포넌트를 import하세요. Click UI 0.13이 동작을 잘못 처리하는 곳은 다시 그리지 말고 `components/ui/`에서 한 번 감싸고, 래퍼의 주석에 업스트림 이슈를 명시하세요(`ui/SplitButton`: [ClickHouse/click-ui 이슈 #1272](https://github.com/ClickHouse/click-ui/issues/1272)).

Click UI 컴포넌트가 아닌 경우(구조가 다르거나 Click UI에 맞는 것이 없는 경우: 목록 행, 칩, 스켈레톤)에만 Loom CSS로 요소를 그리세요. 이때 모든 색상, 패딩, 간격, 반경, 글꼴은 그 요소가 대신하는 컴포넌트의 `--click-*` 토큰에서 가져오고, 그 컴포넌트에 토큰이 없는 간격이나 여백은 Click UI의 일반 `--spaces-*` 스케일에서 가져와서 `styles.css`의 components 레이어에 이름 있는 클래스 하나로 작성합니다. 직접 입력한 px도, Tailwind 임의 값(`px-[var(--click-…)]`)도 안 됩니다. 직접 정한 크기는 측정한 값이어야 하며 클래스의 주석에 명시합니다. 클래스의 주석에는 그 클래스가 대신하는 컴포넌트와 그 컴포넌트가 맞지 않는 이유를 적습니다. Click UI 컴포넌트의 마크업(`[data-testid=…]`, 자식 요소)에 손을 대는 CSS는 해당 컴포넌트의 래퍼에 속하며 Click UI 릴리스마다 다시 확인합니다.

이유: import한 컴포넌트는 키보드와 포커스 동작을 함께 제공하는데, 다시 그린 요소는 이를 직접 구현해야 하고 잘못 구현하기 쉽습니다. 토큰으로 그린 요소는 CSS 비용만 들므로, 컴포넌트가 쓸모없이 스크립트를 싣게 되는 정적 셸(행 프레임, 칩)에 적합합니다. 그리고 토큰은 Click UI의 안정적인 계약인 반면 마크업은 그렇지 않습니다.

<a id="styles"></a>

## 스타일

- **레이어 순서** (`index.html`에 인라인으로, 스타일시트보다 앞에): `properties, theme, base, clickui, components, utilities`. Click UI는 Tailwind의 리셋 위, 유틸리티 아래에 놓입니다. 따라서 Click UI 컴포넌트에는 레이아웃 유틸리티(마진, 너비, flex 배치)만 적용하세요. 색상이나 패딩 유틸리티는 Click UI를 덮어씁니다.
- **토큰**은 `src/styles.css`에 있습니다. Loom의 시맨틱 이름(`--color-bg`, `--color-surface`, `--color-text-muted`, `--color-accent`, `--color-success`, 타입 스케일, 반경, 그림자)은 Loom 자체 요소를 위해 Click UI의 변수로부터 정의됩니다. Click UI 컴포넌트는 Click UI의 변수를 직접 읽습니다.
- **색상**은 Click UI 변수이거나 그로부터 파생한 값(`color-mix` 틴트, `bg-x/15`)이며, `styles.css`는 Click UI의 변수를 하나도 다시 정의하지 않습니다. 포커스 링은 모든 포커스 가능한 요소에서 Click UI의 아웃라인 색상, 2 px 두께, 2 px 오프셋입니다. Click UI는 타입이 있는 버튼의 링을 해당 버튼 자체의 테두리 색으로 그리며(보조 버튼은 1.6:1로 3:1 미만), 체크박스, 스위치, 탭, 링크, 대화상자 닫기 버튼에는 링을 그리지 않습니다. 링 재정의는 모든 CSS 레이어 밖, `styles.css`의 `*:focus-visible` 옆에 둬야 합니다. 그렇지 않으면 레이어가 없는 링이 우선합니다. 클리핑 상자를 가로지르는 행(탭, Explorer 아웃라인 항목, 작업 레인의 헤더와 행, 네임스페이스 선택기의 옵션, 그룹 버튼과 그룹의 "Show all", 창을 가득 채우는 Library의 텍스트 미리보기)이나 빈틈없이 쌓인 행(인박스)은 링을 안쪽에 그립니다. 다른 컨트롤을 담은 클리핑 상자(Explorer 패싯, 툴바의 스크롤되는 필터, 작업 레인의 "Show all", 선택기의 철회 버튼과 "Find public namespaces to join" 버튼)는 그 컨트롤에 4 px의 여유를 둡니다. `frontend/e2e/focus-rings.spec.ts`는 모든 페이지를 Tab 키로 순회하며, 상자에 잘리는 링이 있으면 실패합니다.
- **탭 행**: `styles.css`는 모든 탭 레이블을 한 줄로 유지하고, 페이지보다 넓은 탭 행은 자체 상자 안에서 가로로 스크롤되게 하며, 탭의 포커스 링은 그 안쪽에 그려집니다.
- **이유와 함께 비활성화**: Click UI는 기본 방식으로 비활성화된 버튼만 스타일링합니다. `ui/Unavailable`은 버튼에 `data-unavailable`(버튼의 타입과 함께)을 표시하고, `styles.css`는 그 버튼에 Click UI의 비활성화 색상을 적용합니다.
- **위에 쌓이는 레이어**: 페이지 위의 어시스턴트 패널은 z-[15], 확장된 창도 같습니다(스크림은 z-[14]이며, `<main>`이 페이지 자체의 z-index를 격리합니다). 그 위에 메모리 인스펙터의 오버레이는 z-20, 대화상자는 z-50(확인 창은 z-[60]), 토스트는 z-[65], 모든 Radix 포퍼(메뉴, 셀렉트, 팝오버, 툴팁)는 z-[70]입니다.
- **타입**: Click UI의 크기, 10~32 px입니다. 캡션은 12 px(`text-xs`), 작은 배지는 10 px, 작은 아바타는 11 px이고, 페이지 제목은 20 px입니다. Inter와 Inconsolata(latin 및 latin-ext)는 `styles.css`에서 Click UI의 글꼴 패밀리 이름으로 선언됩니다.

<a id="what-ships-of-click-ui"></a>

## Click UI에서 포함되는 것

- `vite.config.ts`는 Click UI의 국기, 로고, 결제 마크 맵을 빈 모듈(`src/lib/emptyAssetMap.ts`)로 alias합니다. Click UI의 `Icon`은 어떤 이름이든 확인할 수 있도록 이 맵들을 통째로 import하지만, 대시보드는 인터페이스 아이콘만 그리기 때문입니다. 이렇게 하면 Click UI가 내보내는 스크립트는 ~325 kB gz 대신 ~95 kB gz가 됩니다.
- `vite.config.ts`는 셸이 정적으로 import하는 Click UI 모듈을 모든 페이지가 로드하는 하나의 `clickui` 청크(~64 kB gz)에 넣습니다. 나머지(Select와 그 정렬 가능한 목록, Table, Pagination)는 Rollup이 이를 사용하는 지연 로딩 뷰로 나누고, 1 kB 미만의 공유 청크(지연 로딩 뷰에서만 쓰는 `fieldError` 같은 프리미티브)는 함께 로드되는 청크에 병합합니다. 부트 스크립트가 198 kB gz를 넘으면 `npm run budget`이 실패합니다.
- Click UI의 `Badge`는 렌더링할 때마다 텍스트의 크기를 측정합니다. 이를 길게 나열한 목록은 행을 메모이제이션하고 `MemoryCard`처럼 안정적인 props를 전달해야 합니다. 그렇지 않으면 변경 하나가 모든 행의 크기를 다시 측정하게 됩니다. 렌더링 중에 만든 콜백, set, 엘리먼트는 매번 새로운 prop이 되므로 목록은 이를 `useCallback` 또는 `useMemo`로 만듭니다. 목록의 검색 텍스트를 어디에 두는지도 여기에서 정해집니다. 메모이제이션된 행에서는 페이지가 텍스트를 보관하므로 행이 이를 쓸 수 있고(태그 선택), 그 텍스트의 `useDeferredValue`로 로드된 행을 필터링하므로 키 입력 시 행이 다시 렌더링되기 전에 입력란이 먼저 그려집니다(Library, Profile, Audit log, Namespaces in your org, Repositories, Tasks. Memories는 대신 서버로 보내는 검색을 디바운스합니다). 메모이제이션되지 않은 행에서 확정된 텍스트가 서버 읽기를 시작하는 경우에는 입력란을 `ui/DraftSearch`로 만들고, 키 입력 시 입력란만 렌더링합니다(Runs는 잠시 멈춘 뒤 읽고, Conversations는 Enter에서 읽습니다).
- `src/lib/clickUiFixes.ts`는 Click UI 0.13의 Select가 옵션 목록에서 이름을 붙이지 않은 요소, 즉 필터 필드, 지우기 버튼, 이를 담는 다이얼로그에 이름을 붙입니다. 또한 Button의 `loading` 상태에서도 키보드 포커스를 유지합니다. Click UI는 이 상태를 `disabled`로 구현하므로 포커스가 페이지로 떨어지는데, 눌린 버튼이 다시 활성화되면 포커스를 되찾고 버튼이 연 다이얼로그는 닫힐 때 포커스를 버튼으로 돌려줍니다(`ui/Modal`). 자체 읽기가 실행되는 동안 페이지에 남아 있는 버튼(Refresh, Load more, Search)은 대신 `ui/BusyButton`입니다. 이 버튼은 내내 포커스를 유지하며, 마지막 페이지가 Load more를 제거하면 `lib/useLastPageFocus`가 그 페이지에서 추가된 첫 번째 행에 포커스를 넘기고, 없으면 표시된 마지막 행, 그것도 없으면 목록의 검색란에 넘깁니다.

<a id="follow-a-click-ui-release"></a>

## Click UI 릴리스 따라가기

`npm --prefix frontend i --save-exact @clickhouse/click-ui@<version>`을 실행한 다음 아래의 프런트엔드 검사를 실행합니다. 프리미티브 표와 `clickUiFixes.ts`에 있는 각 우회 코드를 릴리스 노트와 대조해 다시 확인하고, Click UI가 해당 문제를 수정했다면 그 우회 코드를 삭제합니다. Click UI에 새 아이콘, 플래그 또는 로고 맵이 추가되면 alias에 추가해야 할 수 있습니다.

<a id="rules-a-change-is-checked-against"></a>

## 변경 사항을 검토하는 규칙

- 모든 컨트롤에는 Click UI 컴포넌트나 위의 Loom 프리미티브를 사용합니다. 버튼, 필드, 다이얼로그, 탭, 배지를 Tailwind 스타일로 흉내 낸 것은 버그입니다.
- 색상, 둥근 모서리, 그림자, 글자 크기는 토큰에서 가져옵니다. TSX에는 `text-[13px]`, `rounded-[6px]`, 16진수 색상, 원시 팔레트 클래스를 쓰지 않습니다.
- 모든 레이블, 제목, 버튼에는 문장형 대소문자(sentence case)를 사용합니다.
- **문구는 사용자가 보는 것을 쉬운 말로 나타냅니다.** 모든 페이지에서 화면에 표시되는 텍스트에는 operation, API 경로, 쿼리 필드의 이름을 쓰지 않습니다. 독자가 복사하거나 검색할 operation 이름(이벤트의 type, 서버가 보고하는 operation, 정확한 이름 목록)은 세부 정보 패널, mono 필드, `ExactNames` 목록, 또는 실행 시점에 값을 넣어 주는 툴팁에만 표시하며, 문장이나 리터럴에는 넣지 않습니다. `views/copy.rules.test.ts`는 리터럴 텍스트에서 operation 이름과 API 경로를 검사하며, 검사하지 않는 항목은 파일 머리글에 나열되어 있습니다. 쿼리 필드는 리뷰에 맡깁니다. 문장 안에서 mono로 표기한 operation 이름도 여전히 본문이며, 테스트가 mono 텍스트를 건너뛰므로 리뷰에서 잡아냅니다. 유일한 예외는 개발자가 복사하는 코드로, 페이지의 SDK 또는 클라이언트 설정 섹션(Tokens, Profile의 "Your own bot")에 해당합니다. 페이지가 환경 변수의 이름을 쓰는 경우는 대시보드가 제공하는 것을 모두 거친 뒤에도 독자가 운영자에게 그 설정을 이름으로 요청해야 할 때뿐이며, 운영자의 단계로 씁니다. 예: "an operator sets `LOOM_X` to …". 그 외에는 이 배포의 운영자가 켤 수 있다고 씁니다. 운영자 페이지(Health, 리포지토리의 Health 탭, 플랫폼 페이지, **Settings** › **Models & keys**, 어시스턴트의 점검 카드)에서는 replica, lease, extractor, embedder와 함께 설정, 테이블, 워커의 이름도 쓸 수 있습니다. 그 밖의 곳에서는 사용자가 보는 내용으로 설명합니다("this file type can't be read"). 용어는 하나로 통일하고, 각 용어는 처음 나오는 곳에서 한 번만 설명합니다([네임스페이스와 공유](https://github.com/ClickHouse/loom/blob/main/docs/features/namespaces-and-sharing.md)). 네임스페이스는 프로젝트 또는 에이전트의 메모리이고, 컬렉션은 문서 라이브러리이며 대시보드에서 네임스페이스라고 부르지 않습니다. 리포지토리는 인덱싱된 코드입니다. 해당하는 위치는 네임스페이스 선택기, Namespaces in your org, Audit log, Library, Repositories입니다.
- 페이지 제목은 아이콘이 없는 `PageHeader`로 붙입니다. 패널, 카드, 페인 섹션의 제목은 `SectionHeading`(Click UI의 `Title`, 14px semibold)으로 붙입니다. `.section-label`(12px, 흐린 색)은 목록, 메뉴, 레일 안의 그룹에 레이블을 붙일 때 씁니다.
- 토글은 레이블을 하나만 유지하고, 상태는 `aria-pressed`로 전달합니다. 몇 가지 옵션 중 하나를 고르는 경우는 `ButtonGroup`을 씁니다.
- 필드의 힌트는 `aria-describedby`로 컨트롤과 연결합니다.
- `Alert`의 도입 문구는 `title`이 아니라 `text` 안의 `<strong>`으로 넣습니다. Click UI는 title을 `<h6>`으로 렌더링하여 제목 순서를 깨뜨립니다.
- 한 화면에는 기본 버튼을 하나만 둡니다. 푸터는 오른쪽 정렬이며 보조 버튼이 먼저, 기본 버튼이 마지막에 옵니다.
- 사용할 수 없고 그 이유를 알려 주는 툴팁이 있는 컨트롤은 `disabled` 대신 `aria-disabled="true"`를 사용하여 이유에 계속 접근할 수 있게 합니다.
- 호출자의 역할로 쓸 수 없는 페이지 내 컨트롤은 `ui/Unavailable`로 계속 보이게 둡니다. 그 이유는 `lib/namespaceRole.ts`의 `needsRole(role, namespace)`, 또는 게이트가 걸린 op의 경우 `lib/callReason.ts`의 `callReason(op, …)`이며, 후자는 역할을 아직 알 수 없거나 서버가 결정하는 경우도 알려 줍니다(해당 경우는 doc 주석에 나열되어 있습니다). 대시보드가 역할을 알 수 없는 컨트롤은 서버가 결정한다는 문구를 보여 주고 사용할 수 없게 합니다. 단, 메모리 네임스페이스에서 Library의 쓰기는 예외로, 사용 가능한 상태로 두고 서버가 결정하게 합니다. 일괄 쓰기만 담고 있는 목록의 선택은 그 쓰기를 사용할 수 있는 곳에서만 표시합니다(`useRowSelection`의 활성화와 체크박스 렌더링을 같은 조건으로 합니다).
- 역할이 쓸 수 없는 메뉴 항목은 `aria-disabled`로 하고, Radix의 `disabled`는 쓰지 않습니다. 그 속성은 키보드 포커스에서 항목을 빼 버리기 때문입니다. 이유는 항목들 아래에 한 번만 표시하며, `ui/GatedItem` 또는 `ui/RowMenu` 항목의 `reason`을 사용합니다. 아이콘 버튼에는 `ui/GatedIconButton`을 씁니다.
- 다음은 대시보드가 토큰의 사용 가능 여부를 알 수 있는 곳에서만 제공합니다. Library의 Sources 섹션, Repositories에서는 리포지토리의 Docs 및 Sources 패싯과 트래커의 레코드 소스, 리포지토리 카드의 유지관리자 컨트롤(Settings, 브랜치의 Re-index와 Remove branch, Add branch), 트래커의 Edit mapping, Rotate, Retire입니다. 리포지토리의 Access 탭은 읽기 권한자에게 접근 컨트롤을 이유와 함께 보여 줍니다. 제한된 네임스페이스 자체의 게이트(그것을 지정한 조직의 토큰과 직접 부여)는 그 설정, 예산 한도, 리포지토리의 Access 탭, Refresh now에서 확인하며, 나머지 컨트롤은 역할을 따르고, 서버는 그 게이트를 통과하지 못한 토큰을 거부합니다. 헤더의 New 메뉴는 대시보드가 아는 역할을 따르고 나머지는 서버가 결정합니다. 리포지토리의 네임스페이스에서는 멤버의 메모리 쓰기(추가, 행 편집 또는 삭제)가 역할과 상관없이 사용 가능하며, 그 행이 해당 멤버의 것인지는 서버가 확인합니다. 네임스페이스가 아니라 조직 또는 플랫폼을 위한 항목만 사용할 수 없는 역할에게는 숨깁니다. 플랫폼 페이지, Health의 Workers, Operation mix, Projection failures 탭, Settings › Models & keys, 관리자 드로어의 내비게이션이 그에 해당합니다.
- ARIA보다 시맨틱 요소를 먼저 씁니다. WAI-ARIA 패턴이 필요한 위젯(그리드, 콤보박스)은 주석에 그 패턴의 이름을 적습니다.
- 텍스트는 4.5:1, 포커스 링은 3:1을 충족해야 하며, Click UI 다크 자체의 값인 세 가지 예외가 있습니다. 필드의 플레이스홀더 텍스트는 3.5:1, 완료되지 않은 온보딩 단계의 제목은 2.6:1이고, 컨트롤 경계(체크박스와 라디오 1.6:1, 필드 1.5:1, 스위치 2.6:1)는 3:1 미만입니다.
- 애니메이션은 `styles.css`에 두며, 이 파일은 `prefers-reduced-motion: reduce`에서 Click UI의 다이얼로그, 팝오버, 메뉴, 툴팁 등장 효과와 함께 애니메이션을 끕니다.

<a id="lists-loading-and-navigation"></a>

## 목록, 로딩, 탐색

- **색은 의미를 전달할 때만 쓰고, 장식에는 쓰지 않습니다.** 상태는 Click UI `Badge`(success, warning, danger, info, neutral)로, 피드백은 `Alert`로 표시합니다. 그 밖에 색을 쓰는 곳은 링크, 포커스 링, 하나뿐인 기본 동작, 선택된 행의 색조, 차트입니다. 태그, 에이전트, 키, 개수, 제목, 아이콘, 타임스탬프는 중립색을 유지하며, 경고가 아닌 것에는 경고색을 쓰지 않습니다.
- **목록 행은 `ui/ListRow`입니다.** Click UI의 CardHorizontal 토큰을 사용하며, 마우스를 올리면 색조가 입혀지고, 상세가 열려 있는 행에는 활성 테두리가 적용되며, 체크된 행에는 Click UI Table의 행 색조가 적용됩니다. 다만 상태는 체크박스가 나타내므로 이 색조는 옅고, Click UI의 Table은 활성 행에만 이 색조를 줍니다. 태그는 `ui/TagList`(윤곽선 칩 세 개, 이후 "+N")입니다. 거친 포인터(coarse pointer) 환경에서는 행의 누를 수 있는 태그가 한 줄에 머물며 잘립니다. 각 태그의 44 px 터치 영역이 24 px 상자 밖까지 뻗어 있어 둘째 줄이 있으면 첫째 줄 아래에 가려지기 때문이며, 드로어에서는 전체를 보여 줍니다. 에이전트는 채워진 중립 배지입니다. 비교 가능한 필드가 늘어서는 열(실행, 사용자, 감사)은 Click UI `Table`을 씁니다. 키로 지정되는 행(메모리, 프로필 섹션)은 12 px 모노스페이스 키를 앞에 두고 값을 14 px 본문으로 표시합니다. CardHorizontal이 제목 아래에 설명을 두는 것과 반대인데, 키는 식별자이고 독자가 훑어 읽는 것은 값이기 때문입니다. 메모리 행은 이 값을 두 줄로 제한합니다. 한 줄로 하면 대부분의 값이 문장 중간에서 잘렸기 때문이며, 목록은 너비 1400 px에서 두 줄에 들어가는 320자를 `memory.list`에 요청합니다. 태그가 있는 행은 126 px이고, 너비 640 px 미만에서는 메타 정보가 별도의 줄을 차지하므로 148 px입니다.
- **목록 페이지에서 고정(sticky)되는 요소는 `ui/ListToolbar` 하나뿐입니다.** 검색, 적용 중인 필터, 개수가 48 px 높이의 한 행에 들어갑니다. 선택된 행에 대한 동작은 같은 행에서 이 자리를 대신하므로, 툴바가 행 위로 커지지 않습니다. 목록 안의 섹션 제목은 고정되지 않습니다. 목록 옆의 측면 열(라이브러리의 트리)은 행의 높이를 차지하지 않으므로 고정될 수 있습니다.
- **페이지 이동은 `ui/ListPager`입니다.** 목록 아래에 Click UI의 Pagination을 전체 막대 형태로 두며, 범위, 페이지, 보기별로 기억되는 25/50/100 페이지 크기를 보여 줍니다. 페이지를 바꾸면 포커스가 목록 맨 위로 이동하고 해당 페이지가 안내됩니다. 전체 개수가 없는 커서 방식에는 "Load more"를, 순위가 매겨진 검색 결과에는 "Show more"를 둡니다. 전부 읽었지만 긴 목록은 "Show more"로 한 단계씩 그립니다(`lib/useShowMore`). 멤버 대화 상자 안의 목록 중 한 단계보다 긴 것은 모두 전체 개수를 표시합니다.
- **로딩은 `ui/Loading` 또는 `ui/LoadingSkeleton`입니다.** "Loading <thing>" 라벨이 붙은 Click UI 로더, 또는 행 모양을 본뜬 라벨 있는 스켈레톤이며, 둘 다 300 ms 전에는 나타나지 않습니다. 새로고침 중에는 화면에 있는 내용을 유지하고 `aria-busy`로 표시합니다. `LoadingSkeleton` 밖에서 `Skeleton`을 그대로 쓰는 것은 버그입니다(`Loading.rules.test.ts`가 검사합니다). 로더에는 최소 표시 시간이 없습니다. 데이터가 도착하면 바로 사라지며, 더 오래 붙들면 데이터가 늦어집니다. 빠른 응답이 로더를 깜빡이게 하는 일은 300 ms 대기가 이미 막아 줍니다.
- **무언가를 읽을 때는 현재 위치를 유지합니다.** 관련 객체는 드로어, 대화 상자, 팝오버에서 엽니다. 다른 도구로 이동할 때는 탐색하며 기록(history)에 쌓고, 뒤로 가기를 누르면 스크롤, 열려 있던 드로어, 포커스가 복원됩니다.
- **대시보드에는 키보드 단축키가 없고 키 힌트도 표시하지 않습니다.** 전역 조합 키, 한 글자 키, 키 칩이 없으며, 툴팁이나 `aria-keyshortcuts`에 키를 명시하지도 않습니다. 위젯은 표준 키(리스트박스, 메뉴, 트리, 그리드, 탭 목록, 대화 상자에서 WAI-ARIA 패턴이 정하는 화살표, Home/End, Enter, Space, Escape)를 유지하고, 텍스트 필드는 Enter로 전송하며, 모든 동작은 눈에 보이는 컨트롤로 제공합니다. Ask AI 필드는 Enter로 전송하고 Shift+Enter로 줄을 추가하며, 검색에서는 Enter가 강조된 옵션을 선택합니다.
- **아이콘은 Click UI의 것(`@clickhouse/click-ui/Icon`)을 쓰며,** 아이콘을 대신하는 텍스트 문자는 쓰지 않습니다.
- **행의 시간은 `ui/ListTime`입니다**(`lib/format.ts`의 `listTime`). "just now", "12m ago", "3h ago", "3d ago" 다음에는 "Sep 20" 또는 "Sep 20, 2025" 형식이며, `<time dateTime>` 안에 넣고 툴팁에 UTC 시간을 표시합니다. 서버 시간은 `parseChTime`으로 읽으며, `Date.parse`나 `new Date(<string>)`은 쓰지 않습니다. 이 둘은 시간대 없는 시간을 로컬 시간으로 읽기 때문입니다(`format.rules.test.ts`는 `Date.parse`가 있으면 실패합니다).
- **행별 동작은 `ui/RowMenu`입니다.** 행의 맨 오른쪽에 "Actions for <name>"이라는 이름의 항상 보이는 `dots-vertical` 버튼이 있고 그 뒤에 Click UI의 Dropdown이 붙습니다. 파괴적인 항목은 danger 항목으로 표시합니다. 인라인으로 두는 다른 동작은 최대 하나입니다. 행 내용의 한 줄은 그것이 요약하는 대상으로 연결되는 링크일 수 있으며(공개 네임스페이스 행의 가이드 요약은 Namespaces in your org의 가이드를 엽니다), 이는 열기 버튼 위에 놓이는 `.list-row-control`입니다. 이 경우 메뉴에서는 해당 동작을 뺍니다.
- **보여 줄 행이 없는 목록은 `ui/ListState`입니다.** 상태는 세 가지입니다. `empty`(`EmptyState`: 그곳에 무엇이 나타날지와 이를 채우는 하나의 동작을 안내합니다. 에이전트만 채울 수 있으면 **Connect an agent**, 운영자만 할 수 있으면 "an operator …"이며, [페이지 문서](https://github.com/ClickHouse/loom/blob/main/docs/features/dashboard.md#the-pages)에 설명되어 있습니다. Repositories는 첫 실행 단계가 `EmptyState` 안에 들어 있어 이를 직접 그립니다), `filtered`("No documents match tag:billing"과 `Clear filters`를 한 줄에 표시), `error`("Couldn't load documents", 메시지, 요청 ID, 보조 `Retry`이며 빈 상태로 표시하지 않습니다)입니다.
- **오류는 무슨 일이 일어났는지와 무엇을 해야 하는지를 말하고, 확실하지 않은 원인은 거론하지 않습니다.** `humanizeError`(`lib/errorWords.ts`, 한 줄을 구성하는 순서가 여기에 있습니다)가 모든 실패의 문구를 만듭니다. 도입 문구, 서버의 말, 다시 시도해야 하는지 여부로 이루어지며, 다시 시도 여부는 본문(`retryable`, `retry_after_s`, 카테고리)에서 가져오고 코드에 딸린 문장에서는 가져오지 않습니다(대시보드가 직접 만드는 `SIGNED_IN_ELSEWHERE`와 `ANSWER_UNREADABLE`은 코드에서 가져옵니다). 서버 메시지가 이미 그 조언을 담고 있으면 그대로 둡니다. 자체 도입 문구가 있는 호출자는 `bare`를 전달하여 "Couldn't delete x: Couldn't do that: …"처럼 읽히는 일이 없게 하고, 읽기만 한 호출자는 `read`를 전달하여 "it may have been saved"를 뺍니다. 쓰기에서는 이를 포함하는 것이 안전한 기본값입니다. 토스트는 `toastError(context, e)`를 거치며, 직접 문장을 만드는 경우(일괄 작업 중 얼마나 처리되었는지)에는 오류 문구를 `bare`로 만든 `toastErrorLine`을 씁니다. 실패한 읽기는 "Couldn't load" 줄 옆에 이유를 밝힙니다(`errorLine`). `PUBLICATION_PENDING`으로 응답받은 쓰기는 커밋된 것이고 목록과 검색만 뒤처지므로, `toastError`는 이를 정보로 표시하며 그 문구는 페이지에 아직 이전 상태가 보일 수 있으니 다시 보내지 말라고 안내합니다. 쓰기가 일어난 페이지도 이를 완료된 것으로 취급합니다. 쓰기 지점의 catch는 `committedAnyway(context, e)`(`lib/errorWords.ts`, 부팅 청크 밖)를 호출하며, 이 함수는 `toastError`처럼 토스트를 띄우고 이 응답이면 true를 반환합니다(실패를 인라인으로 표시하는 대화 상자는 먼저 `isPublicationPending`을 확인합니다). true이면 성공 후처럼 계속 진행하여(대화 상자를 닫고, 낙관적 변경을 유지하고, 목록을 다시 읽음) 화면에 반복을 유도하는 것이 남지 않게 합니다. 이름 바꾸기는 유일한 예외입니다. `memory.move`는 리디렉션하거나 이전 키를 삭제하기 전에 이 응답을 주므로, 폼이 열린 채로 남고 Rename을 한 번 더 누르면 이동이 끝난다고 안내합니다. `knowledge.restore`가 당장은 거부하는(`BACKEND_UNAVAILABLE`) 삭제의 Undo는 알림에 다시 나타납니다. 커밋된 쓰기에 대해서만 페이지가 직접 내는 줄은 `toastCommitted`를 거치며, 이는 요청 ID와 오류의 읽기 시간을 붙여 줍니다. 거부된 호출에 대해 페이지가 직접 내는 정보 줄은 같은 일을 하는 `toastWithId`를 거치고, `toastErrorLine`은 그 오류 톤 버전입니다. `lib/errorWords.rules.test.ts`는 도입 문구와, 오류의 말이 그것이 들어간 줄의 끝에 오는지를 검사합니다. 서버의 코드는 [오류 레퍼런스](https://github.com/ClickHouse/loom/blob/main/docs/reference/errors.md)에 있으며, 대시보드가 직접 만드는 세 가지는 `api.ts`의 `SIGNED_IN_ELSEWHERE`(경합한 로그인)와 `ANSWER_UNREADABLE`(`readJson`), 그리고 `api/assistantChat.ts`의 `STREAM_ENDED`입니다. `npm run budget`은 빌드가 문구를 담은 청크의 이름 `errorWords-*`를 유지하는지 검사합니다.
- **행 목록은 `useRowNav`로 탭 정지 지점이 하나입니다**(`lib/useRowNav.ts`, 행의 컨테이너에 적용). `↑/↓`는 이동, `Home/End`는 처음과 끝으로 이동, `Enter`는 열기, `Space`는 행의 체크박스 토글이며, Escape는 페이지가 처리합니다. 행의 열기 버튼에는 `data-row-open`이 붙고, 포커스 링은 버튼이 아니라 행에 그려집니다(`.list-row:has([data-row-open]:focus-visible)`). 현재 행의 컨트롤(태그 칩, 메뉴)만 열기 버튼 다음의 탭 정지 지점이 되고, Tab은 그중 마지막에서 목록 밖으로 나갑니다. 다른 행의 컨트롤은 모두 tabindex -1이며, 그중 하나에 포커스하면(클릭) 그 행이 현재 행이 됩니다. 체크박스는 탭 정지 지점이 아닙니다(`Space`를 씁니다). 좌우 그리드 이동은 없습니다. 행이 무언가를 여는 Click UI Table(Runs, 검토 대기열)도 행 목록입니다. 열기 버튼에 `data-row-open`이 붙고, `useRowNav`는 그 `tr`을 행으로 취급합니다. 행에 동작만 있는 테이블(감사 이벤트의 Details, 가드레일의 Delete)은 다른 테이블처럼 동작마다 탭 정지 지점을 둡니다. 유일한 예외는 Tasks 목록(`views/tasks/TaskList.tsx`)으로, 레인으로 이루어진 WAI-ARIA 그리드이며 좌우 키로 행과 그 안의 컨트롤 사이를 이동합니다.
- **에이전트는 `AgentBadge`입니다.** 최대 180 px이며 넘치면 말줄임표로 줄이고, 잘렸을 때 툴팁에 전체 이름을 보여 줍니다. 그 밖의 신원 이름(작성자, 사용자 ID)은 `NameBadge`라는 같은 배지를 씁니다.
- 무거운 Click UI 컴포넌트를 가져오는 Loom 프리미티브(`ListToolbar`, `ListPager`, `RowMenu`, `ListState`)는 `components/ui/index.ts`가 아니라 경로로 직접 임포트해야 합니다. 그렇지 않으면 모든 페이지의 부팅 청크에 포함됩니다.

<a id="before-a-change-goes-to-review"></a>

## 변경을 리뷰에 올리기 전에

목록 페이지의 모든 변경은 리뷰 전에 아래 항목으로 확인합니다. 각 줄은 목록 페이지에서 흔히 생기는 결함 유형입니다.

- **한 릴리스 뒤처진 서버**: 서버는 새로 추가된 모든 요청 입력을 무시하고(`ignored_args`에 이름을 남깁니다), 페이지는 그 서버가 응답하는 내용만으로도 동작해야 합니다(전체 개수 없음, 첫 페이지 다시 표시, 정확히 일치하는 매칭). 입력을 무시하는 가짜 서버로 테스트합니다.
- **대소문자**: 입력한 필터는 서버와 브라우저 모두에서 대소문자를 구분하지 않고 일치해야 하며, 정확 일치 필드로 보내는 값은 저장된 그대로 보냅니다. 예외는 Memories에서 입력한 에이전트 하나뿐입니다. `memory.list`는 에이전트를 정확히 일치시키므로, 목록이 아직 본 적 없는 에이전트는 입력한 그대로 보냅니다.
- **상태**: 로딩, 비어 있음, 필터와 일치하는 항목 없음, 오류, 아직 진행 중인 읽기는 서로 다른 다섯 가지 화면입니다. 툴바는 모든 화면에서 표시하며, "일치하는 항목 없음"은 현재 필터로 읽은 결과에 대해서만 표시합니다.
- **전체 집합**: 필터, 정렬, 개수, 전체 선택은 서버가 보유한 전체를 대상으로 하거나, 대상 범위를 밝혀야 합니다("최신 1,000개").
- **포커스**: 포커스를 가진 채 언마운트되는 요소(버튼, 바, 페이지)는 포커스를 명확한 다른 위치로 넘겨야 하며, 페이지가 바뀌면 보이는 제목으로 포커스가 이동해야 합니다.
- **휴대폰**: 터치 포인터(`hasTouch`)를 사용해 375 px에서 확인합니다. 잘리거나 너비가 0인 요소가 없어야 하고(행에서 잘린 태그는 예외이며, 서랍에서 전체를 보여 줍니다), 터치 대상은 44 px이며, 첫 번째 행은 크롬 예산 안에 들어와야 합니다.
- **복사본이 아닌 프리미티브**: 훅, 시간 형식, 상태 패턴의 두 번째 복사본은 대신 `lib/` 또는 `components/ui`로 옮깁니다. 원시 스켈레톤, 16진수 색상, 글리프 아이콘은 버그입니다.
- **유니코드**: 대소문자를 구분하지 않는 매칭은 한 곳에서 양쪽을 모두 접습니다(행과 요청 값에 ClickHouse `lowerUTF8`을 사용하고, A–Z만 접는 `lower`나 Python의 두 번째 접기는 쓰지 않습니다). ASCII가 아닌 값으로 테스트합니다.
- **늦게 도착한 응답**: 페이지가 시작하는 모든 읽기(로드, 다시 로드, Refresh, Retry, Load more)는 더 새로운 읽기에 추월될 수 있습니다. 페이지가 이미 떠난 범위나 필터에 대한 응답은 버립니다(중단하거나 범위 스탬프를 확인합니다).
- **변경이 건드린 부분**: 이전 동작을 설명하는 주석, 테스트, 문서는 같은 변경에서 함께 갱신합니다.

<a id="checks"></a>

## 점검

- `make lint`와 CI는 `frontend/src`에 대해 Biome의 접근성 규칙을 실행합니다(`biome.json` 오버라이드). 억제(suppression)에는 해당 규칙과 사유를 명시합니다.
- `frontend/e2e/a11y.spec.ts`는 모든 라우트를 데스크톱 및 휴대폰 너비에서, 그리고 셸의 대화상자, 메뉴, 팝오버에 대해 axe(WCAG 2.0–2.2 A 및 AA)를 실행합니다. 탐색기 스펙도 1280×720에서 실행됩니다.
- `npm --prefix frontend run budget`은 부트 스크립트, 스타일시트, 인덱스 청크의 크기에 상한을 둡니다. `e2e/perf-budget.spec.ts`와 `e2e/route-budget.spec.ts`는 페인트 시간, 롱 태스크, 레이아웃 시프트에 상한을 둡니다.
