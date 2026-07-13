import type { APDLApi } from './api';
import {
  type ConsentState,
  type PartialAPDLConfig,
  resolveConfig,
  type ResolvedConfig,
} from './config';
import { API_KEY_HEADER, SDK_IDENTIFIER, SDK_IDENTIFIER_HEADER } from './constants';
import { generateId, type ExperimentContext } from './types';
import { Transport } from './transport';
import { OfflineStorage } from './storage';
import { EventQueue } from './event-queue';
import { ManualCapture } from '../capture/manual';
import { SessionManager } from '../capture/session';
import { ContextCollector } from '../capture/context';
import { AutoCapture } from '../capture/auto-capture';
import { HealthCapture } from '../capture/health';
import { FlagCache } from '../flags/cache';
import { FlagEvaluator } from '../flags/evaluator';
import { parseFlagConfigResult } from '../flags/schema';
import type { EvalContext, FlagEvaluationOptions, FlagEvaluationResult } from '../flags/types';
import { SSEConnection } from '../sse/connection';
import { SSEHandlers } from '../sse/handlers';
import { ComponentRegistry } from '../ui/registry';
import { UIRenderer } from '../ui/renderer';
import { SlotManager } from '../ui/slot';
import type { ComponentDefinition, UIConfig } from '../ui/components/types';
import { BannerComponent } from '../ui/components/banner';
import { ModalComponent } from '../ui/components/modal';
import { CTAButtonComponent } from '../ui/components/cta-button';
import { CardComponent } from '../ui/components/card';
import { ToastComponent } from '../ui/components/toast';
import { InlineMessageComponent } from '../ui/components/inline-message';
import { ConsentManager } from '../privacy/consent';
import { Scrubber, type ScrubFunction } from '../privacy/scrubber';
import { CookielessIdentity } from '../privacy/cookieless';

const ANON_ID_KEY = 'apdl_anonymous_id';
const FEATURE_FLAG_EXPOSURE_EVENT = '$feature_flag_exposure';

interface ActiveFlagState {
  variant: string;
  version: number | null;
}

/**
 * Main APDL client.
 * Orchestrates all SDK subsystems: event tracking, feature flags,
 * SSE real-time updates, UI components, and privacy controls.
 */
export class APDLClient implements APDLApi {
  private config: ResolvedConfig;
  private transport: Transport;
  private storage: OfflineStorage;
  private eventQueue: EventQueue;
  private manualCapture: ManualCapture;
  private sessionManager: SessionManager;
  private contextCollector: ContextCollector;
  private autoCapture: AutoCapture;
  private healthCapture: HealthCapture;
  private flagCache: FlagCache;
  private flagEvaluator: FlagEvaluator;
  private sseConnection: SSEConnection;
  private sseHandlers: SSEHandlers;
  private componentRegistry: ComponentRegistry;
  private uiRenderer: UIRenderer;
  private slotManager: SlotManager;
  private consentManager: ConsentManager;
  private scrubber: Scrubber;
  private variantChangeListeners: Map<string, Set<(variant: string | null) => void>> = new Map();
  private featureFlagExposureKeys: Set<string> = new Set();
  private missingFlagWarnings: Set<string> = new Set();
  private activeFlagStatesByPage: Map<string, Map<string, ActiveFlagState>> = new Map();
  private experimentContext: ExperimentContext = { attributes: {} };

  /** UI namespace */
  public ui: {
    register: (definition: ComponentDefinition) => void;
    render: (config: UIConfig, target: HTMLElement) => HTMLElement | null;
    onSlotUpdate: (callback: (slotId: string, element: HTMLElement) => void) => () => void;
  };

  /** Consent namespace */
  public consent: {
    get: () => ConsentState;
    update: (partial: Partial<ConsentState>) => void;
    onUpdate: (callback: (state: ConsentState) => void) => () => void;
  };

  /** Privacy namespace */
  public privacy: {
    addScrubber: (fn: ScrubFunction) => void;
    removeScrubber: (fn: ScrubFunction) => void;
  };

  /** Experiments namespace */
  public experiments: {
    setContext: (context: ExperimentContext) => void;
    getContext: () => ExperimentContext;
    clearContext: () => void;
  };

  /** Debug namespace */
  public debug: {
    enable: () => void;
    disable: () => void;
    getQueue: () => unknown[];
    flush: () => Promise<void>;
  };

