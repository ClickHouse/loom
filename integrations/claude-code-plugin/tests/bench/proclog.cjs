// Preloaded (NODE_OPTIONS=--require) into every node process a hook starts,
// detached ones included: one line at start and one at exit, so the harness
// can count processes and tell how long each lived.
const fs = require("node:fs");
const log = process.env.LOOM_BENCH_PROCLOG;
if (log) {
  const script = (process.argv[1] || "").split("/").pop();
  const line = (o) => {
    try {
      fs.appendFileSync(log, JSON.stringify(o) + "\n");
    } catch {
      /* a lost line shows up as a process with no exit */
    }
  };
  const boot = process.uptime() * 1000;
  line({
    k: "start",
    pid: process.pid,
    ppid: process.ppid,
    script,
    event: process.argv[2] || "",
    boot,
    at: Date.now(),
  });
  // `cpu` is the process's own user and system time: unlike `life`, it does
  // not grow with what else the machine is running.
  const cpu = () => {
    const u = process.cpuUsage();
    return (u.user + u.system) / 1000;
  };
  process.on("exit", () =>
    line({ k: "exit", pid: process.pid, life: process.uptime() * 1000, cpu: cpu(), at: Date.now() }),
  );
}
