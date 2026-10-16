# Dashboard design

The dashboard at `/` is built on ClickHouse's design system, [Click UI](https://github.com/ClickHouse/click-ui), in its light theme. This page says where the look and behaviour come from, how to change them, and the rules a dashboard change is checked against.

## Click UI is the component library

`@clickhouse/click-ui` (pinned exactly in `frontend/package.json`) is a runtime dependency. `src/main.tsx` loads its light tokens and page defaults directly (the `click-ui-styles` alias in `vite.config.ts`) instead of mounting `ClickUIProvider`, whose barrel import would ship every component's CSS and the dark theme; the Radix tooltip provider Click UI's components expect is mounted there directly; `index.html` sets `data-cui-theme="light"`. Controls are Click UI components, imported by subpath (`@clickhouse/click-ui/Button`), so only the components in use ship:

| Need                                   | Use                                                                                       |
| -------------------------------------- | ----------------------------------------------------------------------------------------- |
| a button                               | `Button` (`type`: primary, secondary, empty, danger; always pass `htmlType`)              |
| a field                                | `TextField`, `SearchField`, `PasswordField`, `NumberField`, `TextAreaField`, `Select`     |
| a check or switch                      | `Checkbox`, `Switch` (labels are plain strings)                                           |
| tabs, a menu, a popover                | `Tabs` (name the `Tabs.TriggersList`), `Dropdown`, `Popover` (with Loom's triggers below) |
| a table, a badge, an alert, pagination | `Table`, `Badge`, `Alert`, `Pagination`, `ProgressBar`                                    |
| a text link or a link-styled action    | `Link` (`component="button"` for an action)                                               |
| an icon                                | `Icon` with a Click UI name, `aria-hidden` unless it is the only content                  |

A few Loom primitives in `src/components` wrap Click UI where Click UI 0.13 falls short. Use them, not the Click UI component underneath:

| Primitive                                  | Wraps                    | Because                                                                                                                                             |
| ------------------------------------------ | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ui/Modal`, `ModalFooter`                  | `Dialog`                 | Click UI's close button is named after its icon ("cross"), its overlay has no z-index, and focus returns to the opener only from a `Dialog.Trigger` |
| `IconButton`                               | `IconButton` + `Tooltip` | an icon-only control needs a name and a tooltip; a Radix trigger's `type="button"` lands on Click UI's variant prop                                 |
| `ui/Menu`: `MenuTrigger`, `PopoverTrigger` | Radix triggers           | Click UI's `Dropdown.Trigger` and `Popover.Trigger` put `aria-expanded` on a wrapping `div` and return focus to it                                  |
| `ui/Unavailable`                           | —                        | a control that is unavailable for a stated reason: `aria-disabled`, its tooltip and description                                                     |
| `ui/ChoiceGroup`                           | `RadioGroup`             | Click UI's group label does not name the group, and a non-text item label is announced as "[object Object]"                                         |
| `ui/Tooltip` (`Tip`)                       | `Tooltip`                | one tooltip shape; it uses the provider `main.tsx` mounts (specs get it from `src/test/rtl.tsx`)                                                    |
| `ui/TabTrigger`                            | `Tabs.Trigger`           | Click UI's Icon is a block, so an icon stacks above the label without a wrapper                                                                     |
| `Toast` (`toast()`)                        | —                        | Click UI's Toast has no keyed replacement or request-id line and its close is named "cross"; Loom's card uses Click UI's toast tokens, bottom right |
| `ConfirmDialog` (`confirmAction()`)        | `Modal` + `Button`       | Click UI's ConfirmationDialog focuses the confirm button; Loom focuses Cancel                                                                       |

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
- **Disabled with a reason**: Click UI styles only a natively disabled button; `styles.css` gives an `aria-disabled` Click UI button the same look by matching Click UI's class prefixes (`button__`, `button_empty__`).
- **Layers on top**: dialogs z-50 (a confirm z-[60]), toasts z-[65], every Radix popper (menus, selects, popovers, tooltips) z-[70].
- **Type**: Click UI's sizes, 12, 14, 16, 18, 20 and 32 px. Click UI's 10 px step (captions, small badges) is left out: captions are 12 px (`text-xs`), and `styles.css` sets the small badge to 12 px. Page titles are 20 px. Inter and Inconsolata, latin and latin-ext, are declared in `styles.css` under Click UI's family names.

## What ships of Click UI

- `vite.config.ts` aliases Click UI's flag, logo and payment-mark maps to an empty module (`src/lib/emptyAssetMap.ts`): Click UI's `Icon` imports them whole to resolve any name, and the dashboard draws only interface icons. Click UI ships ~95 kB gz of script this way instead of ~325 kB.
- `src/lib/clickUiFixes.ts` names what Click UI 0.13's Select leaves unnamed in its option list: the filter field, its clear button and the dialog that holds them.

## Follow a Click UI release

`npm --prefix frontend i --save-exact @clickhouse/click-ui@<version>`, then run the frontend checks below. Re-check each workaround in the primitives table and in `clickUiFixes.ts` against the release notes: delete a workaround once Click UI fixes it. A new icon, flag or logo map in Click UI may need adding to the alias.

## Rules a change is checked against

- Use the Click UI component, or the Loom primitive above, for every control. A Tailwind-styled imitation of a button, field, dialog, tab or badge is a bug.
- Colours, radii, shadows and text sizes come from tokens: no `text-[13px]`, `rounded-[6px]`, hex colour or raw palette class in TSX.
- Sentence case for every label, title and button.
- An `Alert` takes its lead-in as `<strong>` in `text`, not `title`: Click UI renders the title as an `<h6>`, which breaks the heading order.
- One primary button per surface. Footers are right-aligned, secondary first, the primary last.
- A control that is unavailable and has a tooltip saying why uses `aria-disabled="true"` instead of `disabled`, so the reason stays reachable.
- Semantic elements before ARIA; a widget that needs a WAI-ARIA pattern (a grid, a combobox) names the pattern in a comment.
- Text holds 4.5:1, focus rings and control boundaries 3:1.
- Animations live in `styles.css`, which turns them off under `prefers-reduced-motion: reduce`, along with Click UI's dialog, popover, menu and tooltip entrances.

## Checks

- `make lint` and CI run Biome's accessibility rules over `frontend/src` (`biome.json` overrides). A suppression names its rule and the reason.
- `frontend/e2e/a11y.spec.ts` runs axe (WCAG 2.0–2.2 A and AA) on every route at desktop and phone width, and on the shell's dialog and menu.
- `npm --prefix frontend run budget` caps the boot scripts, the stylesheets and the index chunk; `e2e/perf-budget.spec.ts` and `e2e/route-budget.spec.ts` cap paint time, long tasks and layout shift.
