import type { ComponentDefinition, RenderContext } from './types';

/**
 * Toast notification component.
 * Auto-dismisses after a configurable duration, positioned at bottom-right.
 */
export const ToastComponent: ComponentDefinition = {
  name: 'toast',

  schema: {
    type: 'object',
    required: ['message'],
    properties: {
      message: { type: 'string', description: 'Toast message text' },
      type: {
        type: 'string',
        default: 'info',
        enum: ['info', 'success', 'warning', 'error'],
      },
      duration: { type: 'number', default: 5000, description: 'Auto-dismiss time in ms' },
      dismissible: { type: 'boolean', default: true },
    },
  },

  render(props: Record<string, unknown>, context: RenderContext): HTMLElement {
    const message = (props.message as string) || '';
    const type = (props.type as string) || 'info';
    const duration = (props.duration as number) ?? 5000;
    const dismissible = props.dismissible !== false;

    const typeColors: Record<string, { bg: string; border: string; icon: string }> = {
      info: { bg: '#e8f4fd', border: '#1a73e8', icon: '\u2139\uFE0F' },
      success: { bg: '#e6f4ea', border: '#34a853', icon: '\u2705' },
      warning: { bg: '#fef7e0', border: '#f9ab00', icon: '\u26A0\uFE0F' },
      error: { bg: '#fce8e6', border: '#ea4335', icon: '\u274C' },
    };

    const colors = typeColors[type] || typeColors.info;

    // Container
    const toast = document.createElement('div');
    toast.setAttribute('data-apdl-component', 'toast');
    toast.style.cssText = `
      position: fixed;
      bottom: 24px;
      right: 24px;
      z-index: 10001;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      background-color: ${colors.bg};
      border-left: 4px solid ${colors.border};
      border-radius: 4px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      color: #1a1a1a;
      max-width: 400px;
      animation: apdl-toast-in 0.3s ease-out;
    `;

    // Add animation keyframes
    const styleId = 'apdl-toast-styles';
    if (typeof document !== 'undefined' && !document.getElementById(styleId)) {
      const style = document.createElement('style');
      style.id = styleId;
      style.textContent = `
        @keyframes apdl-toast-in {
          from { transform: translateX(100%); opacity: 0; }
          to { transform: translateX(0); opacity: 1; }
        }
        @keyframes apdl-toast-out {
          from { transform: translateX(0); opacity: 1; }
          to { transform: translateX(100%); opacity: 0; }
        }
      `;
      document.head.appendChild(style);
    }

    // Message text
    const textEl = document.createElement('span');
    textEl.textContent = message;
    textEl.style.cssText = 'flex: 1;';
    toast.appendChild(textEl);

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
        color: #666;
        padding: 0 4px;
        line-height: 1;
      `;
      closeBtn.addEventListener('click', () => {
        context.track('toast_dismissed', { message, type });
        animateOut(toast, context);
      });
      toast.appendChild(closeBtn);
    }

    // Auto-dismiss
    if (duration > 0) {
      setTimeout(() => {
        animateOut(toast, context);
      }, duration);
    }

    context.track('toast_rendered', { message, type });
    return toast;
  },

  destroy(element: HTMLElement): void {
    element.remove();
  },
};

function animateOut(element: HTMLElement, context: RenderContext): void {
  element.style.animation = 'apdl-toast-out 0.3s ease-in forwards';
  setTimeout(() => {
    context.dismiss();
  }, 300);
}
