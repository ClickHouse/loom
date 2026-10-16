// Testing Library for the specs: `render` wraps every tree in the tooltip provider main.tsx
// mounts around the app, so a component renders under test as it does in the app. Specs
// import from here, not from "@testing-library/react".
import * as RadixTooltip from "@radix-ui/react-tooltip";
import type { ReactElement, ReactNode } from "react";
import { render as baseRender, type RenderOptions } from "@testing-library/react/pure";

export * from "@testing-library/react/pure";

export function render(ui: ReactElement, options: RenderOptions = {}) {
  const Outer = options.wrapper;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <RadixTooltip.Provider delayDuration={300}>{Outer ? <Outer>{children}</Outer> : children}</RadixTooltip.Provider>
  );
  return baseRender(ui, { ...options, wrapper });
}