  constructor(config: PartialAPDLConfig) {
    this.config = resolveConfig(config, { strict: true });

    // Privacy subsystems
    this.consentManager = new ConsentManager(
      this.config.consent,
      this.config.persistence
    );
    this.scrubber = new Scrubber();

    // Core transport
    this.transport = new Transport(this.config.auth.clientKey, {
      debug: this.config.debug,
    });
    this.storage = new OfflineStorage({ projectId: this.config.projectId });
    this.eventQueue = new EventQueue(
      this.config,
      this.transport,
      this.storage,
      this.scrubber,
      this.consentManager
    );

    // Session and context
    this.sessionManager = new SessionManager(this.config.persistence);
    this.contextCollector = new ContextCollector();

    // Anonymous ID
    const anonymousId = this.resolveAnonymousId();

    // Manual capture
    this.manualCapture = new ManualCapture(
      this.eventQueue,
      this.sessionManager,
      this.contextCollector,
      anonymousId
    );

    // Auto-capture
    this.autoCapture = new AutoCapture(
      this.config.autoCapture,
      this.manualCapture
    );

    // Feature flags
    this.flagCache = new FlagCache({
      persist: this.shouldPersistFlagCache(),
      storageKey: this.flagStorageKey(),
    });
    this.flagEvaluator = new FlagEvaluator(this.flagCache);

    // Wire up flag change notifications to per-key listeners
    this.flagCache.onChange(() => this.onEvaluationContextChanged());

    // Health capture
    this.healthCapture = new HealthCapture(
      this.config.autoCapture,
      this.manualCapture,
      this.contextCollector,
      () => this.activeFlagSnapshot()
    );

    // UI subsystem
    this.componentRegistry = new ComponentRegistry();
    this.registerBuiltInComponents();
    this.uiRenderer = new UIRenderer(
      this.componentRegistry,
      this.manualCapture,
      this.config.debug,
      (component, slotId, error) => {
        this.healthCapture.captureComponentRenderError(component, slotId, error);
      }
    );
    this.slotManager = new SlotManager();

    // SSE
    const sseUrl = `${this.config.endpoint}/v1/stream`;
    this.sseConnection = new SSEConnection(
      sseUrl,
      this.config.auth.clientKey,
      this.config.debug
    );
    this.sseHandlers = new SSEHandlers(
      this.flagCache,
      this.slotManager,
      this.config.debug
    );
    this.sseConnection.onMessage((msg) => this.sseHandlers.handle(msg));

    // Public namespace bindings
    this.ui = {
      register: (definition: ComponentDefinition) => {
        this.componentRegistry.register(definition);
      },
      render: (uiConfig: UIConfig, target: HTMLElement) => {
        return this.uiRenderer.render(uiConfig, target);
      },
      onSlotUpdate: (callback: (slotId: string, element: HTMLElement) => void) => {
        return this.slotManager.onSlotDiscovered(callback);
      },
    };

    this.consent = {
      get: () => this.consentManager.get(),
      update: (partial: Partial<ConsentState>) => this.consentManager.update(partial),
      onUpdate: (callback: (state: ConsentState) => void) =>
        this.consentManager.onUpdate(callback),
    };

    this.privacy = {
      addScrubber: (fn: ScrubFunction) => this.scrubber.addScrubber(fn),
      removeScrubber: (fn: ScrubFunction) => this.scrubber.removeScrubber(fn),
    };

    this.experiments = {
      setContext: (context: ExperimentContext) => {
        this.experimentContext = this.normalizeExperimentContext(context);
        this.onEvaluationContextChanged();
      },
      getContext: () => this.copyExperimentContext(this.experimentContext),
      clearContext: () => {
        this.experimentContext = { attributes: {} };
        this.onEvaluationContextChanged();
      },
    };

    this.debug = {
      enable: () => {
        (this.config as { debug: boolean }).debug = true;
      },
      disable: () => {
        (this.config as { debug: boolean }).debug = false;
      },
      getQueue: () => this.eventQueue.getQueue(),
      flush: () => this.eventQueue.flush(),
    };

    // Initialization: start subsystems
    this.initialize();
  }

  // ── Event tracking ────────────────────────────────────────────

  /**
   * Tracks a custom event.
   */
  track(event: string, properties?: Record<string, unknown>): void {
    this.manualCapture.trackEvent(event, properties);
  }

  /**
   * Identifies the current user.
   */
  identify(userId: string, traits?: Record<string, unknown>): void {
    this.manualCapture.identifyUser(userId, traits);
  }

  /**
   * Associates the user with a group.
   */
  group(groupId: string, traits?: Record<string, unknown>): void {
    this.manualCapture.groupUser(groupId, traits);
  }

