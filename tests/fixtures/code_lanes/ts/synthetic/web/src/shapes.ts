// Heritage bases for the hierarchy fixture
export interface Shape {
  area(): number;
}

export abstract class Base {
  abstract area(): number;
  describe(): string {
    return `area ${this.area()}`;
  }
}
