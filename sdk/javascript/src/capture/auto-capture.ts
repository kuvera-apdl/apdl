import type { AutoCaptureConfig } from '../core/config';
import type { ManualCapture } from './manual';
import { shouldCapture } from '../privacy/no-capture';

interface ClickRecord {
  target: EventTarget | null;
  timestamp: number;
}

const SENSITIVE_AUTOCOMPLETE_TOKENS = new Set([
  'current-password',
  'new-password',
  'one-time-code',
  'transaction-amount',
  'transaction-currency',
]);

const SENSITIVE_EDITABLE_HINT =
  /(?:^|[-_\s])(password|passwd|passcode|one[-_\s]?time[-_\s]?code|otp|card[-_\s]?(?:number|security|cvc|cvv)|cvc|cvv)(?:$|[-_\s])/i;

/**
 * Auto-capture module that listens for DOM events and generates
 * tracking events automatically based on configuration.
 */
export class AutoCapture {
  private config: AutoCaptureConfig;
  private capture: ManualCapture;
  private active = false;

  // Listener references for cleanup
  private clickHandler: ((e: MouseEvent) => void) | null = null;
  private submitHandler: ((e: SubmitEvent) => void) | null = null;
  private inputHandler: ((e: Event) => void) | null = null;
  private scrollHandler: (() => void) | null = null;
  private popstateHandler: (() => void) | null = null;

  // Rage click tracking
  private recentClicks: ClickRecord[] = [];
  private readonly RAGE_CLICK_THRESHOLD = 3;
  private readonly RAGE_CLICK_WINDOW = 500;

  // Scroll depth tracking
  private scrollThresholds = new Set<number>([25, 50, 75, 100]);
  private reportedThresholds = new Set<number>();
  private currentPageUrl = '';

  constructor(config: AutoCaptureConfig, capture: ManualCapture) {
    this.config = config;
    this.capture = capture;
  }

  /**
   * Starts auto-capture by attaching DOM event listeners.
   */
  start(): void {
    if (this.active) return;
    if (typeof document === 'undefined' || typeof window === 'undefined') return;

    this.active = true;

    // Page views
    if (this.config.pageViews) {
      this.capture.pageView();
      this.currentPageUrl = window.location.href;

      this.popstateHandler = () => {
        if (window.location.href !== this.currentPageUrl) {
          this.currentPageUrl = window.location.href;
          this.resetScrollTracking();
          this.capture.pageView();
        }
      };
      window.addEventListener('popstate', this.popstateHandler);
    }

    // Click tracking
    if (this.config.clicks || this.config.rage_clicks) {
      this.clickHandler = (e: MouseEvent) => {
        const target = e.target;
        if (
          !(target instanceof Element) ||
          !shouldCapture(target) ||
          this.isSensitiveClickEvent(e, target)
        ) {
          return;
        }

        if (this.config.clicks) {
          this.capture.trackEvent('$click', {
            tag: target.tagName?.toLowerCase(),
            x: e.clientX,
            y: e.clientY,
          });
        }

        if (this.config.rage_clicks) {
          this.detectRageClick(e);
        }
      };
      document.addEventListener('click', this.clickHandler, true);
    }

    // Form submission tracking
    if (this.config.formSubmissions) {
      this.submitHandler = (e: SubmitEvent) => {
        const form = e.target as HTMLFormElement | null;
        if (!form || !shouldCapture(form)) return;

        this.capture.trackEvent('$form_submit', {
          formMethod: form.method || undefined,
        });
      };
      document.addEventListener('submit', this.submitHandler, true);
    }

    // Input change tracking (debounced)
    if (this.config.inputChanges) {
      this.inputHandler = (e: Event) => {
        const target = e.target as HTMLInputElement | null;
        if (!target || !shouldCapture(target)) return;

        const tagName = target.tagName?.toLowerCase();
        if (tagName !== 'input' && tagName !== 'select' && tagName !== 'textarea') {
          return;
        }

        // Never capture the actual value for privacy
        this.capture.trackEvent('$input_change', {
          tag: tagName,
          inputType: target.type || undefined,
          hasValue: !!target.value,
        });
      };
      document.addEventListener('change', this.inputHandler, true);
    }

    // Scroll depth tracking
    if (this.config.scrollDepth) {
      this.resetScrollTracking();

      let scrollTimeout: ReturnType<typeof setTimeout> | null = null;
      this.scrollHandler = () => {
        if (scrollTimeout) return;
        scrollTimeout = setTimeout(() => {
          scrollTimeout = null;
          this.trackScrollDepth();
        }, 150);
      };
      window.addEventListener('scroll', this.scrollHandler, { passive: true });
    }
  }

