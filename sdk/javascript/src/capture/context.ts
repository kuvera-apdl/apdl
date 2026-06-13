import type { EventContext } from '../core/types';
import { SDK_VERSION } from '../core/types';

/**
 * Collects device, browser, and page context for event enrichment.
 */
export class ContextCollector {
  /**
   * Collects the current browser/device context.
   * Safe to call in non-browser environments — missing fields are omitted.
   */
  collect(): EventContext {
    const context: EventContext = {
      library: {
        name: '@apdl-oss/sdk',
        version: SDK_VERSION,
      },
    };

    if (typeof navigator !== 'undefined') {
      const ua = navigator.userAgent || '';
      const browserInfo = this.parseBrowser(ua);
      const osInfo = this.parseOS(ua);
      const deviceType = this.parseDeviceType(ua);

      context.browser = browserInfo;
      context.os = osInfo;
      context.device = { type: deviceType };
      context.locale = navigator.language || undefined;
    }

    if (typeof screen !== 'undefined') {
      context.screen = {
        width: screen.width,
        height: screen.height,
      };
    }

    if (typeof window !== 'undefined') {
      context.viewport = {
        width: window.innerWidth,
        height: window.innerHeight,
      };
    }

    if (typeof document !== 'undefined') {
      context.referrer = document.referrer || undefined;
    }

    try {
      context.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch {
      // Intl not available
    }

    if (typeof window !== 'undefined' && typeof window.location !== 'undefined') {
      context.page = {
        url: window.location.href,
        title: typeof document !== 'undefined' ? document.title : '',
        path: window.location.pathname,
        search: window.location.search,
      };
    }

    return context;
  }

  private parseBrowser(ua: string): { name: string; version: string } {
    // Order matters: check more specific patterns first
    const browsers: Array<{ name: string; pattern: RegExp }> = [
      { name: 'Edge', pattern: /Edg(?:e|A|iOS)?\/(\d+[\d.]*)/ },
      { name: 'Opera', pattern: /(?:OPR|Opera)\/(\d+[\d.]*)/ },
      { name: 'Chrome', pattern: /(?:Chrome|CriOS)\/(\d+[\d.]*)/ },
      { name: 'Firefox', pattern: /(?:Firefox|FxiOS)\/(\d+[\d.]*)/ },
      { name: 'Safari', pattern: /Version\/(\d+[\d.]*).*Safari/ },
      { name: 'IE', pattern: /(?:MSIE |Trident.*rv:)(\d+[\d.]*)/ },
    ];

    for (const { name, pattern } of browsers) {
      const match = ua.match(pattern);
      if (match) {
        return { name, version: match[1] };
      }
    }

    return { name: 'Unknown', version: '0' };
  }

  private parseOS(ua: string): { name: string; version: string } {
    const osPatterns: Array<{ name: string; pattern: RegExp }> = [
      { name: 'iOS', pattern: /(?:iPhone|iPad|iPod).*OS (\d+[_\d]*)/ },
      { name: 'Android', pattern: /Android (\d+[\d.]*)/ },
      { name: 'Windows', pattern: /Windows NT (\d+[\d.]*)/ },
      { name: 'macOS', pattern: /Mac OS X (\d+[_\d.]*)/ },
      { name: 'Linux', pattern: /Linux/ },
      { name: 'Chrome OS', pattern: /CrOS/ },
    ];

    for (const { name, pattern } of osPatterns) {
      const match = ua.match(pattern);
      if (match) {
        const version = (match[1] || '0').replace(/_/g, '.');
        return { name, version };
      }
    }

    return { name: 'Unknown', version: '0' };
  }

  private parseDeviceType(ua: string): string {
    if (/tablet|ipad|playbook|silk/i.test(ua)) {
      return 'tablet';
    }
    if (/mobile|iphone|ipod|android.*mobile|windows phone/i.test(ua)) {
      return 'mobile';
    }
    return 'desktop';
  }
}
