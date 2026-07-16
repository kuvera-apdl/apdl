# Custom-events ETL Framework

Every record entering the APDL warehouse — behavioral events, decisions,
external partner feeds — is wrapped in the same **canonical envelope** keyed by
a `_schema` discriminator, and lands in one of the v2 ClickHouse tables
(`events_v2`, `decisions_v2`, `feeds_v2`). This framework (`etl/`) captures the
transform-and-load lifecycle once as a **Template Method** base class, routes
records to the right transform with a **registry** keyed on `_schema`, and
scaffolds new custom event types with a **Jinja generator**.

It is a standalone package: it owns its own minimal envelope contract and does
not import any APDL service code, so the same transforms run for live traffic,
backfills, and replays. It also does not talk to ClickHouse — it produces rows
and hands them to a `Loader`, which is the single seam a warehouse writer plugs
into.

## The lifecycle

```
decode  →  validate  →  enrich  →  build_row     (→ load, done by the pipeline)
(parse)    (reject)     (derive)   (map to rows)
```

`BaseTransform.process()` (in `etl/base.py`) implements this skeleton and
isolates failures: any exception in any phase becomes a DLQ entry instead of
crashing the batch. Subclasses override only the parts that vary:

| Hook | Required | Purpose |
|------|----------|---------|
| `decode(raw, ctx)` | no | Parse raw input into a validated envelope (defaults to validating the dict against `envelope_model`). Override for EDI/CSV sources. |
| `validate(envelope, ctx)` | no | Cross-field checks beyond the model; raise to route the record to the DLQ. |
| `enrich(envelope, ctx)` | no | Run the declared enricher chain (defaults to it); override for bespoke derivation. |
| `build_row(envelope, ctx, enrichment)` | **yes** | Map the envelope to one or more warehouse rows. |

The invariant parts — envelope validation, the enricher chain, error isolation,
DLQ construction — live in the base class.

### Declarative configuration

Class attributes configure the fixed parts:

```python
@register_transform
class TrackTransform(_EventTransform):
    schema = "track@1"            # registry key = the _schema discriminator
    target_table = "events_v2"    # destination ClickHouse table
    dlq_table = "events_dlq_v2"   # where failures land
    enrichers = ("device", "geo") # enricher chain, run in order
    columns = EVENTS_V2_COLUMNS   # declared output columns (loader + docs)
```

## Enrichers

Enrichment is a declarative, ordered chain. A transform lists the enrichers it
wants by name; the framework resolves and runs them, merging each one's output
(later wins) into a single `enrichment` dict that `build_row` consumes.
Enrichers are pure functions of `(envelope, ctx)`, so the same chain runs
identically on replays and live traffic, and a failing enricher is logged and
skipped — enrichment never knocks a record into the DLQ on its own.

Two dependency-free built-ins ship (`etl/enrichment.py`):

* **`device`** — User-Agent heuristic → `device_type` / `browser` / `os_name`.
* **`geo`** — normalises the location signal already on the envelope.

Swapping in a MaxMind-backed `geo` or a `ua-parser` `device` is a matter of
registering a new enricher under the same name with `@register_enricher` — no
transform changes.

## Registry & the pipeline

Transforms self-register via `@register_transform`. Importing `etl` registers
all built-ins as a side effect. `EtlPipeline` (`etl/pipeline.py`) is fully
data-driven: for each record it reads `_schema`, resolves the registered
transform (unrouted schemas go straight to the DLQ), runs it, and routes the
rows to the loader or the failure to the DLQ loader. Adding a custom event type
requires **no pipeline change**.

```python
from etl import EtlPipeline, CollectingLoader, EtlContext

pipeline = EtlPipeline(CollectingLoader())
ctx = EtlContext(project_id="project42", received_at=..., ip="203.0.113.7", source="sdk-js@2.4.1")
result = pipeline.process_record(raw_envelope, ctx)
```

### Loaders

`Loader` is a one-method protocol (`load(target, rows)`). Two implementations
ship:

* **`CollectingLoader`** — accumulates rows in memory; for tests and dry runs.
* **`BatchingLoader`** — buffers per target table and flushes through a `sink`
  callable when a batch fills. A production ClickHouse writer implements that
  `sink` (it receives `(target_table, rows)` and issues the INSERT) and stays
  entirely outside this package.

## Built-in transforms

| Schema | Target | Notes |
|--------|--------|-------|
| `track@1`, `page@1`, `screen@1`, `identify@1`, `group@1`, `alias@1` | `events_v2` | Behavioral events; share `_EventTransform`. |
| `flag_eval@1`, `exposure@1`, `agent_action@1`, `personalization@1` | `decisions_v2` | Decisions; share `_DecisionTransform`, identical column set per schema. |
| `partner.shipments.csv@1` | `feeds_v2` | Worked example of the external-feed pattern. |

## Scaffolding a new custom event

```bash
cd pipeline/etl
python scripts/new_transform.py refund.issued@1 \
    --description "A refund was issued to a customer" \
    --target-table events_v2 \
    --enrichers device geo \
    --validate          # include a validate() rejection hook
```

This writes `etl/transforms/refund_issued.py`, registers it in
`etl/transforms/__init__.py`, and the pipeline will route `refund.issued@1`
records to it automatically. Use `--dry-run` to preview. Then fill in the
`build_row` TODO and run the tests.

## Testing & linting

```bash
make test-etl   # pytest (pure, no external deps)
make lint-etl   # ruff check
```