  /**
   * Tracks a page view.
   */
  page(name?: string, properties?: Record<string, unknown>): void {
    this.manualCapture.pageView(name, properties);
  }

  /**
   * Resets the user identity and session.
   */
  reset(): void {
    this.manualCapture.reset();
    // Generate a new anonymous ID
    const newId = generateId();
    this.manualCapture.setAnonymousId(newId);
    this.persistAnonymousId(newId);
  }

  // ── Feature flags ─────────────────────────────────────────────

  /**
   * Evaluates a feature flag and returns its variant.
   */
  getVariant(key: string, options?: FlagEvaluationOptions): string | null {
    return this.getVariantDetails(key, options).variant;
  }

  /**
   * Evaluates a feature flag and returns explanation details.
   */
  getVariantDetails(key: string, options?: FlagEvaluationOptions): FlagEvaluationResult {
    const result = this.flagEvaluator.evaluate(key, this.getEvalContext());
    this.warnMissingFlag(result);
    this.rememberActiveFlag(result, options);
    this.logFeatureFlagExposure(result, options);
    return result;
  }

  /**
   * Registers a callback for variant changes.
   * Returns an unsubscribe function.
   */
  onVariantChange(key: string, callback: (variant: string | null) => void): () => void {
    if (!this.variantChangeListeners.has(key)) {
      this.variantChangeListeners.set(key, new Set());
    }
    const listeners = this.variantChangeListeners.get(key)!;
    listeners.add(callback);

    return () => {
      listeners.delete(callback);
      if (listeners.size === 0) {
        this.variantChangeListeners.delete(key);
      }
    };
  }

  // ── Shutdown ──────────────────────────────────────────────────

  /**
   * Gracefully shuts down the SDK, flushing remaining events.
   */
  async shutdown(): Promise<void> {
    this.autoCapture.stop();
    this.healthCapture.stop();
    this.sseConnection.disconnect();
    this.slotManager.stop();
    this.uiRenderer.cleanupAll();
    await this.eventQueue.flush();
    this.eventQueue.stop();
  }

  // ── Private ───────────────────────────────────────────────────

  private initialize(): void {
    // Start event queue (flush timer + offline drain)
    void this.eventQueue.start();

    // Start auto-capture
    this.autoCapture.start();

    // Start health capture
    this.healthCapture.start();

    // Start SSE connection for real-time config
    this.sseConnection.connect();

    // Start slot manager
    this.slotManager.start();

    // Fetch initial flag configuration
    void this.fetchInitialFlags();

    // Handle cookieless mode
    if (this.config.privacyMode === 'cookieless') {
      void this.setupCookielessId();
    }
  }

  private async fetchInitialFlags(): Promise<void> {
    try {
      const url = `${this.config.endpoint}/v1/flags`;
      const response = await fetch(url, {
        headers: {
          [API_KEY_HEADER]: this.config.auth.clientKey,
          [SDK_IDENTIFIER_HEADER]: SDK_IDENTIFIER,
        },
      });

      if (response.ok) {
        const data = await response.json();
        const result = parseFlagConfigResult(data);
        if (result !== null) {
          if (result.flags.length > 0 || result.invalid_keys.length === 0) {
            this.flagCache.set(result.flags, 'initial_fetch', result.invalid_keys);
          } else {
            this.flagCache.markInvalid(result.invalid_keys, 'initial_fetch');
          }
        }
      }
    } catch {
      if (this.config.debug) {
        console.warn('APDL: Failed to fetch initial flags');
      }
    }
  }

  private async setupCookielessId(): Promise<void> {
    try {
      const cookieless = new CookielessIdentity(this.config.auth.clientKey);
      const anonId = await cookieless.generateAnonymousId();
      this.manualCapture.setAnonymousId(anonId);
    } catch {
      if (this.config.debug) {
        console.warn('APDL: Failed to generate cookieless ID');
      }
    }
  }

  private resolveAnonymousId(): string {
    if (this.config.privacyMode === 'cookieless') {
      // Will be replaced async during initialization
      return generateId();
    }

    // Try to restore from persistence
    if (this.config.persistence !== 'memory') {
      try {
        if (typeof localStorage !== 'undefined') {
          const stored = localStorage.getItem(ANON_ID_KEY);
          if (stored) return stored;
        }
      } catch {
        // Storage unavailable
      }
    }

    const id = generateId();
    this.persistAnonymousId(id);
    return id;
  }

