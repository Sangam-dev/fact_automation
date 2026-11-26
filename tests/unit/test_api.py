from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from api import workers
from api.api import SALES_ORDER_AUTOMATION, create_app
from api.jobs import JobStore


API_KEY = "test-api-key"
PAYLOAD = {
    "header": {
        "proforma_no": "TEST",
        "customer_order_no": "2222",
        "currency": "NPR",
        "customer_code": "CE-CUS2542",
        "salesman_code": "1534",
    },
    "lines": [
        {"product_code": "BRM-003", "quantity": 1000, "rate": 100},
    ],
}


def make_client(tmp_path):
    store = JobStore(tmp_path / "runs.sqlite3")
    app = create_app(job_store=store, api_key=API_KEY)
    return TestClient(app), store


def auth_headers(idempotency_key: str = "event-001") -> dict[str, str]:
    return {
        "X-API-Key": API_KEY,
        "Idempotency-Key": idempotency_key,
    }


def test_health_reports_worker_presence(tmp_path):
    client, store = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "desktop_worker_online": False,
    }
    store.heartbeat("test-worker")
    assert client.get("/health").json()["desktop_worker_online"] is True


def test_sales_order_is_validated_and_queued(tmp_path):
    client, store = make_client(tmp_path)

    response = client.post(
        "/api/v1/sales-orders", json=PAYLOAD, headers=auth_headers()
    )

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["automation_id"] == SALES_ORDER_AUTOMATION
    assert accepted["status"] == "queued"
    record = store.get_run(accepted["run_id"])
    assert record is not None
    assert record.status == "queued"

    status_response = client.get(
        f"/api/v1/runs/{accepted['run_id']}",
        headers={"X-API-Key": API_KEY},
    )
    assert status_response.status_code == 200
    assert status_response.json()["run_id"] == accepted["run_id"]


def test_same_idempotency_key_returns_same_run(tmp_path):
    client, _ = make_client(tmp_path)
    headers = auth_headers("event-retry-1")

    first = client.post("/api/v1/sales-orders", json=PAYLOAD, headers=headers)
    second = client.post("/api/v1/sales-orders", json=PAYLOAD, headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.json()["run_id"] == second.json()["run_id"]


def test_reusing_idempotency_key_for_different_data_is_conflict(tmp_path):
    client, _ = make_client(tmp_path)
    changed = {
        **PAYLOAD,
        "lines": [{"product_code": "BRM-003", "quantity": 2000, "rate": 100}],
    }

    first = client.post(
        "/api/v1/sales-orders", json=PAYLOAD, headers=auth_headers("event-reuse")
    )
    second = client.post(
        "/api/v1/sales-orders", json=changed, headers=auth_headers("event-reuse")
    )

    assert first.status_code == 202
    assert second.status_code == 409


def test_api_key_is_required(tmp_path):
    client, _ = make_client(tmp_path)

    response = client.post(
        "/api/v1/sales-orders",
        json=PAYLOAD,
        headers={"Idempotency-Key": "event-no-auth"},
    )

    assert response.status_code == 401


def test_invalid_payload_is_rejected_before_queueing(tmp_path):
    client, store = make_client(tmp_path)
    invalid = {**PAYLOAD, "unexpected": "not allowed"}

    response = client.post(
        "/api/v1/sales-orders", json=invalid, headers=auth_headers("event-invalid")
    )

    assert response.status_code == 422
    assert not store.db_path.exists()


def test_worker_claim_and_completion_clear_queued_payload(tmp_path):
    store = JobStore(tmp_path / "runs.sqlite3")
    record, created = store.create_run(
        SALES_ORDER_AUTOMATION, PAYLOAD, "event-worker"
    )
    assert created

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.record.run_id == record.run_id
    assert claimed.record.status == "running"
    assert '"proforma_no":"TEST"' in claimed.payload_json

    store.complete_run(
        record.run_id,
        status="succeeded",
        exit_code=0,
        message="completed",
        log_path=None,
    )
    assert store.claim_next() is None
    completed = store.get_run(record.run_id)
    assert completed is not None
    assert completed.status == "succeeded"


def test_worker_launches_registered_cli_and_records_confirmed_save(
    tmp_path, monkeypatch
):
    store = JobStore(tmp_path / "runs.sqlite3")
    record, _ = store.create_run(SALES_ORDER_AUTOMATION, PAYLOAD, "event-execute")
    claimed = store.claim_next()
    assert claimed is not None
    commands = []

    class FinishedProcess:
        returncode = 0

        def poll(self):
            return self.returncode

    def fake_popen(command, **kwargs):
        commands.append(command)
        kwargs["stdout"].write("Outcome: saved (fact_automation_01)\\n")
        return FinishedProcess()

    monkeypatch.setattr(workers, "project_root", lambda: tmp_path)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers._execute_run(claimed, store, "test-worker")

    completed = store.get_run(record.run_id)
    assert completed is not None
    assert completed.status == "succeeded"
    assert commands[0][2:5] == ["automation", "run", SALES_ORDER_AUTOMATION]
    assert "--from-screen" in commands[0]
    values_path = commands[0][commands[0].index("--values") + 1]
    assert not Path(values_path).exists()


def test_worker_marks_unconfirmed_save_for_review(tmp_path):
    log_path = tmp_path / "runner.log"
    log_path.write_text(
        "Outcome: unconfirmed (fact_automation_01)\\n", encoding="utf-8"
    )

    run_status, _ = workers._result_status(1, log_path)

    assert run_status == "needs_review"


def test_interrupted_job_is_marked_for_review_not_requeued(tmp_path):
    store = JobStore(tmp_path / "runs.sqlite3")
    record, _ = store.create_run(SALES_ORDER_AUTOMATION, PAYLOAD, "event-crash")
    assert store.claim_next() is not None

    assert store.mark_interrupted_runs_for_review() == 1
    recovered = store.get_run(record.run_id)
    assert recovered is not None
    assert recovered.status == "needs_review"
    assert store.claim_next() is None
