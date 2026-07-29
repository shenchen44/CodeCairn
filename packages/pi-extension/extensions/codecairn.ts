import { createHash, randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import { appendFile, mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

type EvidenceReference = {
  path: string;
  statement: string;
  line?: number;
  symbol?: string;
  source_event_id?: string;
};

type Decision = {
  decision_id: string;
  summary: string;
  rationale: string;
  alternatives: string[];
  affected_paths: string[];
  evidence: EvidenceReference[];
  risks: string[];
  verification_plan: string[];
  status: "accepted";
  created_at: string;
};

type GateMode = "block" | "warn" | "off";

const mutatingTools = new Set(["edit", "write"]);
const observedPaths = new Set<string>();
const pendingInspections = new Map<string, string>();
const toolStartedAt = new Map<string, number>();
const partialUpdateAt = new Map<string, number>();
const decisions = new Map<string, Decision>();
let captureQueue: Promise<void> = Promise.resolve();

function gateMode(): GateMode {
  const configured = process.env.CODECAIRN_GATE_MODE?.toLowerCase();
  if (configured === "warn" || configured === "off") return configured;
  return "block";
}

function sanitized(value: unknown, key = "", depth = 0): unknown {
  if (depth > 12) return "[TRUNCATED]";
  if (/authorization|api[_-]?key|access[_-]?token|secret|password|cookie/i.test(key)) {
    return "[REDACTED]";
  }
  if (Array.isArray(value)) {
    return value.slice(0, 500).map((item) => sanitized(item, "", depth + 1));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .slice(0, 200)
        .map(([itemKey, item]) => [itemKey, sanitized(item, itemKey, depth + 1)]),
    );
  }
  if (typeof value === "string") {
    return value
      .replace(
        /(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16})/g,
        "[REDACTED]",
      )
      .slice(0, 50000);
  }
  return value;
}

function repositoryPath(path: string, cwd: string): string {
  const absolute = resolve(cwd, path);
  const root = resolve(cwd);
  if (absolute !== root && !absolute.startsWith(`${root}/`)) {
    throw new Error(`Path is outside the repository: ${path}`);
  }
  return absolute === root ? "." : absolute.slice(root.length + 1);
}

function sessionId(ctx: ExtensionContext): string {
  return ctx.sessionManager.getSessionId() || "unknown-session";
}

