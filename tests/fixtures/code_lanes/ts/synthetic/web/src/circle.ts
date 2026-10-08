import { Base, Shape } from './shapes';

// An imported class and interface bind through the import
export class Circle extends Base implements Shape {
  constructor(private readonly r: number) {
    super();
  }
  area(): number {
    return Math.PI * this.r * this.r;
  }
}

class LocalBase {
  tag(): string {
    return 'local';
  }
}

// a same-file base binds at 1.0 whatever the repository calls its own LocalBase
export class Tagged extends LocalBase {
  tag(): string {
    return 'tagged';
  }
}

// a runtime global is a library base: no edge
export class ShapeError extends Error {}
