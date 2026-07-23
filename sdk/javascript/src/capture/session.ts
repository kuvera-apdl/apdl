import { generateId } from '../core/types';
import type { PersistenceMode } from '../core/config';
import {
  scopedBrowserStorageKey,
  type DeploymentStorageScope,
} from '../core/storage-scope';

const SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes

interface SessionData {
  id: string;
  startedAt: number;
  lastActivityAt: number;
  eventCount: number;
  pageCount: number;
}

/**
 * Client-side session management with 30-minute inactivity timeout.
 * Persists session to localStorage with in-memory fallback.
 */
export class SessionManager {
  private session: SessionData | null = null;
  private persistence: PersistenceMode;
  private storageKey: string;
  private enabled: boolean;

  constructor(
    persistence: PersistenceMode = 'localStorage',
    scope: DeploymentStorageScope,
    enabled = true
  ) {
    this.persistence = persistence;
    this.storageKey = scopedBrowserStorageKey('session', scope);
    this.enabled = enabled;
    if (enabled) {
      this.session = this.restore();
    } else {
      this.removePersisted();
    }
  }

  /**
   * Returns the current session ID, rotating if the session has expired.
   */
  getSessionId(): string {
    this.touch();
    return this.requireSession().id;
  }

  /**
   * Returns the full session data.
   */
  getSession(): Readonly<SessionData> {
    this.touch();
    return { ...this.requireSession() };
  }

  /**
   * Records activity — rotates session if inactive for 30+ minutes.
   */
  touch(): void {
    this.assertEnabled();
    const now = Date.now();
    const session = this.session;
    if (
      session === null ||
      now - session.lastActivityAt > SESSION_TIMEOUT_MS
    ) {
      this.session = this.createSession();
    } else {
      session.lastActivityAt = now;
    }
    this.persist();
  }

  /**
   * Increments the event counter for the current session.
   */
  recordEvent(): void {
    this.touch();
    this.requireSession().eventCount++;
    this.persist();
  }

  /**
   * Increments the page view counter for the current session.
   */
  recordPage(): void {
    this.touch();
    this.requireSession().pageCount++;
    this.persist();
  }

  /**
   * Forces a new session to start.
   */
  reset(): void {
    if (!this.enabled) {
      this.clear();
      return;
    }
    this.session = this.createSession();
    this.persist();
  }

  /**
   * Enables or disables analytics session state.
   *
   * A disabled manager neither restores nor creates a session. Disabling also
   * removes any state written while analytics consent was granted.
   */
  setEnabled(enabled: boolean): void {
    if (enabled === this.enabled) return;

    this.enabled = enabled;
    if (!enabled) {
      this.clear();
      return;
    }

    this.session = this.restore();
  }

  /** Clears both the in-memory session and its browser-storage record. */
  clear(): void {
    this.session = null;
    this.removePersisted();
  }

  private createSession(): SessionData {
    const now = Date.now();
    return {
      id: generateId(),
      startedAt: now,
      lastActivityAt: now,
      eventCount: 0,
      pageCount: 0,
    };
  }

  private persist(): void {
    if (
      !this.enabled ||
      this.session === null ||
      this.persistence !== 'localStorage'
    ) {
      return;
    }

    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(this.storageKey, JSON.stringify(this.session));
      }
    } catch {
      // localStorage may be full or unavailable; silently ignore
    }
  }

  private restore(): SessionData | null {
    if (!this.enabled || this.persistence !== 'localStorage') return null;

    try {
      if (typeof localStorage === 'undefined') return null;

      const raw = localStorage.getItem(this.storageKey);
      if (!raw) return null;

      const data = JSON.parse(raw) as SessionData;

      // Validate required fields
      if (
        typeof data.id !== 'string' ||
        typeof data.startedAt !== 'number' ||
        typeof data.lastActivityAt !== 'number'
      ) {
        return null;
      }

      // Check if session has expired
      if (Date.now() - data.lastActivityAt > SESSION_TIMEOUT_MS) {
        return null;
      }

      return data;
    } catch {
      return null;
    }
  }

  private removePersisted(): void {
    if (this.persistence !== 'localStorage') return;

    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.removeItem(this.storageKey);
      }
    } catch {
      // localStorage may be unavailable; the in-memory state is still cleared.
    }
  }

  private assertEnabled(): void {
    if (!this.enabled) {
      throw new Error('APDL: analytics session is unavailable without consent');
    }
  }

  private requireSession(): SessionData {
    if (this.session === null) {
      throw new Error('APDL: analytics session is not initialized');
    }
    return this.session;
  }
}
