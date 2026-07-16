"""Prompt transcript bounds at the editor and JSONB persistence boundary."""

from __future__ import annotations

import json

import pytest

from app.editor.prompts import (
    PROMPT_ENTRY_MAX_BYTES,
    PROMPT_TRANSCRIPT_MAX_BYTES,
    append_prompt,
    bound_prompt_entry,
    replace_latest_prompt,
    serialized_prompt_bytes,
)
from app.store import changesets as changeset_store
from tests.fakes import FakePool


def _large_prompt(label: str, character: str) -> dict[str, str | None]:
    return {
        "stage": "edit",
        "label": label,
        "system": None,
        "user": (character * 100_000) + f" tail:{label}",
        "notes": None,
    }


def test_prompt_entry_is_byte_bounded_with_head_tail_marker() -> None:
    bounded = bound_prompt_entry(_large_prompt("initial", "A"))

    assert serialized_prompt_bytes(bounded) <= PROMPT_ENTRY_MAX_BYTES
    assert bounded["user"].startswith("A" * 20)
    assert bounded["user"].endswith("tail:initial")
    assert "[…truncated " in bounded["user"]


def test_running_transcript_preserves_first_and_newest_entries() -> None:
    transcript: list[dict] = []
    for index in range(10):
        append_prompt(transcript, _large_prompt(f"attempt-{index}", str(index)))

    assert serialized_prompt_bytes(transcript) <= PROMPT_TRANSCRIPT_MAX_BYTES
    assert transcript[0]["label"] == "attempt-0"
    assert transcript[-1]["label"] == "attempt-9"
    marker = next(item for item in transcript if item["stage"] == "transcript")
    assert "middle prompt entries" in marker["notes"]
    assert "omitted 7 middle prompt entries" in marker["notes"]


def test_replacing_latest_prompt_reapplies_entry_and_aggregate_bounds() -> None:
    transcript: list[dict] = []
    append_prompt(transcript, _large_prompt("brief", "A"))

    replace_latest_prompt(
        transcript,
        {
            **transcript[-1],
            "notes": "fallback: " + ("detail " * PROMPT_ENTRY_MAX_BYTES),
        },
    )

    assert transcript[-1]["stage"] == "edit"
    assert "truncated" in transcript[-1]["notes"]
    assert serialized_prompt_bytes(transcript[-1]) <= PROMPT_ENTRY_MAX_BYTES
    assert serialized_prompt_bytes(transcript) <= PROMPT_TRANSCRIPT_MAX_BYTES


@pytest.mark.asyncio
async def test_set_prompts_enforces_entry_and_aggregate_storage_limits() -> None:
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs-prompts", "demo")
    prompts = [_large_prompt(f"attempt-{index}", str(index)) for index in range(10)]

    await changeset_store.set_prompts(pool, "cs-prompts", prompts)

    stored = pool.store["changesets"]["cs-prompts"]["prompts"]
    assert len(stored.encode("utf-8")) <= PROMPT_TRANSCRIPT_MAX_BYTES
    decoded = json.loads(stored)
    assert all(
        serialized_prompt_bytes(item) <= PROMPT_ENTRY_MAX_BYTES for item in decoded
    )
    assert decoded[0]["label"] == "attempt-0"
    assert decoded[-1]["label"] == "attempt-9"
    assert any(item["stage"] == "transcript" for item in decoded)


@pytest.mark.asyncio
async def test_prompt_transcript_redacts_provider_values_and_token_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_secret = "configured-provider-value-without-a-known-prefix"
    github_token = "ghp_" + ("a" * 40)
    fine_grained_github_token = "github_pat_" + ("b" * 32)
    bearer = "opaque-bearer-value-without-a-known-prefix"
    aws_secret = "opaque-aws-secret-access-key-value"
    proxy_url = "http://proxy-user:proxy-password@proxy.internal:8080"
    monkeypatch.setenv("OPENAI_API_KEY", provider_secret)
    pool = FakePool()
    pool.add_connection("demo")
    pool.add_changeset("cs-redacted-prompts", "demo")
    prompt = {
        "stage": "edit",
        "label": f"attempt using {provider_secret}",
        "system": f"Authorization: Bearer {bearer}",
        "user": (
            f"provider={provider_secret}\n"
            f"github={github_token}\n"
            f"fine_grained={fine_grained_github_token}\n"
            f"AWS_SECRET_ACCESS_KEY={aws_secret}\n"
            f"HTTPS_PROXY={proxy_url}\n"
            "DATABASE_URL=postgresql://user:password@db.internal/apdl\n"
            '"api_key": "quoted secret with spaces"'
        ),
        "notes": "OPENAI_API_KEY=another-secret-value",
    }

    transcript: list[dict] = []
    append_prompt(transcript, prompt)
    bounded = transcript[0]
    await changeset_store.set_prompts(
        pool,
        "cs-redacted-prompts",
        [prompt],
    )
    stored = pool.store["changesets"]["cs-redacted-prompts"]["prompts"]

    for secret in (
        provider_secret,
        github_token,
        fine_grained_github_token,
        bearer,
        aws_secret,
        proxy_url,
        "postgresql://user:password@db.internal/apdl",
        "quoted secret with spaces",
        "another-secret-value",
    ):
        assert secret not in json.dumps(bounded)
        assert secret not in stored
    assert json.dumps(bounded).count("[REDACTED]") >= 8
    assert serialized_prompt_bytes(bounded) <= PROMPT_ENTRY_MAX_BYTES
    assert len(stored.encode("utf-8")) <= PROMPT_TRANSCRIPT_MAX_BYTES
