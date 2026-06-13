import { parseFlagConfigs } from './schema';
import type { FlagConfig, FlagConfigSource } from './types';

type FlagChangeCallback = (flags: FlagConfig[]) => void;

interface FlagCacheOptions {
  persist?: boolean;
  storageKey?: string;
}

const DEFAULT_STORAGE_KEY = 'apdl_flags';
const STORAGE_SCHEMA_VERSION = 2;

/**
 * In-memory flag configuration store with change notification.
 */
export class FlagCache {
  private flags: Map<string, FlagConfig> = new Map();
  private sources: Map<string, FlagConfigSource> = new Map();
  private invalidSources: Map<string, FlagConfigSource> = new Map();
  private version = 0;
  private listeners: Set<FlagChangeCallback> = new Set();
  private persistEnabled: boolean;
  private storageKey: string;

  constructor(options: FlagCacheOptions = {}) {
    this.persistEnabled = options.persist === true;
    this.storageKey = options.storageKey ?? DEFAULT_STORAGE_KEY;

    if (this.persistEnabled) {
      this.restorePersisted();
    }
  }

  /**
   * Bulk-updates all flag configurations.
   * Increments the version counter and notifies listeners.
   */
  set(
    flags: FlagConfig[],
    source: FlagConfigSource = 'memory',
    invalidKeys: string[] = []
  ): void {
    this.flags.clear();
    this.sources.clear();
    this.invalidSources.clear();
    for (const flag of flags) {
      this.flags.set(flag.key, flag);
      this.sources.set(flag.key, source);
    }
    for (const key of invalidKeys) {
      this.invalidSources.set(key, source);
    }
    this.version++;
    this.persistFlags();
    this.notifyListeners(flags);
  }

  /**
   * Returns the configuration for a single flag by key.
   */
  get(key: string): FlagConfig | undefined {
    return this.flags.get(key);
  }

  /**
   * Marks keyed records as malformed while preserving unrelated cached flags.
   */
  markInvalid(keys: string[], source: FlagConfigSource = 'memory'): void {
    for (const key of keys) {
      this.flags.delete(key);
      this.sources.delete(key);
      this.invalidSources.set(key, source);
    }
    this.version++;
    this.persistFlags();
    this.notifyListeners(this.getAll());
  }

  /**
   * Returns where a flag configuration most recently came from.
   */
  getSource(key: string): FlagConfigSource | null {
    return this.sources.get(key) ?? null;
  }

  /**
   * Returns whether the most recent config source contained a malformed record.
   */
  isInvalid(key: string): boolean {
    return this.invalidSources.has(key);
  }

  /**
   * Returns where a malformed flag configuration most recently came from.
   */
  getInvalidSource(key: string): FlagConfigSource | null {
    return this.invalidSources.get(key) ?? null;
  }

  /**
   * Returns all flag configurations.
   */
  getAll(): FlagConfig[] {
    return Array.from(this.flags.values());
  }

  /**
   * Returns the current monotonic version counter.
   * Increments each time flags are updated.
   */
  getVersion(): number {
    return this.version;
  }

  /**
   * Registers a callback to be called when flags change.
   * Returns an unsubscribe function.
   */
  onChange(callback: FlagChangeCallback): () => void {
    this.listeners.add(callback);
    return () => {
      this.listeners.delete(callback);
    };
  }

  private notifyListeners(flags: FlagConfig[]): void {
    for (const listener of this.listeners) {
      try {
        listener(flags);
      } catch {
        // Listener errors should not break the notification chain
      }
    }
  }

  private persistFlags(): void {
    if (!this.persistEnabled) return;

    try {
      if (typeof localStorage === 'undefined') return;

      localStorage.setItem(
        this.storageKey,
        JSON.stringify({
          schema_version: STORAGE_SCHEMA_VERSION,
          project_id: 'local_storage',
          flags: this.getAll(),
        })
      );
    } catch {
      // Storage may be unavailable or full; keep the in-memory cache.
    }
  }

  private restorePersisted(): void {
    try {
      if (typeof localStorage === 'undefined') return;

      const raw = localStorage.getItem(this.storageKey);
      if (!raw) return;

      const parsed = JSON.parse(raw) as unknown;
      const flags = parseFlagConfigs(parsed);
      if (flags === null) {
        localStorage.removeItem(this.storageKey);
        return;
      }

      this.flags.clear();
      this.sources.clear();
      this.invalidSources.clear();
      for (const flag of flags) {
        this.flags.set(flag.key, flag);
        this.sources.set(flag.key, 'local_storage');
      }
      this.version++;
    } catch {
      try {
        localStorage.removeItem(this.storageKey);
      } catch {
        // Ignore cleanup failure.
      }
    }
  }
}
