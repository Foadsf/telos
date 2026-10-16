// telos.js -- Telos status toasts for OpenCode (https://github.com/Foadsf/telos).
//
// Install: copy to ~/.config/opencode/plugins/telos.js and restart OpenCode.
// Needs `telos` on PATH. The Telos tools themselves come from the MCP server that
// `telos bootstrap-agents` registers in the project's opencode.json.
//
// In a repository with a `.telos/` directory, when a session starts and whenever the agent goes
// idle, this reads `telos pulse` and shows a TUI toast: what is ready, leased and quarantined, and
// any integrity violation (as an error toast). It changes nothing and does nothing elsewhere.
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

const TIMEOUT_MS = 60000;

function findRoot(start) {
  let dir = path.resolve(start);
  for (;;) {
    if (fs.existsSync(path.join(dir, ".telos", "config.toml"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function pulse(root) {
  return new Promise((resolve) => {
    let out = "";
    let done = false;
    const finish = (v) => {
      if (!done) {
        done = true;
        resolve(v);
      }
    };
    let child;
    try {
      child = spawn("telos", ["--root", root, "--format", "json", "pulse", "--agent", "opencode"], {
        windowsHide: true,
        stdio: ["ignore", "pipe", "ignore"],
      });
    } catch {
      finish(null);
      return;
    }
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {}
      finish(null);
    }, TIMEOUT_MS);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (c) => (out += c));
    child.on("error", () => {
      clearTimeout(timer);
      finish(null);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (code !== 0) return finish(null);
      try {
        finish(JSON.parse(out));
      } catch {
        finish(null);
      }
    });
  });
}

function summarize(p) {
  if (!p) return { variant: "warning", message: "Telos state unreadable; run `telos pulse` in a terminal." };
  const leasable = (p.ready_mikado_leaves || []).filter((l) => l.contract !== false);
  const ready = leasable.length;
  const leased = (p.active_leases || []).length;
  const quarantined = (p.quarantined_tasks || []).length;
  const integrity = Object.keys(p.integrity_violations || {}).length;
  const c = p.counts;
  const counts = c ? ` | ${c.tasks_done ?? c.done}/${c.tasks_total ?? c.total} done` : "";
  let message = `${ready} ready | ${leased} leased | ${quarantined} quarantined${counts}`;
  const top = leasable[0];
  if (top) message += `\nnext: ${top.title}`;
  if (integrity) {
    return { variant: "error", message: `${integrity} integrity violation(s): state edited outside Telos. ${message}` };
  }
  return { variant: quarantined ? "warning" : "info", message };
}

export default {
  id: "telos",
  server: async ({ directory, client }) => {
    const root = findRoot(directory);
    if (!root) return {};
    let last = "";

    async function show(force) {
      const s = summarize(await pulse(root));
      if (!force && s.message === last) return;
      last = s.message;
      try {
        await client.tui.showToast({ body: { title: "Telos", message: s.message, variant: s.variant, duration: 6000 } });
      } catch {}
    }

    return {
      event: async ({ event }) => {
        const type = event && event.type;
        if (type === "session.created") await show(true);
        else if (type === "session.idle") await show(false);
      },
    };
  },
};
