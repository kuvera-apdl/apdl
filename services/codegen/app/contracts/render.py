"""Deterministic model-facing rendering of exact dependency evidence."""

from __future__ import annotations

from app.contracts.models import ContractBundle, ContractEvidence, ContractResolution


def _render_evidence(evidence: ContractEvidence) -> list[str]:
    lines = [
        f"### `{evidence.package_name}` `{evidence.exact_version}`",
        "",
        f"- Ecosystem: `{evidence.ecosystem}`",
        f"- Package boundary: `{evidence.package_path}`",
        f"- Contract ID: `{evidence.contract_id}`",
        "- Evidence sources:",
    ]
    for source in evidence.sources:
        lines.append(
            f"  - `{source.kind.value}` `{source.relative_path}` "
            f"sha256 `{source.sha256}`"
        )
    if evidence.symbols:
        lines.extend(["", "#### Installed symbols", ""])
        for symbol in evidence.symbols:
            refs = ", ".join(f"`{value}`" for value in symbol.source_ids)
            lines.append(
                f"- `{symbol.qualified_name}` ({symbol.kind.value}): "
                f"`{symbol.signature}` — sources {refs}"
            )
    if evidence.lifecycle_facts:
        lines.extend(["", "#### Lifecycle facts", ""])
        for fact in evidence.lifecycle_facts:
            refs = ", ".join(f"`{value}`" for value in fact.source_ids)
            lines.append(f"- {fact.statement} Sources: {refs}.")
    if evidence.examples:
        lines.extend(["", "#### Compile-checked examples", ""])
        for example in evidence.examples:
            lines.extend(
                [
                    f"Checked with `{example.command}` ({example.tool_version}).",
                    f"```{example.language.lower()}",
                    example.snippet,
                    "```",
                ]
            )
    return lines


def _render_resolution(resolution: ContractResolution) -> list[str]:
    lines: list[str] = []
    if resolution.evidence is not None:
        lines.extend(_render_evidence(resolution.evidence))
    else:
        lines.extend(
            [
                f"### `{resolution.request.package_name}`",
                "",
                "No exact installed contract evidence is available.",
            ]
        )
    if resolution.blockers:
        lines.extend(["", "#### Resolution blockers and warnings", ""])
        for blocker in sorted(
            resolution.blockers,
            key=lambda item: (item.severity, item.code.value, item.message),
        ):
            lines.append(
                f"- **{blocker.severity} / {blocker.code.value}:** {blocker.message}"
            )
    return lines


def render_contract_bundle(bundle: ContractBundle) -> str:
    """Render only strict evidence; checked examples exist only after a pass."""
    lines = [
        "## Exact installed dependency contracts",
        "",
        "These records are repository-grounding evidence, not GitHub CI results.",
    ]
    for resolution in bundle.resolutions:
        lines.extend(["", *_render_resolution(resolution)])
    return "\n".join(lines).strip() + "\n"
