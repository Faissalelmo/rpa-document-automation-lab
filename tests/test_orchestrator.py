"""The queue contract the robot relies on - the same behaviour UiPath Orchestrator guarantees."""
import pytest
from fastapi.testclient import TestClient

from rpa_lab import orchestrator

Q = "FACTURES_FOURNISSEURS"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "QUEUE_DB", tmp_path / "q.db")
    return TestClient(orchestrator.create_app())


def add(c, ref, priority="Normal", **content):
    return c.post("/odata/Queues/UiPathODataSvc.AddQueueItem",
                  json={"itemData": {"Name": Q, "Reference": ref, "Priority": priority, "SpecificContent": content}})


def start(c, robot="R1"):
    return c.post("/odata/Queues/UiPathODataSvc.StartTransaction",
                  json={"transactionData": {"Name": Q, "RobotIdentifier": robot}})


def fail(c, item_id, etype, reason="X"):
    return c.post(f"/odata/Queues({item_id})/UiPathODataSvc.SetTransactionResult", json={"transactionResult": {
        "IsSuccessful": False, "ProcessingException": {"Type": etype, "Reason": reason, "Details": "d"}}})


def test_unique_reference_rejects_duplicates(client):
    assert add(client, "abc").status_code == 201
    assert add(client, "abc").status_code == 409


def test_priority_then_fifo(client):
    add(client, "low", "Low")
    add(client, "n1")
    add(client, "high", "High")
    add(client, "n2")
    order = [start(client).json()["Reference"] for _ in range(4)]
    assert order == ["high", "n1", "n2", "low"]
    assert start(client).status_code == 204  # queue empty


def test_item_is_handed_to_one_robot_only(client):
    add(client, "only")
    assert start(client, "R1").status_code == 200
    assert start(client, "R2").status_code == 204


def test_business_exception_is_never_retried(client):
    add(client, "b")
    item = start(client).json()
    assert fail(client, item["Id"], "BusinessException", "BE_UNKNOWN_SUPPLIER").status_code == 204
    assert start(client).status_code == 204
    stats = client.get(f"/api/queues/{Q}/stats").json()
    assert stats["by_status"] == {"Failed": 1}
    assert stats["business_exceptions_by_reason"] == {"BE_UNKNOWN_SUPPLIER": 1}


def test_application_exception_is_retried_up_to_the_limit(client):
    add(client, "s")
    max_retry = orchestrator.load_config()["QueueMaxRetryNumber"]
    for attempt in range(max_retry + 1):
        item = start(client).json()
        assert item["RetryNumber"] == attempt
        assert item["Reference"] == "s"  # the clone keeps the reference (= the ERP idempotency key)
        fail(client, item["Id"], "ApplicationException")
    assert start(client).status_code == 204
    statuses = sorted(i["Status"] for i in client.get(f"/api/queues/{Q}/items").json())
    assert statuses == ["Failed"] + ["Retried"] * max_retry


def test_manual_retry_only_for_failed_items(client):
    add(client, "m")
    item = start(client).json()
    assert client.post(f"/api/queues/{Q}/items/{item['Id']}/retry").status_code == 400  # still InProgress
    client.post(f"/odata/Queues({item['Id']})/UiPathODataSvc.SetTransactionResult",
                json={"transactionResult": {"IsSuccessful": True}})
    assert client.post(f"/api/queues/{Q}/items/{item['Id']}/retry").status_code == 400  # Successful


def test_crashed_robot_item_becomes_abandoned(client, monkeypatch):
    add(client, "a")
    start(client)
    monkeypatch.setattr(orchestrator, "load_config", lambda: {"QueueMaxRetryNumber": 2, "InProgressTimeoutSeconds": -1})
    client2 = TestClient(orchestrator.create_app())  # same DB, timeout already exceeded
    assert start(client2).status_code == 204
    assert client2.get(f"/api/queues/{Q}/stats").json()["by_status"] == {"Abandoned": 1}
