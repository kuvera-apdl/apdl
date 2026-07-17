"""Strict canonical project-ID contract tests."""

import pytest
from pydantic import TypeAdapter, ValidationError

from app.models.schemas import PROJECT_ID_PATTERN, ProjectId


PROJECT_ID_ADAPTER = TypeAdapter(ProjectId)


def test_project_id_contract_matches_the_repository_authority():
    assert PROJECT_ID_PATTERN == r"^[A-Za-z0-9]{1,64}$"
    assert PROJECT_ID_ADAPTER.validate_python("a") == "a"
    assert PROJECT_ID_ADAPTER.validate_python("A1" * 32) == "A1" * 32
    assert PROJECT_ID_ADAPTER.validate_python("123") == "123"


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        1,
        1.0,
        b"demo",
        "",
        "demo-project",
        "demo_project",
        " demo",
        "demo ",
        "a" * 65,
    ],
)
def test_project_id_contract_rejects_coercion_and_noncanonical_strings(value):
    with pytest.raises(ValidationError):
        PROJECT_ID_ADAPTER.validate_python(value)
