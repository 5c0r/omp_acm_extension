import { basename } from "node:path";

import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

import { acmRequest } from "./client";

type MessagePayload = { role: string; content: string };
type Probe = {
  question: string;
  reference_answer: string;
  summary_answer: string;
  verdict: "correct" | "partial" | "wrong";
};
type CompactResponse = {
  summary: string;
  validation_score: number;
  compression_ratio: number;
  probes: Probe[];
};

function messages(messages: readonly unknown[]): MessagePayload[] {
  return messages.map(message => {
    if (typeof message === "object" && message !== null) {
      const value = message as { role?: unknown; content?: unknown; summary?: unknown };
      const role = typeof value.role === "string" ? value.role : "unknown";
      const isCompactionSummary = role === "compactionSummary";
      const content = typeof value.content === "string"
        ? value.content
        : isCompactionSummary && typeof value.summary === "string"
          ? value.summary
          : JSON.stringify(value.content) ?? "";
      return { role: isCompactionSummary ? "assistant" : role, content };
    }
    return { role: "unknown", content: String(message) };
  });
}

function fileOps(value: unknown): { read: string[]; written: string[]; edited: string[] } {
  const operations = value as { read?: unknown; written?: unknown; edited?: unknown } | undefined;
  const paths = (key: "read" | "written" | "edited") => {
    const raw = operations?.[key];
    return raw instanceof Set ? [...raw].filter((path): path is string => typeof path === "string") : [];
  };
  return { read: paths("read"), written: paths("written"), edited: paths("edited") };
}
function acmPreserveData(previous: Record<string, unknown> | undefined, response: CompactResponse) {
  const preserved = Object.fromEntries(
    Object.entries(previous ?? {}).filter(([key]) => key !== "openaiRemoteCompaction" && key !== "snapcompact"),
  );
  return {
    ...preserved,
    acm: {
      validationScore: response.validation_score,
      compressionRatio: response.compression_ratio,
      probes: response.probes,
    },
  };
}
function projectScope(cwd: string) {
  try {
    const result = Bun.spawnSync(["git", "-C", cwd, "rev-parse", "--show-toplevel"]);
    if (result.exitCode === 0) return `project:${basename(new TextDecoder().decode(result.stdout).trim()).toLowerCase()}`;
  } catch {}
  return `project:${basename(cwd).toLowerCase()}`;
}
function toolResult(response: unknown) {
  return {
    content: [{ type: "text" as const, text: response === null ? "ACM service unavailable" : JSON.stringify(response) }],
    details: response ?? {},
  };
}




