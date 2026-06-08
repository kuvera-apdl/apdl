import { onCLS, onINP, onLCP, type MetricType } from 'web-vitals';
import type { AutoCaptureConfig } from '../core/config';
import type { ManualCapture } from './manual';
import type { ContextCollector } from './context';

export const FRONTEND_ERROR_EVENT = '$frontend_error';
export const WEB_VITAL_EVENT = '$web_vital';

type ActiveFlagSnapshot = {
  active_flags: Record<string, string>;
  active_flag_versions: Record<string, number>;
};

type ActiveFlagProvider = () => ActiveFlagSnapshot;

const MAX_MESSAGE_LENGTH = 1024;
const MAX_STACK_LENGTH = 4096;

/**
 * Captures browser health signals and attaches the currently evaluated flag
 * state so guardrails can attribute frontend failures to active flags.
 */
export class HealthCapture {
  private config: AutoCaptureConfig;
  private capture: ManualCapture;
  private contextCollector: ContextCollector;
  private activeFlags: ActiveFlagProvider;
  private active = false;
  private errorHandler: ((event: ErrorEvent) => void) | null = null;
  private rejectionHandler: ((event: PromiseRejectionEvent) => void) | null = null;

  constructor(
    config: AutoCaptureConfig,
    capture: ManualCapture,
    contextCollector: ContextCollector,
    activeFlags: ActiveFlagProvider
  ) {
    this.config = config;
    this.capture = capture;
    this.contextCollector = contextCollector;
    this.activeFlags = activeFlags;
  }

  start(): void {
    if (this.active) return;
    if (typeof window === 'undefined') return;

    this.active = true;

    if (this.config.frontend_errors) {
      this.errorHandler = (event: ErrorEvent) => {
        this.captureError('javascript_error', {
          message: event.message || this.messageFromUnknown(event.error),
          source: event.filename || '',
          line: finiteNumberOrNull(event.lineno),
          column: finiteNumberOrNull(event.colno),
          stack: stackFromUnknown(event.error),
        });
      };
      window.addEventListener('error', this.errorHandler);

      this.rejectionHandler = (event: PromiseRejectionEvent) => {
        this.captureError('unhandled_rejection', {
          message: this.messageFromUnknown(event.reason),
          source: '',
          line: null,
          column: null,
          stack: stackFromUnknown(event.reason),
        });
      };
      window.addEventListener('unhandledrejection', this.rejectionHandler);
    }

    if (this.config.web_vitals) {
      this.startWebVitals();
    }
  }

  stop(): void {
    if (!this.active) return;
    this.active = false;

    if (typeof window === 'undefined') return;

    if (this.errorHandler) {
      window.removeEventListener('error', this.errorHandler);
      this.errorHandler = null;
    }

    if (this.rejectionHandler) {
      window.removeEventListener('unhandledrejection', this.rejectionHandler);
      this.rejectionHandler = null;
    }
  }

  captureComponentRenderError(
    component: string,
    slotId: string,
    error: unknown
  ): void {
    if (!this.config.frontend_errors) return;

    this.captureError('component_render_error', {
      message: this.messageFromUnknown(error),
      source: '',
      line: null,
      column: null,
      stack: stackFromUnknown(error),
      component,
      slotId,
    });
  }

  private startWebVitals(): void {
    const report = (metric: MetricType) => {
      if (!this.active) return;
      if (metric.name !== 'CLS' && metric.name !== 'INP' && metric.name !== 'LCP') {
        return;
      }

      const snapshot = this.activeFlags();
      this.capture.trackEvent(WEB_VITAL_EVENT, {
        metric: metric.name,
        value: metric.value,
        rating: metric.rating === 'needs-improvement'
          ? 'needs_improvement'
          : metric.rating,
        delta: metric.delta,
        id: metric.id,
        navigation_type: metric.navigationType,
        page: this.currentPagePath(),
        active_flags: snapshot.active_flags,
        active_flag_versions: snapshot.active_flag_versions,
      });
    };

    try {
      onCLS(report);
      onINP(report);
      onLCP(report);
    } catch {
      // Browser support varies; health capture should never break the SDK.
    }
  }

  private captureError(
    errorType: string,
    details: {
      message: string;
      source: string;
      line: number | null;
      column: number | null;
      stack: string;
      component?: string;
      slotId?: string;
    }
  ): void {
    if (!this.active) return;

    const snapshot = this.activeFlags();
    this.capture.trackEvent(FRONTEND_ERROR_EVENT, {
      error_type: errorType,
      message: truncate(details.message, MAX_MESSAGE_LENGTH),
      page: this.currentPagePath(),
      component: details.component ?? '',
      slot_id: details.slotId ?? '',
      source: details.source,
      line: details.line,
      column: details.column,
      stack: truncate(details.stack, MAX_STACK_LENGTH),
      active_flags: snapshot.active_flags,
      active_flag_versions: snapshot.active_flag_versions,
    });
  }

  private currentPagePath(): string {
    return this.contextCollector.collect().page?.path ?? '';
  }

  private messageFromUnknown(value: unknown): string {
    if (value instanceof Error && value.message) {
      return value.message;
    }
    if (typeof value === 'string') {
      return value;
    }
    try {
      return JSON.stringify(value) ?? String(value);
    } catch {
      return String(value);
    }
  }
}

function finiteNumberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function stackFromUnknown(value: unknown): string {
  if (value instanceof Error && typeof value.stack === 'string') {
    return value.stack;
  }
  return '';
}

function truncate(value: string, maxLength: number): string {
  return value.length > maxLength ? value.slice(0, maxLength) : value;
}
