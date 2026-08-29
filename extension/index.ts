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

type BrowseMemory = {
  id: number;
  kind: string;
  content: string;
  scope: string;
  status: string;
  importance: number;
  pinned: boolean;
  entities?: Array<{ id: number; canonical_name: string; aliases?: string[] }>;
};

const MAX_HARVEST_BYTES = 24 * 1024;

function textContent(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return undefined;
  const text = value.flatMap(block => {
    if (typeof block !== "object" || block === null) return [];
    const item = block as { type?: unknown; text?: unknown };
    return item.type === "text" && typeof item.text === "string" ? [item.text] : [];
  }).join("\n");
  return text.trim() ? text : undefined;
}

function messages(messages: readonly unknown[]): MessagePayload[] {
  return messages.flatMap(message => {
    if (typeof message !== "object" || message === null) return [];
    const value = message as { role?: unknown; content?: unknown; summary?: unknown };
    const role = typeof value.role === "string" ? value.role : "unknown";
    const content = role === "compactionSummary" && typeof value.summary === "string"
      ? value.summary
      : textContent(value.content);
    return content?.trim() ? [{ role: role === "compactionSummary" ? "assistant" : role, content }] : [];
  });
}

function capHarvestText(text: string): string {
  let bytes = 0;
  for (let index = 0; index < text.length;) {
    const codePoint = text.codePointAt(index)!;
    const width = codePoint <= 0x7f ? 1 : codePoint <= 0x7ff ? 2 : codePoint <= 0xffff ? 3 : 4;
    if (bytes + width > MAX_HARVEST_BYTES) return text.slice(0, index);
    bytes += width;
    index += codePoint > 0xffff ? 2 : 1;
  }
  return text;
}

function fileOps(value: unknown): { read: string[]; written: string[]; edited: string[] } {
  const operations = value as { read?: unknown; written?: unknown; edited?: unknown } | undefined;
  const paths = (key: "read" | "written" | "edited") => {
    const raw = operations?.[key];
    return raw instanceof Set ? [...raw].filter((path): path is string => typeof path === "string") : [];
  };
  return { read: paths("read"), written: paths("written"), edited: paths("edited") };
}

type AcmMode = "full" | "memory" | "compaction";
type ModePreset = { ingest: boolean; anticipate: boolean; compactHook: boolean };
type ResolvedMode = ModePreset & { mode: AcmMode };

const MODE_PRESETS: Record<AcmMode, ModePreset> = {
  full: { ingest: true, anticipate: true, compactHook: true },
  memory: { ingest: true, anticipate: true, compactHook: false },
  compaction: { ingest: false, anticipate: false, compactHook: true },
};

function resolveMode(pi: ExtensionAPI): ResolvedMode {
  const flag = pi.getFlag("acm-mode");
  const raw = process.env.ACM_MODE ?? (typeof flag === "string" && flag ? flag : undefined);
  if (raw === undefined) return { mode: "full", ...MODE_PRESETS.full };
  if (raw in MODE_PRESETS) {
    const mode = raw as AcmMode;
    return { mode, ...MODE_PRESETS[mode] };
  }
  pi.logger.warn(`ACM mode '${raw}' unknown; falling back to 'full' (expected full | memory | compaction)`);
  return { mode: "full", ...MODE_PRESETS.full };
}


