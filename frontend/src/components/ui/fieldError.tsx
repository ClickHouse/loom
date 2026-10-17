import type { ReactNode } from "react";

/**
 * The props that tie an error to its field: spread `{...fieldError("x-error", message)}` on a
 * TextField or TextAreaField instead of passing `error`. Click UI draws the error as a bare
 * <div> beside the input and sets neither `aria-invalid` nor `aria-describedby`. Without a
 * message it adds nothing.
 */
export function fieldError(id: string, message: ReactNode) {
  if (!message) return {};
  return { error: <span id={id}>{message}</span>, "aria-invalid": true, "aria-describedby": id };
}
