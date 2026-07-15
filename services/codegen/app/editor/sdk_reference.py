"""Per-repo APDL SDK integration references handed to the editing agent.

The failure this fixes: the SDK the agent must instrument (`@apdl-oss/sdk` for
the browser, `apdl` for Python) lives in the repo's ``node_modules`` /
site-packages, which is NOT in Aider's repo map — so "use the SDK" is
unactionable and the agent guesses (e.g. pushing to a ``window.apdl`` global the
SDK never reads). These references give the agent the exact, correct call path
for the two things codegen actually does — emit an event, identify a user — as a
recipe, not an exhaustive API dump.

They are STANDING reference (stable across changesets), so like the conventions
they ride Aider's ``--read`` cacheable prefix. Unlike the conventions they are
language-scoped: only the reference for an SDK the repo actually depends on is
attached, so a Python service isn't handed browser globals and vice-versa.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# --- JS / TypeScript browser SDK -------------------------------------------

JS_SDK_REFERENCE_MD = """\
# APDL JS SDK reference (`@apdl-oss/sdk`) — read-only

This repo depends on `@apdl-oss/sdk`. Emit analytics through THIS SDK. Every
call below enqueues to the SDK's own transport → the ingestion backend and is
tagged with the SDK's resolved identity, so events join the app's identity graph.

## Emitting — get the client via `APDL.init`, nothing else
The package's exported entrypoint is the `APDL` class. There is NO lowercase
`apdl` module-singleton export in the published SDK — `import { apdl }` resolves
to nothing and FAILS the build. `APDL.init()` is an idempotent singleton:
repeated calls resolve the SAME client instance (keyed by the env client key), so
calling it at a new call site REUSES the app's existing client rather than
creating a second one. It is SSR-safe (returns an inert no-op on the server) and
falls back to env config, so a bare `APDL.init()` needs no arguments. Never
construct `APDLClient` by hand.

  ```ts
  import { APDL } from '@apdl-oss/sdk';
  const apdl = APDL.init();                    // reuses the app's env-configured client
  apdl.track('event_name', { key: 'value' });  // custom event
  apdl.identify(userId, { plan: 'pro' });       // set identity + traits
  apdl.page('/route', { section: 'home' });     // pageview
  apdl.getVariant('flag_key');                  // feature-flag / experiment variant
  ```

Apps usually init once in a bootstrap component (e.g. `APDLInit` calling
`APDL.init({ endpoint, auth, ... })`); a bare `APDL.init()` elsewhere returns
that same instance, so reuse it freely — do NOT re-pass config or open a client.

Client surface (`APDLClient`): `track`, `identify`, `group`, `page`, `reset`,
`getVariant`, `getVariantDetails`, `onVariantChange`, `shutdown`.

## Rules
- Consent is enforced INSIDE the SDK — do not add a second consent gate.
- NEVER push events to `window.apdl`, `window.dataLayer`, or any array/global,
  and never hand-roll `fetch`/`sendBeacon` to the ingestion URL. The SDK does not
  read those sinks; such events reach no backend and carry no identity.
- If an existing app helper (e.g. a local `trackEvent`) is the intended path,
  FIRST confirm it terminates in an `APDL.init` client's `track`. If it
  writes to a `window.*` global instead, it is broken — call the SDK directly.
- A test that asserts emission should spy on the SDK's `track` (or the client
  returned by `APDL.init`), never on a `window` global.
"""

# --- Python server-side SDK -------------------------------------------------

PYTHON_SDK_REFERENCE_MD = """\
# APDL Python SDK reference (`apdl`) — read-only

This repo depends on the `apdl` server-side SDK. Emit analytics through THIS
SDK; each call enqueues to the SDK's transport → the ingestion backend.

## Usage
```python
from apdl import APDL

client = APDL.init(api_key="...")           # or APDL.init(APDLConfig(...))
client.track("event_name", {"key": "value"})
client.identify(user_id, {"plan": "pro"})
client.page("/route", {"section": "home"})
client.flush()                              # force-send buffered events

# or manage lifecycle with the context manager (flushes on exit):
with APDL.init(api_key="...") as client:
    client.track("event_name", {"key": "value"})
```

## Rules
- Reuse the app's existing client/singleton if one is already constructed — do
  NOT spin up a second client per call site.
- NEVER post raw JSON to the ingestion endpoint by hand; use `client.track`.
- Call `client.flush()` (or use the context manager) before a short-lived
  process exits, or buffered events are lost.
"""

#: Read-only filenames written outside the clone (so they never enter the diff).
_JS_REFERENCE = ("APDL_SDK_JS.md", JS_SDK_REFERENCE_MD)
_PYTHON_REFERENCE = ("APDL_SDK_PYTHON.md", PYTHON_SDK_REFERENCE_MD)

#: The exact browser SDK package name in a JS manifest.
_JS_SDK_PACKAGE = "@apdl-oss/sdk"

#: The Python SDK's distribution/import name is exactly ``apdl``. Match it as a
#: standalone dependency token — ``apdl``, ``apdl==1.0``, ``apdl>=1``, ``"apdl"``,
#: ``apdl[extra]`` — but NOT ``apdl-codegen`` / ``myapdl`` / ``apdlx``.
_PY_APDL_RE = re.compile(r"""(?im)(?:^|[\s"',\[=])apdl(?=[\s"',\]<>=!~;\[]|$)""")


def _reads_js_sdk(repo_dir: Path) -> bool:
    """True when ``package.json`` declares a dependency on ``@apdl-oss/sdk``."""
    package_json = repo_dir / "package.json"
    if not package_json.is_file():
        return False
    try:
        data = json.loads(package_json.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return False
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict) and _JS_SDK_PACKAGE in section:
            return True
    return False


def _reads_python_sdk(repo_dir: Path) -> bool:
    """True when a Python manifest declares a dependency on the ``apdl`` package.

    Scans ``pyproject.toml`` and any ``requirements*.txt`` as text (no TOML parse
    needed — we only need to spot the dependency token), matching ``apdl`` as a
    whole package name so sibling names like ``apdl-codegen`` don't false-match.
    """
    candidates = [repo_dir / "pyproject.toml", *repo_dir.glob("requirements*.txt")]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _PY_APDL_RE.search(text):
            return True
    return False


def detect_sdk_references(repo_dir: Path) -> list[tuple[str, str]]:
    """Return the ``(filename, markdown)`` SDK references the repo depends on.

    Zero, one, or both (a full-stack repo may use both SDKs). Only references for
    an SDK actually present in the repo's manifests are returned, so the agent is
    never handed a reference for a language it isn't using.
    """
    references: list[tuple[str, str]] = []
    if _reads_js_sdk(repo_dir):
        references.append(_JS_REFERENCE)
    if _reads_python_sdk(repo_dir):
        references.append(_PYTHON_REFERENCE)
    return references
