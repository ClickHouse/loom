// The host's hook payload as text. Its own module with no imports: the
// recording launcher (hooks/loom-record.mjs) reads the payload and returns,
// and loads nothing else on the way.

const MAX_STDIN_CHARS = 32 * 1024 * 1024;

/** Stdin read to its end: "" when it is empty, cannot be read or holds more
 *  than MAX_STDIN_CHARS (an oversized payload is dropped, not parsed). Gives
 *  up waiting after a second with what arrived, so a host that never closes
 *  stdin cannot hold a hook open. */
export function readStdinText() {
  return new Promise((resolve) => {
    let raw = "";
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      resolve(raw);
    };
    try {
      process.stdin.setEncoding("utf8");
      process.stdin.on("data", (c) => {
        raw += c;
        if (raw.length > MAX_STDIN_CHARS) {
          raw = "";
          done();
          process.stdin.destroy();
        }
      });
      process.stdin.on("end", done);
      process.stdin.on("error", done);
    } catch {
      done();
    }
    setTimeout(done, 1000).unref();
  });
}
