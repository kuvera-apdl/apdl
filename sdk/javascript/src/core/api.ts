import type { ConsentState } from './config';
import type { ExperimentContext } from './types';
import type { FlagEvaluationOptions, FlagEvaluationResult } from '../flags/types';
import type { ComponentDefinition, UIConfig } from '../ui/components/types';
import type { ScrubFunction } from '../privacy/scrubber';

/**
 * The public surface of an APDL client.
 *
 * Both the real {@link APDLClient} and the inert no-op client returned during
 * SSR or when configuration is absent implement this interface, so callers can
 * use `init()`'s result uniformly without null checks.
 */
export interface APDLApi {
  track(event: string, properties?: Record<string, unknown>): void;
  identify(userId: string, traits?: Record<string, unknown>): void;
  group(groupId: string, traits?: Record<string, unknown>): void;
  page(name?: string, properties?: Record<string, unknown>): void;
  reset(): void;

  getVariant(key: string, options?: FlagEvaluationOptions): string | null;
  getVariantDetails(key: string, options?: FlagEvaluationOptions): FlagEvaluationResult;
  onVariantChange(key: string, callback: (variant: string | null) => void): () => void;

  shutdown(): Promise<void>;

  ui: {
    register: (definition: ComponentDefinition) => void;
    render: (config: UIConfig, target: HTMLElement) => HTMLElement | null;
    onSlotUpdate: (callback: (slotId: string, element: HTMLElement) => void) => () => void;
  };

  consent: {
    get: () => ConsentState;
    update: (partial: Partial<ConsentState>) => void;
    onUpdate: (callback: (state: ConsentState) => void) => () => void;
  };

  privacy: {
    addScrubber: (fn: ScrubFunction) => void;
    removeScrubber: (fn: ScrubFunction) => void;
  };

  experiments: {
    setContext: (context: ExperimentContext) => void;
    getContext: () => ExperimentContext;
    clearContext: () => void;
  };

  debug: {
    enable: () => void;
    disable: () => void;
    getQueue: () => unknown[];
    flush: () => Promise<void>;
  };
}
