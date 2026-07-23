import type { TrackEvent, EventContext } from '../core/types';
import { generateId } from '../core/types';
import type { EventQueue } from '../core/event-queue';
import type { SessionManager } from './session';
import type { ContextCollector } from './context';

/**
 * Manual event capture methods.
 * Each method creates a properly structured TrackEvent and enqueues it.
 */
export class ManualCapture {
  private queue: EventQueue;
  private sessionManager: SessionManager;
  private contextCollector: ContextCollector;
  private anonymousIdProvider: () => string;
  private canCapture: () => boolean;
  private userId: string | undefined;
  private traits: Record<string, unknown> = {};
  private groupId: string | undefined;
  private groupTraits: Record<string, unknown> = {};

  constructor(
    queue: EventQueue,
    sessionManager: SessionManager,
    contextCollector: ContextCollector,
    anonymousIdProvider: () => string,
    canCapture: () => boolean
  ) {
    this.queue = queue;
    this.sessionManager = sessionManager;
    this.contextCollector = contextCollector;
    this.anonymousIdProvider = anonymousIdProvider;
    this.canCapture = canCapture;
  }

  /**
   * Tracks a custom event with optional properties.
   */
  trackEvent(eventName: string, properties?: Record<string, unknown>): void {
    if (!this.canCapture()) return;

    const event = this.buildEvent('track', {
      event: eventName,
      properties: properties ?? {},
    });
    this.sessionManager.recordEvent();
    this.queue.enqueue(event);
  }

  /**
   * Identifies the current user and merges traits.
   */
  identifyUser(userId: string, traits?: Record<string, unknown>): void {
    if (!this.canCapture()) return;

    this.userId = userId;
    if (traits) {
      this.traits = { ...this.traits, ...traits };
    }

    const event = this.buildEvent('identify', {
      event: 'identify',
      traits: this.traits,
    });
    this.queue.enqueue(event);
  }

  /**
   * Associates the user with a group (company, team, etc).
   */
  groupUser(groupId: string, traits?: Record<string, unknown>): void {
    if (!this.canCapture()) return;

    this.groupId = groupId;
    if (traits) {
      this.groupTraits = { ...this.groupTraits, ...traits };
    }

    const event = this.buildEvent('group', {
      event: 'group',
      groupId,
      traits: this.groupTraits,
    });
    this.queue.enqueue(event);
  }

  /** Tracks a page view with a query-free URL and path. */
  pageView(name?: string, properties?: Record<string, unknown>): void {
    if (!this.canCapture()) return;

    const context = this.contextCollector.collect();

    const pageProps: Record<string, unknown> = {
      ...(properties ?? {}),
      url: context.page?.url ?? '',
      path: context.page?.path ?? '',
    };

    if (name) {
      pageProps.name = name;
    }

    const event = this.buildEvent('page', {
      event: 'page',
      properties: pageProps,
    });

    this.sessionManager.recordPage();
    this.queue.enqueue(event);
  }

  /**
   * Resets user identity and traits (e.g., on logout).
   */
  reset(): void {
    this.userId = undefined;
    this.traits = {};
    this.groupId = undefined;
    this.groupTraits = {};
    this.sessionManager.reset();
  }

  /**
   * Returns the current user ID.
   */
  getUserId(): string | undefined {
    return this.userId;
  }

  /**
   * Returns the anonymous ID.
   */
  getAnonymousId(): string {
    return this.anonymousIdProvider();
  }

  /**
   * Returns the current user traits.
   */
  getTraits(): Record<string, unknown> {
    return { ...this.traits };
  }

  /**
   * Returns the current group ID.
   */
  getGroupId(): string | undefined {
    return this.groupId;
  }

  private buildEvent(
    type: TrackEvent['type'],
    overrides: Partial<TrackEvent>
  ): TrackEvent {
    const context: EventContext = this.contextCollector.collect();

    return {
      type,
      userId: this.userId,
      anonymousId: this.anonymousIdProvider(),
      groupId: this.groupId,
      context,
      timestamp: new Date().toISOString(),
      messageId: generateId(),
      sessionId: this.sessionManager.getSessionId(),
      ...overrides,
    };
  }
}
