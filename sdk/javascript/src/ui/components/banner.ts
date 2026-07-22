import type { ComponentDefinition, RenderContext } from './types';
import { requireSafeUiUrl } from '../url-policy';

/**
 * Dismissible banner component.
 * Renders a top-of-page banner with text, optional CTA button, and dismiss button.
 */
export const BannerComponent: ComponentDefinition = {
  name: 'banner',

  schema: {
    type: 'object',
    required: ['text'],
    properties: {
      text: { type: 'string', description: 'Banner message text' },
      ctaText: { type: 'string', description: 'CTA button label' },
      ctaHref: { type: 'string', description: 'CTA button link URL' },
      backgroundColor: { type: 'string', default: '#1a73e8' },
      textColor: { type: 'string', default: '#ffffff' },
      dismissible: { type: 'boolean', default: true },
      position: {
        type: 'string',
        default: 'top',
        enum: ['top', 'bottom'],
      },
    },
  },

  render(props: Record<string, unknown>, context: RenderContext): HTMLElement {
    const text = (props.text as string) || '';
    const ctaText = props.ctaText as string | undefined;
    const ctaHref = props.ctaHref as string | undefined;
    const safeCtaHref = ctaHref === undefined
      ? null
      : requireSafeUiUrl(ctaHref, 'banner.ctaHref');
    const backgroundColor = (props.backgroundColor as string) || '#1a73e8';
    const textColor = (props.textColor as string) || '#ffffff';
    const dismissible = props.dismissible !== false;
    const position = (props.position as string) || 'top';

    const banner = document.createElement('div');
    banner.setAttribute('data-apdl-component', 'banner');
    banner.style.cssText = `
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 12px;
      padding: 12px 16px;
      background-color: ${backgroundColor};
      color: ${textColor};
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      line-height: 1.4;
      position: relative;
      z-index: 9999;
      ${position === 'bottom' ? 'position: fixed; bottom: 0; left: 0; right: 0;' : ''}
    `;

    // Text
    const textEl = document.createElement('span');
    textEl.textContent = text;
    banner.appendChild(textEl);

    // CTA button
    if (ctaText) {
      const cta = document.createElement(safeCtaHref === null ? 'button' : 'a');
      cta.textContent = ctaText;
      if (safeCtaHref === null) {
        (cta as HTMLButtonElement).type = 'button';
      } else {
        (cta as HTMLAnchorElement).href = safeCtaHref;
        (cta as HTMLAnchorElement).rel = 'noopener noreferrer';
      }
      cta.style.cssText = `
        display: inline-block;
        padding: 6px 16px;
        background-color: ${textColor};
        color: ${backgroundColor};
        border-radius: 4px;
        text-decoration: none;
        font-weight: 600;
        font-size: 13px;
        white-space: nowrap;
        cursor: pointer;
        border: none;
      `;
      cta.addEventListener('click', (e) => {
        if (safeCtaHref === null) {
          e.preventDefault();
        }
        context.track('banner_cta_clicked', {
          text: ctaText,
          href: safeCtaHref ?? undefined,
        });
      });
      banner.appendChild(cta);
    }

    // Dismiss button
    if (dismissible) {
      const dismiss = document.createElement('button');
      dismiss.textContent = '\u00D7';
      dismiss.setAttribute('aria-label', 'Dismiss');
      dismiss.style.cssText = `
        position: absolute;
        right: 12px;
        top: 50%;
        transform: translateY(-50%);
        background: none;
        border: none;
        color: ${textColor};
        font-size: 20px;
        cursor: pointer;
        padding: 4px 8px;
        line-height: 1;
        opacity: 0.8;
      `;
      dismiss.addEventListener('click', () => {
        context.track('banner_dismissed', { text });
        context.dismiss();
      });
      banner.appendChild(dismiss);
    }

    context.track('banner_rendered', { text });
    return banner;
  },

  destroy(element: HTMLElement): void {
    element.remove();
  },
};
