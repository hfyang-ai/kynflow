export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const DEFAULT_GET_TIMEOUT_MS = 30_000;
// Agent 分析 / 评测 / 周报等写操作可能调用 LLM，耗时远高于普通读写，给足超时。
const DEFAULT_MUTATION_TIMEOUT_MS = 180_000;
const STREAM_TIMEOUT_MS = 30 * 60_000;

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export type RequestOptions = {
  signal?: AbortSignal;
  timeoutMs?: number;
};

/** 合并调用方 signal 与超时 signal，避免请求悬挂后 UI 永久停留在 loading。 */
function resolveSignal(options: RequestOptions, fallbackTimeout: number) {
  const timeoutMs = options.timeoutMs ?? fallbackTimeout;
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  if (!options.signal) return { signal: timeoutSignal, cleanup: () => undefined };

  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal.aborted) abort();
  else options.signal.addEventListener("abort", abort, { once: true });
  timeoutSignal.addEventListener("abort", abort, { once: true });

  return {
    signal: controller.signal,
    cleanup: () => {
      options.signal?.removeEventListener("abort", abort);
      timeoutSignal.removeEventListener("abort", abort);
    },
  };
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === "AbortError";
}

async function request<T>(
  path: string,
  init: RequestInit,
  options: RequestOptions = {},
): Promise<T> {
  const fallbackTimeout =
    init.method === "GET" ? DEFAULT_GET_TIMEOUT_MS : DEFAULT_MUTATION_TIMEOUT_MS;
  const { signal, cleanup } = resolveSignal(options, fallbackTimeout);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, { ...init, signal });
    if (!response.ok) await throwApiError(response);
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  } catch (error) {
    if (isAbortError(error) && !options.signal?.aborted) {
      throw new Error("请求超时，请检查后端服务是否可用。");
    }
    throw error;
  } finally {
    cleanup();
  }
}

export function apiGet<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, { method: "GET", headers: { Accept: "application/json" } }, options);
}

export function apiPost<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>(
    path,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    },
    options,
  );
}

export function apiPatch<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>(
    path,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    },
    options,
  );
}

export function apiUpload<T>(
  path: string,
  body: FormData,
  options: RequestOptions = {},
): Promise<T> {
  return request<T>(
    path,
    { method: "POST", body, headers: { Accept: "application/json" } },
    { timeoutMs: 5 * 60_000, ...options },
  );
}

export function apiDelete<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, { method: "DELETE", headers: { Accept: "application/json" } }, options);
}

export async function apiPostStream(
  path: string,
  body: unknown,
  onEvent: (event: string, data: unknown) => void,
  options: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<void> {
  const { signal, cleanup } = resolveSignal(options, STREAM_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok) await throwApiError(response);
    if (!response.body) throw new Error("Streaming response body is not available.");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const dispatch = (raw: string) => {
      if (!raw.trim()) return;
      // 兼容 \n 与 \r\n 两种行结束符。
      const lines = raw.split(/\r?\n/);
      const event = lines.find((line) => line.startsWith("event: "))?.slice(7) ?? "message";
      const dataText = lines
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (!dataText) return;
      // 单条事件解析失败不应中断整条流。
      try {
        onEvent(event, JSON.parse(dataText));
      } catch {
        console.warn("[apiPostStream] 无法解析的 SSE 事件", { event, dataText });
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        // 服务端未以空行结尾时，缓冲区里的最后一条事件也要派发。
        dispatch(buffer);
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split(/\r?\n\r?\n/);
      buffer = events.pop() ?? "";
      for (const rawEvent of events) dispatch(rawEvent);
    }
  } finally {
    cleanup();
  }
}

async function throwApiError(response: Response): Promise<never> {
  const fallback = `${response.status} ${response.statusText}`.trim();
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    let message: string | null = null;
    try {
      const payload = await response.json();
      message = readApiErrorMessage(payload);
    } catch {
      message = null;
    }
    throw new ApiError(message || fallback, response.status);
  }
  const text = (await response.text()).trim();
  throw new ApiError(text || fallback, response.status);
}

function readApiErrorMessage(payload: unknown): string | null {
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return null;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item)
          return String((item as { msg: unknown }).msg);
        return typeof item === "string" ? item : null;
      })
      .filter(Boolean);
    return messages.length ? messages.join("; ") : null;
  }
  const message = (payload as { message?: unknown }).message;
  return typeof message === "string" ? message : null;
}
