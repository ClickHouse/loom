import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const child = spawn(
  path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
  [
    "-NoLogo",
    "-NoProfile",
    "-NonInteractive",
    "-File",
    fileURLToPath(new URL("./windows-job-witness.ps1", import.meta.url)),
  ],
  // Guardian diagnostics must survive this relay's deliberate termination.
  { stdio: ["pipe", "pipe", "inherit"], windowsHide: true },
);
child.stdin.end();
child.stdout.pipe(process.stdout);
child.on("error", (error) => {
  throw error;
});
child.on("exit", (code) => process.exit(code ?? 1));
