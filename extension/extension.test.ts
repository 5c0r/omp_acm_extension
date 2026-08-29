import { expect, test, vi } from "bun:test";
import { basename } from "node:path";


import acmExtension, { shouldAutoArm } from "./index";

type ExtensionHandler = (event: never, ctx: never) => Promise<unknown> | unknown;
type RegisteredTool = {
  name: string;
  execute: (...args: never[]) => Promise<unknown>;
};
type CommandHandler = (args: string, ctx: never) => Promise<void> | void;
type RegisteredCommand = { handler: CommandHandler };

type RegisteredFlag = { name: string; description: string; type?: "boolean" | "string" };


function projectScope(): string {
  const result = Bun.spawnSync(["git", "-C", process.cwd(), "rev-parse", "--show-toplevel"]);
  return `project:${basename(new TextDecoder().decode(result.stdout).trim()).toLowerCase()}`;
}


function extensionStub(initialFlag?: unknown) {
  let flagValue = initialFlag;
  const handlers: Record<string, ExtensionHandler> = {};
  const labels: string[] = [];
  const tools: RegisteredTool[] = [];
  const commands: string[] = [];
  const commandHandlers: Record<string, CommandHandler> = {};
  const flags: RegisteredFlag[] = [];
  const warnings: string[] = [];
  const schema = { optional: () => schema };

  acmExtension({
    setLabel: (label: string) => labels.push(label),
    on: (event: string, handler: ExtensionHandler) => {
      handlers[event] = handler;
    },
    registerTool: (tool: RegisteredTool) => tools.push(tool),
    registerCommand: (name: string, command: RegisteredCommand) => {
      commands.push(name);
      commandHandlers[name] = command.handler;
    },
    registerFlag: (name: string, flag: { description: string; type?: "boolean" | "string" }) => flags.push({ name, ...flag }),
    getFlag: () => flagValue,
    logger: { warn: (message: string) => warnings.push(message) },
    zod: {
      object: () => schema,
      string: () => schema,
      number: () => schema,
      boolean: () => schema,
      array: () => schema,
    },
  } as never);
  return { handlers, labels, tools, commands, commandHandlers, flags, warnings, setFlag: (value: unknown) => { flagValue = value; } };
}

test("factory labels Agentic Context Management", () => {
  const { labels } = extensionStub();

  expect(labels).toEqual(["Agentic Context Management"]);
});

test("registers complete ACM tool and command surface", () => {
  const { tools, commands } = extensionStub();

  expect(tools.map(tool => tool.name)).toEqual(["acm_fetch", "acm_ingest", "acm_compact", "acm_architect", "acm_status", "acm_consolidate"]);
});

test("registers acm-mode flag", () => {
  expect(extensionStub().flags).toEqual([
    { name: "acm-mode", description: "ACM subsystem preset: full | memory | compaction", type: "string" },
  ]);
});

