import type { FlagConfig } from '../flags/types';
import type { FlagCache } from '../flags/cache';
import {
  extractFlagConfig,
  extractInvalidFlagKey,
  parseFlagConfigResult,
  parseFlagConfigs,
} from '../flags/schema';
import { isIdentifier } from '../flags/targeting-contract';

interface SSEMessage {
  type: string;
  data: string;
  id?: string;
}

/**
 * Routes SSE messages to the appropriate subsystems.
 * Handles flag updates and heartbeats.
 */
export class SSEHandlers {
  private flagCache: FlagCache;
  private projectId: string;
  private debug: boolean;

  constructor(flagCache: FlagCache, projectId: string, debug = false) {
    this.flagCache = flagCache;
    this.projectId = projectId;
    this.debug = debug;
  }

  /**
   * Dispatches an SSE message to the appropriate handler.
   */
  handle(message: SSEMessage): void {
    switch (message.type) {
      case 'config':
        this.handleFlagsUpdate(message.data);
        break;

      case 'flag_update':
        this.handleFlagUpdate(message.data);
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

  private handleFlagsUpdate(data: string): void {
    try {
      const parsed = JSON.parse(data) as unknown;
      const result = parseFlagConfigResult(parsed);
      if (result !== null && result.project_id === this.projectId) {
        if (result.flags.length > 0 || result.invalid_keys.length === 0) {
          this.flagCache.set(result.flags, 'sse', result.invalid_keys);
        } else {
          this.flagCache.markInvalid(result.invalid_keys, 'sse');
        }
        if (this.debug) {
          console.debug(`APDL: Updated ${result.flags.length} flags from SSE`);
        }
      }
    } catch (err) {
      if (this.debug) {
        console.error('APDL: Failed to parse config:', err);
      }
    }
  }

  private handleFlagUpdate(data: string): void {
    try {
      const parsed = JSON.parse(data) as unknown;
      const flags = parseFlagConfigs(parsed);
      if (flags !== null && flags.length > 0) {
        this.mergeFlags(flags);
        return;
      }

      if (!isRecord(parsed)) {
        return;
      }

      const authoritativeVersion = parseAuthoritativeVersion(parsed.version);
      if (authoritativeVersion === null) return;

      if (
        parsed.action === 'flag_removed'
        && isIdentifier(parsed.key)
      ) {
        this.flagCache.removeIfNewer(parsed.key, authoritativeVersion);
        return;
      }

      const fullFlag = extractFlagConfig(parsed.flag) ?? extractFlagConfig(parsed);
      if (fullFlag) {
        this.flagCache.upsertIfNewer(fullFlag, authoritativeVersion, 'sse');
        return;
      }

      const invalidKey = extractInvalidFlagKey(parsed.flag) ?? extractInvalidFlagKey(parsed);
      if (invalidKey) {
        this.flagCache.markInvalidIfNewer(
          invalidKey,
          authoritativeVersion,
          'sse'
        );
      }
    } catch (err) {
      if (this.debug) {
        console.error('APDL: Failed to parse flag_update:', err);
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
    for (const flag of flags) {
      this.flagCache.upsertIfNewer(flag, flag.version, 'sse');
    }
  }
}

function isRecord(input: unknown): input is Record<string, unknown> {
  return typeof input === 'object' && input !== null && !Array.isArray(input);
}

function parseAuthoritativeVersion(input: unknown): number | null {
  return typeof input === 'number' && Number.isInteger(input) && input >= 1
    ? input
    : null;
}
