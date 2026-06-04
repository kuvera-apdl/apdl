#!/usr/bin/env python3
"""Scaffold a new ETL transform from the Jinja template.

Generates ``etl/transforms/<module>.py`` for a custom ``_schema`` and registers
it by appending an import to ``etl/transforms/__init__.py`` (so importing
``etl`` picks it up). Fill in the ``build_row`` TODO afterwards.

Examples
--------
    python scripts/new_transform.py refund.issued@1 \\
        --description "A refund was issued to a customer" \\
        --target-table events_v2 \\
        --enrichers device geo \\
        --validate

    python scripts/new_transform.py edi.x12.850@1 \\
        --description "EDI 850 purchase order" \\
        --target-table feeds_v2 \\
        --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
TRANSFORMS_DIR = ROOT / "etl" / "transforms"
INIT_FILE = TRANSFORMS_DIR / "__init__.py"


def module_name(schema: str) -> str:
    """'refund.issued@1' -> 'refund_issued'; 'track@1' -> 'track'."""
    base = schema.split("@", 1)[0]
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", base).strip("_").lower()
    if not slug:
        raise ValueError(f"cannot derive a module name from schema '{schema}'")
    return slug


def class_name(schema: str) -> str:
    """'refund.issued@1' -> 'RefundIssuedTransform'."""
    parts = re.split(r"[^0-9a-zA-Z]+", schema.split("@", 1)[0])
    camel = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return f"{camel}Transform"


def render(args: argparse.Namespace) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("transform.py.jinja")
    return template.render(
        schema=args.schema,
        description=args.description,
        target_table=args.target_table,
        class_name=class_name(args.schema),
        enrichers_repr=repr(tuple(args.enrichers)),
        has_validate=args.validate,
    )


def register_in_init(module: str) -> bool:
    """Add ``module`` to the import line in transforms/__init__.py. Idempotent."""
    text = INIT_FILE.read_text()
    pattern = re.compile(r"from etl\.transforms import ([^\n]+?)(\s*#[^\n]*)?\n")
    match = pattern.search(text)
    if not match:
        return False
    names = [n.strip() for n in match.group(1).split(",") if n.strip()]
    if module in names:
        return True
    names = sorted(set(names) | {module})
    comment = match.group(2) or "  # noqa: F401  (registration side-effect)"
    new_line = f"from etl.transforms import {', '.join(names)}{comment}\n"
    INIT_FILE.write_text(text[: match.start()] + new_line + text[match.end():])
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new ETL transform.")
    parser.add_argument("schema", help="the _schema discriminator, e.g. 'refund.issued@1'")
    parser.add_argument("--description", default="", help="one-line transform description")
    parser.add_argument(
        "--target-table",
        default="events_v2",
        help="destination ClickHouse table (default: events_v2)",
    )
    parser.add_argument(
        "--enrichers",
        nargs="*",
        default=[],
        metavar="NAME",
        help="enricher names to run in order (e.g. device geo)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="include a validate() hook stub for cross-field rejection",
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing module")
    parser.add_argument("--dry-run", action="store_true", help="print the file instead of writing")
    args = parser.parse_args(argv)

    if "@" not in args.schema:
        parser.error("schema must include a version, e.g. 'refund.issued@1'")
    if not args.description:
        args.description = f"{args.schema} -> {args.target_table}."

    module = module_name(args.schema)
    out_path = TRANSFORMS_DIR / f"{module}.py"
    rendered = render(args)

    if args.dry_run:
        print(f"# --- would write {out_path} ---\n")
        print(rendered)
        print(f"# --- would register module '{module}' in {INIT_FILE.name} ---")
        return 0

    if out_path.exists() and not args.force:
        parser.error(f"{out_path} already exists (use --force to overwrite)")

    out_path.write_text(rendered)
    if not register_in_init(module):
        print(
            f"WARNING: wrote {out_path} but could not auto-register it; add "
            f"'{module}' to the import in {INIT_FILE} manually.",
            file=sys.stderr,
        )
    print(f"Created {out_path}")
    print(f"Registered '{args.schema}' ({class_name(args.schema)}).")
    print("Next: fill in the build_row TODO, then run the tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
