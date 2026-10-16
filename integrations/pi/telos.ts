// telos.ts -- Telos for pi (https://github.com/Foadsf/telos).
//
// Install: copy to ~/.pi/agent/extensions/telos.ts (global) and run /reload in an open session.
// Needs `telos` and `telos-mcp` on PATH (`uv tool install git+https://github.com/Foadsf/telos`).
//
// Inside a repository that has a `.telos/` directory this extension:
//   * registers the Telos implementer tools (telos_pulse, telos_checkout_leaf, telos_submit_verification, ...)
//     as native pi tools. They are read from `telos-mcp`'s own tools/list, so names, schemas and the
//     role boundary are exactly the MCP server's: no contract locking, no unquarantine.
//   * shows a footer status (ready / leased / quarantined / integrity) that refreshes after every turn.
//   * adds /telos (state), /telos-schedule (CPM + Monte Carlo) and /telos-next (work the top ready task).
// Human-only actions (contract-lock, unquarantine, attest-done, reseal) stay in an interactive terminal.
// Outside a Telos repository it does nothing.
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const AGENT = "pi";
const STATUS_KEY = "telos";
const CALL_TIMEOUT_MS = 15 * 60 * 1000; // verification runs the project's test suite
const QUICK_TIMEOUT_MS = 60 * 1000;

function findRoot(start: string): string | null {
  let dir = path.resolve(start);
  for (;;) {
    if (fs.existsSync(path.join(dir, ".telos", "config.toml"))) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

type RunResult = { code: number | null; out: string; err: string };

function run(cmd: string, args: string[], stdin: string, timeoutMs: number): Promise<RunResult> {
  return new Promise((resolve) => {
    let out = "";
    let err = "";
    let done = false;
    const finish = (code: number | null) => {
      if (!done) {
        done = true;
        resolve({ code, out, err });
      }
    };
    let child;
    try {
      child = spawn(cmd, args, { windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
    } catch (e) {
      err = String(e);
      finish(127);
      return;
    }
    const timer = setTimeout(() => {
      err += `\ntimed out after ${timeoutMs} ms`;
      try {
        child.kill();
      } catch {}
      finish(124);
    }, timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (c: string) => (out += c));
    child.stderr.on("data", (c: string) => (err += c));
    child.on("error", (e: Error) => {
      clearTimeout(timer);
      err += String(e);
      finish(127);
    });
    child.on("close", (code: number | null) => {
      clearTimeout(timer);
      finish(code);
    });
    child.stdin.on("error", () => {});
    child.stdin.end(stdin);
  });
}

// One JSON-RPC exchange with the implementer MCP server (stdio, one process per call).
async function mcp(root: string, method: string, params: object, timeoutMs: number): Promise<any> {
  const lines = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "pi-telos", version: "1" } } },
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method, params },
  ].map((m) => JSON.stringify(m)).join("\n") + "\n";
  const r = await run("telos-mcp", ["--root", root], lines, timeoutMs);
  for (const line of r.out.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const msg = JSON.parse(line);
      if (msg.id === 2) return msg.result;
    } catch {}
  }
  throw new Error(`telos-mcp gave no answer (exit ${r.code}): ${(r.err || r.out).trim().slice(-400)}`);
}

async function pulseJson(root: string): Promise<any | null> {
  const r = await run("telos", ["--root", root, "--format", "json", "pulse", "--agent", AGENT], "", QUICK_TIMEOUT_MS);
  if (r.code !== 0) return null;
  try {
    return JSON.parse(r.out);
  } catch {
    return null;
  }
}

function statusLine(p: any | null): string {
  if (!p) return "telos: unreadable (run `telos pulse` in a terminal)";
  const ready = (p.ready_mikado_leaves || []).filter((l: any) => l.contract !== false).length;
  const leased = (p.active_leases || []).length;
  const quarantined = (p.quarantined_tasks || []).length;
  const integrity = Object.keys(p.integrity_violations || {}).length;
  const c = p.counts;
  const done = c ? `${c.tasks_done ?? c.done}/${c.tasks_total ?? c.total} done` : "";
  const parts = [`${ready} ready`, `${leased} leased`, `${quarantined} quarantined`];
  if (integrity) parts.push(`${integrity} INTEGRITY VIOLATION${integrity > 1 ? "S" : ""}`);
  if (done) parts.push(done);
  return "telos: " + parts.join(" | ");
}