function runCapture(cwd: string, payload: Record<string, unknown>): Promise<void> {
  return new Promise((done) => {
    const command = process.env.CODECAIRN_BIN || "cairn";
    const child = spawn(
      command,
      ["capture", "ingest", "--host", "pi", "--repo", cwd],
      { stdio: ["pipe", "ignore", "pipe"] },
    );
    let errorText = "";
    let settled = false;
    const finish = async (failure?: string) => {
      if (settled) return;
      settled = true;
      if (failure) await spool(cwd, payload, failure);
      done();
    };
    child.stderr.on("data", (chunk) => {
      errorText += String(chunk).slice(0, 2000);
    });
    child.on("error", async (error) => {
      await finish(`${error.name}:${error.message}`);
    });
    child.on("close", async (code) => {
      await finish(code === 0 ? undefined : errorText || `exit:${code}`);
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

async function spool(
  cwd: string,
  payload: Record<string, unknown>,
  reason: string,
): Promise<void> {
  const directory = join(homedir(), ".codecairn", "spool", "pi");
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const record = {
    repository: cwd,
    payload: sanitized(payload),
    failure: sanitized(reason),
    spooled_at: new Date().toISOString(),
  };
  await appendFile(
    join(directory, "events.jsonl"),
    `${JSON.stringify(record)}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
}

function record(
  ctx: ExtensionContext,
  eventType: string,
  payload: Record<string, unknown> = {},
  parentEventId?: string,
): string {
  const eventId = `pi_${randomUUID()}`;
  const envelope = sanitized({
    event_id: eventId,
    session_id: sessionId(ctx),
    event_type: eventType,
    parent_event_id: parentEventId,
    timestamp: new Date().toISOString(),
    model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : undefined,
    ...payload,
  }) as Record<string, unknown>;
  captureQueue = captureQueue.then(() => runCapture(ctx.cwd, envelope));
  return eventId;
}

function toolPath(
  input: Record<string, unknown>,
  cwd: string,
): string | undefined {
  const raw = input.path ?? input.file_path;
  if (typeof raw !== "string" || !raw) return undefined;
  return repositoryPath(raw, cwd);
}

function matchingDecision(path: string): Decision | undefined {
  return [...decisions.values()]
    .reverse()
    .find((decision) => decision.affected_paths.includes(path));
}

function isDecision(value: unknown): value is Decision {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<Decision>;
  return (
    typeof candidate.decision_id === "string"
    && typeof candidate.summary === "string"
    && Array.isArray(candidate.affected_paths)
    && candidate.status === "accepted"
  );
}

function updateStatus(ctx: ExtensionContext): void {
  if (ctx.hasUI) {
    ctx.ui.setStatus(
      "codecairn",
      `Cairn: ${decisions.size} decision${decisions.size === 1 ? "" : "s"} · ${gateMode()}`,
    );
  }
}

async function captureGitSnapshot(
  pi: ExtensionAPI,
  ctx: ExtensionContext,
  phase: "session_start" | "agent_settled",
): Promise<void> {
  const [head, branch, status, patch] = await Promise.all([
    pi.exec("git", ["rev-parse", "HEAD"], { cwd: ctx.cwd, timeout: 5000 }),
    pi.exec("git", ["branch", "--show-current"], { cwd: ctx.cwd, timeout: 5000 }),
    pi.exec("git", ["status", "--porcelain"], { cwd: ctx.cwd, timeout: 5000 }),
    pi.exec("git", ["diff", "--no-ext-diff", "--binary", "HEAD"], {
      cwd: ctx.cwd,
      timeout: 10000,
    }),
  ]);
  if (head.code !== 0) return;
  // A successful snapshot establishes repository-level structural evidence.
  // New files can cite "." after the parent repository has been inspected.
  observedPaths.add(".");
  const snapshot = {
    head_sha: head.stdout.trim(),
    branch: branch.stdout.trim(),
    dirty: Boolean(status.stdout.trim()),
    changed_paths: status.stdout
      .split("\n")
      .filter(Boolean)
      .map((line) => line.slice(3).trim())
      .slice(0, 500),
    patch_fingerprint: createHash("sha256").update(patch.stdout).digest("hex"),
  };
  const gitSnapshotId = createHash("sha256")
    .update(JSON.stringify(snapshot))
    .digest("hex");
  record(ctx, "git_snapshot_captured", {
    phase,
    git_snapshot_id: gitSnapshotId,
    snapshot,
  });
}

export default function codecairnExtension(pi: ExtensionAPI): void {
  pi.on("session_start", async (event, ctx) => {
    observedPaths.clear();
    pendingInspections.clear();
    toolStartedAt.clear();
    partialUpdateAt.clear();
    decisions.clear();
    for (const entry of ctx.sessionManager.getEntries()) {
      if (entry.type !== "custom" || entry.customType !== "codecairn.decision") {
        continue;
      }
      const data = entry.data as { decision?: unknown } | undefined;
      if (isDecision(data?.decision)) {
        decisions.set(data.decision.decision_id, data.decision);
      }
    }
    updateStatus(ctx);
    record(ctx, "session_started", {
      reason: event.reason,
      previous_session_file: event.previousSessionFile,
      restored_decision_count: decisions.size,
    });
    await captureGitSnapshot(pi, ctx, "session_start");
  });

  pi.on("before_agent_start", (event, ctx) => {
    record(ctx, "task_submitted", { prompt: event.prompt });
    if (process.env.CODECAIRN_REVIEW_READ_ONLY === "1") {
      return {
        systemPrompt: `${event.systemPrompt}

## CodeCairn read-only review policy

This turn answers a review question. You may inspect repository content, search,
and explain findings. Do not edit or write files and do not invoke shell
commands; mutation-capable tools are blocked for this turn.`,
      };
    }
    return {
      systemPrompt: `${event.systemPrompt}

## CodeCairn proof-carrying change policy

You have a cairn_decision tool. Before every edit or write, you MUST call
cairn_decision and record the implementation summary, rationale, exact affected
paths, evidence, alternatives, risks, and verification plan. Then perform the
mutation. Do not treat this as optional.

For an existing file, inspect the relevant file before recording the decision.
For a new file, inspect the parent directory or a similar project file first;
repository path "." may be cited as structural evidence. If a mutation is
blocked, call cairn_decision and retry the mutation. Never skip the decision
because the requested change appears small.`,
    };
  });

  pi.on("tool_call", async (event, ctx) => {
    if (
      process.env.CODECAIRN_REVIEW_READ_ONLY === "1"
      && ["bash", "edit", "write"].includes(event.toolName)
    ) {
      const reason = (
        `Tool ${event.toolName} is unavailable in a read-only review turn.`
      );
      record(ctx, "read_only_tool_blocked", {
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        reason,
      }, event.toolCallId);
      return { block: true, reason };
    }
    let path: string | undefined;
    try {
      path = toolPath(event.input, ctx.cwd);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      record(ctx, "mutation_blocked", {
        tool_name: event.toolName,
        reason,
      }, event.toolCallId);
      return { block: true, reason };
    }

    if (["read", "grep", "find", "ls"].includes(event.toolName) && path) {
      pendingInspections.set(event.toolCallId, path);
    }

    record(ctx, "tool_started", {
      tool_name: event.toolName,
      tool_call_id: event.toolCallId,
      affected_paths: path ? [path] : [],
      input: event.input,
    }, event.toolCallId);
    toolStartedAt.set(event.toolCallId, Date.now());

    if (!mutatingTools.has(event.toolName) || !path) return undefined;
    const decision = matchingDecision(path);
    if (decision) {
      record(ctx, "mutation_allowed", {
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        affected_paths: [path],
        decision_id: decision.decision_id,
      }, event.toolCallId);
      return undefined;
    }

    const mode = gateMode();
    if (mode === "off") {
      record(ctx, "mutation_allowed", {
        tool_name: event.toolName,
        tool_call_id: event.toolCallId,
        affected_paths: [path],
        decision_id: null,
        gate_mode: mode,
      }, event.toolCallId);
      return undefined;
    }
    const reason = (
      `Mutation blocked: no accepted cairn_decision covers ${path}. `
      + "Call cairn_decision with this affected path, rationale, evidence, "
      + "risks, and verification plan, then retry the mutation. For a new "
      + 'file, inspect its parent directory and cite path "." as evidence.'
    );
    record(ctx, "mutation_blocked", {
      tool_name: event.toolName,
      tool_call_id: event.toolCallId,
      affected_paths: [path],
      reason,
      gate_mode: mode,
    }, event.toolCallId);
    if (ctx.hasUI) ctx.ui.notify(reason, "warning");
    if (mode === "block") {
      return { block: true, reason };
    }
    return undefined;
  });

  pi.on("tool_result", (event, ctx) => {
    const inspectedPath = pendingInspections.get(event.toolCallId);
    if (inspectedPath && !event.isError) observedPaths.add(inspectedPath);
    pendingInspections.delete(event.toolCallId);
    record(ctx, "tool_finished", {
      tool_name: event.toolName,
      tool_call_id: event.toolCallId,
      is_error: event.isError,
      duration_ms: Math.max(
        0,
        Date.now() - (toolStartedAt.get(event.toolCallId) || Date.now()),
      ),
      affected_paths: inspectedPath ? [inspectedPath] : [],
      output: event.content,
    }, event.toolCallId);
    toolStartedAt.delete(event.toolCallId);
    partialUpdateAt.delete(event.toolCallId);
  });

  pi.on("tool_execution_update", (event, ctx) => {
    const now = Date.now();
    const last = partialUpdateAt.get(event.toolCallId) || 0;
    if (now - last < 1000) return;
    partialUpdateAt.set(event.toolCallId, now);
    record(ctx, "tool_progress", {
      tool_name: event.toolName,
      tool_call_id: event.toolCallId,
      partial_result: event.partialResult,
    }, event.toolCallId);
  });

  pi.on("turn_start", (event, ctx) => {
    record(ctx, "turn_started", {
      turn_index: event.turnIndex,
      source_timestamp: event.timestamp,
    });
  });

  pi.on("turn_end", (event, ctx) => {
    record(ctx, "turn_finished", {
      turn_index: event.turnIndex,
      tool_result_count: event.toolResults.length,
    });
  });

  pi.on("agent_settled", async (_event, ctx) => {
    record(ctx, "agent_settled");
    await captureGitSnapshot(pi, ctx, "agent_settled");
    updateStatus(ctx);
  });

  pi.on("session_shutdown", async (event, ctx) => {
    record(ctx, "session_ended", { reason: event.reason });
    await captureQueue;
    if (ctx.hasUI) ctx.ui.setStatus("codecairn", undefined);
  });

  pi.registerTool({
    name: "cairn_decision",
    label: "CodeCairn Decision",
    description:
      "Record the evidence-backed implementation decision before editing or writing files. " +
      "Each affected path must be repository-relative and should have been inspected.",
    promptSnippet: "Record an evidence-backed decision before mutating code.",
    promptGuidelines: [
      "Call cairn_decision before edit or write.",
      "Name exact affected paths, evidence, alternatives, risks, and verification steps.",
    ],
    parameters: Type.Object({
      summary: Type.String({ minLength: 1 }),
      rationale: Type.String({ minLength: 1 }),
      alternatives: Type.Optional(Type.Array(Type.String())),
      affected_paths: Type.Array(Type.String(), { minItems: 1 }),
      evidence: Type.Array(
        Type.Object({
          path: Type.String(),
          statement: Type.String(),
          line: Type.Optional(Type.Number({ minimum: 1 })),
          symbol: Type.Optional(Type.String()),
          source_event_id: Type.Optional(Type.String()),
        }),
      ),
      risks: Type.Optional(Type.Array(Type.String())),
      verification_plan: Type.Array(Type.String(), { minItems: 1 }),
    }),
    execute: async (_toolCallId, params, _signal, _onUpdate, ctx) => {
      const paths = params.affected_paths.map((path) => repositoryPath(path, ctx.cwd));
      const evidence = params.evidence.map((item) => ({
        ...item,
        path: repositoryPath(item.path, ctx.cwd),
      }));
      const unobserved = evidence
        .map((item) => item.path)
        .filter((path) => !observedPaths.has(path));
      if (unobserved.length > 0) {
        throw new Error(
          `Evidence paths must be inspected first: ${[...new Set(unobserved)].join(", ")}`,
        );
      }
      const decision: Decision = {
        decision_id: `decision_${randomUUID()}`,
        summary: params.summary,
        rationale: params.rationale,
        alternatives: params.alternatives || [],
        affected_paths: [...new Set(paths)].sort(),
        evidence,
        risks: params.risks || [],
        verification_plan: params.verification_plan,
        status: "accepted",
        created_at: new Date().toISOString(),
      };
      decisions.set(decision.decision_id, decision);
      updateStatus(ctx);
      const eventId = record(ctx, "decision_recorded", {
        affected_paths: decision.affected_paths,
        decision,
      });
      pi.appendEntry("codecairn.decision", {
        event_id: eventId,
        decision,
      });
      return {
        content: [{
          type: "text",
          text: `Decision ${decision.decision_id} recorded for ${decision.affected_paths.join(", ")}`,
        }],
        details: { decision_id: decision.decision_id, event_id: eventId },
      };
    },
  });

  pi.registerCommand("cairn", {
    description: "Open the CodeCairn review workspace",
    handler: async (args, ctx) => {
      if (args.trim() === "status") {
        ctx.ui.notify(
          `${decisions.size} decisions, ${observedPaths.size} inspected paths`,
          "info",
        );
        return;
      }
      await captureQueue;
      const command = process.env.CODECAIRN_BIN || "cairn";
      const child = spawn(command, ["review", "--repo", ctx.cwd], {
        detached: true,
        stdio: "ignore",
      });
      child.unref();
      ctx.ui.notify("CodeCairn review workspace is opening", "info");
    },
  });
}
