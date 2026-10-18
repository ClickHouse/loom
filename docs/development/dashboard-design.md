# Dashboard design

The dashboard at `/` is built on ClickHouse's design system, [Click UI](https://github.com/ClickHouse/click-ui), in its light theme. This page says where the look and behaviour come from, how to change them, and the rules a dashboard change is checked against.

## Click UI is the component library

`@clickhouse/click-ui` (pinned exactly in `frontend/package.json`) is a runtime dependency. `src/main.tsx` loads its light tokens and page defaults directly (the `click-ui-styles` alias in `vite.config.ts`) instead of mounting `ClickUIProvider`, whose barrel import would ship every component's CSS and the dark theme; the Radix tooltip provider Click UI's components expect is mounted there directly; `index.html` sets `data-cui-theme="light"`. Controls are Click UI components, imported by subpath (`@clickhouse/click-ui/Button`), so only the components the dashboard imports are bundled, and those only lazy views use load with those views:

| Need                                   | Use                                                                                       |
| -------------------------------------- | ----------------------------------------------------------------------------------------- |
| a button                               | `Button` (`type`: primary, secondary, empty, danger; always pass `htmlType`)              |
| a field                                | `TextField`, `SearchField`, `PasswordField`, `NumberField`, `TextAreaField`, `Select`     |
| a check or switch                      | `Checkbox`, `Switch` (labels are plain strings)                                           |
| tabs, a menu, a popover                | `Tabs` (name the `Tabs.TriggersList`), `Dropdown`, `Popover` (with Loom's triggers below) |
| a table, a badge, an alert, pagination | `Table`, `Badge`, `Alert`, `Pagination`, `ProgressBar`                                    |
| a text link or a link-styled action    | `Link` (`component="button"` for an action; `ui/ExternalLink` for a new tab)              |
| an icon                                | `Icon` with a Click UI name, `aria-hidden` unless it is the only content                  |

A few Loom primitives in `src/components` wrap Click UI where Click UI 0.13 falls short. Use them, not the Click UI component underneath:

| Primitive                                        | Wraps                    | Because                                                                                                                                                                                                                                            |
| ------------------------------------------------ | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ui/Modal`, `ModalFooter`                        | `Dialog`                 | Click UI's close button is named after its icon ("cross"), its overlay has no z-index, and focus returns to the opener only from a `Dialog.Trigger`                                                                                                |
| `IconButton`                                     | `IconButton` + `Tooltip` | an icon-only control needs a name and a tooltip; a Radix trigger's `type="button"` lands on Click UI's variant prop                                                                                                                                |
| `ui/Menu`: `MenuTrigger`, `PopoverTrigger`       | Radix triggers           | Click UI's `Dropdown.Trigger` and `Popover.Trigger` put `aria-expanded` on a wrapping `div` and return focus to it                                                                                                                                 |
| `ui/Unavailable`                                 | —                        | a control that is unavailable for a stated reason: `aria-disabled`, its tooltip and description                                                                                                                                                    |
| `ui/PageHeader`                                  | —                        | every page's title row: a 20 px h1 with no icon, as Click UI's page titles, the scope badge beside it and the actions at the end                                                                                                                   |
| `ui/ChoiceGroup`                                 | `RadioGroup`             | Click UI's group label does not name the group, and a non-text item label is announced as "[object Object]"                                                                                                                                        |
| `ui/ExternalLink`                                | `Link`                   | Click UI's `icon` prop names its icon ("popout"), read after the link text; this hides the icon and says "(opens in a new tab)"                                                                                                                    |
| `ui/Tooltip` (`Tip`)                             | `Tooltip`                | one tooltip shape, on the provider `main.tsx` mounts (specs get it from `src/test/rtl.tsx`); Click UI's `disabled` switches Radix between controlled and uncontrolled, and a tip that is off still counts as open, so the next tip skips its delay |
| `ui/TabTrigger`                                  | `Tabs.Trigger`           | Click UI's Icon is a block, so an icon stacks above the label without a wrapper                                                                                                                                                                    |
| `Toast` (`toast()`)                              | —                        | Click UI's Toast has no keyed replacement or request-id line and its close is named "cross"; Loom's card uses Click UI's toast tokens, bottom right                                                                                                |
| `ConfirmDialog` (`confirmAction()`)              | `Modal` + `Button`       | Click UI's ConfirmationDialog focuses the confirm button; Loom focuses Cancel                                                                                                                                                                      |
| `ui/fieldError` (`{...fieldError(id, message)}`) | a field's `error`        | Click UI draws the error as a bare `<div>` and sets neither `aria-invalid` nor `aria-describedby`; this ties the message to the input                                                                                                              |
| `ui/Alert`                                       | `Alert`                  | Click UI's Alert puts a `role` on its root but its props type declares none; a live alert takes `role="alert"` or `role="status"`, with no wrapper                                                                                                 |
| `ui/Stat`                                        | `BigStat`                | one figure in a row of them: the small BigStat with a hint under the value, which BigStat has no slot for                                                                                                                                          |

Loom keeps a few elements of its own, on Click UI tokens:

- `Chip` (Click UI has no filter chip), `EmptyState` and `Skeleton` (none in Click UI).
- `Panel`: Click UI's card look with a header slot and a scrolling body, which Click UI's `Panel` container does not have.
- The memory inspector drawer: a Radix dialog sheet, because Click UI's `Flyout` collapses to a hover strip on narrow screens and takes no ref.
- Disclosure toggles: a native button with `aria-expanded` and a Click UI chevron, because Click UI's `Collapsible` sets no `aria-expanded`.
- Triggers that show an avatar or a namespace name: native buttons on tokens; Click UI's `empty` button is link text.
- Date and date-time fields (the graph's "Valid as of", a task's due time): native `date` and `datetime-local` inputs drawn with Click UI's field tokens, because Click UI's `DatePicker` takes no `name` or `id` for a label and has no time.
- The tracker source's repository list: a native input inside a field-styled box that turns each entry into a removable tag, because Click UI has no tag input.
- File pickers: a hidden native `file` input opened by a Click UI `Button`.
- Sidebar nav items: anchors drawn with Click UI's `--click-sidebar-*` tokens. Click UI's `SidebarNavigationItem` is a `div` whose label may hold a link, which would put the selected state on the `div`, not the link.
- Loom-specific widgets (the graph canvas, the task grid and board, the patterns board), with Click UI controls inside them.

## Styles

- **Layer order** (inline in `index.html`, before any stylesheet): `properties, theme, base, clickui, components, utilities`. Click UI sits above Tailwind's reset and below the utilities, so put only layout utilities (margin, width, flex placement) on a Click UI component; a colour or padding utility would override Click UI.
- **Tokens** in `src/styles.css`: Loom's semantic names (`--color-bg`, `--color-surface`, `--color-text-muted`, `--color-accent`, `--color-success`, the type scale, radii, shadows) are defined from Click UI's variables, for Loom's own elements. Click UI components read Click UI's variables directly.
- **Accessibility corrections**: Click UI 0.13's light theme misses WCAG AA in a few places (link and empty-button blue, info alert text, opaque success and info badges, solid default and warning badges, placeholder text, the field, checkbox, radio and switch strokes, the avatar's hover background, and its 10 px small badge and 11 px small avatar). `styles.css` redefines those Click UI variables with the next palette step that passes, so every component picks the fix up. The focus ring is Click UI's outline blue, 2 px at a 2 px offset, on every focusable element: Click UI draws a typed button's ring in that button's own stroke colour (a secondary button's is 1.5:1, under 3:1) and draws none on checkboxes, switches, tabs, links and its dialog close.
- **Tab rows**: `styles.css` keeps every tab label on one line and lets a tab row wider than the page scroll sideways in its own box, with the tab's focus ring drawn inside it.
- **Disabled with a reason**: Click UI styles only a natively disabled button; `ui/Unavailable` marks its button `data-unavailable` (with the button's type), and `styles.css` gives that button Click UI's disabled colours.
- **Layers on top**: dialogs z-50 (a confirm z-[60]), toasts z-[65], every Radix popper (menus, selects, popovers, tooltips) z-[70].
- **Type**: Click UI's sizes, 12, 14, 16, 18, 20 and 32 px. Click UI's 10 px step (captions, small badges) is left out: captions are 12 px (`text-xs`), and `styles.css` sets the small badge to 12 px. Page titles are 20 px. Inter and Inconsolata, latin and latin-ext, are declared in `styles.css` under Click UI's family names.

## What ships of Click UI

- `vite.config.ts` aliases Click UI's flag, logo and payment-mark maps to an empty module (`src/lib/emptyAssetMap.ts`): Click UI's `Icon` imports them whole to resolve any name, and the dashboard draws only interface icons. Click UI ships ~95 kB gz of script this way instead of ~325 kB.
- `vite.config.ts` puts the Click UI modules the shell imports statically in one `clickui` chunk (~64 kB gz) that every page loads; Rollup splits the rest (Select and its sortable list, Table, Pagination) into the lazy views that use them, and merges a shared chunk under 1 kB (a primitive such as `fieldError` that only lazy views use) into one that loads with it. `npm run budget` fails when the boot scripts pass 198 kB gz.
- Click UI's `Badge` measures its text on every render. A long list of them memoises its rows and passes them stable callbacks, as `MemoryCard` does, or one change re-measures every row.
- `src/lib/clickUiFixes.ts` names what Click UI 0.13's Select leaves unnamed in its option list: the filter field, its clear button and the dialog that holds them. It also keeps keyboard focus through a Button's `loading` state, which Click UI implements as `disabled` and so drops focus to the page: the button pressed takes focus back when it is enabled again, and a dialog it opened returns focus to it (`ui/Modal`).

## Follow a Click UI release

`npm --prefix frontend i --save-exact @clickhouse/click-ui@<version>`, then run the frontend checks below. Re-check each workaround in the primitives table and in `clickUiFixes.ts` against the release notes: delete a workaround once Click UI fixes it. A new icon, flag or logo map in Click UI may need adding to the alias.

## Rules a change is checked against

- Use the Click UI component, or the Loom primitive above, for every control. A Tailwind-styled imitation of a button, field, dialog, tab or badge is a bug.
- Colours, radii, shadows and text sizes come from tokens: no `text-[13px]`, `rounded-[6px]`, hex colour or raw palette class in TSX.
- Sentence case for every label, title and button.
- A page is titled with `PageHeader`, which carries no icon. A panel, card or pane section is titled with `SectionHeading` (Click UI's `Title`, 14 px semibold). `.section-label` (12 px, muted) labels a group inside a list, menu or rail.
- A toggle keeps one label; `aria-pressed` carries its state. A single choice among a few options is a `ButtonGroup`.
- A field's hint is tied to its control with `aria-describedby`.
- A shortcut of one character key checks `shortcutOff` (`src/lib/shortcuts.ts`): the help dialog's switch turns them all off (WCAG 2.1.4). `useKeyboard` does this for its bindings.
- An `Alert` takes its lead-in as `<strong>` in `text`, not `title`: Click UI renders the title as an `<h6>`, which breaks the heading order.
- One primary button per surface. Footers are right-aligned, secondary first, the primary last.
- A control that is unavailable and has a tooltip saying why uses `aria-disabled="true"` instead of `disabled`, so the reason stays reachable.
- Semantic elements before ARIA; a widget that needs a WAI-ARIA pattern (a grid, a combobox) names the pattern in a comment.
- Text holds 4.5:1, focus rings and control boundaries 3:1.
- Animations live in `styles.css`, which turns them off under `prefers-reduced-motion: reduce`, along with Click UI's dialog, popover, menu and tooltip entrances.

## Lists, loading and navigation

- **Colour carries meaning, never decoration.** Status is a Click UI `Badge` (success, warning, danger, info, neutral); feedback is an `Alert`. Links, the focus ring, the one primary action, a selected row's tint and charts are the other uses. Tags, agents, keys, counts, headings, icons and timestamps stay neutral, and nothing that is not a warning uses the warning colour.
- **A list row is `ui/ListRow`**, on Click UI's CardHorizontal tokens: hover tints it, the row whose detail is open takes the active stroke, a checked row the tint Click UI's Table gives a selected row. Tags are `ui/TagList` (outlined chips, three, then "+N"); an agent is a filled neutral badge. Columns of comparable fields (runs, users, audit) are a Click UI `Table`. A memory row clamps its value to two lines, so a row with tags is about 108 px: the value is what a reader scans, and one line cut most values mid-sentence.
- **A list page has one sticky element, `ui/ListToolbar`:** the search, the filters in force and the count in one 48 px row. Selected rows' actions take its place in the same row, so it never grows over the rows. Section headings inside a list do not stick.
- **Paging is `ui/ListPager`**, Click UI's Pagination as its full bar, under the list: the range, the pages, a 25/50/100 page size remembered per view. A page change moves focus to the list's top and announces the page. A cursor without a total gets "Load more"; ranked search results get "Show more".
- **Loading is `ui/Loading` or `ui/LoadingSkeleton`**, Click UI's loader with a "Loading <thing>" label, or a labelled skeleton shaped like the rows; neither shows before 300 ms. A refresh keeps what is on screen, marked `aria-busy`. A raw `Skeleton` outside `LoadingSkeleton` is a bug (`Loading.rules.test.ts` checks it). A loader has no minimum time on screen: it unmounts when its data lands, and holding it longer would delay the data; the 300 ms wait already keeps a fast answer from flashing one.
- **Reading something keeps you where you are**: a related object opens in the drawer, a dialog or a popover. Going to another tool navigates, pushes history, and Back restores the scroll, the open drawer and the focus.
- **Shortcut hints** appear only in tooltips, the help dialog and the global search field (`⌘K` on Apple platforms, `Ctrl K` elsewhere). Click UI has no shortcut component; the keys themselves all stay.
- **Icons are Click UI's** (`@clickhouse/click-ui/Icon`), never a text glyph standing in for one.
- **A row's time is `ui/ListTime`** (`listTime` in `lib/format.ts`): "just now", "12m ago", "3h ago", "3d ago", then "Sep 20" or "Sep 20, 2025", in a `<time dateTime>` with the UTC time in a tooltip. A ClickHouse time is read with `parseChTime`, never `replace(" ", "T") + "Z"`.
- **Per-row actions are `ui/RowMenu`**: Click UI's Dropdown behind an always-visible `dots-vertical` button named "Actions for <name>", at the row's far right; a destructive item is the danger item. At most one other action sits inline.
- **A list with no rows to show is `ui/ListState`**: `empty` (the `EmptyState`, one action), `filtered` ("No documents match tag:billing" and `Clear filters` on one line) or `error` ("Couldn't load documents", the message, the request id and a secondary `Retry`, never the empty state).
- **A row list is one tab stop, through `useRowNav`** (`lib/useRowNav.ts`) on the rows' container: `↑/↓` and `j/k` move, `Home/End` jump, `Enter` opens, `Space` toggles the row's checkbox; Escape is the page's. The row's open button carries `data-row-open`, and the focus ring is drawn on the row (`.list-row:has([data-row-open]:focus-visible)`), never on the button. Only the current row's controls (its tag chips, its menu) are tab stops, after its open button, and Tab goes from the last of them out of the list; every other row's are at tabindex -1, and focusing one (a click) makes its row current. Checkboxes are never tab stops (`Space`). There is no Left/Right grid movement.
- **An agent is `AgentBadge`**: at most 180 px with an ellipsis, the whole name in a tooltip when cut.
- A Loom primitive that brings a heavy Click UI component (`ListToolbar`, `ListPager`, `RowMenu`, `ListState`) is imported by path, not from `components/ui/index.ts`, or it lands in every page's boot chunk.

## Before a change goes to review

Every list-page change is checked against these before it is reviewed; each line is a defect class review has found more than once.

- **A server a release behind**: every new request input is ignored by it (and named in `ignored_args`); the page still works with what that server answers (no total, the first page again, exact matching). Test it with a fake server that ignores the input.
- **Case**: a typed filter matches in any case, on the server and in the browser; a value sent to an exact-match field is sent as it is stored.
- **States**: loading, empty, a filter that matches nothing, an error, and a read still in flight are five different screens; the toolbar shows in every one, and "nothing matches" is said only of a read of the current filter.
- **The whole set**: a filter, a sort, a count or a select-all covers what the server holds, or says what it covers ("the newest 1,000").
- **Focus**: anything that unmounts while focused (a button, a bar, a page) hands focus somewhere named; a page change lands on a visible heading.
- **Phone**: 375 px, a coarse pointer: nothing clipped or zero-width, 44 px targets, the first row within the chrome budget.
- **Primitives, not copies**: a second copy of a hook, a time format or a state pattern goes into `lib/` or `components/ui` instead; a raw skeleton, a hex colour or a glyph icon is a bug.
- **Unicode**: a case-insensitive match folds the same way on both sides (ClickHouse `lowerUTF8`, not `lower`, which folds only A–Z); test with a non-ASCII value.
- **Late answers**: every read a page starts (load, reload, Refresh, Retry, Load more) can be overtaken by a newer one; an answer for a scope or filter the page has left is dropped (abort it, or check a scope stamp).
- **Tab stops**: a row list is one tab stop: every control inside a row other than the current one is out of the tab order.
- **Measure as a phone**: chrome budgets and targets are measured with a touch pointer (`hasTouch`), where every control is 44 px.
- **What the change touched**: comments, tests and docs that describe the old behaviour are updated in the same change.

## Checks

- `make lint` and CI run Biome's accessibility rules over `frontend/src` (`biome.json` overrides). A suppression names its rule and the reason.
- `frontend/e2e/a11y.spec.ts` runs axe (WCAG 2.0–2.2 A and AA) on every route at desktop and phone width, and on the shell's dialogs, menus and popovers. The explorer specs also run at 1280×720.
- `npm --prefix frontend run budget` caps the boot scripts, the stylesheets and the index chunk; `e2e/perf-budget.spec.ts` and `e2e/route-budget.spec.ts` cap paint time, long tasks and layout shift.
