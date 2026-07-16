import { parseFlagConfigs } from './schema';
import { isIdentifier } from './targeting-contract';
import type { FlagConfig, FlagConfigSource } from './types';

type FlagChangeCallback = (flags: FlagConfig[]) => void;

interface FlagCacheOptions {
  persist?: boolean;
  storageKey?: string;
}

interface PersistedFlagCache {
  schema_version: 3;
  project_id: string;
  flags: FlagConfig[];
  versions: Record<string, number>;
}

const DEFAULT_STORAGE_KEY = 'apdl_flags';
const STORAGE_SCHEMA_VERSION = 3;
const PERSISTED_CACHE_KEYS = new Set([
  'schema_version',
  'project_id',
  'flags',
  'versions',
]);

/**
 * In-memory flag configuration store with change notification.
 */
export class FlagCache {
  private flags: Map<string, FlagConfig> = new Map();
  private sources: Map<string, FlagConfigSource> = new Map();
  private invalidSources: Map<string, FlagConfigSource> = new Map();
  /** Highest authoritative version observed for active flags and tombstones. */
  private authoritativeVersions: Map<string, number> = new Map();
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
    const previousFlags = new Map(this.flags);
    const previousSources = new Map(this.sources);
    const nextFlags = new Map<string, FlagConfig>();
    const nextSources = new Map<string, FlagConfigSource>();

    for (const flag of flags) {
      const knownVersion = this.authoritativeVersions.get(flag.key);
      const previous = previousFlags.get(flag.key);

      if (knownVersion !== undefined && flag.version <= knownVersion) {
        // Equal/older snapshots cannot overwrite an active value or resurrect
        // a key whose latest authoritative event was a removal.
        if (previous) {
          nextFlags.set(flag.key, previous);
          nextSources.set(flag.key, previousSources.get(flag.key) ?? source);
        }
        continue;
      }

      nextFlags.set(flag.key, flag);
      nextSources.set(flag.key, source);
      this.authoritativeVersions.set(flag.key, flag.version);
    }

