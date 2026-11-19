import { describe, expect, it } from "vitest";
import { SessionCapture, SessionProtection, SessionProtectionUnavailable } from "./session-protection.js";
import { Transport } from "./transport.js";

const wire = 'data: {"mirrored":true}\n\ndata: {"text":"private"}\n\n';
const headers = { "content-type": "text/event-stream" };

function family() {
  const state = { value: "clean" as "clean" | "protected" };
  const root = new SessionProtection({
    async read() {
      return state.value;
    },
    async mark() {
      state.value = "protected";
    },
  });
  return { state, root };
}

describe("protected raw event responses", () => {
  it.each(["reader", "text", "arrayBuffer", "blob", "prototypeText", "clone"])(
    "holds %s bytes until durable protection is acknowledged",
    async (method) => {
      let state: "clean" | "protected" = "clean";
      let entered: (() => void) | undefined;
      let release: (() => void) | undefined;
      const started = new Promise<void>((resolve) => {
        entered = resolve;
      });
      const root = new SessionProtection({
        async read() {
          return state;
        },
        async mark() {
          entered?.();
          await new Promise<void>((resolve) => {
            release = resolve;
          });
          state = "protected";
        },
      });
      const sibling = family();
      const transport = new Transport({
        baseUrl: "http://inert.invalid",
        sessionProtection: root,
        fetch: async () => new Response(wire, { headers }),
        logLevel: "off",
      });
      let returned = false;
      const output = transport
        .stream("/assistant/chat", {}, { capture: new SessionCapture([root, sibling.root]) })
        .then(async (response) => {
          if (method === "prototypeText") return Response.prototype.text.call(response);
          if (method === "text") return response.text();
          if (method === "arrayBuffer") return new TextDecoder().decode(await response.arrayBuffer());
          if (method === "blob") return (await response.blob()).text();
          if (method === "clone") return response.clone().clone().text();
          if (!response.body) throw new Error("missing event body");
          const reader = response.body.getReader();
          let result = "";
          const decoder = new TextDecoder();
          for (;;) {
            const chunk = await reader.read();
            if (chunk.done) break;
            if (!result) expect(state).toBe("protected");
            result += decoder.decode(chunk.value, { stream: true });
          }
          return result;
        })
        .then(
          (value) => {
            returned = true;
            return { value };
          },
          (error: unknown) => {
            returned = true;
            return { error };
          },
        );
      try {
        await started;
        expect(returned).toBe(false);
        expect(sibling.state.value).toBe("protected");
        release?.();
        expect(await output).toEqual({ value: wire });
      } finally {
        release?.();
        await output;
      }
    },
  );

  it.each(['data: {"mirrored":"true"}\n\n', 'data: {"mirrored":true', "data: []\n\n"])(
    "withholds an unverifiable frame %s",
    async (frame) => {
      const { root, state } = family();
      const transport = new Transport({
        baseUrl: "http://inert.invalid",
        sessionProtection: root,
        fetch: async () => new Response(frame, { headers }),
        logLevel: "off",
      });
      await expect(transport.stream("/assistant/chat", {}).then((response) => response.text())).rejects.toBeInstanceOf(
        SessionProtectionUnavailable,
      );
      expect(state.value).toBe("protected");
    },
  );

  it("keeps an ordinary bound stream clean through verified EOF", async () => {
    const { root, state } = family();
    const ordinary = 'data: {"text":"ordinary"}\n\n';
    const transport = new Transport({
      baseUrl: "http://inert.invalid",
      sessionProtection: root,
      fetch: async () => new Response(ordinary, { headers }),
      logLevel: "off",
    });
    expect(await (await transport.stream("/assistant/chat", {})).text()).toBe(ordinary);
    expect(state.value).toBe("clean");
  });

  it("preserves the unbound Response identity and bytes", async () => {
    const response = new Response(wire, { headers });
    const transport = new Transport({ baseUrl: "http://inert.invalid", fetch: async () => response, logLevel: "off" });
    expect(await transport.stream("/assistant/chat", {})).toBe(response);
    expect(await response.text()).toBe(wire);
  });
  it("keeps observation-only streams explicit while protecting every retained family", async () => {
    const retained = family();
    const bound = family();
    await retained.root.mark();
    let sent = 0;
    const transport = new Transport({
      baseUrl: "http://inert.invalid",
      sessionProtection: bound.root,
      logLevel: "off",
      fetch: async () => {
        sent++;
        return new Response(wire, { headers });
      },
    });
    const response = await transport.stream("/assistant/chat", {}, { observe: new SessionCapture([retained.root]) });
    expect(await response.text()).toBe(wire);
    expect(sent).toBe(1);
    expect(retained.state.value).toBe("protected");
    expect(bound.state.value).toBe("protected");
  });
});
