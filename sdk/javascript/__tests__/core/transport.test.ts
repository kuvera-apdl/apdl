import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Transport } from '../../src/core/transport';
import { CLIENT_KEY } from '../helpers';

describe('Transport', () => {
  let transport: Transport;

  beforeEach(() => {
    vi.useFakeTimers();
    transport = new Transport(CLIENT_KEY, { timeout: 5000 });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  describe('send()', () => {
    it('should send a POST request with correct headers', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: new Headers(),
      });
      vi.stubGlobal('fetch', fetchMock);

      const promise = transport.send('https://api.test.dev/v1/events', { data: 'test' });
      await vi.runAllTimersAsync();
      const result = await promise;

      expect(result).toBe('accepted');
      expect(fetchMock).toHaveBeenCalledTimes(1);

      const call = fetchMock.mock.calls[0];
      expect(call[0]).toBe('https://api.test.dev/v1/events');
      expect(call[1]).toMatchObject({
        method: 'POST',
        headers: expect.objectContaining({
          'Content-Type': 'application/json',
          'X-API-Key': CLIENT_KEY,
        }),
        body: JSON.stringify({ data: 'test' }),
      });
      expect(call[1]).not.toHaveProperty('keepalive');
    });

    it('should return true on 2xx response', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: true, status: 200, headers: new Headers() })
      );

      const promise = transport.send('https://api.test.dev/v1/events', {});
      await vi.runAllTimersAsync();
      expect(await promise).toBe('accepted');
    });

    it('should return permanent_rejection on non-transient 4xx without retry', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        headers: new Headers(),
      });
      vi.stubGlobal('fetch', fetchMock);

      const promise = transport.send('https://api.test.dev/v1/events', {});
      await vi.runAllTimersAsync();
      const result = await promise;

      expect(result).toBe('permanent_rejection');
      // Should NOT retry on 4xx
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it.each([302, 600])(
      'should permanently reject unexpected HTTP %i without retry',
      async (status) => {
        const fetchMock = vi.fn().mockResolvedValue({
          ok: false,
          status,
          headers: new Headers(),
        });
        vi.stubGlobal('fetch', fetchMock);

        const result = await transport.send(
          'https://api.test.dev/v1/events',
          {}
        );

        expect(result).toBe('permanent_rejection');
        expect(fetchMock).toHaveBeenCalledTimes(1);
      }
    );

    it('should retry on 429 rate limit', async () => {
      let callCount = 0;
      const fetchMock = vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount <= 2) {
          return Promise.resolve({
            ok: false,
            status: 429,
            headers: new Headers(),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
        });
      });
      vi.stubGlobal('fetch', fetchMock);

      const promise = transport.send('https://api.test.dev/v1/events', {});
      // Keep advancing timers to process retries
      for (let i = 0; i < 20; i++) {
        await vi.advanceTimersByTimeAsync(5000);
      }
      const result = await promise;

      expect(result).toBe('accepted');
      expect(callCount).toBe(3);
    });

    it.each([408, 425])('should retry transient HTTP %i responses', async (status) => {
      const fetchMock = vi.fn()
        .mockResolvedValueOnce({
          ok: false,
          status,
          headers: new Headers(),
        })
        .mockResolvedValueOnce({
          ok: true,
          status: 202,
          headers: new Headers(),
        });
      vi.stubGlobal('fetch', fetchMock);

      const promise = transport.send('https://api.test.dev/v1/events', {});
      await vi.advanceTimersByTimeAsync(5_000);

      expect(await promise).toBe('accepted');
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });

    it('should retry on 5xx server errors', async () => {
      let callCount = 0;
      const fetchMock = vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount <= 1) {
          return Promise.resolve({
            ok: false,
            status: 500,
            headers: new Headers(),
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
        });
      });
      vi.stubGlobal('fetch', fetchMock);

      const promise = transport.send('https://api.test.dev/v1/events', {});
      for (let i = 0; i < 20; i++) {
        await vi.advanceTimersByTimeAsync(5000);
      }
      const result = await promise;

      expect(result).toBe('accepted');
      expect(callCount).toBe(2);
    });

    it('should retry on network error', async () => {
      let callCount = 0;
      const fetchMock = vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount <= 1) {
          return Promise.reject(new Error('Network error'));
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: new Headers(),
        });
      });
      vi.stubGlobal('fetch', fetchMock);

      const promise = transport.send('https://api.test.dev/v1/events', {});
      for (let i = 0; i < 20; i++) {
        await vi.advanceTimersByTimeAsync(5000);
      }
      const result = await promise;

      expect(result).toBe('accepted');
      expect(callCount).toBe(2);
    });

    it('should return retryable after exhausting all retries', async () => {
      const fetchMock = vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        headers: new Headers(),
      });
      vi.stubGlobal('fetch', fetchMock);

      const promise = transport.send('https://api.test.dev/v1/events', {});
      // Advance enough for all 7 retries + initial attempt
      for (let i = 0; i < 40; i++) {
        await vi.advanceTimersByTimeAsync(10000);
      }
      const result = await promise;

      expect(result).toBe('retryable');
      // 1 initial + 7 retries = 8 total
      expect(fetchMock).toHaveBeenCalledTimes(8);
    });

    it('should permanently reject a locally non-serializable payload without fetching', async () => {
      const fetchMock = vi.fn();
      vi.stubGlobal('fetch', fetchMock);

      const result = await transport.send('https://api.test.dev/v1/events', {
        value: BigInt(1),
      });

      expect(result).toBe('permanent_rejection');
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });

  describe('sendKeepalive()', () => {
    it('should send a header-authenticated keepalive request', async () => {
      const fetchMock = vi.fn().mockResolvedValue({ ok: true });
      vi.stubGlobal('fetch', fetchMock);

      const result = await transport.sendKeepalive(
        'https://api.test.dev/v1/events',
        { test: true }
      );

      expect(result).toBe('accepted');
      expect(fetchMock).toHaveBeenCalledWith(
        'https://api.test.dev/v1/events',
        expect.objectContaining({
          method: 'POST',
          keepalive: true,
          headers: expect.objectContaining({ 'X-API-Key': CLIENT_KEY }),
        })
      );
    });

    it('should classify a permanent server rejection', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn().mockResolvedValue({ ok: false, status: 400 })
      );

      const result = await transport.sendKeepalive(
        'https://api.test.dev/v1/events',
        {}
      );

      expect(result).toBe('permanent_rejection');
    });

    it('should classify a network error as retryable', async () => {
      vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));

      const result = await transport.sendKeepalive(
        'https://api.test.dev/v1/events',
        {}
      );

      expect(result).toBe('retryable');
    });

    it.each([408, 425, 429, 503])(
      'should classify keepalive HTTP %i as retryable',
      async (status) => {
        vi.stubGlobal(
          'fetch',
          vi.fn().mockResolvedValue({ ok: false, status })
        );

        const result = await transport.sendKeepalive(
          'https://api.test.dev/v1/events',
          {}
        );

        expect(result).toBe('retryable');
      }
    );

    it.each([302, 600])(
      'should classify keepalive HTTP %i as a permanent rejection',
      async (status) => {
        vi.stubGlobal(
          'fetch',
          vi.fn().mockResolvedValue({ ok: false, status })
        );

        const result = await transport.sendKeepalive(
          'https://api.test.dev/v1/events',
          {}
        );

        expect(result).toBe('permanent_rejection');
      }
    );
  });
});
