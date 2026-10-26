"""A late budget finalizer may only fail its own still-running Kanban claim."""

import logging
from types import SimpleNamespace

import pytest

from agent.turn_finalizer import _resolve_budget_fallback
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def worker(tmp_path, monkeypatch):
    path = tmp_path / "kanban.db"
    conn = kbc.connect(path)
    task_id = kb.create_task(conn, title="Budget ownership", assignee="builder")
    claim = kb.claim_task(conn, task_id, claimer="builder:original")
    assert claim is not None
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claim.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claim.claim_lock)
    try:
        yield conn, task_id, claim
    finally:
        conn.close()


def _finish_budget():
    _resolve_budget_fallback(
        SimpleNamespace(max_iterations=160, iteration_budget=SimpleNamespace(remaining=0)),
        final_response="handoff complete", api_call_count=160, interrupted=False,
        failed=False, messages=[], _turn_exit_reason="unknown",
        _pending_verification_response=None, _pending_verification_response_previewed=False,
        logger=logging.getLogger(__name__),
    )


def _board_state(conn):
    return tuple(
        [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("tasks", "task_runs", "task_events")
    )


@pytest.mark.parametrize(
    "state",
    ["review", "reviewer_claimed", "wrong_claim", "run_closed", "not_running",
     "wrong_run_claim", "missing_run", "malformed_run", "zero_run",
     "missing_claim", "blank_claim", "missing_db"],
)
def test_late_or_unidentified_budget_finalizer_leaves_board_unchanged(worker, monkeypatch, state):
    conn, task_id, claim = worker
    if state in {"review", "reviewer_claimed"}:
        assert kb.request_review(
            conn, task_id, summary="Ready for QA", reviewer="qa",
            expected_run_id=claim.current_run_id,
        )
        if state == "reviewer_claimed":
            reviewer = kb.claim_review_task(conn, task_id, claimer="qa:new")
            assert reviewer is not None
            assert reviewer.current_run_id != claim.current_run_id
    elif state == "wrong_claim":
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "other-worker")
    elif state in {"run_closed", "not_running", "wrong_run_claim"}:
        with kb.write_txn(conn):
            if state == "run_closed":
                conn.execute("UPDATE task_runs SET ended_at = 1 WHERE id = ?", (claim.current_run_id,))
            elif state == "not_running":
                conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
            else:
                conn.execute("UPDATE task_runs SET claim_lock = 'other-worker' WHERE id = ?",
                             (claim.current_run_id,))
    else:
        key, value = {
            "missing_run": ("HERMES_KANBAN_RUN_ID", None),
            "malformed_run": ("HERMES_KANBAN_RUN_ID", "not-an-id"),
            "zero_run": ("HERMES_KANBAN_RUN_ID", "0"),
            "missing_claim": ("HERMES_KANBAN_CLAIM_LOCK", None),
            "blank_claim": ("HERMES_KANBAN_CLAIM_LOCK", "   "),
            "missing_db": ("HERMES_KANBAN_DB", None),
        }[state]
        if state == "missing_db":
            ambient_path = kb.kanban_db_path()
            monkeypatch.setattr(kb, "kanban_db_path", lambda **_kwargs: ambient_path)
        if value is None:
            monkeypatch.delenv(key)
        else:
            monkeypatch.setenv(key, value)
    before = _board_state(conn)
    _finish_budget()
    assert _board_state(conn) == before


@pytest.mark.parametrize("max_retries", [1, 2])
def test_current_budget_exhaustion_counts_once_and_preserves_failure_limit(worker, max_retries):
    conn, task_id, claim = worker
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET max_retries = ? WHERE id = ?", (max_retries, task_id))
    _finish_budget()
    task = kb.get_task(conn, task_id)
    assert task.consecutive_failures == 1
    assert task.status == ("blocked" if max_retries == 1 else "ready")
    assert task.current_run_id is None
    assert task.claim_lock is None
    run = kb.latest_run(conn, task_id)
    assert run.id == claim.current_run_id
    assert run.outcome == ("gave_up" if max_retries == 1 else "timed_out")
    assert run.ended_at is not None
    assert "160/160" in run.error
    events = kb.list_events(conn, task_id)
    assert sum(event.kind in {"timed_out", "gave_up"} for event in events) == 1
    after = _board_state(conn)
    _finish_budget()
    assert _board_state(conn) == after