function describePulse(p: any | null): string {
  if (!p) return "Telos state could not be read. Run `telos pulse` in a terminal to see the error.";
  const out: string[] = ["## Telos", statusLine(p), ""];
  for (const l of (p.ready_mikado_leaves || []).filter((x: any) => x.contract !== false)) {
    out.push(`- ready  ${l.task_id}  ${l.title}  (~${l.est_min} min${l.critical ? ", critical" : ""})`);
  }
  for (const l of p.active_leases || []) {
    out.push(`- leased ${l.task_id}  ${l.title}  token ${l.fencing_token}, strikes ${l.strikes}`);
  }
  for (const b of p.blocked_tasks || []) out.push(`- blocked ${b.task_id}  ${b.title}: ${b.why}`);
  for (const q of p.quarantined_tasks || []) out.push(`- quarantined ${q}  (a human runs \`telos unquarantine\`)`);
  for (const [id, why] of Object.entries(p.integrity_violations || {})) out.push(`- INTEGRITY ${id}: ${why}`);
  out.push("", "Human-only, in an interactive terminal: `telos contract-lock`, `telos unquarantine`, `telos attest-done`, `telos reseal`.");
  return out.join("\n");
}

export default function (pi: ExtensionAPI) {
  let root: string | null = null;
  let registered = false;

  async function refresh(ctx: any) {
    if (!root || !ctx.hasUI) return;
    ctx.ui.setStatus(STATUS_KEY, statusLine(await pulseJson(root)));
  }

  pi.on("session_start", async (_event, ctx) => {
    root = findRoot(ctx.cwd);
    if (!root) return;
    if (!registered) {
      let tools: any[] = [];
      try {
        tools = ((await mcp(root, "tools/list", {}, QUICK_TIMEOUT_MS)) || {}).tools || [];
      } catch (e) {
        if (ctx.hasUI) ctx.ui.notify(`Telos tools unavailable: ${String(e).slice(0, 200)}`, "error");
      }
      for (const t of tools) {
        const name = String(t.name);
        pi.registerTool({
          name,
          label: name.replace(/^telos_/, "Telos ").replace(/_/g, " "),
          description: String(t.description || name),
          promptSnippet: String(t.description || name),
          parameters: Type.Unsafe(t.inputSchema || { type: "object", properties: {} }),
          async execute(_id: string, params: any) {
            const res = await mcp(root as string, "tools/call", { name, arguments: params || {} }, CALL_TIMEOUT_MS);
            const text = ((res && res.content) || []).map((c: any) => c.text || "").join("\n") || "(no output)";
            return { content: [{ type: "text", text }], details: { isError: !!(res && res.isError) } };
          },
        });
      }
      if (tools.length) {
        pi.registerTool({
          name: "telos_protocol",
          label: "Telos protocol",
          description: "Return the rules for working in this Telos repository.",
          promptSnippet: "Rules for working in a Telos-managed repository",
          promptGuidelines: [
            "This repository is managed by Telos: lease a task with telos_checkout_leaf (no task_id = the top ready one), change only the code under test, and close it with telos_submit_verification (next: true leases the following task in the same call). Never edit .telos/, locked tests, conftest.py or pytest configuration. telos_pulse shows what is blocked and why.",
          ],
          parameters: Type.Object({}),
          async execute() {
            return { content: [{ type: "text", text: describePulse(await pulseJson(root as string)) }], details: {} };
          },
        });
      }
      registered = true;
    }
    await refresh(ctx);
  });

  pi.on("turn_end", async (_event, ctx) => {
    await refresh(ctx);
  });

  pi.registerCommand("telos", {
    description: "Telos state of this repository (ready, leased, quarantined, integrity)",
    handler: async (_args, ctx) => {
      if (!root) root = findRoot(ctx.cwd);
      if (!root) {
        ctx.ui.notify("No .telos/ here: not a Telos repository (`telos init` creates one).", "warning");
        return;
      }
      const p = await pulseJson(root);
      pi.sendMessage({ customType: "telos", content: describePulse(p), display: true });
      await refresh(ctx);
    },
  });

  pi.registerCommand("telos-schedule", {
    description: "Telos critical path and Monte Carlo schedule (P50/P80/P95)",
    handler: async (_args, ctx) => {
      if (!root) root = findRoot(ctx.cwd);
      if (!root) {
        ctx.ui.notify("No .telos/ here: not a Telos repository.", "warning");
        return;
      }
      const res = await mcp(root, "tools/call", { name: "telos_schedule_pert", arguments: { trials: 2000 } }, QUICK_TIMEOUT_MS);
      const text = ((res && res.content) || []).map((c: any) => c.text || "").join("\n");
      pi.sendMessage({ customType: "telos", content: "## Telos schedule\n" + text, display: true });
    },
  });

  pi.registerCommand("telos-next", {
    description: "Lease the top ready Telos task and work it through verification",
    handler: async (args, ctx) => {
      if (!root) root = findRoot(ctx.cwd);
      if (!root) {
        ctx.ui.notify("No .telos/ here: not a Telos repository.", "warning");
        return;
      }
      const text =
        "Work the next Telos task: lease it with telos_checkout_leaf (no task_id), change only the code under " +
        "test, and close it with telos_submit_verification. Report the state and receipt Telos returns. If " +
        "nothing can be leased, say why and stop." + (args ? `\nFocus: ${args}` : "");
      if (ctx.isIdle()) pi.sendUserMessage(text);
      else pi.sendUserMessage(text, { deliverAs: "followUp" });
    },
  });
}
