#!/usr/bin/env python3
"""Safely prepare the dedicated host directory for Codegen LLM broker sockets."""

from __future__ import annotations

from pathlib import Path

from app.config import codegen_llm_broker_dir
from app.llm.broker_directory import prepare_broker_root


def prepare_llm_broker_dir() -> Path:
    """Create one missing dedicated directory or validate it without chmod."""
    path = Path(codegen_llm_broker_dir())
    return prepare_broker_root(path)


if __name__ == "__main__":
    print(prepare_llm_broker_dir())
