import { afterEach, describe, expect, it, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { cleanup, renderHook } from '@testing-library/react';
import { APDLProvider, useAPDL } from '../../src/react';
import { NoopClient, type APDLApi } from '../../src';

afterEach(() => {
  cleanup();
});

function wrapperWith(client: APDLApi) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(APDLProvider, { client }, children);
  };
}

describe('@apdl-oss/sdk/react adapter', () => {
  it('provides the injected client through useAPDL', () => {
    const client = new NoopClient();
    const { result } = renderHook(() => useAPDL(), {
      wrapper: wrapperWith(client),
    });

    expect(result.current).toBe(client);
  });

  it('returns an inert no-op client when used outside a provider', () => {
    const { result } = renderHook(() => useAPDL());

    expect(result.current).toBeInstanceOf(NoopClient);
    expect(result.current.getVariant('flag')).toBeNull();
    expect(() => result.current.track('event')).not.toThrow();
  });

  it('routes tracking calls to the injected client', () => {
    const client = new NoopClient();
    const trackSpy = vi.spyOn(client, 'track');

    const { result } = renderHook(() => useAPDL(), {
      wrapper: wrapperWith(client),
    });
    result.current.track('cta_clicked', { id: 'hero' });

    expect(trackSpy).toHaveBeenCalledWith('cta_clicked', { id: 'hero' });
  });

  it('keeps the same client across re-renders (stable singleton ref)', () => {
    const client = new NoopClient();
    const { result, rerender } = renderHook(() => useAPDL(), {
      wrapper: wrapperWith(client),
    });

    const first = result.current;
    rerender();

    expect(result.current).toBe(first);
  });

  it('falls back to a no-op client when no config or env is provided', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const Wrapper = ({ children }: { children: ReactNode }) =>
      createElement(APDLProvider, {}, children);

    const { result } = renderHook(() => useAPDL(), { wrapper: Wrapper });

    expect(result.current).toBeInstanceOf(NoopClient);
    warn.mockRestore();
  });
});
