import type { ComponentDefinition, RenderContext } from './types';

/**
 * Inline contextual message component.
 * Renders an inline message with info, warning, success, or error styling.
 */
export const InlineMessageComponent: ComponentDefinition = {
  name: 'inline-message',

  schema: {
    type: 'object',
    required: ['text'],
    properties: {
      text: { type: 'string', description: 'Message text content' },
      type: {
        type: 'string',
        default: 'info',
        enum: ['info', 'warning', 'success', 'error'],
      },
      title: { type: 'string', description: 'Optional bold title' },
      dismissible: { type: 'boolean', default: false },
      icon: { type: 'boolean', default: true },
    },
  },

  render(props: Record<string, unknown>, context: RenderContext): HTMLElement {
    const text = (props.text as string) || '';
    const type = (props.type as string) || 'info';
    const title = props.title as string | undefined;
    const dismissible = props.dismissible === true;
    const showIcon = props.icon !== false;

    const typeConfig: Record<
      string,
      { bg: string; border: string; text: string; icon: string }
    > = {
      info: {
        bg: '#e8f4fd',
        border: '#1a73e8',
        text: '#0d47a1',
        icon: '\u24D8',
      },
      warning: {
        bg: '#fef7e0',
        border: '#f9ab00',
        text: '#e65100',
        icon: '\u26A0',
      },
      success: {
        bg: '#e6f4ea',
        border: '#34a853',
        text: '#1b5e20',
        icon: '\u2713',
      },
      error: {
        bg: '#fce8e6',
        border: '#ea4335',
        text: '#b71c1c',
        icon: '\u2717',
      },
    };

    const config = typeConfig[type] || typeConfig.info;

    const container = document.createElement('div');
    container.setAttribute('data-apdl-component', 'inline-message');
    container.setAttribute('role', type === 'error' ? 'alert' : 'status');
    container.style.cssText = `
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 12px 16px;
      background-color: ${config.bg};
      border-left: 4px solid ${config.border};
      border-radius: 4px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      color: ${config.text};
      line-height: 1.5;
      position: relative;
    `;

    // Icon
    if (showIcon) {
      const iconEl = document.createElement('span');
      iconEl.textContent = config.icon;
      iconEl.style.cssText = `
        font-size: 16px;
        flex-shrink: 0;
        margin-top: 1px;
      `;
      iconEl.setAttribute('aria-hidden', 'true');
      container.appendChild(iconEl);
    }

    // Text content wrapper
    const contentWrapper = document.createElement('div');
    contentWrapper.style.cssText = 'flex: 1;';

    if (title) {
      const titleEl = document.createElement('strong');
      titleEl.textContent = title;
      titleEl.style.cssText = `
        display: block;
        margin-bottom: 4px;
        font-weight: 600;
      `;
      contentWrapper.appendChild(titleEl);
    }

    const textEl = document.createElement('span');
    textEl.textContent = text;
    contentWrapper.appendChild(textEl);
    container.appendChild(contentWrapper);

    // Dismiss button
    if (dismissible) {
      const closeBtn = document.createElement('button');
      closeBtn.textContent = '\u00D7';
      closeBtn.setAttribute('aria-label', 'Dismiss');
      closeBtn.style.cssText = `
        background: none;
        border: none;
        font-size: 18px;
        cursor: pointer;
        color: ${config.text};
        padding: 0 4px;
        line-height: 1;
        flex-shrink: 0;
        opacity: 0.7;
      `;
      closeBtn.addEventListener('click', () => {
        context.track('inline_message_dismissed', { text, type });
        context.dismiss();
      });
      container.appendChild(closeBtn);
    }

    context.track('inline_message_rendered', { text, type });
    return container;
  },

  destroy(element: HTMLElement): void {
    element.remove();
  },
};
