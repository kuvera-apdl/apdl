import { APDLClient } from './core/client';
import type { APDLConfig } from './core/config';

// Re-export all public types
export type {
  APDLConfig,
  AutoCaptureConfig,
  ConsentState,
  ResolvedConfig,
} from './core/config';

export type { TrackEvent, EventContext } from './core/types';

export type {
  FlagConfig,
  TargetingRule,
  Condition,
  Variant,
  FlagResult,
  EvalContext,
} from './flags/types';

export type {
  ComponentDefinition,
  ComponentSchema,
  SchemaProperty,
  RenderContext,
  UIConfig,
} from './ui/components/types';

export type { ScrubFunction } from './privacy/scrubber';

// Re-export the client class
export { APDLClient };

// Re-export hash utilities for advanced usage
export { hashBucket, isInRollout, percentageBucket } from './flags/hash';

/**
 * APDL namespace — the primary entry point for the SDK.
 * Use APDL.init(config) to create a client instance.
 */
export const APDL = {
  /**
   * Initializes the APDL SDK and returns a client instance.
   */
  init(config: APDLConfig): APDLClient {
    return new APDLClient(config);
  },
};

