import type { PersistenceMode } from './config';

const PROJECT_ID_PATTERN = /^[a-zA-Z0-9]{1,64}$/;
const STORAGE_KEY_VERSION = 2;

export type BrowserStorageKind =
  | 'anonymous_id'
  | 'consent'
  | 'flags'
  | 'session';

export interface DeploymentStorageScope {
  deploymentOrigin: string;
  projectId: string;
}

/**
 * Returns the canonical browser-storage key for one APDL deployment and
 * project. The endpoint resolver has already reduced the deployment to an
 * HTTP(S) origin; the checks here keep direct internal callers fail-closed.
 */
export function scopedBrowserStorageKey(
  kind: BrowserStorageKind,
  scope: DeploymentStorageScope
): string {
  assertDeploymentStorageScope(scope);
  return `apdl_${kind}_v${STORAGE_KEY_VERSION}_${encodeURIComponent(
    scope.deploymentOrigin
  )}_${scope.projectId}`;
}

/**
 * Removes browser keys from the former project-only namespace. Those values
 * cannot be attributed to a deployment and must never be restored.
 */
export function rejectLegacyProjectStorage(
  projectId: string,
  persistence: PersistenceMode
): void {
  if (persistence !== 'localStorage' || !PROJECT_ID_PATTERN.test(projectId)) {
    return;
  }

  try {
    if (typeof localStorage === 'undefined') return;
    for (const kind of [
      'anonymous_id',
      'consent',
      'flags',
      'session',
    ] satisfies BrowserStorageKind[]) {
      localStorage.removeItem(`apdl_${kind}_${projectId}`);
    }
  } catch {
    // Storage may be unavailable. New scoped keys are still the only keys read.
  }
}

export function assertDeploymentStorageScope(
  scope: DeploymentStorageScope
): void {
  if (!PROJECT_ID_PATTERN.test(scope.projectId)) {
    throw new Error('APDL: persistent storage requires a canonical project ID');
  }

  let parsed: URL;
  try {
    parsed = new URL(scope.deploymentOrigin);
  } catch {
    throw new Error('APDL: persistent storage requires a canonical deployment origin');
  }

  if (
    (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
    || parsed.origin !== scope.deploymentOrigin
    || parsed.pathname !== '/'
    || parsed.search !== ''
    || parsed.hash !== ''
    || parsed.username !== ''
    || parsed.password !== ''
  ) {
    throw new Error('APDL: persistent storage requires a canonical deployment origin');
  }
}
