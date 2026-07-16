"""Unit tests for the changeset lifecycle state machine."""

import pytest

from app.models.changeset import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    ChangesetStatus,
    InvalidTransition,
    assert_transition,
    can_transition,
)


def test_happy_path_is_reachable():
    path = [
        ChangesetStatus.queued,
        ChangesetStatus.cloning,
        ChangesetStatus.editing,
        ChangesetStatus.pushing,
        ChangesetStatus.pr_open,
        ChangesetStatus.merged,
    ]
    for frm, to in zip(path, path[1:]):
        assert can_transition(frm, to), f"{frm} → {to} should be allowed"


def test_terminal_states_have_no_exits():
    assert TERMINAL_STATUSES == frozenset(
        {
            ChangesetStatus.merged,
            ChangesetStatus.error,
        }
    )
    for terminal in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()
        assert not can_transition(terminal, ChangesetStatus.queued)


def test_cannot_skip_stages():
    assert not can_transition(ChangesetStatus.queued, ChangesetStatus.merged)
    assert not can_transition(ChangesetStatus.editing, ChangesetStatus.pr_open)


def test_assert_transition_raises_on_illegal_move():
    with pytest.raises(InvalidTransition):
        assert_transition(ChangesetStatus.merged, ChangesetStatus.queued)


def test_abandon_allowed_from_open_states():
    for frm in (
        ChangesetStatus.queued,
        ChangesetStatus.pr_open,
    ):
        assert can_transition(frm, ChangesetStatus.abandoned)


def test_github_reopen_restores_an_abandoned_pr():
    assert can_transition(ChangesetStatus.abandoned, ChangesetStatus.pr_open)


def test_lifecycle_statuses_do_not_encode_external_ci():
    assert {status.value for status in ChangesetStatus} == {
        "queued",
        "cloning",
        "editing",
        "pushing",
        "pr_open",
        "merged",
        "abandoned",
        "error",
    }


def test_every_status_has_a_transition_entry():
    for status in ChangesetStatus:
        assert status in ALLOWED_TRANSITIONS
