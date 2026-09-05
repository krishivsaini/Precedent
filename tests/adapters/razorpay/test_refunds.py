"""The refund client's error taxonomy.

Every branch below only executes when something has gone wrong with a live payment API,
which is the worst possible moment for it to be running for the first time. The responses
are the shapes a real test-mode account actually returned during the Ring 5 probe — in
particular, the 409 and the 400 both carry `"code": "BAD_REQUEST_ERROR"`, which is why the
SDK cannot tell them apart and this module exists.
"""

import httpx
import pytest

from precedent.adapters.razorpay.refunds import (
    RefundClient,
    RefundConflict,
    RefundRejected,
    RefundUnavailable,
)
from precedent.config import RazorpayConfig

CONFIG = RazorpayConfig(key_id="rzp_test_x", key_secret="secret", webhook_secret="whsec")

CREATED = {
    "id": "rfnd_TEST123", "entity": "refund", "amount": 10000, "currency": "INR",
    "payment_id": "pay_TEST", "status": "processed",
}
CONFLICT = {"error": {
    "code": "BAD_REQUEST_ERROR",
    "description": "Different request with the same idempotency key has already been processed.",
}}
SHORT_KEY = {"error": {
    "code": "BAD_REQUEST_ERROR",
    "description": "The idempotency key must be at least 10 characters long.",
    "reason": "input_validation_failed",
}}


def client_returning(status_code, json_body=None, text=None, handler=None):
    def respond(request: httpx.Request) -> httpx.Response:
        if handler:
            return handler(request)
        if text is not None:
            return httpx.Response(status_code, text=text)
        return httpx.Response(status_code, json=json_body)

    return RefundClient(CONFIG, http_client=httpx.Client(transport=httpx.MockTransport(respond)))


KEY = "rem-0123456789"


class TestTheRequestItSends:
    def test_the_idempotency_header_is_present_on_every_call(self):
        # Measured: without this header the API creates a *new refund every time*. It is the
        # only thing between a retry and a double refund, so its presence is asserted rather
        # than assumed.
        seen = {}

        def handler(request):
            seen["headers"] = dict(request.headers)
            seen["url"] = str(request.url)
            return httpx.Response(200, json=CREATED)

        client_returning(200, handler=handler).create_refund("pay_TEST", 10000, KEY)
        assert seen["headers"]["x-refund-idempotency"] == KEY
        assert seen["url"].endswith("/payments/pay_TEST/refund")

    def test_the_amount_goes_out_as_integer_paise(self):
        seen = {}

        def handler(request):
            import json
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=CREATED)

        client_returning(200, handler=handler).create_refund("pay_TEST", 10000, KEY)
        assert seen["body"]["amount"] == 10000
        assert isinstance(seen["body"]["amount"], int)


class TestItRefusesBadRequestsBeforeSending:
    def test_a_short_key_is_refused_locally(self):
        # The API answers this with a 400 carrying the same body code as a 409 — the exact
        # distinction this module preserves. Cheaper to never send it.
        with pytest.raises(ValueError, match="at least 10"):
            client_returning(200, CREATED).create_refund("pay_TEST", 10000, "short")

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_amount_is_refused(self, bad):
        with pytest.raises(ValueError):
            client_returning(200, CREATED).create_refund("pay_TEST", bad, KEY)

    def test_a_float_amount_is_refused(self):
        with pytest.raises(TypeError):
            client_returning(200, CREATED).create_refund("pay_TEST", 100.0, KEY)


class TestTheErrorTaxonomy:
    """Three exception types because the three cases need opposite responses: stop and
    reconcile, fix and retry, retry unchanged."""

    def test_409_raises_conflict_and_not_a_generic_bad_request(self):
        with pytest.raises(RefundConflict) as exc:
            client_returning(409, CONFLICT).create_refund("pay_TEST", 10000, KEY)
        assert "already used for a different request" in str(exc.value)

    def test_a_400_with_the_same_body_code_is_a_different_exception(self):
        # The whole reason for bypassing the SDK. Both responses say BAD_REQUEST_ERROR;
        # only the status line separates them, and the SDK discards the status line.
        with pytest.raises(RefundRejected):
            client_returning(400, SHORT_KEY).create_refund("pay_TEST", 10000, KEY)

    def test_a_conflict_is_not_a_rejection(self):
        # Asserted explicitly: if RefundConflict ever became a subclass of RefundRejected,
        # a caller's `except RefundRejected` would start swallowing conflicts, and a
        # conflict swallowed is a refund whose existence nobody checks.
        assert not issubclass(RefundConflict, RefundRejected)
        assert not issubclass(RefundRejected, RefundConflict)

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_a_5xx_is_unavailable_rather_than_rejected(self, status):
        with pytest.raises(RefundUnavailable) as exc:
            client_returning(status, {"error": {"description": "upstream"}}).create_refund(
                "pay_TEST", 10000, KEY
            )
        assert "retry under the same key" in str(exc.value)

    def test_a_transport_failure_is_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("no route to host")

        with pytest.raises(RefundUnavailable):
            client_returning(200, handler=handler).create_refund("pay_TEST", 10000, KEY)

    def test_a_non_json_error_body_does_not_mask_the_status_code(self):
        # A 502 from a load balancer is HTML. Calling .json() on it raises, and losing the
        # status code would turn the one useful fact into a traceback.
        with pytest.raises(RefundUnavailable) as exc:
            client_returning(502, text="<html>Bad Gateway</html>").create_refund(
                "pay_TEST", 10000, KEY
            )
        assert "502" in str(exc.value)


class TestTheSuccessPath:
    def test_it_returns_the_refund_id_and_amount(self):
        result = client_returning(200, CREATED).create_refund("pay_TEST", 10000, KEY)
        assert result.refund_id == "rfnd_TEST123"
        assert result.amount_paise == 10000
        assert result.status == "processed"
        assert result.http_status == 200

    def test_a_replay_is_indistinguishable_from_a_create_at_this_layer(self):
        # Measured: a byte-identical replay returns 200 with the *original* refund id. This
        # layer cannot tell the two apart and does not pretend to — knowing whether a call
        # was a replay is `usecases.remediate`'s job, from its own records.
        result = client_returning(200, CREATED).create_refund("pay_TEST", 10000, KEY)
        assert result.refund_id == CREATED["id"]
