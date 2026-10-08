// synthetic: the class whose methods typed.ts's receivers
// resolve to — `build` is a MEMBER definition, so a member call binds to it
// (field-based) and a typed receiver binds to it at 0.9
export class QueryBuilder {
  constructor(private readonly view: string) {}
  build(): string {
    return "built:" + this.view;
  }
}

// A factory's declared return type types the binding its
// caller holds — `const b = makeBuilder(view); b.build()` binds to QueryBuilder::build
export function makeBuilder(view: string): QueryBuilder {
  return new QueryBuilder(view);
}
