import { describe, expect, it } from 'vitest';
import type { TrackEvent } from '../../src/core/types';
import { Scrubber } from '../../src/privacy/scrubber';

function createEvent(properties: Record<string, unknown>): TrackEvent {
  return {
    type: 'track',
    event: 'test_event',
    anonymousId: 'anon-1',
    properties,
    context: {},
    timestamp: '2026-07-13T00:00:00.000Z',
    messageId: 'msg-1',
    sessionId: 'session-1',
  };
}

describe('Scrubber', () => {
  it('scrubs nested arrays without changing their shape', () => {
    const scrubbed = new Scrubber().scrub(createEvent({
      contacts: [
        ['alice@example.com'],
        { emails: ['bob@example.com'] },
      ],
    }));

    expect(scrubbed?.properties).toEqual({
      contacts: [
        ['[REDACTED]'],
        { emails: ['[REDACTED]'] },
      ],
    });
  });

  it('preserves dates while cloning the event', () => {
    const occurredAt = new Date('2026-07-13T12:00:00.000Z');
    const event = createEvent({ occurredAt });

    const scrubbed = new Scrubber().scrub(event);
    const scrubbedDate = scrubbed?.properties?.occurredAt;

    expect(scrubbedDate).toEqual(occurredAt);
    expect(scrubbedDate).not.toBe(occurredAt);
    expect(event.properties?.occurredAt).toBe(occurredAt);
  });
});
