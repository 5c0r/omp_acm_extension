const ACM_URL = process.env.ACM_URL ?? "http://localhost:8927";

function timeout(path: string, method: "GET" | "POST" | "PATCH"): number {
  if (path === "/compact") return Number.parseInt(process.env.ACM_COMPACT_TIMEOUT_MS ?? "300000", 10) || 300_000;
  if (path === "/compact/match") return 8_000;
  if (path === "/architect") return 30_000;
  if (path === "/fetch") return 60_000;
  return method === "GET" ? 2_000 : 5_000;
}

export async function acmRequest<T>(
  path: string,
  method: "GET" | "POST" | "PATCH" = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T | null> {
  try {
    const signals = [AbortSignal.timeout(timeout(path, method))];
    if (signal) signals.unshift(signal);
    const response = await fetch(`${ACM_URL}${path}`, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.any(signals),
    });
    return response.ok ? (await response.json()) as T : null;
  } catch {
    return null;
  }
}
