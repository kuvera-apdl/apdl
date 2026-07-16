"""Unsupported APDL custom-events ETL design prototype.

This package is not imported by the supported runtime and its v2 SQL lives
outside the ClickHouse migration path. It explores a schema-routed envelope:
define a transform by subclassing :class:`~etl.base.BaseTransform`, declaring
the ``_schema`` it handles and its prototype target table, and implementing
``build_row``.

Importing this package also imports :mod:`etl.transforms`, registering all
built-in transforms (events / decisions / feeds) as a side effect.

    from etl import EtlPipeline, CollectingLoader, EtlContext

    pipeline = EtlPipeline(CollectingLoader())
    pipeline.process_record(raw_envelope, ctx)
"""

from __future__ import annotations

from etl.base import BaseTransform
from etl.context import DlqEntry, EtlContext, Row, TransformResult, ZERO_UUID
from etl.enrichment import (
    BaseEnricher,
    get_enricher,
    register_enricher,
    run_enrichers,
)
from etl.envelope import CanonicalEnvelope
from etl.loader import BatchingLoader, CollectingLoader, Loader
from etl.pipeline import EtlPipeline, dlq_row
from etl.registry import (
    get_transform,
    is_registered,
    list_transforms,
    register_transform,
    registered_transforms,
)

# Importing the transforms package registers all built-ins. Done last so the
# core symbols above are available while the transform modules import them.
from etl import transforms  # noqa: E402,F401  (registration side-effect)

__all__ = [
    "BaseTransform",
    "BaseEnricher",
    "BatchingLoader",
    "CanonicalEnvelope",
    "CollectingLoader",
    "DlqEntry",
    "EtlContext",
    "EtlPipeline",
    "Loader",
    "Row",
    "TransformResult",
    "ZERO_UUID",
    "dlq_row",
    "get_enricher",
    "get_transform",
    "is_registered",
    "list_transforms",
    "register_enricher",
    "register_transform",
    "registered_transforms",
    "run_enrichers",
    "transforms",
]
