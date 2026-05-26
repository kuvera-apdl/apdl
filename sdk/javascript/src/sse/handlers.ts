import type { FlagConfig } from '../flags/types';
import type { FlagCache } from '../flags/cache';
import { extractFlagConfig, extractFlagConfigs } from '../flags/schema';
import type { SlotManager } from '../ui/slot';

interface SSEMessage {
  type: string;
  data: string;
  id?: string;
}

type UIConfigUpdateCallback = (config: unknown) => void;

/**
 * Routes SSE messages to the appropriate subsystems.
 * Handles flag updates, experiment updates, UI config pushes, and heartbeats.
 */
export class SSEHandlers {
  private flagCache: FlagCache;
  private slotManager: SlotManager | null;
  private uiConfigCallback: UIConfigUpdateCallback | null = null;
  private debug: boolean;

  constructor(
    flagCache: FlagCache,
    slotManager: SlotManager | null,
    debug = false
  ) {
    this.flagCache = flagCache;
    this.slotManager = slotManager;
    this.debug = debug;
  }

  /**
   * Dispatches an SSE message to the appropriate handler.
   */
  handle(message: SSEMessage): void {
    switch (message.type) {
      case 'config':
      case 'flags_update':
        this.handleFlagsUpdate(message.data);
        break;

      case 'flag_update':
        this.handleFlagUpdate(message.data);
        break;

      case 'experiment_update':
        this.handleExperimentUpdate(message.data);
        break;

      case 'ui_config':
        this.handleUIConfig(message.data);
        break;

      case 'heartbeat':
        // Heartbeat is handled by the SSEConnection layer.
        // No additional action needed here.
        if (this.debug) {
          console.debug('APDL: Heartbeat received');
        }
        break;

      case 'message':
        // Generic message — try to parse and route
        this.handleGenericMessage(message.data);
        break;

      default:
        if (this.debug) {
          console.debug(`APDL: Unknown SSE message type: ${message.type}`);
        }
    }
  }

  /**
   * Registers a callback for UI config updates.
   */
  onUIConfigUpdate(callback: UIConfigUpdateCallback): void {
    this.uiConfigCallback = callback;
  }

  private handleFlagsUpdate(data: string): void {
    try {
      const parsed = JSON.parse(data) as unknown;
      if (isFlagCollection(parsed)) {
        const flags = extractFlagConfigs(parsed);
        this.flagCache.set(flags);
        if (this.debug) {
          console.debug(`APDL: Updated ${flags.length} flags from SSE`);
        }
      }
    } catch (err) {
      if (this.debug) {
        console.error('APDL: Failed to parse flags_update:', err);
      }
    }
  }

  private handleFlagUpdate(data: string): void {
    try {
      const parsed = JSON.parse(data) as unknown;
      const flags = extractFlagConfigs(parsed);
      if (flags.length > 0) {
        this.mergeFlags(flags);
        return;
      }

      if (!isRecord(parsed)) {
        return;
      }

      const current = new Map(this.flagCache.getAll().map((flag) => [flag.key, flag]));
      const fullFlag = extractFlagConfig(parsed.flag) ?? extractFlagConfig(parsed);
      if (fullFlag) {
        current.set(fullFlag.key, fullFlag);
        this.flagCache.set(Array.from(current.values()));
        return;
      }

      if (typeof parsed.key !== 'string') {
        return;
      }

      if (parsed.action === 'flag_deleted') {
        current.delete(parsed.key);
        this.flagCache.set(Array.from(current.values()));
        return;
      }

      const existing = current.get(parsed.key);
      if (existing && typeof parsed.enabled === 'boolean') {
        current.set(parsed.key, { ...existing, enabled: parsed.enabled });
        this.flagCache.set(Array.from(current.values()));
      }
    } catch (err) {
      if (this.debug) {
        console.error('APDL: Failed to parse flag_update:', err);
      }
    }
  }

  private handleExperimentUpdate(data: string): void {
    // Experiment updates come as flag updates with variant information
    try {
      const parsed = JSON.parse(data) as unknown;
      const flags = extractFlagConfigs(parsed);
      if (flags.length > 0) {
        // Merge experiment flags into the cache
        this.mergeFlags(flags);
        if (this.debug) {
          console.debug(`APDL: Updated experiments from SSE`);
        }
      }
    } catch (err) {
      if (this.debug) {
        console.error('APDL: Failed to parse experiment_update:', err);
      }
    }
  }

  private handleUIConfig(data: string): void {
    try {
      const parsed = JSON.parse(data) as unknown;
      if (this.uiConfigCallback) {
        this.uiConfigCallback(parsed);
      }
      if (this.slotManager) {
        this.slotManager.refresh();
      }
      if (this.debug) {
        console.debug('APDL: UI config updated from SSE');
      }
    } catch (err) {
      if (this.debug) {
        console.error('APDL: Failed to parse ui_config:', err);
      }
    }
  }

  private handleGenericMessage(data: string): void {
    try {
      const parsed = JSON.parse(data) as { type?: string };
      if (parsed.type) {
        this.handle({ type: parsed.type, data });
      }
    } catch {
      // Not JSON or not routable — ignore
    }
  }

  private mergeFlags(flags: FlagConfig[]): void {
    const existingMap = new Map(this.flagCache.getAll().map((f) => [f.key, f]));

    for (const flag of flags) {
      existingMap.set(flag.key, flag);
    }

    this.flagCache.set(Array.from(existingMap.values()));
  }
}

function isRecord(input: unknown): input is Record<string, unknown> {
  return typeof input === 'object' && input !== null && !Array.isArray(input);
}

function isFlagCollection(input: unknown): boolean {
  return Array.isArray(input) || (isRecord(input) && Array.isArray(input.flags));
}
