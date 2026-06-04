#!/usr/bin/env python3
"""Scaffold a new APDL agent from the Jinja templates.

Generates a framework-compliant agent module and its prompt module, then wires
the agent into the registry by appending its import to ``app/graphs/__init__.py``.
The generated agent subclasses :class:`app.framework.BaseAgent`, so all you fill
in are the ``gather`` / ``build_prompt`` / ``act`` / ``memory_entries`` hooks.

Usage:
    python scripts/new_agent.py churn_predictor \\
        --description "Predict churn risk per segment and propose retention plays" \\
        --requires insights \\
        --produces churn_predictions \\
        --parse-as list \\
        --memory-query "churn retention signals" \\
        --order 25 \\
        --act

Run with --dry-run to preview the files without writing anything.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
except ImportError:  # pragma: no cover - dev dependency
    sys.exit("jinja2 is required. Install dev deps: uv pip install -e '.[dev]'")

SERVICE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = SERVICE_ROOT / "templates"
AGENTS_DIR = SERVICE_ROOT / "app" / "graphs"
PROMPTS_DIR = SERVICE_ROOT / "app" / "llm" / "prompts"
GRAPHS_INIT = AGENTS_DIR / "__init__.py"

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def pascal_case(slug: str) -> str:
    return "".join(part.capitalize() for part in slug.split("_"))


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="snake_case agent name (also the registry key), e.g. churn_predictor")
    p.add_argument("--description", default="", help="One-line description of the agent.")
    p.add_argument("--order", type=int, default=50, help="Pipeline order (lower runs earlier).")
    p.add_argument("--model-tier", choices=["fast", "reasoning"], default="reasoning")
    p.add_argument("--produces", default="", help="State key for output (default: <name>_output).")
    p.add_argument("--requires", default="", help="Comma-separated state keys this agent needs.")
    p.add_argument("--memory-query", default="", help="Semantic query for long-term memory context.")
    p.add_argument("--memory-top-k", type=int, default=5)
    p.add_argument("--parse-as", choices=["object", "list"], default="object")
    p.add_argument("--act", action="store_true", help="Include an act() phase (safety + autonomy gating).")
    p.add_argument("--force", action="store_true", help="Overwrite existing files.")
    p.add_argument("--dry-run", action="store_true", help="Print generated files without writing.")
    return p.parse_args(argv)


def build_context(args: argparse.Namespace) -> dict:
    slug = args.name
    if not SLUG_RE.match(slug):
        sys.exit(f"Invalid agent name '{slug}'. Use snake_case starting with a letter.")

    requires = tuple(r.strip() for r in args.requires.split(",") if r.strip())
    produces = args.produces or f"{slug}_output"

    framework_imports = ["AgentContext", "BaseAgent", "MemoryEntry", "register_agent"]
    if args.act:
        framework_imports += ["GateDecision", "gate_action"]
    framework_imports.sort()

    return {
        "slug": slug,
        "class_name": f"{pascal_case(slug)}Agent",
        "description": args.description or f"{slug} agent",
        "order": args.order,
        "model_tier": args.model_tier,
        "produces": produces,
        "requires_repr": repr(requires),
        "memory_query_repr": repr(args.memory_query) if args.memory_query else "None",
        "memory_top_k": args.memory_top_k,
        "parse_as": args.parse_as,
        "has_act": args.act,
        "prompt_module": slug,
        "system_const": f"{slug.upper()}_SYSTEM",
        "prompt_const": f"{slug.upper()}_PROMPT",
        "framework_imports": framework_imports,
    }


def render(template_name: str, ctx: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    # Collapse blank lines left by conditional blocks (>=3 newlines -> 2).
    out = env.get_template(template_name).render(**ctx)
    return re.sub(r"\n{3,}", "\n\n\n", out)


def register_in_init(slug: str, dry_run: bool) -> str:
    """Insert ``slug`` into the sorted import list in app/graphs/__init__.py."""
    text = GRAPHS_INIT.read_text()
    match = re.search(r"from app\.graphs import \((?P<head>[^\n]*)\n(?P<body>.*?)\n\)", text, re.DOTALL)
    if not match:
        return "  ! Could not auto-register; add `import app.graphs.%s` manually." % slug

    modules = re.findall(r"^\s*([a-z_][a-z0-9_]*),", match.group("body"), re.MULTILINE)
    if slug in modules:
        return f"  - {slug} already registered in {GRAPHS_INIT.name}"

    modules = sorted(set(modules) | {slug})
    body = "\n".join(f"    {m}," for m in modules)
    new_block = f"from app.graphs import ({match.group('head')}\n{body}\n)"
    new_text = text[: match.start()] + new_block + text[match.end():]
    if not dry_run:
        GRAPHS_INIT.write_text(new_text)
    return f"  + registered {slug} in {GRAPHS_INIT.name}"


def write_file(path: Path, content: str, *, force: bool, dry_run: bool) -> str:
    if path.exists() and not force:
        sys.exit(f"Refusing to overwrite existing {path} (use --force).")
    if dry_run:
        return f"----- {path} -----\n{content}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"  + wrote {path.relative_to(SERVICE_ROOT)}"


def main(argv: list[str]) -> None:
    args = parse_args(argv)
    ctx = build_context(args)

    agent_code = render("agent.py.jinja", ctx)
    prompts_code = render("prompts.py.jinja", ctx)
    agent_path = AGENTS_DIR / f"{ctx['slug']}.py"
    prompts_path = PROMPTS_DIR / f"{ctx['slug']}.py"

    if args.dry_run:
        print(write_file(prompts_path, prompts_code, force=True, dry_run=True))
        print(write_file(agent_path, agent_code, force=True, dry_run=True))
        print(f"\n(dry-run) would register '{ctx['slug']}' in {GRAPHS_INIT.name}")
        return

    print(f"Scaffolding agent '{ctx['slug']}' ({ctx['class_name']}):")
    print(write_file(prompts_path, prompts_code, force=args.force, dry_run=False))
    print(write_file(agent_path, agent_code, force=args.force, dry_run=False))
    print(register_in_init(ctx["slug"], dry_run=False))
    print("\nNext steps:")
    print(f"  1. Fill in the prompts in app/llm/prompts/{ctx['slug']}.py")
    print(f"  2. Implement the hooks in app/graphs/{ctx['slug']}.py")
    print(f"  3. Trigger it via analysis_types=['{ctx['slug']}']")


if __name__ == "__main__":
    main(sys.argv[1:])
