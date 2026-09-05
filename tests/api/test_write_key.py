"""The shared key on state-changing requests.

Not authentication, and the tests are written to hold it to that narrower claim: reads stay
open, the webhook stays reachable, and with nothing configured the whole thing disappears.
"""

import pytest
from fastapi.testclient import TestClient

from precedent.adapters.storage.db import connect, init_db
from precedent.adapters.storage.records import ExceptionRecord, ResolutionRecord
from precedent.adapters.storage.repositories import (
    ExceptionsRepository,
    ResolutionsRepository,
)
from precedent.api.main import create_app
from precedent.api.write_key import COOKIE

KEY = "a-shared-demo-key"
NOW = "2026-09-05T09:00:00+00:00"


def build(tmp_path, monkeypatch, key=KEY):
    db = str(tmp_path / "w.db")
    conn = connect(db)
    init_db(conn)
    ExceptionsRepository(conn).insert(ExceptionRecord(
        exception_id="exc_1", batch_id="b1", kind="tds_short_payment",
        member_refs=[], detected_at=NOW, status="open", correlation_id="corr_1",
    ))
    ResolutionsRepository(conn).insert(ResolutionRecord(
        resolution_id="res_1", exception_id="exc_1", proposed_by="agent",
        confidence=0.94, rationale="short by ten percent", cited_precedents=[],
        verified=True,
    ))
    conn.commit()
    conn.close()
    if key is None:
        monkeypatch.delenv("PRECEDENT_WRITE_KEY", raising=False)
    else:
        monkeypatch.setenv("PRECEDENT_WRITE_KEY", key)
    return TestClient(create_app(db_path=db))


@pytest.fixture
def locked(tmp_path, monkeypatch):
    with build(tmp_path, monkeypatch) as c:
        yield c


class TestReadingIsNeverGated:
    """The argument is the product. It should be open."""

    @pytest.mark.parametrize("path", ["/", "/corpus", "/refunds", "/deliveries",
                                      "/learns", "/result", "/healthz", "/approvals"])
    def test_every_read_screen_is_reachable_without_the_key(self, locked, path):
        assert locked.get(path).status_code == 200


class TestWritingNeedsTheKey:
    def test_a_decision_without_it_is_refused(self, locked):
        response = locked.post("/exceptions/res_1/decide",
                               data={"human_action": "confirmed"})
        assert response.status_code == 403

    def test_the_refusal_explains_itself_rather_than_just_denying(self, locked):
        body = locked.post("/exceptions/res_1/decide",
                           data={"human_action": "confirmed"},
                           headers={"accept": "text/html"}).text
        assert "read-only" in body
        assert "Browsing is open" in body

    def test_a_json_client_gets_json_back(self, locked):
        response = locked.post("/approvals/res_1", json={"human_action": "confirmed"})
        assert response.status_code == 403
        assert "write key" in response.json()["detail"]

    def test_nothing_was_recorded(self, locked):
        locked.post("/exceptions/res_1/decide", data={"human_action": "confirmed"})
        assert "human_action" in locked.get("/exceptions/res_1").text  # gate still offered


class TestUnlocking:
    def test_the_right_key_lets_a_write_through(self, locked):
        assert locked.get(f"/unlock?key={KEY}", follow_redirects=False).status_code == 303
        # The cookie now rides on every request from this client.
        assert locked.cookies.get(COOKIE) == KEY
        response = locked.post("/exceptions/res_1/decide",
                               data={"human_action": "rejected"},
                               follow_redirects=False)
        assert response.status_code == 303

    def test_a_wrong_key_is_refused_and_sets_nothing(self, locked):
        response = locked.get("/unlock?key=not-it")
        assert response.status_code == 403
        assert locked.cookies.get(COOKIE) is None

    def test_the_key_does_not_stay_in_the_address_bar(self, locked):
        # A key sitting in the URL ends up in a screenshot or a referrer header.
        response = locked.get(f"/unlock?key={KEY}", follow_redirects=False)
        assert response.headers["location"] == "/"

    def test_the_cookie_is_not_readable_from_script(self, locked):
        header = locked.get(f"/unlock?key={KEY}", follow_redirects=False) \
            .headers["set-cookie"].lower()
        assert "httponly" in header
        assert "samesite=lax" in header


class TestTheWebhookIsExempt:
    def test_razorpay_can_still_deliver(self, locked):
        # It authenticates by HMAC over the raw body, which is a stronger check than this
        # one and the only one Razorpay can satisfy — it cannot carry a cookie.
        response = locked.post(
            "/webhooks/razorpay",
            headers={"X-Razorpay-Event-Id": "evt_1", "X-Razorpay-Signature": "wrong"},
            json={"event": "payment.captured"},
        )
        assert response.status_code == 200


class TestUnsetMeansOff:
    def test_a_clone_with_no_key_configured_writes_freely(self, tmp_path, monkeypatch):
        # The absence of a key must never be a silent lock-out.
        with build(tmp_path, monkeypatch, key=None) as client:
            assert client.post("/exceptions/res_1/decide",
                               data={"human_action": "rejected"},
                               follow_redirects=False).status_code == 303

    def test_an_empty_key_is_treated_as_unset(self, tmp_path, monkeypatch):
        with build(tmp_path, monkeypatch, key="   ") as client:
            assert client.post("/exceptions/res_1/decide",
                               data={"human_action": "rejected"},
                               follow_redirects=False).status_code == 303
