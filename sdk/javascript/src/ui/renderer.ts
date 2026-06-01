import type { UIConfig, RenderContext } from './components/types';
import type { ComponentRegistry } from './registry';
import type { ManualCapture } from '../capture/manual';

interface ActiveRender {
  element: HTMLElement;
  componentName: string;
  slotElement: HTMLElement;
}

type RenderErrorCallback = (
  component: string,
  slotId: string,
  error: unknown
) => void;

/**
 * Renders UI components from JSON configuration into DOM slots.
 * Handles cleanup of previous renders and auto-tracks component events.
 */
export class UIRenderer {
  private registry: ComponentRegistry;
  private capture: ManualCapture;
  private activeRenders: Map<string, ActiveRender> = new Map();
  private debug: boolean;
  private renderErrorCallback?: RenderErrorCallback;

  constructor(
    registry: ComponentRegistry,
    capture: ManualCapture,
    debug = false,
    renderErrorCallback?: RenderErrorCallback
  ) {
    this.registry = registry;
    this.capture = capture;
    this.debug = debug;
    this.renderErrorCallback = renderErrorCallback;
  }

  /**
   * Renders a UI config into the target element.
   * Cleans up any previous render in the same slot.
   */
  render(config: UIConfig, targetElement: HTMLElement): HTMLElement | null {
    const slotId =
      config.slotId || targetElement.getAttribute('data-apdl-slot') || 'default';

    // Validate the component
    const errors = this.registry.validate(config.component, config.props);
    if (errors.length > 0) {
      if (this.debug) {
        console.error(
          `APDL: Validation errors for "${config.component}":`,
          errors
        );
      }
      return null;
    }

    const definition = this.registry.get(config.component);
    if (!definition) {
      if (this.debug) {
        console.error(
          `APDL: Component "${config.component}" not found in registry`
        );
      }
      return null;
    }

    // Clean up previous render in this slot
    this.cleanup(slotId);

    // Resolve default props
    const resolvedProps = this.registry.resolveDefaults(
      config.component,
      config.props
    );

    // Create render context
    const context: RenderContext = {
      track: (event: string, properties?: Record<string, unknown>) => {
        this.capture.trackEvent(event, {
          ...properties,
          component: config.component,
          slotId,
        });
      },
      dismiss: () => {
        this.cleanup(slotId);
      },
    };

    // Render the component
    try {
      const element = definition.render(resolvedProps, context);
      targetElement.appendChild(element);

      // Track the render
      this.activeRenders.set(slotId, {
        element,
        componentName: config.component,
        slotElement: targetElement,
      });

      this.capture.trackEvent('component_rendered', {
        component: config.component,
        slotId,
      });

      return element;
    } catch (err) {
      this.renderErrorCallback?.(config.component, slotId, err);
      if (this.debug) {
        console.error(
          `APDL: Error rendering "${config.component}":`,
          err
        );
      }
      return null;
    }
  }

  /**
   * Cleans up a rendered component by slot ID.
   */
  cleanup(slotId: string): void {
    const active = this.activeRenders.get(slotId);
    if (!active) return;

    const definition = this.registry.get(active.componentName);
    if (definition?.destroy) {
      try {
        definition.destroy(active.element);
      } catch {
        // Fallback: manual removal
        active.element.remove();
      }
    } else {
      active.element.remove();
    }

    this.activeRenders.delete(slotId);
  }

  /**
   * Cleans up all active renders.
   */
  cleanupAll(): void {
    for (const slotId of Array.from(this.activeRenders.keys())) {
      this.cleanup(slotId);
    }
  }

  /**
   * Returns the currently active render for a slot.
   */
  getActiveRender(
    slotId: string
  ): { element: HTMLElement; componentName: string } | undefined {
    const active = this.activeRenders.get(slotId);
    if (!active) return undefined;
    return { element: active.element, componentName: active.componentName };
  }
}
