"""Transform registry — routes a record to a transform by its ``_schema``.

Transforms self-register with the ``@register_transform`` decorator under their
``schema`` attribute (e.g. ``"track@1"``, ``"flag_eval@1"``,
``"partner.shipments.csv@1"``). The pipeline dispatcher then looks up the right
transform for an inbound envelope instead of importing each class and
hard-coding an if-chain — so adding a custom event type is a new module, not a
dispatcher edit.

Importing :mod:`etl.transforms` (done for you by ``import etl``) registers all
built-in transforms as an import side-effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from etl.base import BaseTransform

_REGISTRY: dict[str, type["BaseTransform"]] = {}
#: Transforms are stateless, so we memoise one instance per schema rather than
#: re-instantiating on every record in the hot path.
_INSTANCES: dict[str, "BaseTransform"] = {}


def register_transform(cls: type["BaseTransform"]) -> type["BaseTransform"]:
    """Class decorator that registers a transform under its ``schema`` attribute."""
    schema = getattr(cls, "schema", None)
    if not schema:
        raise ValueError(
            f"{cls.__name__} must define a non-empty 'schema' to register"
        )
    if schema in _REGISTRY and _REGISTRY[schema] is not cls:
        raise ValueError(f"Duplicate transform schema '{schema}' ({cls.__name__})")
    _REGISTRY[schema] = cls
    _INSTANCES.pop(schema, None)
    return cls


def get_transform(schema: str) -> "BaseTransform":
    """Return the (memoised) transform instance registered for ``schema``.

    Raises:
        KeyError: If no transform is registered for that schema.
    """
    if schema not in _REGISTRY:
        raise KeyError(
            f"No transform registered for _schema='{schema}'. Known: {sorted(_REGISTRY)}"
        )
    if schema not in _INSTANCES:
        _INSTANCES[schema] = _REGISTRY[schema]()
    return _INSTANCES[schema]


def is_registered(schema: str) -> bool:
    return schema in _REGISTRY


def list_transforms() -> list[str]:
    """Return registered schemas, grouped by target table then schema name."""
    return sorted(_REGISTRY, key=lambda s: (_REGISTRY[s].target_table, s))


def registered_transforms() -> dict[str, type["BaseTransform"]]:
    """Return a copy of the full registry mapping schema -> class."""
    return dict(_REGISTRY)
