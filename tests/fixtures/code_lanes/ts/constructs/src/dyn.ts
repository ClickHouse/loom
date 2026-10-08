// Dynamic imports bind their names
import type { Job } from './overloads';
const lazy = await import('./classExpr');
export async function run(j: Job) {
  const { parse } = await import('./overloads');
  return new lazy.Widget().render() + parse(1);
}