  private persistAnonymousId(id: string): void {
    if (this.config.persistence === 'memory') return;

    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(ANON_ID_KEY, id);
      }
    } catch {
      // Storage unavailable
    }
  }

  private getEvalContext(): EvalContext {
    const userAttributes = this.stringifyAttributes(this.manualCapture.getTraits());
    const experimentAttributes = this.stringifyAttributes(this.experimentContext.attributes);

    return {
      user_id: this.manualCapture.getUserId(),
      anonymous_id: this.manualCapture.getAnonymousId(),
      attributes: {
        ...userAttributes,
        ...experimentAttributes,
      },
    };
  }

  private logFeatureFlagExposure(
    result: FlagEvaluationResult,
    options?: FlagEvaluationOptions
  ): void {
    if (result.variant === null || result.reason === 'not_found' || result.reason === 'invalid_config') {
      return;
    }

    if (!this.consentManager.isGranted('analytics')) {
      return;
    }

    const page = this.currentPagePath(options?.page);
    const component = options?.component ?? '';
    const dedupeKey = this.featureFlagExposureKey(result, page, component);
    if (this.featureFlagExposureKeys.has(dedupeKey)) {
      return;
    }

    this.featureFlagExposureKeys.add(dedupeKey);
    this.manualCapture.trackEvent(FEATURE_FLAG_EXPOSURE_EVENT, {
      flag_key: result.key,
      variant: result.variant,
      reason: result.reason,
      rule_id: result.rule_id,
      rollout_bucket: result.rollout_bucket,
      variant_bucket: result.variant_bucket,
      rollout_percentage: result.rollout_percentage,
      bucket_by: result.bucket_by,
      config_version: result.config_version,
      source: result.source,
      page,
      component,
    });
  }

  private warnMissingFlag(result: FlagEvaluationResult): void {
    if (result.reason !== 'not_found' || this.missingFlagWarnings.has(result.key)) {
      return;
    }

    this.missingFlagWarnings.add(result.key);
    console.warn(
      `APDL: Feature flag '${result.key}' is missing or archived; returning null variant.`
    );
  }

  private featureFlagExposureKey(
    result: FlagEvaluationResult,
    page: string,
    component: string
  ): string {
    const userId = this.manualCapture.getUserId();
    const identity = userId
      ? `user:${userId}`
      : `anon:${this.manualCapture.getAnonymousId()}`;

    return JSON.stringify([
      this.sessionManager.getSessionId(),
      identity,
      result.key,
      result.config_version,
      result.variant,
      page,
      component,
    ]);
  }

  private currentPagePath(pageOverride?: string): string {
    return pageOverride ?? this.contextCollector.collect().page?.path ?? '';
  }

  private notifyFlagListeners(): void {
    for (const [key, listeners] of this.variantChangeListeners) {
      const result = this.flagEvaluator.evaluate(key, this.getEvalContext());
      for (const listener of listeners) {
        try {
          listener(result.variant);
        } catch {
          // Listener errors should not propagate
        }
      }
    }
  }

  private rememberActiveFlag(
    result: FlagEvaluationResult,
    options?: FlagEvaluationOptions
  ): void {
    const page = this.currentPagePath(options?.page);
    const pageStates = this.activeFlagStatesByPage.get(page);

    if (result.variant === null || result.reason === 'not_found' || result.reason === 'invalid_config') {
      pageStates?.delete(result.key);
      if (pageStates?.size === 0) {
        this.activeFlagStatesByPage.delete(page);
      }
      return;
    }

    const targetPageStates = pageStates ?? new Map<string, ActiveFlagState>();
    targetPageStates.set(result.key, {
      variant: result.variant,
      version: result.config_version,
    });
    this.activeFlagStatesByPage.set(page, targetPageStates);
  }

  /** Re-evaluates remembered flags and notifies listeners after anything that can change evaluation results. */
  private onEvaluationContextChanged(): void {
    this.refreshActiveFlagStates();
    this.notifyFlagListeners();
  }

  private refreshActiveFlagStates(): void {
    for (const [page, states] of Array.from(this.activeFlagStatesByPage.entries())) {
      for (const key of Array.from(states.keys())) {
        const result = this.flagEvaluator.evaluate(key, this.getEvalContext());
        if (result.variant === null || result.reason === 'not_found' || result.reason === 'invalid_config') {
          states.delete(key);
        } else {
          states.set(key, {
            variant: result.variant,
            version: result.config_version,
          });
        }
      }
      if (states.size === 0) {
        this.activeFlagStatesByPage.delete(page);
      }
    }
  }

  private activeFlagSnapshot(): {
    active_flags: Record<string, string>;
    active_flag_versions: Record<string, number>;
  } {
    const activeFlags: Record<string, string> = {};
    const activeFlagVersions: Record<string, number> = {};
    const pageStates = this.activeFlagStatesByPage.get(this.currentPagePath());

    for (const [key, state] of pageStates ?? []) {
      activeFlags[key] = state.variant;
      if (state.version !== null) {
        activeFlagVersions[key] = state.version;
      }
    }

    return {
      active_flags: activeFlags,
      active_flag_versions: activeFlagVersions,
    };
  }

  private normalizeExperimentContext(context: ExperimentContext): ExperimentContext {
    const input = this.assertPlainObject(context, 'experiments context');
    this.assertExactFields(input, ['attributes'], 'experiments context');
    const attributes = this.assertPlainObject(
      input.attributes,
      'experiments context.attributes'
    );

    return {
      attributes: this.cloneExperimentAttributes(attributes),
    };
  }

  private copyExperimentContext(context: ExperimentContext): ExperimentContext {
    return {
      attributes: this.cloneExperimentAttributes(context.attributes),
    };
  }

  private shouldPersistFlagCache(): boolean {
    return this.config.privacyMode === 'standard'
      && this.config.persistence === 'localStorage';
  }

  private flagStorageKey(): string {
    return `apdl_flags_${this.config.projectId}`;
  }

  private registerBuiltInComponents(): void {
    this.componentRegistry.register(BannerComponent);
    this.componentRegistry.register(ModalComponent);
    this.componentRegistry.register(CTAButtonComponent);
    this.componentRegistry.register(CardComponent);
    this.componentRegistry.register(ToastComponent);
    this.componentRegistry.register(InlineMessageComponent);
  }

  private stringifyAttributes(
    attributes: Record<string, unknown>
  ): Record<string, string> {
    const result: Record<string, string> = {};

    for (const [key, value] of Object.entries(attributes)) {
      if (value === undefined || value === null) {
        continue;
      }
      if (typeof value === 'string') {
        result[key] = value;
      } else if (typeof value === 'number' || typeof value === 'boolean') {
        result[key] = String(value);
      } else {
        result[key] = JSON.stringify(value);
      }
    }

    return result;
  }

  private assertPlainObject(value: unknown, path: string): Record<string, unknown> {
    if (!this.isPlainRecord(value)) {
      throw new Error(`APDL: ${path} is required and must be an object`);
    }

    return value;
  }

  private isPlainRecord(value: unknown): value is Record<string, unknown> {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) {
      return false;
    }

    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  private assertExactFields(
    value: Record<string, unknown>,
    supportedFields: string[],
    path: string
  ): void {
    const supported = new Set(supportedFields);
    for (const field of Object.keys(value)) {
      if (!supported.has(field)) {
        throw new Error(`APDL: ${path}.${field} is not supported`);
      }
    }
  }

  private cloneExperimentAttributes(
    attributes: Record<string, unknown>
  ): Record<string, unknown> {
    return this.cloneExperimentValue(attributes) as Record<string, unknown>;
  }

  private cloneExperimentValue(
    value: unknown,
    seen: WeakMap<object, unknown> = new WeakMap()
  ): unknown {
    if (value === null || typeof value !== 'object') {
      return value;
    }

    const existing = seen.get(value);
    if (existing !== undefined) {
      return existing;
    }

    if (value instanceof Date) {
      return new Date(value.getTime());
    }

    if (Array.isArray(value)) {
      const cloned: unknown[] = [];
      seen.set(value, cloned);
      for (const item of value) {
        cloned.push(this.cloneExperimentValue(item, seen));
      }
      return cloned;
    }

    if (value instanceof Map) {
      const cloned = new Map<unknown, unknown>();
      seen.set(value, cloned);
      for (const [mapKey, mapValue] of value.entries()) {
        cloned.set(
          this.cloneExperimentValue(mapKey, seen),
          this.cloneExperimentValue(mapValue, seen)
        );
      }
      return cloned;
    }

    if (value instanceof Set) {
      const cloned = new Set<unknown>();
      seen.set(value, cloned);
      for (const item of value.values()) {
        cloned.add(this.cloneExperimentValue(item, seen));
      }
      return cloned;
    }

    const source = value as Record<string, unknown>;
    const cloned = Object.create(Object.getPrototypeOf(value)) as Record<string, unknown>;
    seen.set(value, cloned);
    for (const key of Object.keys(source)) {
      cloned[key] = this.cloneExperimentValue(source[key], seen);
    }

    return cloned;
  }
}