test("session start probes health then renders connected status footer", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const sessionStart = handlers.session_start;
  expect(sessionStart).toBeTypeOf("function");
  if (!sessionStart) return;

  const originalFetch = globalThis.fetch;
  const setStatus = vi.fn();
  globalThis.fetch = async url => {
    expect(String(url)).toBe("http://localhost:8927/health");
    return new Response(JSON.stringify({ status: "ok" }));
  };
  try {
    await sessionStart({} as never, { hasUI: true, ui: { setStatus } } as never);
    expect(setStatus).toHaveBeenCalledWith("acm", "ACM full · connected");
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("known-offline session freezes failures then recovers without a phantom miss", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const sessionStart = handlers.session_start;
  const context = handlers.context;
  expect(sessionStart).toBeTypeOf("function");
  expect(context).toBeTypeOf("function");
  if (!sessionStart || !context) return;

  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  const setStatus = vi.fn();
  const setWidget = vi.fn();
  let bundleAttempts = 0;
  globalThis.fetch = async url => {
    urls.push(String(url));
    if (String(url).endsWith("/health")) return new Response(null, { status: 503 });
    bundleAttempts += 1;
    return bundleAttempts === 1
      ? new Response(null, { status: 503 })
      : new Response(JSON.stringify({ rendered: "[acm memory] recovered" }));
  };
  const ctx = {
    hasUI: true,
    sessionManager: { getSessionId: () => "offline-session" },
    ui: { setStatus, setWidget },
  };
  try {
    await sessionStart({} as never, ctx as never);
    await context({ messages: [{ role: "user", content: "offline request" }] } as never, ctx as never);
    await context({ messages: [{ role: "user", content: "recovered request" }] } as never, ctx as never);
    expect(urls).toEqual([
      "http://localhost:8927/health",
      "http://localhost:8927/bundle/offline-session",
      "http://localhost:8927/bundle/offline-session",
    ]);
    expect(setStatus).toHaveBeenNthCalledWith(1, "acm", "ACM full · offline");
    expect(setStatus).toHaveBeenNthCalledWith(2, "acm", "ACM full · bundle ✓1 ✗0 · ingest 0 · last compact —");
    expect(setWidget).not.toHaveBeenCalled();
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});


test("/acm status reports service health and stats", async () => {
  const { commandHandlers } = extensionStub();
  const handler = commandHandlers.acm;
  expect(handler).toBeTypeOf("function");
  if (!handler) return;

  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  const notices: string[] = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response(JSON.stringify(String(url).endsWith("/health") ? { status: "ok" } : { stats: { bundle_injected: "3", explicit_fetch: "2" } }));
  };
  try {
    await handler("status", { ui: { notify: (message: string) => notices.push(message) } } as never);
    expect(urls).toEqual(["http://localhost:8927/health", "http://localhost:8927/stats"]);
    expect(notices).toEqual(["ACM status: ok; mode=full; bundle_injected=3; explicit_fetch=2; session ✓0 ✗0; ingest 0"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("/acm browse prints scope summary without interactive UI", async () => {
  const { commandHandlers } = extensionStub();
  const handler = commandHandlers.acm;
  expect(handler).toBeTypeOf("function");
  if (!handler) return;

  const originalFetch = globalThis.fetch;
  const notices: string[] = [];
  const urls: string[] = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response(JSON.stringify({ totals: { scope: { "project:browse-live": 2 } } }));
  };
  try {
    await handler("browse", { hasUI: false, ui: { notify: (message: string) => notices.push(message) } } as never);
    expect(urls).toEqual(["http://localhost:8927/api/ui/dashboard"]);
    expect(notices).toEqual(["ACM browse (non-interactive):\nproject:browse-live (2)"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("/acm browse passes string-only scope options to native UI", async () => {
  const { commandHandlers } = extensionStub();
  const handler = commandHandlers.acm;
  expect(handler).toBeTypeOf("function");
  if (!handler) return;

  const originalFetch = globalThis.fetch;
  let scopeOptions: unknown[] = [];
  globalThis.fetch = async () => new Response(JSON.stringify({ totals: { scope: { "project:browse-live": 2 } } }));
  try {
    await handler("browse", {
      hasUI: true,
      ui: {
        select: async (_title: string, options: unknown[]) => {
          scopeOptions = options;
          return undefined;
        },
        notify: () => undefined,
      },
    } as never);
    expect(scopeOptions).toEqual(["project:browse-live — 2 memories"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});



test("/acm browse passes string-only memory and merge options to native UI", async () => {
  const { commandHandlers } = extensionStub();
  const handler = commandHandlers.acm;
  expect(handler).toBeTypeOf("function");
  if (!handler) return;

  const originalFetch = globalThis.fetch;
  const selectors = new Map<string, unknown[]>();
  const actions = ["Merge into…", "Done"];
  globalThis.fetch = async url => {
    const address = String(url);
    if (address.endsWith("/api/ui/dashboard")) return new Response(JSON.stringify({ totals: { scope: { "project:browse-live": 2 } } }));
    if (address.includes("/api/ui/memories?")) return new Response(JSON.stringify({ items: [
      { id: 41, kind: "fact", content: "Source memory", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false },
      { id: 42, kind: "fact", content: "Target memory", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false },
    ] }));
    if (address.endsWith("/api/ui/memories/41")) return new Response(JSON.stringify({ id: 41, kind: "fact", content: "Source memory", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false, entities: [] }));
    return new Response(JSON.stringify({ id: 42, kind: "fact", content: "Target memory", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false, entities: [] }));
  };
  try {
    await handler("browse", {
      hasUI: true,
      ui: {
        select: async (title: string, options: unknown[]) => {
          selectors.set(title, options);
          if (title === "ACM browse — scope") return "project:browse-live — 2 memories";
          if (title === "ACM browse — memory") return "#41 [fact] Source memory — active";
          if (title === "ACM memory actions") return actions.shift();
          return "#42 [fact] Target memory — active";
        },
        confirm: async () => true,
        notify: () => undefined,
      },
    } as never);
    expect(selectors.get("ACM browse — memory")).toEqual(["#41 [fact] Source memory — active", "#42 [fact] Target memory — active"]);
    expect(selectors.get("Merge into")).toEqual(["#42 [fact] Target memory — active"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("/acm browse edits selected memory through native dialogs", async () => {
  const { commandHandlers } = extensionStub();
  const handler = commandHandlers.acm;
  expect(handler).toBeTypeOf("function");
  if (!handler) return;

  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; method: string | undefined; body?: unknown }> = [];
  const selections = ["project:browse-live — 1 memories", "#41 [fact] Widget API key rotates weekly — active", "Edit", "Done"];
  const editor = vi.fn(async () => "Widget API key rotates monthly");
  globalThis.fetch = async (url, init) => {
    const address = String(url);
    requests.push({ url: address, method: init?.method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (address.endsWith("/api/ui/dashboard")) return new Response(JSON.stringify({ totals: { scope: { "project:browse-live": 1 } } }));
    if (address.includes("/api/ui/memories?")) return new Response(JSON.stringify({ items: [{ id: 41, kind: "fact", content: "Widget API key rotates weekly", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false }] }));
    if (address.endsWith("/api/ui/memories/41")) return new Response(JSON.stringify({ id: 41, kind: "fact", content: "Widget API key rotates weekly", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false, entities: [] }));
    return new Response(JSON.stringify({ id: 41, kind: "fact", content: "Widget API key rotates monthly", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false }));
  };
  try {
    await handler("browse", {
      hasUI: true,
      ui: {
        select: async () => selections.shift(),
        editor,
        notify: () => undefined,
      },
    } as never);
    expect(editor).toHaveBeenCalledWith("Edit ACM memory #41", "Widget API key rotates weekly");
    expect(requests).toContainEqual({
      url: "http://localhost:8927/memories/41",
      method: "PATCH",
      body: { content: "Widget API key rotates monthly" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("/acm browse retains linked entities after a memory mutation", async () => {
  const { commandHandlers } = extensionStub();
  const handler = commandHandlers.acm;
  expect(handler).toBeTypeOf("function");
  if (!handler) return;

  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; method: string | undefined; body?: unknown }> = [];
  const selections = ["project:browse-live — 1 memories", "#41 [fact] Widget API key rotates weekly — active", "Pin", "Add alias", "#9 widget", "Done"];
  globalThis.fetch = async (url, init) => {
    const address = String(url);
    requests.push({ url: address, method: init?.method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (address.endsWith("/api/ui/dashboard")) return new Response(JSON.stringify({ totals: { scope: { "project:browse-live": 1 } } }));
    if (address.includes("/api/ui/memories?")) return new Response(JSON.stringify({ items: [{ id: 41, kind: "fact", content: "Widget API key rotates weekly", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false }] }));
    if (address.endsWith("/api/ui/memories/41")) return new Response(JSON.stringify({ id: 41, kind: "fact", content: "Widget API key rotates weekly", scope: "project:browse-live", status: "active", importance: 0.8, pinned: false, entities: [{ id: 9, canonical_name: "widget" }] }));
    return new Response(JSON.stringify({ id: 41, kind: "fact", content: "Widget API key rotates weekly", scope: "project:browse-live", status: "active", importance: 0.8, pinned: true }));
  };
  try {
    await handler("browse", {
      hasUI: true,
      ui: {
        select: async () => selections.shift(),
        input: async () => "widget-live",
        notify: () => undefined,
      },
    } as never);
    expect(requests).toContainEqual({
      url: "http://localhost:8927/entities/9/aliases",
      method: "POST",
      body: { alias: "widget-live" },
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("/acm selfcheck exercises every ACM endpoint", async () => {
  const { commandHandlers } = extensionStub();
  const handler = commandHandlers.acm;
  expect(handler).toBeTypeOf("function");
  if (!handler) return;

  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body?: Record<string, unknown> }> = [];
  const notices: string[] = [];
  let bundleCalls = 0;
  globalThis.fetch = async (url, init) => {
    const address = String(url);
    requests.push({ url: address, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (address.includes("/bundle/") && ++bundleCalls === 1) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(address.endsWith("/ingest") ? { job_id: 7 } : { ok: true }));
  };
  try {
    await handler("selfcheck", {
      cwd: "/tmp/acm-selfcheck",
      sessionManager: { getSessionId: () => "session-5" },
      ui: { notify: (message: string) => notices.push(message) },
    } as never);
    expect(requests.map(request => request.url)).toEqual([
      "http://localhost:8927/health",
      "http://localhost:8927/architect",
      "http://localhost:8927/ingest",
      "http://localhost:8927/status/7",
      "http://localhost:8927/fetch",
      "http://localhost:8927/anticipate",
      "http://localhost:8927/bundle/acm-selfcheck-session-5",
      "http://localhost:8927/bundle/acm-selfcheck-session-5",
      "http://localhost:8927/compact",
      "http://localhost:8927/consolidate",
      "http://localhost:8927/stats",
    ]);
    expect(requests.filter(request => request.body?.scope).map(request => request.body?.scope)).toEqual([
      "project:acm-selfcheck-session-5",
      "project:acm-selfcheck-session-5",
      "project:acm-selfcheck-session-5",
      "project:acm-selfcheck-session-5",
      "project:acm-selfcheck-session-5",
      "project:acm-selfcheck-session-5",
    ]);
    expect(requests.find(request => request.url.endsWith("/compact"))?.body?.scope).toBe("project:acm-selfcheck-session-5");
    expect(requests.find(request => request.url.endsWith("/fetch"))?.body?.session_id).toBe("session-5");
    expect(requests.find(request => request.url.endsWith("/compact"))?.body?.session_id).toBe("session-5");
    expect(requests.find(request => request.url.endsWith("/anticipate"))?.body?.session_id).toBe("acm-selfcheck-session-5");
    expect(notices).toEqual([[
      "ACM selfcheck",
      "health       PASS",
      "architect    PASS",
      "ingest       PASS",
      "status       PASS",
      "fetch        PASS",
      "anticipate   PASS",
      "bundle       PASS",
      "compact      PASS",
      "consolidate  PASS",
      "stats        PASS",
    ].join("\n")]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("/acm inject toggles runtime bundle injection", async () => {
  const { commandHandlers, handlers } = extensionStub();
  const command = commandHandlers.acm;
  const context = handlers.context;
  expect(command).toBeTypeOf("function");
  expect(context).toBeTypeOf("function");
  if (!command || !context) return;

  const originalFetch = globalThis.fetch;
  const notices: string[] = [];
  const urls: string[] = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response(JSON.stringify({ rendered: "[acm memory]" }));
  };
  const ctx = { sessionManager: { getSessionId: () => "session-6" }, ui: { notify: (message: string) => notices.push(message) } };
  try {
    await command("inject off", ctx as never);
    expect(await context({ messages: [] } as never, ctx as never)).toEqual({});
    await command("inject on", ctx as never);
    expect(await context({ messages: [] } as never, ctx as never)).toEqual({ messages: [{ role: "user", content: "[acm memory]" }] });
    expect(urls).toEqual(["http://localhost:8927/bundle/session-6"]);
    expect(notices).toEqual(["ACM injection off", "ACM injection on"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("tools route scoped requests to ACM endpoints", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { tools } = extensionStub();
  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body?: Record<string, unknown> }> = [];
  const setStatus = vi.fn();
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return new Response(JSON.stringify(String(url).endsWith("/compact")
      ? { summary: "validated", validation_score: 0.9, compression_ratio: 0.2, probes: [] }
      : { ok: true }));
  };
  const context = {
    cwd: "/tmp/acm-tools",
    hasUI: true,
    sessionManager: { getSessionId: () => "session-3" },
    ui: { setStatus },
  };
  const invoke = async (name: string, params: Record<string, unknown>) => {
    const tool = tools.find(candidate => candidate.name === name);
    expect(tool).toBeDefined();
    return tool?.execute("call-1" as never, params as never, new AbortController().signal as never, undefined as never, context as never);
  };
  try {
    await invoke("acm_fetch", { query: "billing", budget_tokens: 600, deep: true });
    await invoke("acm_ingest", { text: "retain this", source_ref: "note-1", scope: "project:shared" });
    await invoke("acm_compact", {
      conversation: "compact this",
      budget_tokens: 100,
      file_ops: { read: ["src/a.ts"], written: [], edited: ["src/a.ts"] },
      custom_instructions: "retain rollout details",
    });
    await invoke("acm_architect", { description: "billing memory", reference: "ADR-1" });
    await invoke("acm_status", { job_id: 7 });
    await invoke("acm_consolidate", { scope: "project:shared" });
    expect(requests).toEqual([
      { url: "http://localhost:8927/fetch", body: { query: "billing", scope: "project:acm-tools", budget_tokens: 600, deep: true, session_id: "session-3" } },
      { url: "http://localhost:8927/ingest", body: { scope: "project:shared", text: "retain this", source_ref: "note-1" } },
      { url: "http://localhost:8927/compact", body: {
        scope: "project:acm-tools",
        conversation: [{ role: "user", content: "compact this" }],
        budget_tokens: 100,
        file_ops: { read: ["src/a.ts"], written: [], edited: ["src/a.ts"] },
        custom_instructions: "retain rollout details",
        session_id: "session-3",
      } },
      { url: "http://localhost:8927/architect", body: { scope: "project:acm-tools", description: "billing memory", reference: "ADR-1" } },
      { url: "http://localhost:8927/status/7", body: undefined },
      { url: "http://localhost:8927/consolidate", body: { scope: "project:shared" } },
    ]);
    expect(setStatus).toHaveBeenCalledWith(
      "acm",
      "ACM full · bundle ✓0 ✗0 · ingest 0 · last compact 0.90/0.20",
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("turn end queues a bounded trajectory for anticipation", async () => {
  const { handlers } = extensionStub();
  const contextHandler = handlers.context;
  const turnEndHandler = handlers.turn_end;
  expect(contextHandler).toBeTypeOf("function");
  expect(turnEndHandler).toBeTypeOf("function");
  if (!contextHandler || !turnEndHandler) return;

  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  const timers: Array<() => Promise<void>> = [];
  const ctx = {
    cwd: "/tmp/acm-turn",
    getContextUsage: () => undefined,
    sessionManager: { getSessionId: () => "session-4" },
    setTimeout: (callback: () => Promise<void>) => timers.push(callback),
  };
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : {} });
    return new Response("{}", { status: 404 });
  };
  try {
    await contextHandler({ messages: [{ role: "user", content: "Review billing rollout." }] } as never, ctx as never);
    requests.length = 0;
    await turnEndHandler({
      message: { role: "assistant", content: "Billing rollout reviewed." },
      toolResults: [{ toolName: "read", content: [{ type: "text", text: "x".repeat(600) }] }],
    } as never, ctx as never);
    expect(timers).toHaveLength(1);
    await timers[0]?.();
    expect(requests[0]).toMatchObject({
      url: "http://localhost:8927/anticipate",
      body: { session_id: "session-4", scope: "project:acm-turn" },
    });
    const trajectory = requests[0]?.body.trajectory as Array<{ role: string; content: string }>;
    expect(trajectory.slice(0, 2)).toEqual([
      { role: "user", content: "Review billing rollout." },
      { role: "assistant", content: "Billing rollout reviewed." },
    ]);
    expect(trajectory[2]?.content).toStartWith("read: ");
    expect(trajectory[2]?.content.length).toBeLessThanOrEqual(512);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("turn end leaves automatic compaction off by default", async () => {
  const originalAutoArm = process.env.ACM_AUTO_ARM;
  delete process.env.ACM_AUTO_ARM;
  const { handlers } = extensionStub();
  const turnEndHandler = handlers.turn_end;
  expect(turnEndHandler).toBeTypeOf("function");
  if (!turnEndHandler) return;

  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  const timers: Array<() => Promise<void>> = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    return new Response("{}");
  };
  try {
    await turnEndHandler(
      { message: { role: "assistant", content: "Ready to compact." }, toolResults: [] },
      {
        cwd: process.cwd(),
        getContextUsage: () => ({ tokens: 61, contextWindow: 100 }),
        sessionManager: {
          getSessionId: () => "session-armed",
          getBranch: () => [{ type: "message", message: { role: "user", content: "History." } }],
        },
        setTimeout: (callback: () => Promise<void>) => timers.push(callback),
      },
    );
    expect(timers).toHaveLength(1);
    await timers[0]?.();
    expect(requests).toHaveLength(1);
    expect(requests[0]?.url).toBe("http://localhost:8927/anticipate");
  } finally {
    globalThis.fetch = originalFetch;
    if (originalAutoArm === undefined) delete process.env.ACM_AUTO_ARM;
    else process.env.ACM_AUTO_ARM = originalAutoArm;
  }
});

test("auto arm gate fires once for a threshold crossing", () => {
  const state = { aboveThreshold: false };
  expect(shouldAutoArm(state, 0.61, "first", 0)).toBeTrue();
  expect(shouldAutoArm(state, 0.8, "first", 1)).toBeFalse();
  expect(shouldAutoArm(state, 0.5, "first", 2)).toBeFalse();
  expect(shouldAutoArm(state, 0.61, "first", 3)).toBeFalse();
  expect(shouldAutoArm(state, 0.5, "first", 4)).toBeFalse();
  expect(shouldAutoArm(state, 0.61, "second", 5)).toBeTrue();
});

test("armed compaction match keeps unrelated preserve data but discards stale payloads", async () => {
  const originalMode = process.env.ACM_MODE;
  process.env.ACM_MODE = "compaction";
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const compactHandler = handlers.session_before_compact;

  expect(compactHandler).toBeTypeOf("function");
  if (!compactHandler) return;

  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  const setStatus = vi.fn();
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    return new Response(
      JSON.stringify({ summary: "validated", validation_score: 0.9, compression_ratio: 0.2, probes: [{ question: "q1", reference_answer: "a1", summary_answer: "a1", verdict: "correct" }] }),
      { status: 200 },
    );
  };
  try {
    const result = await compactHandler(
      {
        signal: new AbortController().signal,
        customInstructions: "keep deployment details",
        preparation: {
          messagesToSummarize: [{ role: "user", content: "history" }],
          turnPrefixMessages: [{ role: "assistant", content: "split turn" }],
          previousSummary: "earlier",
          fileOps: { read: new Set(["deploy.ts"]), written: new Set(), edited: new Set(["deploy.ts"]) },
          previousPreserveData: { openaiRemoteCompaction: { replay: 1 }, snapcompact: { version: 1 }, unrelated: { keep: true } },
          firstKeptEntryId: "entry-1",
          tokensBefore: 42,
        },
      },
      {
        cwd: process.cwd(),
        hasUI: true,
        ui: { setStatus },
      } as never,
    );

    expect(requests).toEqual([
      {
        url: "http://localhost:8927/compact/match",
        body: {
          scope: projectScope(),
          conversation: [{ role: "user", content: "history" }],
          turn_prefix: [{ role: "assistant", content: "split turn" }],
          previous_summary: "earlier",
          custom_instructions: "keep deployment details",
          file_ops: { read: ["deploy.ts"], written: [], edited: ["deploy.ts"] },
          budget_tokens: 1500,
        },
      },
    ]);
    expect(result).toEqual({
      compaction: {
        summary: "validated",
        shortSummary: "ACM validated (score 0.90, ratio 0.20)",
        firstKeptEntryId: "entry-1",
        tokensBefore: 42,
        preserveData: {
          unrelated: { keep: true },
          acm: {
            validationScore: 0.9,
            compressionRatio: 0.2,
            probes: [{ question: "q1", reference_answer: "a1", summary_answer: "a1", verdict: "correct" }],
          },
        },
      },
    });
    expect(setStatus).toHaveBeenCalledWith(
      "acm",
      "ACM compaction · bundle — · ingest 0 · last compact 0.90/0.20",
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.ACM_MODE;
    else process.env.ACM_MODE = originalMode;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});


test("context prepends only a ready session bundle", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const contextHandler = handlers.context;
  expect(contextHandler).toBeTypeOf("function");
  if (!contextHandler) return;

  const originalFetch = globalThis.fetch;
  const setStatus = vi.fn();
  const urls: string[] = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response(JSON.stringify({ rendered: "[acm memory]\n- [project:test] Pro plan" }), { status: 200 });
  };
  try {
    const result = await contextHandler(
      { messages: [{ role: "user", content: "live question" }] } as never,
      {
        hasUI: true,
        sessionManager: { getSessionId: () => "session-1" },
        ui: { setStatus },
      } as never,
    );
    expect(urls).toEqual(["http://localhost:8927/bundle/session-1"]);
    expect(result).toEqual({
      messages: [
        { role: "user", content: "[acm memory]\n- [project:test] Pro plan" },
        { role: "user", content: "live question" },
      ],
    });
    expect(setStatus).toHaveBeenCalledWith(
      "acm",
      "ACM full · bundle ✓1 ✗0 · ingest 0 · last compact —",
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("context records a widget miss when bundle is unavailable", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const contextHandler = handlers.context;
  expect(contextHandler).toBeTypeOf("function");
  if (!contextHandler) return;

  const originalFetch = globalThis.fetch;
  const setStatus = vi.fn();
  globalThis.fetch = async () => new Response(null, { status: 404 });
  try {
    await contextHandler(
      { messages: [{ role: "user", content: "unbundled question" }] } as never,
      {
        hasUI: true,
        sessionManager: { getSessionId: () => "session-miss" },
        ui: { setStatus },
      } as never,
    );
    expect(setStatus).toHaveBeenCalledWith(
      "acm",
      "ACM full · bundle ✓0 ✗1 · ingest 0 · last compact —",
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("ACM_WIDGET=0 disables the status widget", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  process.env.ACM_WIDGET = "0";
  const { handlers } = extensionStub();
  const sessionStart = handlers.session_start;
  const contextHandler = handlers.context;
  expect(sessionStart).toBeTypeOf("function");
  expect(contextHandler).toBeTypeOf("function");
  if (!sessionStart || !contextHandler) return;

  const originalFetch = globalThis.fetch;
  const setStatus = vi.fn();
  globalThis.fetch = async () => new Response(JSON.stringify({ rendered: "[acm memory] enabled" }), { status: 200 });
  try {
    const ctx = {
      hasUI: true,
      sessionManager: { getSessionId: () => "session-disabled" },
      ui: { setStatus },
    };
    await sessionStart({} as never, ctx as never);
    const result = await contextHandler(
      { messages: [{ role: "user", content: "keep injecting" }] } as never,
      ctx as never,
    );
    expect(result).toEqual({
      messages: [
        { role: "user", content: "[acm memory] enabled" },
        { role: "user", content: "keep injecting" },
      ],
    });
    expect(setStatus).not.toHaveBeenCalled();
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("widget failure leaves context injection intact", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const contextHandler = handlers.context;
  expect(contextHandler).toBeTypeOf("function");
  if (!contextHandler) return;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ rendered: "[acm memory] preserved" }), { status: 200 });
  try {
    await expect(contextHandler(
      { messages: [{ role: "user", content: "survive TUI failure" }] } as never,
      {
        hasUI: true,
        sessionManager: { getSessionId: () => "session-throw" },
        ui: { setStatus: () => { throw new Error("TUI unavailable"); } },
      } as never,
    )).resolves.toEqual({
      messages: [
        { role: "user", content: "[acm memory] preserved" },
        { role: "user", content: "survive TUI failure" },
      ],
    });
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("headless context injection skips the status widget", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const contextHandler = handlers.context;
  expect(contextHandler).toBeTypeOf("function");
  if (!contextHandler) return;

  const originalFetch = globalThis.fetch;
  const setStatus = vi.fn();
  globalThis.fetch = async () => new Response(JSON.stringify({ rendered: "[acm memory] headless" }), { status: 200 });
  try {
    await contextHandler(
      { messages: [{ role: "user", content: "headless turn" }] } as never,
      {
        hasUI: false,
        sessionManager: { getSessionId: () => "session-headless" },
        ui: { setStatus },
      } as never,
    );
    expect(setStatus).not.toHaveBeenCalled();
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

if (process.env.ACM_LIVE_TEST === "1") {
  test("live armed match hook completes below OMP's 30-second handler cap", async () => {
    const { handlers } = extensionStub();
    const compactHandler = handlers.session_before_compact;
    expect(compactHandler).toBeTypeOf("function");
    if (!compactHandler) return;
    const messages = Array.from({ length: 30 }, (_, index) => ({
      role: index % 2 ? "assistant" : "user",
      content: `Turn ${index}: analyst team-${index} recorded metric-${index} on 2026-05-${String(index + 1).padStart(2, "0")}, prefers policy-${index}, and reviewed src/module-${index}.ts.`,
    }));
    const fileOps = { read: ["src/billing.ts"], written: ["config/rollout.yaml"], edited: [] };
    const project = `live-${crypto.randomUUID()}`;
    const scope = `project:${project}`;
    const arm = await fetch("http://localhost:8927/compact", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        scope,
        conversation: messages,
        turn_prefix: null,
        previous_summary: null,
        custom_instructions: null,
        file_ops: fileOps,
        budget_tokens: 1500,
        async: true,
        from_extension: true,
      }),
    });
    expect(arm.status).toBe(202);
    const deadline = performance.now() + 60_000;
    let result: unknown = {};
    while (performance.now() < deadline) {
      const handlerStarted = performance.now();
      result = await compactHandler({
        signal: new AbortController().signal,
        preparation: {
          messagesToSummarize: messages,
          turnPrefixMessages: [],
          fileOps: { read: new Set(fileOps.read), written: new Set(fileOps.written), edited: new Set() },
          firstKeptEntryId: "entry-live",
          tokensBefore: 2000,
        },
      } as never, { cwd: `/tmp/${project}` } as never);
      expect(performance.now() - handlerStarted).toBeLessThan(30_000);
      if (result && typeof result === "object" && "compaction" in result) break;
      await Bun.sleep(200);
    }
    const summary = result && typeof result === "object" && "compaction" in result
      && result.compaction && typeof result.compaction === "object" && "summary" in result.compaction
      ? result.compaction.summary
      : undefined;
    expect(summary).toBeTypeOf("string");
  }, 70_000);
}


test("compaction accepts a delayed validated response", async () => {
  const { handlers } = extensionStub();
  const compactHandler = handlers.session_before_compact;
  expect(compactHandler).toBeTypeOf("function");
  if (!compactHandler) return;

  const originalFetch = globalThis.fetch;
  const delayed = Promise.withResolvers<Response>();
  vi.useFakeTimers();
  globalThis.fetch = (_url, init) => {
    const timer = setTimeout(
      () => delayed.resolve(new Response(JSON.stringify({ summary: "validated", validation_score: 0.9, compression_ratio: 0.2, probes: [] }))),
      2_100,
    );
    init?.signal?.addEventListener("abort", () => {
      clearTimeout(timer);
      delayed.reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
    return delayed.promise;
  };
  try {
    const resultPromise = compactHandler({
      signal: new AbortController().signal,
      preparation: {
        messagesToSummarize: [{ role: "user", content: "history" }],
        turnPrefixMessages: [],
        fileOps: { read: new Set(), written: new Set(), edited: new Set() },
        firstKeptEntryId: "entry-1",
        tokensBefore: 42,
      },
    } as never, { cwd: process.cwd() } as never);
    vi.advanceTimersByTime(2_100);
    expect(await resultPromise).toMatchObject({ compaction: { summary: "validated" } });
  } finally {
    globalThis.fetch = originalFetch;
    vi.useRealTimers();
  }
});


test("pre-aborted compaction returns native fallback without fetch", async () => {
  const { handlers } = extensionStub();
  const compactHandler = handlers.session_before_compact;
  expect(compactHandler).toBeTypeOf("function");
  if (!compactHandler) return;

  const controller = new AbortController();
  controller.abort();
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response("{}", { status: 200 });
  };
  try {
    const result = await compactHandler({ signal: controller.signal, preparation: {} } as never, {} as never);
    expect(result).toEqual({});
    expect(calls).toBe(0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("missing armed compaction falls through to native", async () => {
  const { handlers } = extensionStub();
  const compactHandler = handlers.session_before_compact;
  expect(compactHandler).toBeTypeOf("function");
  if (!compactHandler) return;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("{}", { status: 404 });
  try {
    const result = await compactHandler(
      {
        signal: new AbortController().signal,
        preparation: {
          messagesToSummarize: [{ role: "user", content: "unarmed" }],
          turnPrefixMessages: [],
          fileOps: { read: new Set(), written: new Set(), edited: new Set() },
        },
      } as never,
      { cwd: process.cwd() } as never,
    );
    expect(result).toEqual({});
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("agent end ignores ingest responses without a job_id", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const agentEnd = handlers.agent_end;
  expect(agentEnd).toBeTypeOf("function");
  if (!agentEnd) return;

  const originalFetch = globalThis.fetch;
  const timers: Array<() => Promise<void>> = [];
  const setStatus = vi.fn();
  globalThis.fetch = async () => new Response("{}");
  try {
    await agentEnd(
      { messages: [{ role: "user", content: "unsaved fact" }] } as never,
      {
        cwd: process.cwd(),
        hasUI: true,
        sessionManager: { getSessionId: () => "no-job-id" },
        setTimeout: (callback: () => Promise<void>) => timers.push(callback),
        ui: { setStatus },
      } as never,
    );
    expect(setStatus).not.toHaveBeenCalled();
    await timers[0]?.();
    expect(setStatus).not.toHaveBeenCalled();
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("agent end ignores failed ingest responses", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const agentEnd = handlers.agent_end;
  expect(agentEnd).toBeTypeOf("function");
  if (!agentEnd) return;

  const originalFetch = globalThis.fetch;
  const timers: Array<() => Promise<void>> = [];
  const setStatus = vi.fn();
  globalThis.fetch = async () => new Response(null, { status: 503 });
  try {
    await agentEnd(
      { messages: [{ role: "user", content: "unavailable fact" }] } as never,
      {
        cwd: process.cwd(),
        hasUI: true,
        sessionManager: { getSessionId: () => "failed-ingest" },
        setTimeout: (callback: () => Promise<void>) => timers.push(callback),
        ui: { setStatus },
      } as never,
    );
    expect(setStatus).not.toHaveBeenCalled();
    await timers[0]?.();
    expect(setStatus).not.toHaveBeenCalled();
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("agent end enqueues only newly harvested transcript messages", async () => {
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const agentEndHandler = handlers.agent_end;
  expect(agentEndHandler).toBeTypeOf("function");
  if (!agentEndHandler) return;

  const originalFetch = globalThis.fetch;
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  const timers: Array<() => Promise<void>> = [];
  const setStatus = vi.fn();
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ job_id: 1 }), { status: 200 });
  };
  const initial = {
    messages: [
      { role: "user", content: "Use Pro billing." },
      { role: "assistant", content: "I will retain billing scope." },
      { role: "assistant", content: "Existing billing record is stable." },
      { role: "tool", content: "irrelevant tool plumbing" },
    ],
  };
  const postCompaction = {
    messages: [
      { role: "compactionSummary", summary: "ACM summary: Pro billing scope retained." },
      { role: "assistant", content: "I will retain billing scope." },
      { role: "user", content: "Check new billing rollout." },
    ],
  };
  const ctx = {
    cwd: process.cwd(),
    hasUI: true,
    sessionManager: { getSessionId: () => "session-2" },
    setTimeout: (callback: () => Promise<void>) => timers.push(callback),
    ui: { setStatus },
  };
  try {
    await agentEndHandler(initial as never, ctx as never);
    await agentEndHandler(postCompaction as never, ctx as never);
    await agentEndHandler(postCompaction as never, ctx as never);
    expect(timers).toHaveLength(2);
    await timers[0]?.();
    await timers[1]?.();
    expect(requests).toHaveLength(2);
    expect(requests[0]).toMatchObject({
      url: "http://localhost:8927/ingest",
      body: {
        scope: projectScope(),
        source_ref: "session:session-2",
        text: "user: Use Pro billing.\nassistant: I will retain billing scope.\nassistant: Existing billing record is stable.",
      },
    });
    expect(requests[1]?.body.text).toBe("assistant: ACM summary: Pro billing scope retained.\nuser: Check new billing rollout.");
    expect(setStatus).toHaveBeenCalledTimes(2);
    expect(setStatus).toHaveBeenNthCalledWith(
      1,
      "acm",
      "ACM full · bundle ✓0 ✗0 · ingest 3 · last compact —",
    );
    expect(setStatus).toHaveBeenNthCalledWith(
      2,
      "acm",
      "ACM full · bundle ✓0 ✗0 · ingest 5 · last compact —",
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("/acm last-compaction reports latest validated result", async () => {
  const { handlers, commandHandlers } = extensionStub();
  const compactHandler = handlers.session_before_compact;
  const command = commandHandlers.acm;
  expect(compactHandler).toBeTypeOf("function");
  expect(command).toBeTypeOf("function");
  if (!compactHandler || !command) return;

  const originalFetch = globalThis.fetch;
  const notices: string[] = [];
  globalThis.fetch = async () => new Response(JSON.stringify({
    summary: "validated",
    validation_score: 0.9,
    compression_ratio: 0.2,
    probes: [],
  }));
  try {
    await compactHandler({
      signal: new AbortController().signal,
      preparation: {
        messagesToSummarize: [{ role: "user", content: "history" }],
        turnPrefixMessages: [],
        fileOps: { read: new Set(), written: new Set(), edited: new Set() },
      },
    } as never, { cwd: process.cwd() } as never);
    await command("last-compaction", { ui: { notify: (message: string) => notices.push(message) } } as never);
    expect(notices).toEqual(["ACM last-compaction: score 0.90, ratio 0.20"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("--acm-mode selects memory when ACM_MODE is unset", async () => {
  const originalMode = process.env.ACM_MODE;
  delete process.env.ACM_MODE;
  const { commandHandlers, setFlag } = extensionStub();
  setFlag("memory");
  const handler = commandHandlers.acm;
  const originalFetch = globalThis.fetch;
  const notices: string[] = [];
  globalThis.fetch = async url => new Response(JSON.stringify(String(url).endsWith("/health") ? { status: "ok" } : { stats: {} }));
  try {
    await handler?.("status", { ui: { notify: (message: string) => notices.push(message) } } as never);
    expect(notices).toEqual(["ACM status: ok; mode=memory; bundle_injected=0; explicit_fetch=0; session ✓0 ✗0; ingest 0"]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.ACM_MODE;
    else process.env.ACM_MODE = originalMode;
  }
});

test("ACM_MODE overrides --acm-mode", async () => {
  const originalMode = process.env.ACM_MODE;
  process.env.ACM_MODE = "compaction";
  const { commandHandlers, setFlag } = extensionStub();
  setFlag("memory");
  const handler = commandHandlers.acm;
  const originalFetch = globalThis.fetch;
  const notices: string[] = [];
  globalThis.fetch = async url => new Response(JSON.stringify(String(url).endsWith("/health") ? { status: "ok" } : { stats: {} }));
  try {
    await handler?.("status", { ui: { notify: (message: string) => notices.push(message) } } as never);
    expect(notices).toEqual(["ACM status: ok; mode=compaction; bundle_injected=0; explicit_fetch=0; session ✓0 ✗0; ingest 0"]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.ACM_MODE;
    else process.env.ACM_MODE = originalMode;
  }
});

test("unknown ACM_MODE warns once and falls back to full", async () => {
  const originalMode = process.env.ACM_MODE;
  process.env.ACM_MODE = "not-a-mode";
  const { commandHandlers, warnings } = extensionStub();
  const handler = commandHandlers.acm;
  const originalFetch = globalThis.fetch;
  const notices: string[] = [];
  globalThis.fetch = async url => new Response(JSON.stringify(String(url).endsWith("/health") ? { status: "ok" } : { stats: {} }));
  try {
    expect(warnings).toEqual([]);
    await handler?.("status", { ui: { notify: (message: string) => notices.push(message) } } as never);
    expect(warnings).toEqual(["ACM mode 'not-a-mode' unknown; falling back to 'full' (expected full | memory | compaction)"]);
    expect(notices).toEqual(["ACM status: ok; mode=full; bundle_injected=0; explicit_fetch=0; session ✓0 ✗0; ingest 0"]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.ACM_MODE;
    else process.env.ACM_MODE = originalMode;
  }
});

test("compaction context renders the widget without fetching a bundle", async () => {
  const originalMode = process.env.ACM_MODE;
  process.env.ACM_MODE = "compaction";
  const previousWidget = process.env.ACM_WIDGET;
  delete process.env.ACM_WIDGET;
  const { handlers } = extensionStub();
  const context = handlers.context;
  expect(context).toBeTypeOf("function");
  if (!context) return;

  const originalFetch = globalThis.fetch;
  const urls: string[] = [];
  const setStatus = vi.fn();
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response("{}");
  };
  try {
    const result = await context(
      { messages: [{ role: "user", content: "compaction-only session" }] } as never,
      {
        hasUI: true,
        sessionManager: { getSessionId: () => "compaction-widget" },
        ui: { setStatus },
      } as never,
    );
    expect(result).toEqual({});
    expect(urls).toEqual([]);
    expect(setStatus).toHaveBeenCalledWith(
      "acm",
      "ACM compaction · bundle — · ingest 0 · last compact —",
    );
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.ACM_MODE;
    else process.env.ACM_MODE = originalMode;
    if (previousWidget === undefined) delete process.env.ACM_WIDGET;
    else process.env.ACM_WIDGET = previousWidget;
  }
});

test("compaction mode disables automatic ingestion, anticipation, and injection but retains compact hook", async () => {
  const originalMode = process.env.ACM_MODE;
  process.env.ACM_MODE = "compaction";
  const { handlers } = extensionStub();
  const agentEnd = handlers.agent_end;
  const turnEnd = handlers.turn_end;
  const context = handlers.context;
  const compact = handlers.session_before_compact;
  const originalFetch = globalThis.fetch;
  const timers: Array<() => Promise<void>> = [];
  const urls: string[] = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response(JSON.stringify({ summary: "validated", validation_score: 0.9, compression_ratio: 0.2, probes: [] }));
  };
  const ctx = {
    cwd: process.cwd(),
    sessionManager: { getSessionId: () => "mode-session" },
    setTimeout: (callback: () => Promise<void>) => timers.push(callback),
  };
  try {
    await agentEnd?.({ messages: [{ role: "user", content: "remember this" }] } as never, ctx as never);
    await turnEnd?.({ message: { role: "assistant", content: "answer" }, toolResults: [] } as never, ctx as never);
    const contextResult = await context?.({ messages: [{ role: "user", content: "question" }] } as never, ctx as never);
    const compactResult = await compact?.({
      signal: new AbortController().signal,
      preparation: {
        messagesToSummarize: [{ role: "user", content: "history" }],
        turnPrefixMessages: [],
        fileOps: { read: new Set(), written: new Set(), edited: new Set() },
      },
    } as never, ctx as never);
    expect(timers).toEqual([]);
    expect(urls).toEqual(["http://localhost:8927/compact/match"]);
    expect(contextResult).toEqual({});
    expect(compactResult).toMatchObject({ compaction: { summary: "validated" } });
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.ACM_MODE;
    else process.env.ACM_MODE = originalMode;
  }
});

test("memory mode retains automatic ingestion and anticipation but skips compact hook", async () => {
  const originalMode = process.env.ACM_MODE;
  process.env.ACM_MODE = "memory";
  const { handlers } = extensionStub();
  const agentEnd = handlers.agent_end;
  const turnEnd = handlers.turn_end;
  const compact = handlers.session_before_compact;
  const originalFetch = globalThis.fetch;
  const timers: Array<() => Promise<void>> = [];
  const urls: string[] = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response(JSON.stringify({ summary: "validated", validation_score: 0.9, compression_ratio: 0.2, probes: [] }));
  };
  const ctx = {
    cwd: process.cwd(),
    sessionManager: { getSessionId: () => "memory-session" },
    setTimeout: (callback: () => Promise<void>) => timers.push(callback),
  };
  try {
    await agentEnd?.({ messages: [{ role: "user", content: "remember this" }] } as never, ctx as never);
    await turnEnd?.({ message: { role: "assistant", content: "answer" }, toolResults: [] } as never, ctx as never);
    const compactResult = await compact?.({
      signal: new AbortController().signal,
      preparation: {
        messagesToSummarize: [{ role: "user", content: "history" }],
        turnPrefixMessages: [],
        fileOps: { read: new Set(), written: new Set(), edited: new Set() },
      },
    } as never, ctx as never);
    await timers[0]?.();
    await timers[1]?.();
    expect(compactResult).toEqual({});
    expect(urls).toEqual(["http://localhost:8927/ingest", "http://localhost:8927/anticipate"]);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalMode === undefined) delete process.env.ACM_MODE;
    else process.env.ACM_MODE = originalMode;
  }
});
