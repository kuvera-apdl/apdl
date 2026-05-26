const DEFAULT_TIMEOUT = 10000;
const RETRY_DELAYS = [1000, 2000, 4000, 8000, 16000, 32000, 60000];

/**
 * HTTP transport layer with retry logic and beacon support.
 */
export class Transport {
  private timeout: number;
  private apiKey: string;
  private debug: boolean;

  constructor(apiKey: string, options?: { timeout?: number; debug?: boolean }) {
    this.apiKey = apiKey;
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
            'X-API-Key': this.apiKey,
            'X-APDL-SDK': 'js/0.1.0',
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

  /**
   * Sends a payload using navigator.sendBeacon for page unload scenarios.
   * Returns true if the browser accepted the beacon, false otherwise.
   */
  sendBeacon(url: string, payload: unknown): boolean {
    if (typeof navigator === 'undefined' || typeof navigator.sendBeacon !== 'function') {
      return false;
    }

    try {
      const blob = new Blob([JSON.stringify(payload)], {
        type: 'application/json',
      });

      // Append API key as query param since we can't set headers with sendBeacon
      const separator = url.includes('?') ? '&' : '?';
      const beaconUrl = `${url}${separator}api_key=${encodeURIComponent(this.apiKey)}`;

      return navigator.sendBeacon(beaconUrl, blob);
    } catch {
      return false;
    }
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}
