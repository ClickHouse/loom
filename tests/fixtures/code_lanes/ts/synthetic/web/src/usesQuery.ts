// synthetic: a subpath import that only the package's
// `exports` table can resolve — `./query` → `./dist/src/features/query/index.js`
import { QueryBuilder } from "@langfuse/shared/query";
import { z } from "zod";

export const inputSchema = z.object({ view: z.string() });

export function buildQuery(view: string): QueryBuilder {
  return new QueryBuilder(view);
}
