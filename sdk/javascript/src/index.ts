import { APDLClient } from './core/client';

// Re-export all public types
export type {
  APDLConfig,
  PartialAPDLConfig,
  APDLAuthConfig,
  AutoCaptureConfig,
  ConsentState,
  ResolvedConfig,
} from './core/config';

export type { APDLApi } from './core/api';

export type {
  TrackEvent,
  EventContext,
  ExperimentContext,
  DeliveryReport,
} from './core/types';

export type {
  FlagConfig,
  EvalContext,
  VariantConfig,
  FlagRule,
  FlagCondition,
  ConditionOperator,
  RolloutConfig,
  FallthroughConfig,
  FlagEvaluationResult,
  FlagEvaluationReason,
  FlagConfigSource,
  FlagEvaluationOptions,
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

// Re-export the no-op client for advanced/testing usage
export { NoopClient } from './core/noop-client';

// Re-export hash utilities for advanced usage
export { hashBucket, isInRollout, percentageBucket } from './flags/hash';

// Entry points: `init()` / `APDL.init()` for explicit setup, and the lazy
// `apdl` module-scope singleton for zero-config, SSR-safe usage.
import { apdl, APDL, init, maybeAutoStart } from './core/init';
export { apdl, APDL, init };

// Auto-start auto-capture on the first browser tick when env config is present.
maybeAutoStart();
