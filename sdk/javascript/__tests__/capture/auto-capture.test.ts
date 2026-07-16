import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AutoCapture } from '../../src/capture/auto-capture';
import type { ManualCapture } from '../../src/capture/manual';
import type { AutoCaptureConfig } from '../../src/core/config';

const config: AutoCaptureConfig = {
  pageViews: false,
  clicks: true,
  formSubmissions: false,
  inputChanges: false,
  scrollDepth: false,
  rage_clicks: true,
  frontend_errors: false,
  web_vitals: false,
};

function dispatchClick(element: Element, x = 12, y = 34): void {
  element.dispatchEvent(
    new MouseEvent('click', {
      bubbles: true,
      composed: true,
      cancelable: true,
      clientX: x,
      clientY: y,
    })
  );
}

describe('AutoCapture click privacy', () => {
  let autoCapture: AutoCapture;
  let trackEvent: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    trackEvent = vi.fn();
    const capture = {
      trackEvent,
      pageView: vi.fn(),
    } as unknown as ManualCapture;

    autoCapture = new AutoCapture(config, capture);
    autoCapture.start();
  });

  afterEach(() => {
    autoCapture.stop();
    document.body.replaceChildren();
  });

  it('captures structural button metadata without DOM text', () => {
    const button = document.createElement('button');
    button.id = 'save-account';
    button.className = 'primary action';
    button.textContent = 'Secret button copy';
    document.body.append(button);

    dispatchClick(button, 7, 9);

    expect(trackEvent).toHaveBeenCalledOnce();
    expect(trackEvent).toHaveBeenCalledWith('$click', {
      tag: 'button',
      x: 7,
      y: 9,
    });
    expect(trackEvent.mock.calls[0][1]).not.toHaveProperty('text');
  });

  it('does not capture link URLs, IDs, or classes', () => {
    const link = document.createElement('a');
    link.href = 'https://example.test/reset?token=secret-link-token';
    link.id = 'secret-link-id';
    link.className = 'secret-link-class';
    link.addEventListener('click', (event) => event.preventDefault());
    document.body.append(link);

    dispatchClick(link, 5, 6);

    expect(trackEvent).toHaveBeenCalledWith('$click', {
      tag: 'a',
      x: 5,
      y: 6,
    });
    expect(JSON.stringify(trackEvent.mock.calls)).not.toContain('secret-link');
  });

  it('ignores click events whose target is not an element', () => {
    expect(() => {
      document.dispatchEvent(new MouseEvent('click'));
    }).not.toThrow();
    expect(trackEvent).not.toHaveBeenCalled();
  });

  it.each([
    ['text input', () => {
      const input = document.createElement('input');
      input.type = 'text';
      Object.defineProperty(input, 'value', {
        get: () => {
          throw new Error('click capture must not read input.value');
        },
      });
      return input;
    }],
    ['textarea', () => {
      const textarea = document.createElement('textarea');
      textarea.value = 'live textarea secret';
      textarea.textContent = 'default textarea secret';
      return textarea;
    }],
    ['select', () => {
      const select = document.createElement('select');
      const option = document.createElement('option');
      option.value = 'private-value';
      option.textContent = 'private label';
      select.append(option);
      return select;
    }],
    ['contenteditable descendant', () => {
      const editable = document.createElement('div');
      editable.contentEditable = 'true';
      const child = document.createElement('span');
      child.textContent = 'live editable secret';
      editable.append(child);
      return child;
    }],
  ])('never captures DOM text for a %s click or rage click', (_name, createTarget) => {
    const target = createTarget();
    document.body.append(target.parentElement ?? target);

    dispatchClick(target);
    dispatchClick(target);
    dispatchClick(target);

    expect(trackEvent).toHaveBeenCalledTimes(4);
    expect(trackEvent.mock.calls.map(([eventName]) => eventName)).toEqual([
      '$click',
      '$click',
      '$click',
      '$rage_click',
    ]);

    for (const [, properties] of trackEvent.mock.calls) {
      expect(properties).not.toHaveProperty('text');
    }
    expect(JSON.stringify(trackEvent.mock.calls)).not.toContain('secret');
    expect(JSON.stringify(trackEvent.mock.calls)).not.toContain('private');
  });

  it.each([
    ['password input', { type: 'password' }],
    ['file input', { type: 'file' }],
    ['current password autocomplete', { type: 'text', autocomplete: 'current-password' }],
    ['new password autocomplete', { type: 'text', autocomplete: 'new-password' }],
    ['one-time code autocomplete', { type: 'text', autocomplete: 'one-time-code' }],
    ['payment autocomplete', { type: 'text', autocomplete: 'billing cc-number' }],
    ['transaction autocomplete', { type: 'text', autocomplete: 'transaction-amount' }],
  ])('suppresses single and rage clicks for a %s', (_name, attributes) => {
    const input = document.createElement('input');
    input.setAttribute('type', attributes.type);
    if ('autocomplete' in attributes) {
      input.setAttribute('autocomplete', attributes.autocomplete);
    }
    if (attributes.type !== 'file') {
      input.value = 'never capture this live value';
    }
    document.body.append(input);

    dispatchClick(input);
    dispatchClick(input);
    dispatchClick(input);

    expect(trackEvent).not.toHaveBeenCalled();
  });

  it('suppresses sensitive controls inside a shadow root', () => {
    const host = document.createElement('sensitive-field');
    const shadowRoot = host.attachShadow({ mode: 'open' });
    const input = document.createElement('input');
    input.type = 'password';
    input.value = 'shadow-root password';
    shadowRoot.append(input);
    document.body.append(host);

    dispatchClick(input);
    dispatchClick(input);
    dispatchClick(input);

    expect(trackEvent).not.toHaveBeenCalled();
  });

  it('suppresses clicks inside sensitive custom editable ancestors', () => {
    const editable = document.createElement('div');
    editable.setAttribute('role', 'textbox');
    editable.setAttribute('aria-label', 'Card security code');
    const child = document.createElement('span');
    child.textContent = '123';
    editable.append(child);
    document.body.append(editable);

    dispatchClick(child);
    dispatchClick(child);
    dispatchClick(child);

    expect(trackEvent).not.toHaveBeenCalled();
  });

  it('suppresses clicks on labels associated with sensitive controls', () => {
    const label = document.createElement('label');
    label.htmlFor = 'password';
    label.textContent = 'Password';
    const input = document.createElement('input');
    input.id = 'password';
    input.type = 'password';
    document.body.append(label, input);

    dispatchClick(label);

    expect(trackEvent).not.toHaveBeenCalled();
  });

  it('does not count suppressed clicks toward rage-click detection', () => {
    const input = document.createElement('input');
    input.type = 'password';
    document.body.append(input);

    dispatchClick(input);
    dispatchClick(input);
    dispatchClick(input);

    input.type = 'text';
    dispatchClick(input);
    dispatchClick(input);

    expect(trackEvent.mock.calls.map(([eventName]) => eventName)).toEqual([
      '$click',
      '$click',
    ]);

    dispatchClick(input);
    expect(trackEvent.mock.calls.map(([eventName]) => eventName)).toEqual([
      '$click',
      '$click',
      '$click',
      '$rage_click',
    ]);
  });
});