export function shouldAutoArm(
  state: { aboveThreshold: boolean; digest?: string; armedAt?: number },
  usageRatio: number,
  digest: string,
  now: number,
) {
  if (usageRatio <= 0.6) {
    state.aboveThreshold = false;
    return false;
  }
  if (state.aboveThreshold) return false;
  state.aboveThreshold = true;
  if (state.digest === digest && now - (state.armedAt ?? 0) < 600_000) return false;
  state.digest = digest;
  state.armedAt = now;
  return true;
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






export type StatusState = {
  mode: AcmMode;
  bundleHits: number;
  bundleMisses: number;
  ingestCount: number;
  lastCompaction?: CompactResponse;
  online?: boolean;
};

export function renderStatusLine(state: StatusState): string {
  if (state.online === false) return `ACM ${state.mode} · offline`;
  const bundle = state.mode === "compaction" ? "—" : `✓${state.bundleHits} ✗${state.bundleMisses}`;
  const compact = state.lastCompaction
    ? `${state.lastCompaction.validation_score.toFixed(2)}/${state.lastCompaction.compression_ratio.toFixed(2)}`
    : "—";
  return `ACM ${state.mode} · bundle ${bundle} · ingest ${state.ingestCount} · last compact ${compact}`;
}


export default function acmExtension(pi: ExtensionAPI) {
  pi.setLabel("Agentic Context Management");
  pi.registerFlag("acm-mode", { description: "ACM subsystem preset: full | memory | compaction", type: "string" });
  let resolvedMode: ResolvedMode | undefined;
  const getMode = () => resolvedMode ??= resolveMode(pi);
  const harvested = new Map<string, { seen: Set<string>; fifo: string[] }>();
  const latestUser = new Map<string, string>();
  let autoInject = process.env.ACM_AUTO_INJECT !== "0";
  const autoArmEnabled = process.env.ACM_AUTO_ARM === "1";
  let lastCompaction: CompactResponse | undefined;
  const statusLineEnabled = process.env.ACM_WIDGET !== "0";
  let bundleHits = 0;
  let bundleMisses = 0;
  let ingestCount = 0;
  let serviceOnline: boolean | undefined;
  let lastStatusLine: string | undefined;
  const setStatusBar = (
    ctx: { hasUI?: boolean; ui?: { setStatus?: (key: string, text: string | undefined) => void } },
    text: string,
  ) => {
    if (!statusLineEnabled || !ctx.hasUI || !ctx.ui?.setStatus) return;
    try {
      ctx.ui.setStatus("acm", text);
      lastStatusLine = text;
    } catch {
      // ponytail: status line is cosmetic — a failing UI surface must never break the handler
    }
  };
  const updateStatus = (ctx: { hasUI?: boolean; ui?: { setStatus?: (key: string, text: string | undefined) => void } }) => {
    const line = renderStatusLine({
      mode: getMode().mode,
      bundleHits,
      bundleMisses,
      ingestCount,
      lastCompaction,
      online: serviceOnline,
    });
    if (line !== lastStatusLine) setStatusBar(ctx, line);
  };
  pi.on("session_start", async (_event, ctx) => {
    if (!statusLineEnabled || !ctx.hasUI) return;
    serviceOnline = (await acmRequest("/health")) !== null;
    setStatusBar(ctx, `ACM ${getMode().mode} · ${serviceOnline ? "connected" : "offline"}`);
  });
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
        session_id: ctx.sessionManager.getSessionId(),
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
    async execute(_id, params, signal, _onUpdate, ctx) {
      const response = await acmRequest<CompactResponse>("/compact", "POST", {
        scope: projectScope(ctx.cwd),
        session_id: ctx.sessionManager.getSessionId(),
        conversation: [{ role: "user", content: params.conversation }],
        budget_tokens: params.budget_tokens ?? 1500,
        previous_summary: params.previous_summary,
        turn_prefix: params.turn_prefix ? [{ role: "user", content: params.turn_prefix }] : undefined,
        file_ops: params.file_ops,
        custom_instructions: params.custom_instructions,
      }, signal);
      if (response?.validation_score >= 0.8) {
        serviceOnline = true;
        lastCompaction = response;
        updateStatus(ctx);
      }
      return toolResult(response);
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
    description: "ACM status, browse, selfcheck, inject, or last-compaction.",
    handler: async (args, ctx) => {
      const [command, value] = String(args).trim().split(/\s+/, 2);
      if (command === "status") {
        const mode = getMode();
        const [health, stats] = await Promise.all([
          acmRequest<{ status?: string }>("/health"),
          acmRequest<{ stats?: Record<string, unknown> }>("/stats"),
        ]);
        const values = stats?.stats ?? {};
        ctx.ui.notify(`ACM status: ${health?.status ?? "unavailable"}; mode=${mode.mode}; bundle_injected=${values.bundle_injected ?? 0}; explicit_fetch=${values.explicit_fetch ?? 0}; session ✓${bundleHits} ✗${bundleMisses}; ingest ${ingestCount}`, "info");
        return;
      }
      if (command === "browse") {
        const dashboard = await acmRequest<{ totals?: { scope?: Record<string, number> } }>("/api/ui/dashboard");
        const scopes = Object.entries(dashboard?.totals?.scope ?? {}).sort(([left], [right]) => left.localeCompare(right));
        if (!scopes.length) {
          ctx.ui.notify("ACM browse: no memories.", "info");
          return;
        }
        if (!ctx.hasUI) {
          ctx.ui.notify(`ACM browse (non-interactive):\n${scopes.map(([scope, count]) => `${scope} (${count})`).join("\n")}`, "info");
          return;
        }
        const scopeChoices = scopes.map(([scope, count]) => `${scope} — ${count} memories`);
        const selectedScope = await ctx.ui.select("ACM browse — scope", scopeChoices);
        const scope = scopes.find(([candidate, count]) => `${candidate} — ${count} memories` === selectedScope)?.[0];
        if (!scope) return;
        const listed = await acmRequest<{ items?: BrowseMemory[] }>(`/api/ui/memories?scope=${encodeURIComponent(scope)}&limit=100`);
        const memories = listed?.items ?? [];
        if (!memories.length) {
          ctx.ui.notify(`ACM browse: no memories in ${scope}.`, "info");
          return;
        }
        const choices = memories.map(memory => ({
          memory,
          label: `#${memory.id} [${memory.kind}] ${memory.content.slice(0, 72)} — ${memory.status}`,
        }));
        const selectedLabel = await ctx.ui.select("ACM browse — memory", choices.map(choice => choice.label));
        const selected = choices.find(choice => choice.label === selectedLabel)?.memory;
        if (!selected) return;
        let current = (await acmRequest<BrowseMemory>(`/api/ui/memories/${selected.id}`)) ?? selected;
        const refresh = async (memory: BrowseMemory) => (await acmRequest<BrowseMemory>(`/api/ui/memories/${memory.id}`)) ?? memory;

        for (;;) {
          ctx.ui.notify(`ACM memory #${current.id} [${current.kind}] ${current.status}${current.pinned ? " · pinned" : ""}\n${current.content}`, "info");
          const action = await ctx.ui.select("ACM memory actions", [
            "Edit",
            current.status === "active" ? "Archive" : "Restore",
            current.pinned ? "Unpin" : "Pin",
            "Set importance",
            "Merge into…",
            "Add alias",
            ...(current.entities?.some(entity => entity.aliases?.length) ? ["Remove alias"] : []),
            "Done",
          ]);
          if (!action || action === "Done") return;

          if (action === "Edit") {
            const content = await ctx.ui.editor(`Edit ACM memory #${current.id}`, current.content);
            if (!content?.trim()) continue;
            const updated = await acmRequest<BrowseMemory>(`/memories/${current.id}`, "PATCH", { content });
            if (!updated) ctx.ui.notify("ACM browse: edit failed.", "error");
            else current = await refresh(updated);
            continue;
          }
          if (action === "Archive" || action === "Restore") {
            const verb = action.toLowerCase();
            if (!await ctx.ui.confirm(`${action} ACM memory`, `${action} memory #${current.id}?`)) continue;
            const updated = await acmRequest<BrowseMemory>(`/memories/${current.id}/${verb}`, "POST");
            if (!updated) ctx.ui.notify(`ACM browse: ${verb} failed.`, "error");
            else current = await refresh(updated);
            continue;
          }
          if (action === "Pin" || action === "Unpin") {
            const updated = await acmRequest<BrowseMemory>(`/memories/${current.id}`, "PATCH", { pinned: action === "Pin" });
            if (!updated) ctx.ui.notify("ACM browse: pin update failed.", "error");
            else current = await refresh(updated);
            continue;
          }
          if (action === "Set importance") {
            const value = await ctx.ui.input("ACM importance (0–1)", String(current.importance));
            const importance = Number(value);
            if (!Number.isFinite(importance) || importance < 0 || importance > 1) {
              ctx.ui.notify("ACM browse: importance must be 0–1.", "error");
              continue;
            }
            const updated = await acmRequest<BrowseMemory>(`/memories/${current.id}`, "PATCH", { importance });
            if (!updated) ctx.ui.notify("ACM browse: importance update failed.", "error");
            else current = await refresh(updated);
            continue;
          }
          if (action === "Merge into…") {
            const targets = choices.filter(choice => choice.memory.id !== current.id && choice.memory.status === "active");
            const targetLabel = await ctx.ui.select("Merge into", targets.map(choice => choice.label));
            const target = targets.find(choice => choice.label === targetLabel)?.memory;
            if (!target || !await ctx.ui.confirm("Merge ACM memory", `Merge #${current.id} into #${target.id}? Source becomes archived.`)) continue;
            const updated = await acmRequest<BrowseMemory>(`/memories/${current.id}/merge`, "POST", { target_id: target.id });
            if (!updated) ctx.ui.notify("ACM browse: merge failed.", "error");
            else current = await refresh(updated);
            continue;
          }
          const entities = current.entities ?? [];
          if (!entities.length) {
            ctx.ui.notify("ACM browse: no linked entities.", "info");
            continue;
          }
          const entityLabel = await ctx.ui.select(
            action === "Remove alias" ? "Remove alias from entity" : "Add alias to entity",
            entities.map(entity => `#${entity.id} ${entity.canonical_name}`),
          );
          const entity = entities.find(candidate => `#${candidate.id} ${candidate.canonical_name}` === entityLabel);
          if (!entity) continue;
          if (action === "Remove alias") {
            const alias = await ctx.ui.select(`Remove alias from ${entity.canonical_name}`, entity.aliases ?? []);
            if (alias && await acmRequest(`/entities/${entity.id}/aliases`, "DELETE", { alias })) current = await refresh(current);
            continue;
          }
          const alias = await ctx.ui.input(`Alias for ${entity.canonical_name}`);
          if (alias?.trim() && await acmRequest(`/entities/${entity.id}/aliases`, "POST", { alias })) current = await refresh(current);
        }
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
        const fetch = await acmRequest("/fetch", "POST", { query: "ACM selfcheck", scope, budget_tokens: 1, deep: false, session_id: sessionId });
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
          scope,
          session_id: sessionId,
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


  pi.on("session_before_compact", async (event, ctx) => {
    if (!getMode().compactHook || event.signal.aborted) return {};
    const preparation = event.preparation;
    const response = await acmRequest<CompactResponse>("/compact/match", "POST", {
      scope: projectScope(ctx.cwd),
      conversation: messages(preparation.messagesToSummarize),
      turn_prefix: preparation.turnPrefixMessages.length ? messages(preparation.turnPrefixMessages) : null,
      previous_summary: preparation.previousSummary ?? null,
      custom_instructions: event.customInstructions ?? null,
      file_ops: fileOps(preparation.fileOps),
      budget_tokens: 1500,
    }, event.signal);
    if (!response || response.validation_score < 0.8) return {};
    serviceOnline = true;
    lastCompaction = response;
    updateStatus(ctx);
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
    if (!getMode().anticipate) return;
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
    if (!autoArmEnabled) return;
    // ponytail: OMP TurnEndEvent lacks native TreePreparation, whose dynamic cut point,
    // turn prefix, and file ops define exact hook identity. Arm only once it exposes that input.
  });

  pi.on("agent_end", (event, ctx) => {
    if (!getMode().ingest) return;
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
    const transcript = capHarvestText(additions.map(message => `${message.role}: ${message.content}`).join("\n"));
    ctx.setTimeout(
      async () => {
        const result = await acmRequest<{ job_id?: unknown }>("/ingest", "POST", {
          scope: projectScope(ctx.cwd),
          source_ref: `session:${sessionId}`,
          text: transcript,
        });
        if (typeof result?.job_id !== "number") return;
        serviceOnline = true;
        ingestCount += additions.length;
        updateStatus(ctx);
      },
      0,
    );
  });

  pi.on("context", async (event, ctx) => {
    const sessionId = ctx.sessionManager.getSessionId();
    const user = messages(event.messages).reverse().find(message => message.role === "user");
    if (user) latestUser.set(sessionId, user.content);
    const mode = getMode();
    if (!mode.anticipate || !autoInject) {
      updateStatus(ctx);
      return {};
    }
    const bundle = await acmRequest<{ rendered?: unknown }>(`/bundle/${sessionId}`);
    if (typeof bundle?.rendered !== "string" || !bundle.rendered.trim()) {
      if (serviceOnline !== false) {
        bundleMisses += 1;
        updateStatus(ctx);
      }
      return {};
    }
    serviceOnline = true;
    bundleHits += 1;
    updateStatus(ctx);
    return { messages: [{ role: "user", content: bundle.rendered }, ...event.messages] };
  });
}
