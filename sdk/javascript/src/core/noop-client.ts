import type { ConsentState } from './config';
import type { APDLApi } from './api';
import type { ExperimentContext } from './types';
import type { DeliveryReport } from './types';
import type { FlagEvaluationResult } from '../flags/types';

const GRANTED_CONSENT: ConsentState = {
  analytics: true,
  personalization: true,
  experiments: true,
};

function missingFlagResult(key: string): FlagEvaluationResult {
  return {
    key,
    variant: null,
    reason: 'not_found',
    rule_id: null,
    rollout_bucket: null,
    variant_bucket: null,
    rollout_percentage: null,
    bucket_by: null,
    config_version: null,
    source: null,
  };
}

const noop = (): void => {};
const unsubscribe = (): void => {};
const emptyDeliveryReport = (): DeliveryReport => Object.freeze({
  delivered: 0,
  persisted: 0,
  permanentRejections: Object.freeze([]),
  discardedForConsent: 0,
  pending: Object.freeze([]),
  dropped: Object.freeze([]),
});

/**
 * An inert client that satisfies {@link APDLApi} without doing any work.
 *
 * Returned by `init()` during server-side rendering (no `window`) and when
 * configuration is absent in fail-soft mode. Every call is a safe no-op:
 * tracking is dropped, flags resolve to `null`, and subscriptions return an
 * unsubscribe that does nothing — so consumer code runs unchanged.
 */
export class NoopClient implements APDLApi {
  track(): void {}
  identify(): void {}
  group(): void {}
  page(): void {}
  reset(): void {}

  getVariant(): string | null {
    return null;
  }

  getVariantDetails(key: string): FlagEvaluationResult {
    return missingFlagResult(key);
  }

  onVariantChange(): () => void {
    return unsubscribe;
  }

  async shutdown(): Promise<DeliveryReport> {
    return emptyDeliveryReport();
  }

  ui = {
    register: noop,
    render: (): HTMLElement | null => null,
    onSlotUpdate: (): (() => void) => unsubscribe,
  };

  consent = {
    get: (): ConsentState => ({ ...GRANTED_CONSENT }),
    update: noop,
    onUpdate: (): (() => void) => unsubscribe,
  };

  privacy = {
    addScrubber: noop,
    removeScrubber: noop,
  };

  experiments = {
    setContext: noop,
    getContext: (): ExperimentContext => ({ attributes: {} }),
    clearContext: noop,
  };

  debug = {
    enable: noop,
    disable: noop,
    getQueue: (): unknown[] => [],
    flush: async (): Promise<DeliveryReport> => emptyDeliveryReport(),
  };
}

/** Shared inert client; safe to reuse since it holds no per-call state. */
export const noopClient = new NoopClient();
