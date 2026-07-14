import type { ConsentState } from '../core/config';

type ConsentCategory = keyof ConsentState;
type ConsentCallback = (state: ConsentState) => void;

/**
 * Consent state machine.
 * Manages per-category consent (analytics, personalization, experiments)
 * with persistence to localStorage.
 */
export class ConsentManager {
  private state: ConsentState;
  private listeners: Set<ConsentCallback> = new Set();
  private persistence: 'localStorage' | 'cookie' | 'memory';
  private storageKey: string;

  constructor(
    initialState: ConsentState,
    persistence: 'localStorage' | 'cookie' | 'memory' = 'localStorage',
    projectId: string
  ) {
    this.persistence = persistence;
    this.storageKey = `apdl_consent_${projectId}`;

    // Try to restore from persistence, falling back to initial state
    const restored = this.restore();
    this.state = restored ?? { ...initialState };
    this.persist();
  }

  /**
   * Returns the current consent state.
   */
  get(): ConsentState {
    return { ...this.state };
  }

  /**
   * Updates consent state. Partial updates are merged.
   */
  update(partial: Partial<ConsentState>): void {
    const previous = { ...this.state };
    this.state = { ...this.state, ...partial };
    this.persist();

    // Notify listeners if anything changed
    if (
      previous.analytics !== this.state.analytics ||
      previous.personalization !== this.state.personalization ||
      previous.experiments !== this.state.experiments
    ) {
      this.notifyListeners();
    }
  }

  /**
   * Checks if consent is granted for a specific category.
   */
  isGranted(category: ConsentCategory): boolean {
    return this.state[category] === true;
  }

  /**
   * Registers a callback that fires when consent state changes.
   * Returns an unsubscribe function.
   */
  onUpdate(callback: ConsentCallback): () => void {
    this.listeners.add(callback);
    return () => {
      this.listeners.delete(callback);
    };
  }

  /**
   * Grants all consent categories.
   */
  grantAll(): void {
    this.update({
      analytics: true,
      personalization: true,
      experiments: true,
    });
  }

  /**
   * Denies all consent categories.
   */
  denyAll(): void {
    this.update({
      analytics: false,
      personalization: false,
      experiments: false,
    });
  }

  private persist(): void {
    if (this.persistence === 'memory') return;

    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(this.storageKey, JSON.stringify(this.state));
      }
    } catch {
      // Storage may be full or unavailable
    }
  }

  private restore(): ConsentState | null {
    if (this.persistence === 'memory') return null;

    try {
      if (typeof localStorage === 'undefined') return null;

      const raw = localStorage.getItem(this.storageKey);
      if (!raw) return null;

      const parsed = JSON.parse(raw) as ConsentState;

      // Validate shape
      if (
        typeof parsed.analytics !== 'boolean' ||
        typeof parsed.personalization !== 'boolean' ||
        typeof parsed.experiments !== 'boolean'
      ) {
        return null;
      }

      return parsed;
    } catch {
      return null;
    }
  }

  private notifyListeners(): void {
    const snapshot = this.get();
    for (const listener of this.listeners) {
      try {
        listener(snapshot);
      } catch {
        // Listener errors should not break the notification chain
      }
    }
  }
}
