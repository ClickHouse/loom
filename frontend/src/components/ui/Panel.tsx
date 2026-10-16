import { forwardRef, type HTMLAttributes, type ReactNode } from "react";

/**
 * Panel — a bordered surface with an optional header slot and its own
 * scroll. The one card shape, Click UI's card: 4 px corners, the 1 px default stroke,
 * the default or muted background, 16 px padding. With
 * `scroll`, the panel is a flex column that resolves its height from its parent
 * (`min-h-0`) and the body scrolls while the header stays; without it, the panel grows
 * with its content like any block.
 */
export interface PanelProps extends HTMLAttributes<HTMLElement> {
  /** Element rendered; `aside` for a labelled pane, `section` for a titled block. */
  as?: "div" | "section" | "aside";
  /** Rendered above the body, outside the scroll region. */
  header?: ReactNode;
  /** The body owns its scroll; the panel must sit in a bounded box. */
  scroll?: boolean;
  tone?: "surface" | "surface-2";
  padding?: "none" | "sm" | "md";
  /** Extra classes for the body (inside the padding). */
  bodyClassName?: string;
  children?: ReactNode;
}

const PAD = { none: "", sm: "p-2", md: "p-4" } as const;

export const Panel = forwardRef<HTMLElement, PanelProps>(function Panel(
  {
    as: As = "div",
    header,
    scroll = false,
    tone = "surface",
    padding = "md",
    bodyClassName = "",
    className = "",
    children,
    ...rest
  },
  ref,
) {
  const surface = tone === "surface" ? "bg-surface" : "bg-surface-2";
  return (
    <As
      // Polymorphic tag: TS cannot relate ForwardedRef<HTMLElement> to the div|section|aside ref union.
      ref={ref as never}
      className={`rounded-sm border border-border ${surface} ${scroll ? "flex flex-col min-h-0 overflow-hidden" : ""} ${className}`}
      {...rest}
    >
      {header !== undefined && header !== null && (
        <div className={`shrink-0 ${padding === "none" ? "" : `${PAD[padding]} pb-0`}`}>{header}</div>
      )}
      <div
        className={`${scroll ? "min-h-0 flex-1 overflow-y-auto" : ""} ${PAD[padding]} ${header !== undefined && header !== null && padding !== "none" ? "pt-1" : ""} ${bodyClassName}`}
      >
        {children}
      </div>
    </As>
  );
});
