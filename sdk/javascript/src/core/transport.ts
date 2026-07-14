import {
  API_KEY_HEADER,
  SDK_IDENTIFIER,
  SDK_IDENTIFIER_HEADER,
} from './constants';

const DEFAULT_TIMEOUT = 10000;
const RETRY_DELAYS = [1000, 2000, 4000, 8000, 16000, 32000, 60000];

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
   * Returns true if the payload was accepted (2xx), false otherwise.
   */
  async send(url: string, payload: unknown): Promise<boolean> {
    const body = JSON.stringify(payload);

    for (let attempt = 0; attempt <= RETRY_DELAYS.length; attempt++) {
      try {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), this.timeout);

        const response = await fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            [API_KEY_HEADER]: this.clientKey,
            [SDK_IDENTIFIER_HEADER]: SDK_IDENTIFIER,
          },
          body,
          signal: controller.signal,
          keepalive: true,
        });

        clearTimeout(timeoutId);

        if (response.ok) {
          return true;
        }

        // Client errors (4xx except 429) are not retryable
        if (response.status >= 400 && response.status < 500 && response.status !== 429) {
          if (this.debug) {
            console.warn(
              `APDL: Non-retryable error ${response.status} from ${url}`
            );
          }
          return false;
        }

        // 429 or 5xx — retry
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
              await this.sleep(Math.min(retryMs, 60000));
              continue;
            }
          }
        }
      } catch (err) {
        if (this.debug) {
          console.warn(`APDL: Network error on attempt ${attempt + 1}:`, err);
        }
      }

      // Wait before retry, unless this was the last attempt
      if (attempt < RETRY_DELAYS.length) {
        const delay = RETRY_DELAYS[attempt];
        await this.sleep(delay);
      }
    }

    return false;
  }

  /** Sends one header-authenticated request that may outlive page unload. */
  async sendKeepalive(url: string, payload: unknown): Promise<boolean> {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          [API_KEY_HEADER]: this.clientKey,
          [SDK_IDENTIFIER_HEADER]: SDK_IDENTIFIER,
        },
        body: JSON.stringify(payload),
        keepalive: true,
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
