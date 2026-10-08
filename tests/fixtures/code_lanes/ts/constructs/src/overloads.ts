// Overload signatures are one function
export function parse(x: string): string;
export function parse(x: number): number;
export function parse(x: unknown) {
  return x;
}
export function useParse() {
  return parse(1);
}
