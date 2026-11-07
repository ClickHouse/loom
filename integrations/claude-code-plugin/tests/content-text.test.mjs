import "./temp-home.mjs";
import { test } from "node:test";
import assert from "node:assert/strict";
import { blockText, contentText, toolImageTurn, toolResultImageCount } from "../lib/loom-client.mjs";

const png = { type: "image", source: { type: "base64", media_type: "image/png", data: "iVBORw0KGgo=" } };

test("an image block becomes a placeholder that keeps the pointer and drops the bytes", () => {
  assert.equal(blockText(png), "[image: image/png]");
  assert.equal(
    blockText({ type: "image", source: { type: "url", url: "https://x.test/a.jpg", media_type: "image/jpeg" } }),
    "[image: image/jpeg https://x.test/a.jpg]",
  );
  assert.equal(blockText({ type: "image" }), "[image: image]");
  assert.ok(!blockText(png).includes("iVBOR"));
});

test("scaffolding blocks yield nothing; text passes through", () => {
  assert.equal(blockText({ type: "tool_use", name: "Read" }), "");
  assert.equal(blockText({ type: "thinking", thinking: "…" }), "");
  assert.equal(blockText({ type: "tool_result", content: "…" }), "");
  assert.equal(blockText({ type: "text", text: "hi" }), "hi");
  assert.equal(blockText(null), "");
});

test("contentText joins a pasted image beside its text and passes strings through", () => {
  assert.equal(contentText("plain"), "plain");
  assert.equal(contentText([{ type: "text", text: "what is this?" }, png]), "what is this?\n[image: image/png]");
  assert.equal(contentText([{ type: "tool_use" }]), "");
  assert.equal(contentText(undefined), "");
});

test("images returned by tools are counted and become one tool placeholder turn", () => {
  const content = [{ type: "tool_result", tool_use_id: "t1", content: [png, { type: "text", text: "ok" }] }];
  assert.equal(toolResultImageCount(content), 1);
  assert.deepEqual(toolImageTurn(content), { role: "tool", content: "[image: 1 image result returned by a tool]" });
  const two = [{ type: "tool_result", content: [png, png] }];
  assert.equal(toolImageTurn(two).content, "[image: 2 image results returned by a tool]");
  assert.equal(toolImageTurn([{ type: "tool_result", content: "text only" }]), null);
  assert.equal(toolImageTurn("string"), null);
});
