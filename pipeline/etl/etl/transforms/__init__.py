"""Built-in transforms. Importing this package registers them all.

Each module's ``@register_transform`` decorators fire on import, so simply
importing :mod:`etl.transforms` (done for you by ``import etl``) populates the
registry. ``scripts/new_transform.py`` appends a new import line here when it
scaffolds a custom transform.
"""

from __future__ import annotations

from etl.transforms import decisions, events, feeds  # noqa: F401  (registration side-effect)