  /**
   * Stops auto-capture and removes all event listeners.
   */
  stop(): void {
    if (!this.active) return;
    this.active = false;

    if (typeof document === 'undefined' || typeof window === 'undefined') return;

    if (this.clickHandler) {
      document.removeEventListener('click', this.clickHandler, true);
      this.clickHandler = null;
    }

    if (this.submitHandler) {
      document.removeEventListener('submit', this.submitHandler, true);
      this.submitHandler = null;
    }

    if (this.inputHandler) {
      document.removeEventListener('change', this.inputHandler, true);
      this.inputHandler = null;
    }

    if (this.scrollHandler) {
      window.removeEventListener('scroll', this.scrollHandler);
      this.scrollHandler = null;
    }

    if (this.popstateHandler) {
      window.removeEventListener('popstate', this.popstateHandler);
      this.popstateHandler = null;
    }
  }

  private detectRageClick(e: MouseEvent): void {
    const now = Date.now();

    // Clean up old clicks outside the window
    this.recentClicks = this.recentClicks.filter(
      (c) => now - c.timestamp < this.RAGE_CLICK_WINDOW
    );

    this.recentClicks.push({ target: e.target, timestamp: now });

    // Check if we have enough clicks on the same element
    const targetClicks = this.recentClicks.filter(
      (c) => c.target === e.target
    );

    if (targetClicks.length >= this.RAGE_CLICK_THRESHOLD) {
      const target = e.target as Element | null;
      this.capture.trackEvent('$rage_click', {
        tag: target?.tagName?.toLowerCase(),
        clickCount: targetClicks.length,
        x: e.clientX,
        y: e.clientY,
      });

      // Reset so we don't fire repeatedly
      this.recentClicks = [];
    }
  }

  private trackScrollDepth(): void {
    const scrollTop = window.scrollY || document.documentElement.scrollTop;
    const docHeight = Math.max(
      document.body.scrollHeight,
      document.documentElement.scrollHeight,
      document.body.offsetHeight,
      document.documentElement.offsetHeight
    );
    const winHeight = window.innerHeight;
    const scrollableHeight = docHeight - winHeight;

    if (scrollableHeight <= 0) return;

    const scrollPercent = Math.min(
      100,
      Math.round((scrollTop / scrollableHeight) * 100)
    );

    for (const threshold of this.scrollThresholds) {
      if (scrollPercent >= threshold && !this.reportedThresholds.has(threshold)) {
        this.reportedThresholds.add(threshold);
        this.capture.trackEvent('$scroll_depth', {
          threshold,
          percent: scrollPercent,
        });
      }
    }
  }

  private resetScrollTracking(): void {
    this.reportedThresholds.clear();
  }

  /**
   * Sensitive controls are excluded rather than merely redacted. This check
   * happens before rage-click bookkeeping, so excluded clicks cannot later
   * produce a rage-click event.
   */
  private isSensitiveClickEvent(event: MouseEvent, target: Element): boolean {
    if (this.isSensitiveClickTarget(target)) {
      return true;
    }

    // In a web component, event.target is retargeted to the shadow host. The
    // composed path retains the actual control and lets us keep the same
    // privacy boundary for native and custom elements.
    return event.composedPath().some((pathTarget) => {
      return (
        pathTarget instanceof Element &&
        pathTarget !== target &&
        this.isSensitiveClickTarget(pathTarget)
      );
    });
  }

  private isSensitiveClickTarget(target: Element): boolean {
    let current: Element | null = target;

    while (current) {
      if (this.isSensitiveElement(current)) {
        return true;
      }

      // A click on a label (or one of its descendants) can activate the
      // associated control, including a password or payment field.
      if (current.tagName?.toLowerCase() === 'label') {
        const control = (current as HTMLLabelElement).control;
        if (control && this.isSensitiveElement(control)) {
          return true;
        }
      }

      current = current.parentElement;
    }

    return false;
  }

  private isSensitiveElement(element: Element): boolean {
    const tag = element.tagName?.toLowerCase();
    const type = element.getAttribute('type')?.trim().toLowerCase();

    if (tag === 'input' && (type === 'password' || type === 'file')) {
      return true;
    }

    const autocomplete = element.getAttribute('autocomplete');
    if (autocomplete) {
      const tokens = autocomplete.toLowerCase().split(/\s+/);
      if (
        tokens.some(
          (token) =>
            token.startsWith('cc-') ||
            SENSITIVE_AUTOCOMPLETE_TOKENS.has(token)
        )
      ) {
        return true;
      }
    }

    if (!this.isEditableElement(element)) {
      return false;
    }

    // Custom password/OTP/payment widgets may expose semantics through their
    // editable element even when they are not native password inputs.
    return ['name', 'id', 'aria-label'].some((attribute) => {
      const hint = element.getAttribute(attribute);
      return hint !== null && SENSITIVE_EDITABLE_HINT.test(hint);
    });
  }

  private isEditableElement(element: Element): boolean {
    const tag = element.tagName?.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      return true;
    }

    const contenteditable = element.getAttribute('contenteditable');
    if (contenteditable !== null && contenteditable.toLowerCase() !== 'false') {
      return true;
    }

    const role = element.getAttribute('role')?.toLowerCase();
    return role === 'textbox' || role === 'searchbox' || role === 'combobox';
  }
}
