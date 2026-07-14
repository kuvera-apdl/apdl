"""Runtime package.

Keep this module deliberately import-light: GitHub artifact adapters import
``app.runtime.models`` and the collector imports those adapters. Eager re-exports
here would create an import-order-dependent cycle.
"""
