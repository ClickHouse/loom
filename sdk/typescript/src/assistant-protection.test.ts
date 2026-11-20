import { describe, expect, it } from "vitest";
import type { AssistantEvent } from "./assistant.js";
import { LoomClient } from "./client.js";
import {
  AutomaticCaptureSuppressed,
  SessionCapture,
  SessionProtection,
  SessionProtectionUnavailable,
} from "./session-protection.js";

function client(wire: string) {
  let state: "clean" | "protected" = "clean";
  const root = new SessionProtection({
    async read() {
      return state;
    },
    async mark() {
      state = "protected";
    },
  });
  const loom = new LoomClient({
    baseUrl: "http://inert.invalid",
    sessionProtection: root,
    logLevel: "off",
    fetch: async () => new Response(wire, { headers: { "content-type": "text/event-stream" } }),
  });
  return {
    loom,
    get state() {
      return state;
    },
  };
}

describe("protected assistant consumption", () => {
  it.each(["\n", "\r\n", "\r"])("reads multiline %j events after protection and requires DONE", async (newline) => {
    const wire = `data: {"mirrored":true,${newline}data: "id":"answer-1","choices":[{"delta":{"content":"private"}}]}${newline}${newline}data: [DONE]${newline}${newline}`;
    const source = client(wire);
    const seen: AssistantEvent[] = [];
    for await (const event of source.loom.assistant.stream({ question: "q" })) {
      expect(source.state).toBe("protected");
      seen.push(event);
    }
    expect(seen[0]).toEqual({ type: "text", text: "private" });
    expect(seen[1]?.type).toBe("answer");
  });

  it("preserves a constant protection failure instead of changing it into NETWORK", async () => {
    const source = client('data: {"mirrored":"true","text":"private"}\n\n');
    const read = async () => {
      for await (const _event of source.loom.assistant.stream({ question: "q" })) {
        /* consume */
      }
    };
    await expect(read()).rejects.toBeInstanceOf(SessionProtectionUnavailable);
    expect(source.state).toBe("protected");
  });
  it("protects every invocation contributor before yielding typed text", async () => {
    let siblingState: "clean" | "protected" = "clean";
    const sibling = new SessionProtection({
      async read() {
        return siblingState;
      },
      async mark() {
        siblingState = "protected";
      },
    });
    const source = client(
      'data: {"mirrored":true,"id":"answer-1","choices":[{"delta":{"content":"private"}}]}\n\ndata: [DONE]\n\n',
    );
    const input = { question: "q", capture: new SessionCapture([sibling]) };
    for await (const event of source.loom.assistant.stream(input)) {
      expect(siblingState).toBe("protected");
      expect(source.state).toBe("protected");
      if (event.type === "text") expect(event.text).toBe("private");
    }
  });

  it("suppresses an automatic typed request from an already protected contributor", async () => {
    let sent = 0;
    const sibling = new SessionProtection({
      async read() {
        return "protected";
      },
      async mark() {},
    });
    const loom = new LoomClient({
      baseUrl: "http://inert.invalid",
      logLevel: "off",
      fetch: async () => {
        sent++;
        return new Response('data: {"id":"answer-1"}\n\ndata: [DONE]\n\n', {
          headers: { "content-type": "text/event-stream" },
        });
      },
    });
    const input = { question: "q", capture: new SessionCapture([sibling]) };
    const read = async () => {
      for await (const _event of loom.assistant.stream(input)) {
        /* consume */
      }
    };
    await expect(read()).rejects.toBeInstanceOf(AutomaticCaptureSuppressed);
    expect(sent).toBe(0);
  });
});
