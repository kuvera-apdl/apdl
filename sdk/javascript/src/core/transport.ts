import {
  API_KEY_HEADER,
  SDK_IDENTIFIER,
  SDK_IDENTIFIER_HEADER,
} from './constants';

const DEFAULT_TIMEOUT = 10000;
const RETRY_DELAYS = [1000, 2000, 4000, 8000, 16000, 32000, 60000];

export type TransportOutcome =
  | 'accepted'
  | 'retryable'
  | 'payload_rejected'
  | 'permanent_rejection';

/**
 * HTTP transport layer with retry logic and beacon support.
 */
export class Transport {
  private timeout: number;
  private clientKey: string;
  private debug: boolean;

  constructor(clientKey: string, options?: { timeout?: number; debug?: boolean }) {
    this.clientKey = clientKey;
    this.timeout = options?.timeout ?? DEFAULT_TIMEOUT;
    this.debug = options?.debug ?? false;
  }

  /**
   * Sends a payload to the given URL with retry on transient failures.
   * The final outcome distinguishes an accepted payload, a retryable delivery
   * failure, and a permanent HTTP/client rejection that must not be requeued.
   */
  async send(
    url: string,
    payload: unknown,
    signal?: AbortSignal
  ): Promise<TransportOutcome> {
    const body = this.serialize(payload);
    if (body === null) return 'permanent_rejection';

    for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt++) {
      if (signal?.aborted) return 'retryable';

      try {
        const response = await this.fetchWithTimeout(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            [API_KEY_HEADER]: this.clientKey,
            [SDK_IDENTIFIER_HEADER]: SDK_IDENTIFIER,
          },
          body,
        }, signal);

        if (response.ok) {
          return 'accepted';
        }

        // Validation and size rejections are kept distinct so the queue can
        // isolate one bad event without dropping its valid batch neighbors.
        // Other non-transient statuses apply to the request as a whole and
        // must never trigger payload bisection.
        const outcome = this.classifyErrorStatus(response.status);
        if (outcome !== 'retryable') {
          if (this.debug) {
            console.warn(
              `APDL: Non-retryable error ${response.status} from ${url}`
            );
          }
          return outcome;
        }

        // 408/425/429 or 5xx — retry.
        if (this.debug) {
          console.warn(
            `APDL: Retryable error ${response.status} from ${url}, attempt ${attempt + 1}`
          );
        }

        // If we have a Retry-After header for 429, respect it
        if (response.status === 429) {
          const retryAfter = response.headers.get('Retry-After');
          if (retryAfter) {
            const retryMs = parseInt(retryAfter, 10) * 1000;
            if (!isNaN(retryMs) && retryMs > 0) {
              await this.sleep(Math.min(retryMs, 60000), signal);
              continue;
            }
          }
        }
      } catch (err) {
        if (signal?.aborted) return 'retryable';
        if (this.debug) {
          console.warn(`APDL: Network error on attempt ${attempt + 1}:`, err);
        }
      }

      // Wait before retry, unless this was the last attempt
      if (attempt < RETRY_DELAYS.length) {
        const delay = RETRY_DELAYS[attempt];
        await this.sleep(delay, signal);
      }
    }

    return 'retryable';
  }

  /** Sends one header-authenticated request that may outlive page unload. */
  async sendKeepalive(
    url: string,
    payload: unknown,
    signal?: AbortSignal
  ): Promise<TransportOutcome> {
    const body = this.serialize(payload);
    if (body === null) return 'permanent_rejection';

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          [API_KEY_HEADER]: this.clientKey,
          [SDK_IDENTIFIER_HEADER]: SDK_IDENTIFIER,
        },
        body,
        keepalive: true,
        signal,
      });
      if (response.ok) return 'accepted';
      return this.classifyErrorStatus(response.status);
    } catch {
      return 'retryable';
    }
  }

  private serialize(payload: unknown): string | null {
    try {
      const body = JSON.stringify(payload);
      return typeof body === 'string' ? body : null;
    } catch (err) {
      if (this.debug) {
        console.warn('APDL: Payload is not JSON serializable:', err);
      }
      return null;
    }
  }

  private isRetryableStatus(status: number): boolean {
    return (
      status === 408 ||
      status === 425 ||
      status === 429 ||
      (status >= 500 && status <= 599)
    );
  }

  private classifyErrorStatus(
    status: number
  ): Exclude<TransportOutcome, 'accepted'> {
    if (status === 400 || status === 413 || status === 422) {
      return 'payload_rejected';
    }
    return this.isRetryableStatus(status)
      ? 'retryable'
      : 'permanent_rejection';
  }

  private async fetchWithTimeout(
    url: string,
    init: RequestInit,
    externalSignal?: AbortSignal
  ): Promise<Response> {
    const controller = new AbortController();
    const abortFromExternal = () => controller.abort();
    if (externalSignal?.aborted) {
      controller.abort();
    } else {
      externalSignal?.addEventListener('abort', abortFromExternal, { once: true });
    }
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      return await fetch(url, { ...init, signal: controller.signal });
    } finally {
      clearTimeout(timeoutId);
      externalSignal?.removeEventListener('abort', abortFromExternal);
    }
  }

  private sleep(ms: number, signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return Promise.resolve();

    return new Promise((resolve) => {
      const finish = () => {
        clearTimeout(timeoutId);
        signal?.removeEventListener('abort', finish);
        resolve();
      };
      const timeoutId = setTimeout(finish, ms);
      signal?.addEventListener('abort', finish, { once: true });
    });
  }
}
