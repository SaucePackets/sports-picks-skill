"""Real writer acceptance at the research-to-producer boundary; no live agent."""

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mlb_research_producer as producer
import mlb_slate_writer as writer
from test_mlb_slate_writer import scan_row, DAY, read_for
import vig_policy_state

NOW = datetime.fromisoformat(DAY + "T20:00:00+00:00")


def seed(root):
    directory = root / ".picks/research" / DAY
    directory.mkdir(parents=True)
    state = {
        "schema": "mlb-research-queue-v1",
        "day": DAY,
        "games": {
            "1": {
                "status": "ready_for_producer",
                "deadline": (NOW + timedelta(hours=2)).isoformat(),
            }
        },
    }
    (directory / "queue.json").write_text(json.dumps(state))
    return directory


def draft(root, day, directory, nonce, timeout):
    path = root / ".picks/tmp" / f"stage2-{DAY}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps([scan_row(1)]).encode()
    path.write_bytes(raw)
    writer.scan_receipt_path(DAY, root).write_text(
        json.dumps(
            {
                "schema": "mlb-stage2-run-v1",
                "date": DAY,
                "run_nonce": nonce,
                "scan_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    )
    value = writer.skeleton(root, DAY, run_nonce=nonce)
    value["game_reads"] = [
        read_for(
            scan_row(1),
            raw_probability={"away": 0.398, "home": 0.602},
            conservative_probability={"away": 0.398, "home": 0.602},
            uncertainty_haircut=0,
            polymarket_ask={"away": 0.46, "home": 0.62},
            net_edge={"away": -0.062, "home": -0.018},
        )
    ]
    return value


def test_producer_draft_lands_via_real_writer_then_acknowledges(tmp_path):
    directory = seed(tmp_path)
    with vig_policy_state.deployed_policy(tmp_path / "policy"):
        result = producer.dispatch_ready(tmp_path, DAY, now=NOW, producer=draft)
    assert result["status"] == "landed"
    state = json.loads((directory / "queue.json").read_text())
    assert state["games"]["1"]["status"] == "decision_recorded"
    assert state["games"]["1"]["disposition"] == "pass"
    schedule = json.loads(writer.schedule_path_for(tmp_path, DAY).read_text())
    assert schedule["candidates"] == []
    assert (
        producer.dispatch_ready(
            tmp_path, DAY, now=NOW, producer=lambda *a: pytest.fail("duplicate")
        )["status"]
        == "no_due_producer"
    )


@pytest.mark.parametrize("defect", ["wrong_nonce", "unfilled", "self_approved"])
def test_invalid_producer_output_never_lands(tmp_path, defect):
    directory = seed(tmp_path)

    def bad(root, day, directory, nonce, timeout):
        value = draft(root, day, directory, nonce, timeout)
        if defect == "wrong_nonce":
            writer.scan_receipt_path(day, root).write_text("{}")
        elif defect == "unfilled":
            value["game_reads"] = []
        else:
            value["candidates"] = [{"vig_approved": True}]
        return value

    with vig_policy_state.deployed_policy(tmp_path / "policy"):
        expected = {"wrong_nonce":"invalid scan run receipt", "unfilled":"no game_reads entry", "self_approved":"already carries vig_approved"}[defect]
        with pytest.raises(writer.SlateWriteError, match=expected):
            producer.dispatch_ready(tmp_path, DAY, now=NOW, producer=bad)
    assert not writer.schedule_path_for(tmp_path, DAY).exists()
    state = json.loads((directory / "queue.json").read_text())
    assert state["games"]["1"]["producer_attempts"][0]["status"] == "failed"


def test_producer_failures_are_bounded_and_retained(tmp_path):
    directory = seed(tmp_path)

    def fail(*args):
        raise RuntimeError("fixture timeout")

    for i in range(2):
        with pytest.raises(RuntimeError):
            producer.dispatch_ready(tmp_path, DAY, now=NOW, producer=fail)
    assert (
        producer.dispatch_ready(tmp_path, DAY, now=NOW, producer=fail)["status"]
        == "no_due_producer"
    )
    state = json.loads((directory / "queue.json").read_text())
    assert state["games"]["1"]["status"] == "exhausted"
    assert len(state["games"]["1"]["producer_attempts"]) == 2


def test_expired_handoff_does_not_launch_agent(tmp_path):
    seed(tmp_path)
    result = producer.dispatch_ready(
        tmp_path,
        DAY,
        now=NOW + timedelta(hours=3),
        producer=lambda *a: pytest.fail("late"),
    )
    assert result["status"] == "no_due_producer"
