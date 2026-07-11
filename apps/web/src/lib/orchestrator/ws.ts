/**
 * WebSocket connector for `WS /ws/{session_id}` (Phase 6).
 *
 * Behaviours:
 *  - Reconnects up to 3 times on abnormal close.
 *  - Code 1008 (unknown session) terminates without retry — the
 *    server already decided this session is invalid.
 *  - Parses each text frame as `SessionEvent`; malformed frames are
 *    forwarded to `onError` rather than killing the stream.
 */

import type { SessionEvent } from "./schemas";

export interface EventStreamHandle {
  close(): void;
}

export interface EventStreamOptions {
  onEvent: (event: SessionEvent) => void;
  onError?: (error: unknown) => void;
  onClose?: (code: number) => void;
  maxRetries?: number;
}

/** Base delay (ms) for the first reconnect attempt. Doubles each retry. */
const RETRY_BASE_MS = 250;

/** Default max reconnect attempts before giving up. */
const MAX_RETRIES_DEFAULT = 3;

function toWebSocketUrl(httpBaseUrl: string, sessionId: string): string {
  const base = httpBaseUrl.replace(/^http/, "ws").replace(/\/+$/, "");
  return `${base}/ws/${encodeURIComponent(sessionId)}`;
}

export function connectEvents(
  baseUrl: string,
  sessionId: string,
  options: EventStreamOptions,
): EventStreamHandle {
  const maxRetries = options.maxRetries ?? MAX_RETRIES_DEFAULT;
  let attempt = 0;
  let socket: WebSocket | null = null;
  let closedByUser = false;

  const open = (): void => {
    socket = new WebSocket(toWebSocketUrl(baseUrl, sessionId));
    socket.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data as string) as SessionEvent;
        options.onEvent(data);
      } catch (err) {
        options.onError?.(err);
      }
    };
    socket.onerror = (err) => {
      options.onError?.(err);
    };
    socket.onclose = (event) => {
      if (closedByUser) {
        options.onClose?.(event.code);
        return;
      }
      if (event.code === 1008) {
        options.onClose?.(event.code);
        return;
      }
      if (attempt < maxRetries) {
        attempt += 1;
        // Intentional I/O scheduling in a retry back-off loop (ARCH001 exemption):
        // WebSocket reconnects are by definition network I/O repeated over time;
        // the delay grows linearly to avoid hammering the server.
        setTimeout(open, RETRY_BASE_MS * attempt);
        return;
      }
      options.onClose?.(event.code);
    };
  };

  open();

  return {
    close() {
      closedByUser = true;
      socket?.close();
    },
  };
}
