"""One explicit proposal confirmation, one scoped ticket, one durable entry."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_sopcontrol_adapter import _install_registry
from tools.workflow.confirmation import ConfirmationStore
from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import MemoryFreshStore, rows_digest
from tools.workflow.testing_packages import build_workspace


@pytest.fixture
def entry_env(tmp_path, monkeypatch):
    root = tmp_path / "product"
    root.mkdir()
    _install_registry(root)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(root))
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    return build_workspace(tmp_path), root


def preview_entry(ws, action, store, count=1):
    if action == "push":
        scan = dispatch("scan", workspace=ws, payload={"fixture": {"run_id": "scan-entry-one-confirm", "jobs": [
            {"title": "Analyst", "company": "Acme", "url": f"https://example.test/jobs/{i}", "score": "4.1", "lane": "C"}
            for i in range(count)]}})
        assert scan["status"] == "succeeded", scan
        return dispatch("push", workspace=ws, store=store, payload={"run_id": scan["run_id"], "fresh_title": store.title})
    return dispatch("intake", workspace=ws, store=store, payload={"fresh_title": store.title, "items": [
        {"url": f"https://www.linkedin.com/jobs/view/{1000+i}", "title": "Analyst", "employer": "Acme", "platform": "linkedin", "lane": "C"}
        for i in range(count)]})


@pytest.mark.parametrize("action", ["push", "intake"])
@pytest.mark.parametrize("count", [1, 3])
def test_one_confirmation_writes_and_reads_back_without_ticket_prompt(entry_env, action, count):
    ws, root = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, action, store, count)
    assert preview["status"] == "planned", preview
    assert store.row_count() == 0
    out = dispatch(action, workspace=ws, store=store, payload={"confirmation_id": preview["proposal_id"]})
    assert out["status"] == "succeeded", out
    assert not out.get("requires_capability_ticket")
    assert store.row_count() == count
    authorization = out["entry_authorization"]
    assert authorization["confirmation_event_id"]
    assert authorization["authority"] == "gateway_proposal_callback"
    assert authorization["human_presence"] == "unproven"
    ticket_id = authorization["ticket_id"]
    from sopcontrol.tickets import _load_ticket
    assert _load_ticket(root, ticket_id).consumed_at is not None
    public = json.dumps(out)
    assert _load_ticket(root, ticket_id).secret not in public
    ledger = (ws / "02_Tracker/workflow/events.jsonl").read_text()
    assert _load_ticket(root, ticket_id).secret not in ledger
    assert authorization["confirmation_event_id"] in ledger


@pytest.mark.parametrize("action", ["push", "intake"])
def test_model_boolean_cannot_replace_proposal_confirmation(entry_env, action):
    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview_entry(ws, action, store)
    out = dispatch(action, workspace=ws, store=store, payload={"user_confirmed": True, "confirmed": True})
    assert out["status"] != "succeeded"
    assert store.row_count() == 0


@pytest.mark.parametrize("action", ["push", "intake"])
def test_mutated_preview_cannot_be_authorized_even_with_recomputed_row_digest(entry_env, action):
    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, action, store)
    proposals = ConfirmationStore(ws)
    proposal = proposals.load(preview["proposal_id"])
    field = "prepared_rows" if action == "push" else "candidate_rows"
    proposal[field][0]["公司"] = "Changed after preview"
    proposal["incoming_digest" if action == "push" else "candidate_digest"] = rows_digest(proposal[field])
    proposals.save(proposal)
    out = dispatch(action, workspace=ws, store=store, payload={"confirmation_id": proposal["proposal_id"]})
    assert out["status"] == "blocked", out
    assert "entry_preview_changed" in out["blockers"]
    assert store.row_count() == 0


@pytest.mark.parametrize("action", ["push", "intake"])
def test_repeat_confirmation_reads_existing_result_without_another_id(entry_env, action):
    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, action, store)
    payload = {"confirmation_id": preview["proposal_id"]}
    first = dispatch(action, workspace=ws, store=store, payload=payload)
    assert first["status"] == "succeeded", first
    counters = (ws / "02_Tracker/workflow/id_counters.json").read_bytes()
    again = dispatch(action, workspace=ws, store=store, payload=payload)
    assert again["status"] == "succeeded", again
    assert again["idempotent"]
    assert store.row_count() == 1
    assert (ws / "02_Tracker/workflow/id_counters.json").read_bytes() == counters
    assert first["entry_authorization"]["confirmation_event_id"] == again["entry_authorization"]["confirmation_event_id"]


@pytest.mark.parametrize("action", ["push", "intake"])
def test_expired_ticket_has_specific_error_and_does_not_write(entry_env, action):
    from sopcontrol.tickets import _load_ticket, _save_ticket

    ws, root = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, action, store)
    from tools.workflow.entry_authorization import preview_receipt_path
    receipt = json.loads(preview_receipt_path(ws, preview["proposal_id"]).read_text())
    ticket = _load_ticket(root, receipt["ticket_id"])
    ticket.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    _save_ticket(root, ticket)
    out = dispatch(action, workspace=ws, store=store, payload={"confirmation_id": preview["proposal_id"]})
    assert "capability_ticket_expired" in out["blockers"], out
    assert "capability_ticket_required" not in out["blockers"]
    assert store.row_count() == 0


class TimeoutStore(MemoryFreshStore):
    fail_once = False
    wrote_first = False

    def replace_active_if_digest(self, snapshot, expected_digest):
        if self.fail_once:
            self.fail_once = False
            if self.wrote_first:
                super().replace_active_if_digest(snapshot, expected_digest)
            raise TimeoutError("synthetic network timeout")
        return super().replace_active_if_digest(snapshot, expected_digest)


@pytest.mark.parametrize("action", ["push", "intake"])
@pytest.mark.parametrize("wrote_first", [False, True])
def test_network_retry_resumes_the_same_entry_and_number(entry_env, action, wrote_first):
    ws, _ = entry_env
    store = TimeoutStore("fresh_entry", [])
    store.wrote_first = wrote_first
    preview = preview_entry(ws, action, store)
    store.fail_once = True
    payload = {"confirmation_id": preview["proposal_id"]}
    failed = dispatch(action, workspace=ws, store=store, payload=payload)
    assert failed["status"] == "failed", failed
    counters = (ws / "02_Tracker/workflow/id_counters.json").read_bytes()
    retried = dispatch(action, workspace=ws, store=store, payload=payload)
    assert retried["status"] == "succeeded", retried
    assert not retried.get("requires_capability_ticket")
    assert store.row_count() == 1
    assert (ws / "02_Tracker/workflow/id_counters.json").read_bytes() == counters


@pytest.mark.parametrize("action", ["push", "intake"])
def test_concurrent_confirmations_serialize_and_consume_only_once(entry_env, action):
    from concurrent.futures import ThreadPoolExecutor

    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, action, store)
    payload = {"confirmation_id": preview["proposal_id"]}
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: dispatch(action, workspace=ws, store=store, payload=payload), range(2)))
    assert all(out["status"] == "succeeded" for out in results), results
    assert store.row_count() == 1
    assert len({out["entry_authorization"]["confirmation_event_id"] for out in results}) == 1


@pytest.mark.parametrize("action", ["push", "intake"])
def test_new_duplicate_or_wrong_target_is_blocked_before_authorization(entry_env, action):
    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, action, store)
    proposal = ConfirmationStore(ws).load(preview["proposal_id"])
    rows = proposal["prepared_rows" if action == "push" else "candidate_rows"]
    store.rows = [{**rows[0], "岗位编号": "C0-888"}]
    out = dispatch(action, workspace=ws, store=store, payload={"confirmation_id": preview["proposal_id"]})
    assert out["status"] == "blocked", out
    assert not out.get("entry_authorization")
    assert not (ws / "02_Tracker/workflow/id_counters.json").is_file()


def test_expiry_and_missing_credentials_have_distinct_user_messages():
    from tools.workflow.interaction_shell import wrap_result

    expired = wrap_result({"status": "blocked", "blockers": ["capability_ticket_expired"]}, action="push")
    missing = wrap_result({"status": "blocked", "blockers": ["gsheet_credentials_missing"]}, action="push")
    assert "授权已过期" in expired["message"]
    assert "凭据缺失或不可用" in missing["message"]
    assert "retry" not in expired and "retry" not in missing


@pytest.mark.parametrize("action", ["push", "intake"])
def test_changed_actual_sheet_is_not_authorized_by_same_title(entry_env, action):
    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    store.sheet_id = "synthetic-sheet-A"
    preview = preview_entry(ws, action, store)
    store.sheet_id = "synthetic-sheet-B"
    out = dispatch(action, workspace=ws, store=store, payload={"confirmation_id": preview["proposal_id"]})
    assert out["status"] == "blocked", out
    assert "confirmation_target_mismatch" in out["blockers"]
    assert store.row_count() == 0


@pytest.mark.parametrize("action", ["push", "intake"])
def test_direct_engine_uses_the_same_formal_confirmation_contract(entry_env, action):
    from tools.workflow.engine import WorkflowEngine
    from tools.workflow.contracts import ActionRequest

    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, action, store)
    out = WorkflowEngine().execute(ActionRequest(action=action, autonomy_level="A3", confirmation_id=preview["proposal_id"]),
                                   workspace=ws, store=store)
    assert out["status"] == "succeeded", out
    assert store.row_count() == 1


def test_conflicting_confirmation_ids_are_not_silently_selected(entry_env):
    ws, _ = entry_env
    store = MemoryFreshStore("fresh_entry", [])
    preview = preview_entry(ws, "intake", store)
    out = dispatch("intake", workspace=ws, store=store, confirmation_id="different-id",
                   payload={"confirmation_id": preview["proposal_id"]})
    assert "confirmation_binding_mismatch" in out["blockers"]
    assert store.row_count() == 0
