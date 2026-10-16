# Dashboard design

The dashboard at `/` is built on ClickHouse's design system, [Click UI](https://github.com/ClickHouse/click-ui), in its light theme. This page says where the look and behaviour come from, how to change them, and the rules a dashboard change is checked against.

## Click UI is the component library

`@clickhouse/click-ui` (pinned exactly in `frontend/package.json`) is a runtime dependency. `ClickUIProvider theme="light"` wraps the app in `src/main.tsx`, and Click UI's stylesheet and variables load with it. Controls are Click UI components, imported by subpath (`@clickhouse/click-ui/Button`):

| Need                                   | Use                                                                                       |
| -------------------------------------- | ----------------------------------------------------------------------------------------- |
| a button                               | `Button` (`type`: primary, secondary, empty, danger; always pass `htmlType`)              |
| a field                                | `TextField`, `SearchField`, `PasswordField`, `NumberField`, `TextAreaField`, `Select`     |
| a check or switch                      | `Checkbox`, `Switch` (labels are plain strings)                                           |
| tabs, a menu, a popover                | `Tabs` (name the `Tabs.TriggersList`), `Dropdown`, `Popover` (with Loom's triggers below) |
| a table, a badge, an alert, pagination | `Table`, `Badge`, `Alert`, `Pagination`                                                   |
| an icon                                | `Icon` with a Click UI name, `aria-hidden` unless it is the only content                  |

A few Loom primitives in `src/components` wrap Click UI where Click UI 0.13 falls short. Use them, not the Click UI component underneath:

| Primitive                                  | Wraps                    | Because                                                                                                            |
| ------------------------------------------ | ------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `ui/Modal`, `ModalFooter`                  | `Dialog`                 | Click UI's close button has no accessible name and its overlay has no z-index                                      |
| `IconButton`                               | `IconButton` + `Tooltip` | an icon-only control needs a name and a tooltip                                                                    |
| `ui/Menu`: `MenuTrigger`, `PopoverTrigger` | Radix triggers           | Click UI's `Dropdown.Trigger` and `Popover.Trigger` put `aria-expanded` on a wrapping `div` and return focus to it |
| `ui/Unavailable`                           | —                        | a control that is unavailable for a stated reason: `aria-disabled`, its tooltip and description                    |
| `ui/ChoiceGroup`                           | `RadioGroup`             | Click UI's group label does not name the group, and a non-text item label is announced as "[object Object]"        |
| `ui/Tooltip` (`Tip`)                       | `Tooltip`                | carries its own provider and layer                                                                                 |
| `Toast` (`toast()`)                        | —                        | Click UI's toast close button has no name, and Loom shows a request id; Loom's card uses Click UI's toast tokens   |
| `ConfirmDialog` (`confirmAction()`)        | `Modal` + `Button`       | Click UI's ConfirmationDialog focuses the confirm button; Loom focuses Cancel                                      |

Loom keeps its own `Chip`, `Panel`, `EmptyState` and `Skeleton`, on Click UI tokens, because Click UI has no filter chip, empty state or skeleton. Loom-specific widgets (the graph canvas, the task grid and board, the patterns board) are Loom's, with Click UI controls inside them.

## Styles

- **Layer order** (inline in `index.html`, before any stylesheet): `properties, theme, base, clickui, components, utilities`. Click UI sits above Tailwind's reset and below the utilities, so put only layout utilities (margin, width, flex placement) on a Click UI component; a colour or padding utility would override Click UI.
- **Tokens** in `src/styles.css`: Loom's semantic names (`--color-bg`, `--color-surface`, `--color-text-muted`, `--color-accent`, `--color-success`, the type scale, radii, shadows) are defined from Click UI's variables, for Loom's own elements. Click UI components read Click UI's variables directly.
- **Accessibility corrections**: Click UI 0.13's light theme misses WCAG AA in a few places (link and empty-button blue, opaque success and info badges, solid default and warning badges, placeholder text, field and checkbox strokes). `styles.css` redefines those Click UI variables with the next palette step that passes, so every component picks the fix up.
- **Layers on top**: dialogs z-50 (a confirm z-[60]), toasts z-[65], every Radix popper (menus, selects, popovers, tooltips) z-[70].
- **Type**: Click UI's sizes, 12, 14, 16, 18, 20 and 32 px, with an 11 px caption floor; page titles are 20 px. Inter and Inconsolata, latin and latin-ext, are declared in `styles.css` under Click UI's family names.

## Follow a Click UI release

`npm --prefix frontend i --save-exact @clickhouse/click-ui@<version>`, then run the frontend checks below. Re-check each workaround in the primitives table against the release notes: delete a primitive's workaround once Click UI fixes it.

## Rules a change is checked against

- Use the Click UI component, or the Loom primitive above, for every control. A Tailwind-styled imitation of a button, field, dialog, tab or badge is a bug.
- Colours, radii, shadows and text sizes come from tokens: no `text-[13px]`, `rounded-[6px]`, hex colour or raw palette class in TSX.
- Sentence case for every label, title and button.
- One primary button per surface. Footers are right-aligned, secondary first, the primary last.
- A control that is unavailable and has a tooltip saying why uses `aria-disabled="true"` instead of `disabled`, so the reason stays reachable.
- Semantic elements before ARIA; a widget that needs a WAI-ARIA pattern (a grid, a combobox) names the pattern in a comment.
- Text holds 4.5:1, focus rings and control boundaries 3:1.
- Animations live in `styles.css`, which turns them off under `prefers-reduced-motion: reduce`.

## Checks

- `make lint` and CI run Biome's accessibility rules over `frontend/src` (`biome.json` overrides). A suppression names its rule and the reason.
- `frontend/e2e/a11y.spec.ts` runs axe (WCAG 2.0–2.2 A and AA) on every route at desktop and phone width, and on the shell's dialog and menu.
- `npm --prefix frontend run budget` caps the boot scripts, the stylesheets and the index chunk; `e2e/perf-budget.spec.ts` and `e2e/route-budget.spec.ts` cap paint time, long tasks and layout shift.
