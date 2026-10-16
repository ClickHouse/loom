# Dashboard design

The dashboard at `/` uses ClickHouse's design system, [Click UI](https://github.com/ClickHouse/click-ui), in its light theme. This page says where the look comes from, how to change it, and the rules a dashboard change is checked against.

## Where the look comes from

Click UI publishes its design tokens as CSS variables (`--global-color-*`, `--click-<component>-*`) in the `@clickhouse/click-ui` package. The dashboard does not load the package or its React components at runtime. It reads Click UI's values in three layers:

| File                                       | Holds                                                                                                                                                                                                                            |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `frontend/src/click-tokens.css`            | the Click UI variables the dashboard reads, copied from the installed package by `frontend/scripts/click_tokens.mjs`. Generated; do not edit.                                                                                    |
| `frontend/src/styles.css` (`@theme` block) | Loom's semantic tokens (`--color-bg`, `--color-surface`, `--color-text-muted`, `--color-accent`, `--color-success`, the type scale, radii, shadows), each defined as a Click UI variable, or as a Click UI palette step it names |
| components                                 | Tailwind utilities on the semantic tokens (`bg-surface`, `text-text-muted`, `rounded-lg`, `text-sm`), never a Click UI variable or a literal colour                                                                              |

Only the variables `styles.css` reads are copied, about forty of them, instead of Click UI's full 135 kB token sheet.

## Changing the look

- **Follow a Click UI release:** `npm --prefix frontend i -D --save-exact @clickhouse/click-ui@<version>`, then `node frontend/scripts/click_tokens.mjs`. Review the diff of `click-tokens.css`.
- **Read a new Click UI variable:** use it in `styles.css`, then run the generator. It fails if the variable does not exist in Click UI.
- **Add a semantic token:** add it to the `@theme` block with a comment saying what it is for, define it from a Click UI variable, and use it through a Tailwind utility.

`src/clickTokens.test.ts` fails when `click-tokens.css` does not match the installed package and the variables `styles.css` reads.

## Rules a change is checked against

These follow Click UI's own component and accessibility rules (its `AGENTS.md` and `ACCESSIBILITY.md`), where they apply to Loom.

- **Tokens only.** Colours, radii, shadows and text sizes come from the semantic tokens. An arbitrary size such as `text-[13px]` or a raw palette class such as `bg-emerald-50` bypasses the system.
- **Type scale.** Click UI's sizes are 12, 14, 16, 18, 20 and 32 px. `text-xs` is 12, `text-sm` and `text-base` are 14, `text-md` and `text-lg` are 16, and the display steps are 18, 20 and 32. `text-2xs` (11 px) is the floor for captions.
- **Corners.** 2 px checkboxes, 4 px buttons, fields, cards, menus and nav items, 8 px panels and dialogs, full radius for badges and chips.
- **Buttons.** One primary action per surface. In a form or dialog footer the actions are right-aligned, with the secondary action first and the primary last ("Cancel", then "Save"). A `<button>` always states its `type`.
- **Disabled with a reason.** A control that is unavailable and has a tooltip saying why uses `aria-disabled="true"` instead of `disabled`, so it stays focusable and the reason can be read.
- **Semantics before ARIA.** Use a Radix primitive or an existing component first, then a semantic element (`<button>`, `<a href>`, `<input>`, `<label>`), then minimal ARIA. A composite widget that needs a WAI-ARIA pattern (the Patterns board grid, the task list grid) names the pattern in a comment. Never put `onClick` on a `<div>`, `tabIndex` on a non-interactive element, or `aria-label` on an element whose role does not take a name.
- **Contrast.** Text holds 4.5:1 against its background, and focus rings and UI parts hold 3:1. The semantic text tokens are chosen to hold this on every surface.
- **Motion.** The stylesheet turns its animations and transitions off under `prefers-reduced-motion: reduce`. A new animation goes in `styles.css` with the others, so the same rule covers it.

`make lint` runs Biome's accessibility rules over `frontend/src` (the `overrides` in `biome.json`), and CI runs the same check. A suppression names its rule and carries a reason:

```tsx
{
  /* biome-ignore lint/a11y/useSemanticElements: APG grid with roving focus; a <table> would lose the keyboard model */
}
```

The axe checks in the Playwright suite (`npx playwright test` from `frontend/`) cover contrast and names on the rendered pages.
