// synthetic: member calls bind through the receiver's type —
// a parameter annotation, a typed const, a construction, a class field
import { QueryBuilder } from "./queryBuilder";

export class Runner {
  private builder: QueryBuilder;
  constructor(private readonly other: QueryBuilder) {
    this.builder = new QueryBuilder("traces");
  }
  run(view: string, extra: QueryBuilder): string {
    const local: QueryBuilder = new QueryBuilder(view);
    this.builder.build();
    this.other.build();
    extra.build();
    return local.build();
  }
}

// The receiver's type is what the imported factory returns
import { makeBuilder } from "./queryBuilder";
export function viaFactory(view: string): string {
  const built = makeBuilder(view);
  return built.build();
}
