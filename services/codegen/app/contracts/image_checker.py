"""Immutable identities and configs for provider-free semantic checkers.

The worker image owns these compiler installations.  Repository dependency
trees are supplied to the compilers only as read-only source/type data; their
executables, configs, plugins, and environment are never trusted.
"""

from __future__ import annotations

from pathlib import Path

IMAGE_NODE_MODULES = Path("/usr/local/lib/node_modules")
PYRIGHT_VERSION = "1.1.405"
TYPESCRIPT_VERSION = "5.9.3"

PYRIGHT_PACKAGE = "pyright"
PYRIGHT_ENTRYPOINT = Path("index.js")
TYPESCRIPT_PACKAGE = "typescript"
TYPESCRIPT_ENTRYPOINT = Path("bin/tsc")


def pyright_config(*, site_packages: Path) -> dict[str, object]:
    """Return the complete service-owned Pyright policy for one example."""
    return {
        "include": ["contract_example.py"],
        "exclude": [],
        "extraPaths": [site_packages.as_posix()],
        "pythonPlatform": "Linux",
        "pythonVersion": "3.12",
        "typeCheckingMode": "basic",
        "useLibraryCodeForTypes": True,
        "autoSearchPaths": False,
        "reportMissingImports": "error",
        "reportMissingModuleSource": "error",
        "reportMissingTypeStubs": "none",
    }


def typescript_config(*, example_name: str, allow_js: bool) -> dict[str, object]:
    """Return the complete service-owned TypeScript policy for one example."""
    return {
        "compilerOptions": {
            "allowJs": allow_js,
            "checkJs": allow_js,
            "esModuleInterop": True,
            "forceConsistentCasingInFileNames": True,
            "jsx": "preserve",
            "module": "NodeNext",
            "moduleDetection": "force",
            "moduleResolution": "NodeNext",
            "noEmit": True,
            "noErrorTruncation": True,
            "skipLibCheck": True,
            "strict": True,
            "target": "ES2022",
            "types": [],
        },
        "files": [example_name],
    }