export default function acmExtension(pi: ExtensionAPI) {
  pi.setLabel("Agentic Context Management");
  const harvested = new Map<string, { seen: Set<string>; fifo: string[] }>();
  const latestUser = new Map<string, string>();
  let autoInject = process.env.ACM_AUTO_INJECT !== "0";
  let lastCompaction: CompactResponse | undefined;
  const z = pi.zod;

  pi.registerTool({
    name: "acm_fetch",
    label: "ACM Fetch",
    description: "Retrieve scoped ACM memories.",
    parameters: z.object({ query: z.string(), budget_tokens: z.number().optional(), deep: z.boolean().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      return toolResult(await acmRequest("/fetch", "POST", {
        query: params.query,
        scope: projectScope(ctx.cwd),
        budget_tokens: params.budget_tokens ?? 1500,
        deep: params.deep ?? false,
      }, signal));
    },
  });
  pi.registerTool({
    name: "acm_ingest",
    label: "ACM Ingest",
    description: "Queue scoped knowledge for ACM ingestion.",
    parameters: z.object({ text: z.string(), source_ref: z.string().optional(), scope: z.string().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      return toolResult(await acmRequest("/ingest", "POST", {
        scope: params.scope ?? projectScope(ctx.cwd),
        text: params.text,
        source_ref: params.source_ref ?? `session:${ctx.sessionManager.getSessionId()}`,
      }, signal));
    },
  });
  pi.registerTool({
    name: "acm_compact",
    label: "ACM Compact",
    description: "Create a validated ACM conversation summary.",
    parameters: z.object({
      conversation: z.string(),
      budget_tokens: z.number().optional(),
      previous_summary: z.string().optional(),
      turn_prefix: z.string().optional(),
      custom_instructions: z.string().optional(),
      file_ops: z.object({
        read: z.array(z.string()).optional(),
        written: z.array(z.string()).optional(),
        edited: z.array(z.string()).optional(),
      }).optional(),
    }),
    async execute(_id, params, signal) {
      return toolResult(await acmRequest("/compact", "POST", {
        conversation: [{ role: "user", content: params.conversation }],
        budget_tokens: params.budget_tokens ?? 1500,
        previous_summary: params.previous_summary,
        turn_prefix: params.turn_prefix ? [{ role: "user", content: params.turn_prefix }] : undefined,
        file_ops: params.file_ops,
        custom_instructions: params.custom_instructions,
      }, signal));
    },
  });
  pi.registerTool({
    name: "acm_architect",
    label: "ACM Architect",
    description: "Generate scoped ACM memory architecture.",
    parameters: z.object({ description: z.string(), reference: z.string().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      return toolResult(await acmRequest("/architect", "POST", {
        scope: projectScope(ctx.cwd),
        description: params.description,
        reference: params.reference,
      }, signal));
    },
  });
  pi.registerTool({
    name: "acm_status",
    label: "ACM Status",
    description: "Read ACM ingestion job status or aggregate stats.",
    parameters: z.object({ job_id: z.number().optional() }),
    async execute(_id, params, signal) {
      return toolResult(await acmRequest(params.job_id === undefined ? "/stats" : `/status/${params.job_id}`, "GET", undefined, signal));
    },
  });
  pi.registerTool({
    name: "acm_consolidate",
    label: "ACM Consolidate",
    description: "Consolidate scoped ACM memories.",
    parameters: z.object({ scope: z.string().optional() }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      return toolResult(await acmRequest("/consolidate", "POST", { scope: params.scope ?? projectScope(ctx.cwd) }, signal));
    },
  });

  pi.registerCommand("acm", {
    description: "ACM status, selfcheck, inject, or last-compaction.",
    handler: async (args, ctx) => {
      const [command, value] = String(args).trim().split(/\s+/, 2);
      if (command === "status") {
        const [health, stats] = await Promise.all([
          acmRequest<{ status?: string }>("/health"),
          acmRequest<{ stats?: Record<string, unknown> }>("/stats"),
        ]);
        const values = stats?.stats ?? {};
        ctx.ui.notify(`ACM status: ${health?.status ?? "unavailable"}; bundle_injected=${values.bundle_injected ?? 0}; explicit_fetch=${values.explicit_fetch ?? 0}`, "info");
        return;
      }
      if (command === "selfcheck") {
        const sessionId = ctx.sessionManager.getSessionId();
        const scope = `project:acm-selfcheck-${sessionId.toLowerCase().replace(/[^a-z0-9_-]/g, "-")}`;
        const selfcheckSessionId = `acm-selfcheck-${sessionId}`;
        const health = await acmRequest("/health");
        const architect = await acmRequest("/architect", "POST", { scope, description: "ACM selfcheck" });
        const ingest = await acmRequest<{ job_id?: unknown }>("/ingest", "POST", {
          scope,
          text: "ACM selfcheck",
          source_ref: `selfcheck:${sessionId}`,
        });
        const jobId = typeof ingest?.job_id === "number" ? ingest.job_id : 0;
        const status = await acmRequest(`/status/${jobId}`);
        const fetch = await acmRequest("/fetch", "POST", { query: "ACM selfcheck", scope, budget_tokens: 1, deep: false });
        const anticipate = await acmRequest("/anticipate", "POST", {
          session_id: selfcheckSessionId,
          scope,
          trajectory: [{ role: "user", content: "ACM selfcheck" }],
        });
        let bundle = null;
        for (let attempt = 0; attempt < 15 && !bundle; attempt += 1) {
          bundle = await acmRequest(`/bundle/${selfcheckSessionId}`);
          if (!bundle && attempt < 14) await Bun.sleep(Math.min(250 * 2 ** attempt, 5_000));
        }
        const compact = await acmRequest("/compact", "POST", {
          conversation: [{ role: "user", content: "ACM selfcheck" }],
          budget_tokens: 1,
        });
        const consolidate = await acmRequest("/consolidate", "POST", { scope });
        const stats = await acmRequest("/stats");
        const checks = { health, architect, ingest, status, fetch, anticipate, bundle, compact, consolidate, stats };
        const passed = Object.values(checks).every(Boolean);
        ctx.ui.notify(
          `ACM selfcheck\n${Object.entries(checks).map(([name, result]) => `${name.padEnd(13)}${result ? "PASS" : "FAIL"}`).join("\n")}`,
          passed ? "info" : "error",
        );
        return;
      }
      if (command === "inject" && (value === "on" || value === "off")) {
        autoInject = value === "on";
        ctx.ui.notify(`ACM injection ${value}`, "info");
        return;
      }
      if (command === "last-compaction") {
        ctx.ui.notify(lastCompaction
          ? `ACM last-compaction: score ${lastCompaction.validation_score.toFixed(2)}, ratio ${lastCompaction.compression_ratio.toFixed(2)}`
          : "ACM last-compaction: none", "info");
        return;
      }
      ctx.ui.notify("ACM commands: status, selfcheck, inject on|off, last-compaction", "info");
    },
  });


  pi.on("session_before_compact", async (event, _ctx) => {
    if (event.signal.aborted) return {};
    const preparation = event.preparation;
    const response = await acmRequest<CompactResponse>("/compact", "POST", {
      conversation: messages(preparation.messagesToSummarize),
      turn_prefix: preparation.turnPrefixMessages.length ? messages(preparation.turnPrefixMessages) : undefined,
      previous_summary: preparation.previousSummary,
      custom_instructions: event.customInstructions,
      file_ops: fileOps(preparation.fileOps),

      budget_tokens: 1500,
    }, event.signal);
    if (!response || response.validation_score < 0.8) return {};
    lastCompaction = response;
    return {
      compaction: {
        summary: response.summary,
        shortSummary: `ACM validated (score ${response.validation_score.toFixed(2)}, ratio ${response.compression_ratio.toFixed(2)})`,
        firstKeptEntryId: preparation.firstKeptEntryId,
        tokensBefore: preparation.tokensBefore,
        preserveData: acmPreserveData(preparation.previousPreserveData, response),
      },
    };
  });

  pi.on("turn_end", (event, ctx) => {
    const sessionId = ctx.sessionManager.getSessionId();
    const assistant = messages([event.message])[0];
    const trajectory = [
      ...(latestUser.has(sessionId) ? [{ role: "user", content: latestUser.get(sessionId)! }] : []),
      ...(assistant ? [{ role: "assistant", content: assistant.content }] : []),
      ...event.toolResults.map(result => {
        const value = result as { toolName?: unknown; content?: unknown };
        const content = typeof value.content === "string" ? value.content : JSON.stringify(value.content);
        return { role: "tool", content: `${typeof value.toolName === "string" ? value.toolName : "tool"}: ${content}`.slice(0, 512) };
      }),
    ];
    if (!trajectory.length) return;
    ctx.setTimeout(
      async () => {
        await acmRequest("/anticipate", "POST", {
          session_id: sessionId,
          scope: projectScope(ctx.cwd),
          trajectory,
        });
      },
      0,
    );
  });

  pi.on("agent_end", (event, ctx) => {
    const sessionId = ctx.sessionManager.getSessionId();
    const history = messages(event.messages);
    const seen = harvested.get(sessionId) ?? { seen: new Set<string>(), fifo: [] };
    const additions = history.filter(message => {
      if (message.role !== "user" && message.role !== "assistant") return false;
      const fingerprint = Bun.hash(`${message.role}|${JSON.stringify(message.content)}`).toString();
      if (seen.seen.has(fingerprint)) return false;
      seen.seen.add(fingerprint);
      seen.fifo.push(fingerprint);
      if (seen.fifo.length > 512) seen.seen.delete(seen.fifo.shift()!);
      return true;
    });
    if (!additions.length) return;
    harvested.set(sessionId, seen);
    const transcript = additions.map(message => `${message.role}: ${message.content}`).join("\n");
    ctx.setTimeout(
      async () => {
        await acmRequest("/ingest", "POST", {
          scope: projectScope(ctx.cwd),
          source_ref: `session:${sessionId}`,
          text: transcript,
        });
      },
      0,
    );
  });

  pi.on("context", async (event, ctx) => {
    const sessionId = ctx.sessionManager.getSessionId();
    const user = messages(event.messages).reverse().find(message => message.role === "user");
    if (user) latestUser.set(sessionId, user.content);
    if (!autoInject) return {};
    const bundle = await acmRequest<{ rendered?: unknown }>(`/bundle/${sessionId}`);
    if (typeof bundle?.rendered !== "string" || !bundle.rendered.trim()) return {};
    return { messages: [{ role: "user", content: bundle.rendered }, ...event.messages] };
  });
}
