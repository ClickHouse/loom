// The plugin's `toolset` option, read as the server reads a toolset value.

/** The lower-cased, de-duplicated parts of a toolset value, in the order
 *  given: parts split on `+`, a space or a comma, as the server's
 *  `toolset_parts` splits them. An unknown part takes the deployment's default
 *  toolset on the server, which a hook cannot know. */
export function toolsetParts(toolset) {
  return [
    ...new Set(
      String(toolset || "")
        .toLowerCase()
        .split(/[+ ,]+/)
        .filter(Boolean),
    ),
  ];
}

/** Whether the server reads the value as no toolset it serves: only curated
 *  rosters join a union, so one holding `all`, `readonly` or `assistant` (each
 *  a whole listing) takes the deployment's default toolset. */
export function unknownUnion(parts) {
  return parts.length > 1 && parts.some((p) => ["all", "readonly", "assistant"].includes(p));
}
