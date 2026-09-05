"""Refund creation over raw HTTP, deliberately *not* through the `razorpay` SDK.

Ring 0 left refund creation unimplemented with a note that the SDK's idempotency-header
wiring had to be verified against a live account before being relied on. It was, and the
verification changed the design.

**What the live test-mode probe found** (five calls against a real captured payment):

| request | result |
| --- | --- |
| first call with `X-Refund-Idempotency` | `200`, new `rfnd_` id |
| same key, byte-identical body | `200`, **the same `rfnd_` id** — a true replay, no second refund |
| same key, different amount | `409 Conflict` |
| no header at all | `200`, **a second refund every time** |
| key shorter than 10 characters | `400`, `input_validation_failed` |

Two things follow. First, the header is the only thing standing between a retry and a
double refund — it is not optional. Second, and the reason this module exists:

**The SDK cannot express the 409 that spec §4 requires be handled.** `razorpay.Client.request`
reads `response.status_code` only to decide 2xx-or-not, then picks the exception class from
the *body's* `error.code`. Both the 409 and the 400 above carry `"code": "BAD_REQUEST_ERROR"`,
so both arrive as `BadRequestError` with nothing to tell them apart. "You reused a key for a
different request" and "your key is malformed" need opposite responses — the first means stop
and reconcile, the second means fix and retry — and through the SDK they are the same object.

So refund creation talks to `POST /v1/payments/{id}/refund` directly, where the status line
is visible. Order creation and payment fetch stay on the SDK in `client.py`: they have no
idempotency semantics to lose.
"""

from dataclasses import dataclass

import httpx

from precedent.config import RazorpayConfig
from precedent.domain.remediation import MIN_IDEMPOTENCY_KEY_LENGTH

API_BASE = "https://api.razorpay.com/v1"


class RefundConflict(RuntimeError):
    """HTTP 409 — this idempotency key was already used for a *different* request.

    Never retry this. The key is derived from the intent, so a 409 means two different
    intents collided on one key, or an earlier attempt sent something else under it. Either
    way a refund may already exist and the correct next step is to look, not to send again.
    """


class RefundRejected(RuntimeError):
    """A 4xx that is not 409 — the request is wrong and will stay wrong.

    Retrying is pointless and, if the fault is a malformed amount, harmful.
    """


class RefundUnavailable(RuntimeError):
    """5xx, timeout, or transport failure — the outcome is genuinely unknown.

    Distinct from `RefundRejected` because the response is different: this one may be
    retried, and *must* be retried under the same idempotency key, which is the only reason
    retrying it is safe at all.
    """


@dataclass(frozen=True)
class RefundResult:
    refund_id: str
    payment_id: str
    amount_paise: int
    status: str
    http_status: int


class RefundClient:
    """`http_client` is injectable so the failure paths above can be tested without a
    network — every one of them is a branch that only ever runs on a bad day, which is
    precisely when it must not be the first time the code has executed."""

    def __init__(
        self,
        config: RazorpayConfig,
        http_client: httpx.Client | None = None,
        base_url: str = API_BASE,
        timeout: float = 60.0,
    ):
        self._auth = (config.key_id, config.key_secret)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http = http_client

    def create_refund(
        self,
        payment_id: str,
        amount_paise: int,
        idempotency_key: str,
        notes: dict | None = None,
    ) -> RefundResult:
        if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
            raise TypeError(f"amount_paise must be an int, got {amount_paise!r}")
        if amount_paise <= 0:
            raise ValueError(f"amount_paise must be positive, got {amount_paise}")
        if len(idempotency_key) < MIN_IDEMPOTENCY_KEY_LENGTH:
            # Refused here rather than at the API, because the API's answer for this is a
            # 400 carrying the same body code as a 409 — a distinction this module exists
            # to preserve, and which is cheaper to keep by never sending the bad request.
            raise ValueError(
                f"idempotency key must be at least {MIN_IDEMPOTENCY_KEY_LENGTH} characters "
                f"(Razorpay returns 400 below that); got {len(idempotency_key)}"
            )

        url = f"{self._base_url}/payments/{payment_id}/refund"
        headers = {
            "Content-Type": "application/json",
            "X-Refund-Idempotency": idempotency_key,
        }
        payload = {"amount": amount_paise, "speed": "normal", "notes": notes or {}}

        try:
            response = self._request(url, headers, payload)
        except httpx.HTTPError as exc:
            raise RefundUnavailable(f"refund request to {payment_id} failed: {exc}") from exc

        if response.status_code == 409:
            raise RefundConflict(
                f"idempotency key {idempotency_key!r} was already used for a different "
                f"request on {payment_id}: {_describe(response)}"
            )
        if response.status_code >= 500:
            raise RefundUnavailable(
                f"Razorpay returned {response.status_code} for {payment_id}: "
                f"{_describe(response)} — outcome unknown, retry under the same key"
            )
        if response.status_code >= 400:
            raise RefundRejected(
                f"Razorpay refused the refund on {payment_id} with "
                f"{response.status_code}: {_describe(response)}"
            )

        body = response.json()
        return RefundResult(
            refund_id=body["id"],
            payment_id=body.get("payment_id", payment_id),
            amount_paise=int(body["amount"]),
            status=body.get("status", "unknown"),
            http_status=response.status_code,
        )

    def _request(self, url: str, headers: dict, payload: dict) -> httpx.Response:
        if self._http is not None:
            return self._http.post(url, auth=self._auth, headers=headers, json=payload)
        with httpx.Client(timeout=self._timeout) as client:
            return client.post(url, auth=self._auth, headers=headers, json=payload)


def _describe(response: httpx.Response) -> str:
    """The API's own words, or the raw text if it did not send JSON.

    A 502 from a load balancer is HTML, and `response.json()` on it raises — losing the
    status code that was the only useful part of the answer.
    """
    try:
        error = response.json().get("error", {})
    except ValueError:
        return response.text[:200]
    return error.get("description") or str(error)[:200]
