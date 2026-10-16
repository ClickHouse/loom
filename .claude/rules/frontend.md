---
paths:
  - "frontend/**"
---

# Dashboard

- Biome formats and lints JS/TS/JSON/CSS: run `make format` from the repository root, where the pinned Biome reads `biome.json`.
- `npm --prefix frontend run test` (vitest + axe), `npm --prefix frontend run build` (`tsc -b` + Vite) and `npm --prefix frontend run budget` before pushing.
