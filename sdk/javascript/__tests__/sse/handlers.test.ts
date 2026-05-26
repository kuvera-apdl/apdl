import { describe, expect, it } from 'vitest';
import { FlagCache } from '../../src/flags/cache';
import { SSEHandlers } from '../../src/sse/handlers';

describe('SSEHandlers', () => {
  it('should load canonical flags from config events', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify([{
        key: 'booking-flow',
        enabled: true,
        variant_type: 'boolean',
        default_value: 'false',
        rollout_percentage: 50,
        rules: [],
        variants: [],
      }]),
    });

    expect(cache.get('booking-flow')).toMatchObject({
      key: 'booking-flow',
      enabled: true,
      rollout_percentage: 50,
      rules: [],
      variants: [],
    });
  });

  it('should replace cache from flags_update payloads', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'flags_update',
      data: JSON.stringify({
        flags: [{
          key: 'existing',
          enabled: true,
          variant_type: 'boolean',
          default_value: 'false',
          rollout_percentage: 100,
          rules: [],
          variants: [],
        }],
      }),
    });

    expect(cache.getAll()).toHaveLength(1);
    expect(cache.get('existing')?.rollout_percentage).toBe(100);
  });

  it('should merge full flag_update payloads', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify([{
        key: 'existing',
        enabled: true,
        variant_type: 'boolean',
        default_value: 'false',
        rollout_percentage: 100,
        rules: [],
        variants: [],
      }]),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_created',
        flag: {
          key: 'created',
          enabled: true,
          variant_type: 'boolean',
          default_value: 'false',
          rollout_percentage: 20,
          rules: [],
          variants: [],
        },
      }),
    });

    expect(cache.get('existing')).toBeDefined();
    expect(cache.get('created')).toMatchObject({
      key: 'created',
      enabled: true,
      rollout_percentage: 20,
    });
  });

  it('should apply enabled-only deltas to cached flags for backward compatibility', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify([{
        key: 'toggle-me',
        enabled: true,
        variant_type: 'boolean',
        default_value: 'false',
        rollout_percentage: 100,
        rules: [],
        variants: [],
      }]),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_updated',
        key: 'toggle-me',
        enabled: false,
      }),
    });

    expect(cache.get('toggle-me')?.enabled).toBe(false);
  });

  it('should remove cached flags on delete events', () => {
    const cache = new FlagCache();
    const handlers = new SSEHandlers(cache, null);

    handlers.handle({
      type: 'config',
      data: JSON.stringify([{
        key: 'delete-me',
        enabled: true,
        variant_type: 'boolean',
        default_value: 'false',
        rollout_percentage: 100,
        rules: [],
        variants: [],
      }]),
    });

    handlers.handle({
      type: 'flag_update',
      data: JSON.stringify({
        action: 'flag_deleted',
        key: 'delete-me',
      }),
    });

    expect(cache.get('delete-me')).toBeUndefined();
  });
});