    this.flags.clear();
    this.sources.clear();
    this.invalidSources.clear();
    for (const [key, flag] of nextFlags) {
      this.flags.set(key, flag);
      this.sources.set(key, nextSources.get(key) ?? source);
    }
    for (const key of invalidKeys) {
      this.invalidSources.set(key, source);
    }
    this.version++;
    this.persistFlags();
    this.notifyListeners(this.getAll());
  }

  /** Apply one canonical update only when its event version advances the key. */
  upsertIfNewer(
    flag: FlagConfig,
    authoritativeVersion: number,
    source: FlagConfigSource = 'sse'
  ): boolean {
    if (
      flag.version !== authoritativeVersion
      || !this.isNewerVersion(flag.key, authoritativeVersion)
    ) {
      return false;
    }

    this.flags.set(flag.key, flag);
    this.sources.set(flag.key, source);
    this.invalidSources.delete(flag.key);
    this.authoritativeVersions.set(flag.key, authoritativeVersion);
    this.didMutate();
    return true;
  }

  /** Record a deletion tombstone and remove the flag only for a newer event. */
  removeIfNewer(
    key: string,
    authoritativeVersion: number
  ): boolean {
    if (!this.isNewerVersion(key, authoritativeVersion)) return false;

    this.flags.delete(key);
    this.sources.delete(key);
    this.invalidSources.delete(key);
    this.authoritativeVersions.set(key, authoritativeVersion);
    this.didMutate();
    return true;
  }

  /** Mark a malformed keyed update only when its envelope version is newer. */
  markInvalidIfNewer(
    key: string,
    authoritativeVersion: number,
    source: FlagConfigSource = 'sse'
  ): boolean {
    if (!this.isNewerVersion(key, authoritativeVersion)) return false;

    this.flags.delete(key);
    this.sources.delete(key);
    this.invalidSources.set(key, source);
    this.authoritativeVersions.set(key, authoritativeVersion);
    this.didMutate();
    return true;
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

  /** Highest observed per-flag version, including deletion tombstones. */
  getAuthoritativeVersion(key: string): number | null {
    return this.authoritativeVersions.get(key) ?? null;
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

  private isNewerVersion(key: string, version: number): boolean {
    return Number.isInteger(version)
      && version >= 1
      && version > (this.authoritativeVersions.get(key) ?? 0);
  }

  private didMutate(): void {
    this.version++;
    this.persistFlags();
    this.notifyListeners(this.getAll());
  }

  private persistFlags(): void {
    if (!this.persistEnabled) return;

    try {
      if (typeof localStorage === 'undefined') return;

      const persisted: PersistedFlagCache = {
        schema_version: STORAGE_SCHEMA_VERSION,
        project_id: 'local_storage',
        flags: this.getAll(),
        versions: Object.fromEntries(this.authoritativeVersions),
      };
      localStorage.setItem(this.storageKey, JSON.stringify(persisted));
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
      const flags = this.parsePersistedFlags(parsed);
      if (flags === null) {
        localStorage.removeItem(this.storageKey);
        return;
      }

      this.flags.clear();
      this.sources.clear();
      this.invalidSources.clear();
      this.authoritativeVersions.clear();
      for (const flag of flags) {
        const persistedVersion = this.readPersistedVersion(parsed, flag.key);
        const authoritativeVersion = Math.max(flag.version, persistedVersion ?? 0);
        this.authoritativeVersions.set(flag.key, authoritativeVersion);

        // A ledger version newer than the stored value represents a tombstone
        // or otherwise proves that the active value is stale.
        if (authoritativeVersion > flag.version) continue;

        this.flags.set(flag.key, flag);
        this.sources.set(flag.key, 'local_storage');
      }
      this.restoreTombstoneVersions(parsed);
      this.version++;
    } catch {
      try {
        localStorage.removeItem(this.storageKey);
      } catch {
        // Ignore cleanup failure.
      }
    }
  }

  private parsePersistedFlags(input: unknown): FlagConfig[] | null {
    if (
      !isRecord(input)
      || input.schema_version !== STORAGE_SCHEMA_VERSION
      || !hasOnlyKeys(input, PERSISTED_CACHE_KEYS)
    ) {
      return null;
    }

    const versions = input.versions;
    if (
      !isRecord(versions)
      || !Object.entries(versions).every(
        ([key, version]) => isIdentifier(key) && isPositiveInteger(version)
      )
    ) {
      return null;
    }

    const flags = parseFlagConfigs({
      schema_version: 2,
      project_id: input.project_id,
      flags: input.flags,
    });
    if (
      flags === null
      || flags.some((flag) => versions[flag.key] !== flag.version)
    ) {
      return null;
    }

    return flags;
  }

  private readPersistedVersion(input: unknown, key: string): number | null {
    if (!isRecord(input) || input.schema_version !== STORAGE_SCHEMA_VERSION) return null;
    if (!isRecord(input.versions)) return null;
    const version = input.versions[key];
    return isPositiveInteger(version) ? version : null;
  }

  private restoreTombstoneVersions(input: unknown): void {
    if (!isRecord(input) || input.schema_version !== STORAGE_SCHEMA_VERSION) return;
    if (!isRecord(input.versions)) return;

    for (const [key, rawVersion] of Object.entries(input.versions)) {
      if (!isPositiveInteger(rawVersion)) continue;
      const activeVersion = this.flags.get(key)?.version ?? 0;
      this.authoritativeVersions.set(
        key,
        Math.max(activeVersion, rawVersion)
      );
    }
  }
}

function isRecord(input: unknown): input is Record<string, unknown> {
  return typeof input === 'object' && input !== null && !Array.isArray(input);
}

function hasOnlyKeys(
  input: Record<string, unknown>,
  allowed: Set<string>
): boolean {
  return Object.keys(input).every((key) => allowed.has(key));
}

function isPositiveInteger(input: unknown): input is number {
  return typeof input === 'number' && Number.isInteger(input) && input >= 1;
}
