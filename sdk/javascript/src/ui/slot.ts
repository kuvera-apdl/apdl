const SLOT_ATTRIBUTE = 'data-apdl-slot';

type SlotCallback = (slotId: string, element: HTMLElement) => void;

/**
 * SlotManager finds [data-apdl-slot] elements in the DOM and watches
 * for new ones via MutationObserver.
 */
export class SlotManager {
  private slots: Map<string, HTMLElement> = new Map();
  private observer: MutationObserver | null = null;
  private callbacks: Set<SlotCallback> = new Set();
  private active = false;

  /**
   * Starts scanning the DOM for slot elements and observing for new ones.
   */
  start(): void {
    if (this.active) return;
    if (typeof document === 'undefined') return;

    this.active = true;

    // Initial scan
    this.scanDocument();

    // Watch for new slots
    if (typeof MutationObserver !== 'undefined') {
      this.observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
          for (const node of Array.from(mutation.addedNodes)) {
            if (node instanceof HTMLElement) {
              this.scanElement(node);
            }
          }
        }
      });

      this.observer.observe(document.body, {
        childList: true,
        subtree: true,
      });
    }
  }

  /**
   * Pauses DOM observation while preserving subscribers for a later restart.
   */
  pause(): void {
    this.active = false;
    if (this.observer) {
      this.observer.disconnect();
      this.observer = null;
    }
    this.slots.clear();
  }

  /** Stops observing and releases all subscribers. */
  stop(): void {
    this.pause();
    this.callbacks.clear();
  }

  /**
   * Registers a callback that fires when a new slot is discovered.
   * Returns an unsubscribe function.
   */
  onSlotDiscovered(callback: SlotCallback): () => void {
    this.callbacks.add(callback);
    return () => {
      this.callbacks.delete(callback);
    };
  }

  /**
   * Returns a slot element by its ID.
   */
  getSlot(slotId: string): HTMLElement | undefined {
    return this.slots.get(slotId);
  }

  /**
   * Returns all discovered slot IDs.
   */
  getSlotIds(): string[] {
    return Array.from(this.slots.keys());
  }

  /**
   * Forces a re-scan of the document for slots.
   * Called when UI config is updated via SSE.
   */
  refresh(): void {
    if (typeof document === 'undefined') return;
    this.scanDocument();
  }

  private scanDocument(): void {
    const elements = document.querySelectorAll(`[${SLOT_ATTRIBUTE}]`);
    elements.forEach((el) => {
      if (el instanceof HTMLElement) {
        this.registerSlot(el);
      }
    });
  }

  private scanElement(element: HTMLElement): void {
    // Check the element itself
    if (element.hasAttribute(SLOT_ATTRIBUTE)) {
      this.registerSlot(element);
    }

    // Check descendants
    const descendants = element.querySelectorAll(`[${SLOT_ATTRIBUTE}]`);
    descendants.forEach((el) => {
      if (el instanceof HTMLElement) {
        this.registerSlot(el);
      }
    });
  }

  private registerSlot(element: HTMLElement): void {
    const slotId = element.getAttribute(SLOT_ATTRIBUTE);
    if (!slotId) return;

    // Skip if already registered with the same element
    if (this.slots.get(slotId) === element) return;

    this.slots.set(slotId, element);
    this.notifyCallbacks(slotId, element);
  }

  private notifyCallbacks(slotId: string, element: HTMLElement): void {
    for (const callback of this.callbacks) {
      try {
        callback(slotId, element);
      } catch {
        // Listener errors should not break the notification chain
      }
    }
  }
}
