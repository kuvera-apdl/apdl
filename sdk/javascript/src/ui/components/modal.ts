import type { ComponentDefinition, RenderContext } from './types';
import { requireSafeUiUrl } from '../url-policy';

/**
 * Modal overlay component.
 * Renders a centered modal with backdrop, close button, and configurable content.
 */
export const ModalComponent: ComponentDefinition = {
  name: 'modal',

  schema: {
    type: 'object',
    required: ['title'],
    properties: {
      title: { type: 'string', description: 'Modal title text' },
      body: { type: 'string', description: 'Modal body text' },
      ctaText: { type: 'string', description: 'Primary action button text' },
      ctaHref: { type: 'string', description: 'Primary action button link' },
      cancelText: { type: 'string', description: 'Cancel/secondary button text' },
      width: { type: 'string', default: '480px' },
      closeOnBackdrop: { type: 'boolean', default: true },
    },
  },

  render(props: Record<string, unknown>, context: RenderContext): HTMLElement {
    const title = (props.title as string) || '';
    const body = (props.body as string) || '';
    const ctaText = props.ctaText as string | undefined;
    const ctaHref = props.ctaHref as string | undefined;
    const safeCtaHref = ctaHref === undefined
      ? null
      : requireSafeUiUrl(ctaHref, 'modal.ctaHref');
    const cancelText = props.cancelText as string | undefined;
    const width = (props.width as string) || '480px';
    const closeOnBackdrop = props.closeOnBackdrop !== false;

    // Backdrop
    const backdrop = document.createElement('div');
    backdrop.setAttribute('data-apdl-component', 'modal');
    backdrop.style.cssText = `
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background-color: rgba(0, 0, 0, 0.5);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10000;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    `;

    if (closeOnBackdrop) {
      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) {
          context.track('modal_backdrop_dismissed', { title });
          context.dismiss();
        }
      });
    }

    // Modal container
    const modal = document.createElement('div');
    modal.style.cssText = `
      background: #ffffff;
      border-radius: 8px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
      max-width: ${width};
      width: 90%;
      max-height: 90vh;
      overflow-y: auto;
      position: relative;
    `;

    // Close button
    const closeBtn = document.createElement('button');
    closeBtn.textContent = '\u00D7';
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.style.cssText = `
      position: absolute;
      top: 12px;
      right: 12px;
      background: none;
      border: none;
      font-size: 24px;
      cursor: pointer;
      color: #666;
      padding: 4px 8px;
      line-height: 1;
    `;
    closeBtn.addEventListener('click', () => {
      context.track('modal_closed', { title });
      context.dismiss();
    });
    modal.appendChild(closeBtn);

    // Header
    const header = document.createElement('div');
    header.style.cssText = `
      padding: 24px 24px 0;
    `;
    const titleEl = document.createElement('h2');
    titleEl.textContent = title;
    titleEl.style.cssText = `
      margin: 0;
      font-size: 20px;
      font-weight: 600;
      color: #1a1a1a;
    `;
    header.appendChild(titleEl);
    modal.appendChild(header);

    // Body
    if (body) {
      const bodyEl = document.createElement('div');
      bodyEl.style.cssText = `
        padding: 16px 24px;
        color: #4a4a4a;
        font-size: 14px;
        line-height: 1.6;
      `;
      bodyEl.textContent = body;
      modal.appendChild(bodyEl);
    }

    // Footer with buttons
    if (ctaText || cancelText) {
      const footer = document.createElement('div');
      footer.style.cssText = `
        padding: 16px 24px 24px;
        display: flex;
        justify-content: flex-end;
        gap: 8px;
      `;

      if (cancelText) {
        const cancelBtn = document.createElement('button');
        cancelBtn.textContent = cancelText;
        cancelBtn.style.cssText = `
          padding: 8px 20px;
          border: 1px solid #ddd;
          border-radius: 4px;
          background: #fff;
          color: #333;
          font-size: 14px;
          cursor: pointer;
        `;
        cancelBtn.addEventListener('click', () => {
          context.track('modal_cancelled', { title });
          context.dismiss();
        });
        footer.appendChild(cancelBtn);
      }

      if (ctaText) {
        const ctaBtn = document.createElement(
          safeCtaHref === null ? 'button' : 'a'
        );
        ctaBtn.textContent = ctaText;
        if (safeCtaHref === null) {
          (ctaBtn as HTMLButtonElement).type = 'button';
        } else {
          (ctaBtn as HTMLAnchorElement).href = safeCtaHref;
          (ctaBtn as HTMLAnchorElement).rel = 'noopener noreferrer';
        }
        ctaBtn.style.cssText = `
          display: inline-block;
          padding: 8px 20px;
          background-color: #1a73e8;
          color: #fff;
          border-radius: 4px;
          text-decoration: none;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          border: none;
        `;
        ctaBtn.addEventListener('click', (e) => {
          if (safeCtaHref === null) {
            e.preventDefault();
          }
          context.track('modal_cta_clicked', {
            title,
            ctaText,
            ctaHref: safeCtaHref ?? undefined,
          });
        });
        footer.appendChild(ctaBtn);
      }

      modal.appendChild(footer);
    }

    backdrop.appendChild(modal);
    context.track('modal_rendered', { title });
    return backdrop;
  },

  destroy(element: HTMLElement): void {
    element.remove();
  },
};
