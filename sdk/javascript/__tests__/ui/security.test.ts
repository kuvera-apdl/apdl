import { afterEach, describe, expect, it, vi } from 'vitest';
import { BannerComponent } from '../../src/ui/components/banner';
import { CardComponent } from '../../src/ui/components/card';
import { CTAButtonComponent } from '../../src/ui/components/cta-button';
import { InlineMessageComponent } from '../../src/ui/components/inline-message';
import { ModalComponent } from '../../src/ui/components/modal';
import { ToastComponent } from '../../src/ui/components/toast';
import type { ComponentDefinition, RenderContext } from '../../src/ui/components/types';

const context: RenderContext = {
  track: vi.fn(),
  dismiss: vi.fn(),
};

describe('built-in UI security', () => {
  afterEach(() => {
    document.body.replaceChildren();
    vi.clearAllMocks();
  });

  it('renders modal body markup as inert text', () => {
    const payload = [
      '<img src=x onerror="window.__apdlXss = true">',
      '<svg onload="window.__apdlXss = true"></svg>',
      '<script>window.__apdlXss = true</script>',
    ].join('');

    const element = ModalComponent.render({
      title: 'Untrusted content',
      body: payload,
    }, context);

    expect(element.querySelector('img, svg, script')).toBeNull();
    expect(element.textContent).toContain(payload);
  });

  it('uses Trusted Types-compatible text and element sinks in every built-in', () => {
    const descriptor = Object.getOwnPropertyDescriptor(
      Element.prototype,
      'innerHTML'
    );
    expect(descriptor).toBeDefined();
    Object.defineProperty(Element.prototype, 'innerHTML', {
      ...descriptor,
      set: () => {
        throw new TypeError('Trusted Types policy rejected innerHTML');
      },
    });

    try {
      const renders: Array<[ComponentDefinition, Record<string, unknown>]> = [
        [BannerComponent, { text: 'Banner', dismissible: true }],
        [CardComponent, { title: 'Card', description: 'Description' }],
        [CTAButtonComponent, { text: 'CTA' }],
        [ModalComponent, { title: 'Modal', body: '<strong>text only</strong>' }],
        [ToastComponent, { message: 'Toast', duration: 0, dismissible: true }],
        [InlineMessageComponent, { text: 'Inline', dismissible: true }],
      ];

      for (const [component, props] of renders) {
        expect(() => component.render(props, context)).not.toThrow();
      }
    } finally {
      Object.defineProperty(Element.prototype, 'innerHTML', descriptor!);
    }
  });

  it.each(
    [
      [BannerComponent, {
        text: 'Banner',
        ctaText: 'Open',
        ctaHref: 'javascript:alert(1)',
      }],
      [CardComponent, {
        title: 'Card',
        ctaText: 'Open',
        ctaHref: 'data:text/html,<script>alert(1)</script>',
      }],
      [CardComponent, {
        title: 'Card',
        imageUrl: 'data:image/svg+xml,<svg onload=alert(1)>',
      }],
      [CTAButtonComponent, {
        text: 'Open',
        href: 'java\nscript:alert(1)',
      }],
      [ModalComponent, {
        title: 'Modal',
        ctaText: 'Open',
        ctaHref: '//evil.example/path',
      }],
    ] as Array<[ComponentDefinition, Record<string, unknown>]>
  )(
    'rejects unsafe URL props in %s',
    (component, props) => {
      expect(() => component.render(props, context))
        .toThrow('must be an absolute HTTP(S) URL');
    }
  );

  it('keeps href-less actions as buttons and hardens new-window links', () => {
    const hrefLess = CTAButtonComponent.render({ text: 'Submit' }, context);
    const linked = CTAButtonComponent.render({
      text: 'Open',
      href: 'https://example.test/path',
      target: '_blank',
    }, context);

    expect(hrefLess).toBeInstanceOf(HTMLButtonElement);
    expect((hrefLess as HTMLButtonElement).type).toBe('button');
    expect(linked).toBeInstanceOf(HTMLAnchorElement);
    expect((linked as HTMLAnchorElement).href).toBe('https://example.test/path');
    expect((linked as HTMLAnchorElement).rel).toBe('noopener noreferrer');
  });

  it('rejects non-allowlisted anchor targets', () => {
    expect(() => CTAButtonComponent.render({
      text: 'Open',
      href: 'https://example.test/path',
      target: 'named-frame',
    }, context)).toThrow('cta-button.target must be one of: _self, _blank');
  });
});
