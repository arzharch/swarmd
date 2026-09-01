"use client";

/**
 * Every call the dashboard makes to the control plane, and the operator token
 * that authorises the ones that spend money.
 *
 * WHY THIS EXISTS. The control plane gates mutating endpoints and the event
 * stream on an operator token (SWARMD_API_TOKEN). Reads are ungated, so a
 * browser without the token renders a complete-looking dashboard and only
 * discovers it cannot do anything when a run comes back 401 and the stream
 * closes with 1008. Routing every request through here means the token is
 * attached in exactly one place, and its absence is something the UI can say
 * out loud instead of something the operator infers from a failure.
 *
 * This is NOT user authentication. swarmd is single-operator (ADR-013): one
 * credential, one principal, no accounts. The token is the same one the CLI
 * sends, held in localStorage because the alternative -- a cookie -- would be
 * sent by the browser on requests this app did not make.
 */

const STORAGE_KEY = "swarmd-operator-token";
const HEADER = "X-Swarmd-Token";

type Listener = (token: string) => void;
const listeners = new Set<Listener>();

export function operatorToken(): string {
  // localStorage throws outright in some privacy modes rather than returning
  // null, and a dashboard that white-screens because it could not read a
  // preference is worse than one with no token.
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setOperatorToken(token: string): void {
  try {
    if (token) window.localStorage.setItem(STORAGE_KEY, token);
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Held for this page's lifetime only. Better than refusing to work.
  }
  for (const listener of listeners) listener(token);
}

export function onTokenChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** `fetch` with the operator token attached when there is one. */
export async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const token = operatorToken();
  const headers: Record<string, string> = {
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  if (init?.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (token) headers[HEADER] = token;
  return fetch(path, { ...init, headers });
}

/**
 * `apiFetch` plus the JSON parse and the error that a caller would otherwise
 * write itself. A 401 is rewritten into the sentence that says what to do,
 * because `401: {"error":"operator token required"}` in a red line at the
 * bottom of a panel is not that sentence.
 */
export async function apiJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error(
        operatorToken()
          ? "operator token rejected — check the token in the top bar"
          : "operator token required — set it in the top bar",
      );
    }
    throw new Error(`${response.status}: ${(await response.text()).slice(0, 200)}`);
  }
  return (await response.json()) as T;
}

/**
 * The event stream's URL, token included.
 *
 * A websocket handshake carries no Authorization header the browser will let
 * us set, which is why the control plane accepts `?token=` here. It is a
 * same-origin loopback or in-cluster URL and is never logged by this app; the
 * server accepts the header form too, for clients that can send one.
 */
export function streamUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const token = operatorToken();
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${protocol}://${window.location.host}/api/stream${query}`;
}

export type AuthState = { token_required: boolean; token_ok: boolean };

/** What the control plane wants, and whether the stored token satisfies it. */
export async function authState(): Promise<AuthState> {
  return apiJson<AuthState>("/api/auth");
}
