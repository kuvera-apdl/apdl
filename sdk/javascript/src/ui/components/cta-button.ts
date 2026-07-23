import type { ComponentDefinition, RenderContext } from './types';
import { requireSafeUiTarget, requireSafeUiUrl } from '../url-policy';

/**
 * Configurable CTA (call-to-action) button component.
 * Supports text, href, and style variants (primary, secondary, outline).
 */
export const CTAButtonComponent: ComponentDefinition = {
  name: 'cta-button',

  schema: {
    type: 'object',
    required: ['text'],
    properties: {
      text: { type: 'string', description: 'Button label text' },
      href: { type: 'string', description: 'Link URL' },
      variant: {
        type: 'string',
        default: 'primary',
        enum: ['primary', 'secondary', 'outline'],
      },
      size: {
        type: 'string',
        default: 'medium',
        enum: ['small', 'medium', 'large'],
      },
      fullWidth: { type: 'boolean', default: false },
      target: {
        type: 'string',
        default: '_self',
        enum: ['_self', '_blank'],
      },
    },
  },

  render(props: Record<string, unknown>, context: RenderContext): HTMLElement {
    const text = (props.text as string) || '';
    const href = props.href as string | undefined;
    const safeHref = href === undefined
      ? null
      : requireSafeUiUrl(href, 'cta-button.href');
    const variant = (props.variant as string) || 'primary';
    const size = (props.size as string) || 'medium';
    const fullWidth = props.fullWidth === true;
    const target = requireSafeUiTarget(
      props.target ?? '_self',
      'cta-button.target'
    );

    const isLink = safeHref !== null;
    const el = document.createElement(isLink ? 'a' : 'button');
    el.setAttribute('data-apdl-component', 'cta-button');
    el.textContent = text;

    if (isLink) {
      (el as HTMLAnchorElement).href = safeHref;
      (el as HTMLAnchorElement).target = target;
      (el as HTMLAnchorElement).rel = 'noopener noreferrer';
    } else {
      (el as HTMLButtonElement).type = 'button';
    }

    // Size-specific padding
    const sizeStyles: Record<string, string> = {
      small: 'padding: 6px 12px; font-size: 12px;',
      medium: 'padding: 10px 20px; font-size: 14px;',
      large: 'padding: 14px 28px; font-size: 16px;',
    };

    // Variant-specific colors
    const variantStyles: Record<string, string> = {
      primary: `
        background-color: #1a73e8;
        color: #ffffff;
        border: 2px solid #1a73e8;
      `,
      secondary: `
        background-color: #6c757d;
        color: #ffffff;
        border: 2px solid #6c757d;
      `,
      outline: `
        background-color: transparent;
        color: #1a73e8;
        border: 2px solid #1a73e8;
      `,
    };

    el.style.cssText = `
      display: inline-block;
      ${sizeStyles[size] || sizeStyles.medium}
      ${variantStyles[variant] || variantStyles.primary}
      border-radius: 4px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-weight: 600;
      text-decoration: none;
      text-align: center;
      cursor: pointer;
      transition: opacity 0.15s ease;
      ${fullWidth ? 'width: 100%; box-sizing: border-box;' : ''}
    `;

    el.addEventListener('mouseenter', () => {
      el.style.opacity = '0.85';
    });
    el.addEventListener('mouseleave', () => {
      el.style.opacity = '1';
    });

    el.addEventListener('click', (e) => {
      if (!isLink) {
        e.preventDefault();
      }
      context.track('cta_button_clicked', {
        text,
        href: safeHref ?? undefined,
        variant,
      });
    });

    context.track('cta_button_rendered', { text, variant });
    return el;
  },

  destroy(element: HTMLElement): void {
    element.remove();
  },
};
