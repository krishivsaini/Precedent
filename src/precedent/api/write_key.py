"""A shared key on the actions that change something. Deliberately not authentication.

`product_design.md` §6 scopes auth out, and this does not put it back. There are no accounts,
no sessions, no identity — everyone who holds the key is the same anonymous operator, exactly
as before. What this stops is narrower and real: the deployed demo is a public URL where a
single POST spends model credits, writes into a corpus that is later retrieved from, and can
reach a refund gate. A crawler following forms, or one visitor working through the queue,
empties it for everyone after them.

**Reads are never gated.** Anyone can browse the queue, a case, the corpus, the measurement.
That is the whole argument and it should be open. Only state changes need the key.

**The webhook is exempt**, because it authenticates itself: `webhooks.py` verifies an HMAC
signature over the raw body, which is a stronger check than this one and the only one Razorpay
can satisfy — it cannot carry a cookie.

**Unset means off.** With no `PRECEDENT_WRITE_KEY` in the environment this module does nothing
at all, so a clone runs exactly as it did and the test suite never sees it. That is the right
default for something whose absence should never be a silent lock-out.
"""

import os
import secrets

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

#: Razorpay signs its deliveries; it cannot hold a cookie. Every other write is gated.
EXEMPT_PATHS = frozenset({"/webhooks/razorpay"})

COOKIE = "precedent_write_key"


def configured_key() -> str:
    return os.environ.get("PRECEDENT_WRITE_KEY", "").strip()


def _holds_the_key(request: Request, expected: str) -> bool:
    # compare_digest rather than ==, so a wrong key cannot be recovered a character at a
    # time from response timing. Cheap, and the alternative is indefensible.
    presented = request.cookies.get(COOKIE, "")
    return bool(presented) and secrets.compare_digest(presented, expected)


def install(app) -> None:
    """Gate every state-changing request, and add the one route that hands out the key."""

    @app.middleware("http")
    async def require_write_key(request: Request, call_next):
        expected = configured_key()
        if (
            not expected
            or request.method not in {"POST", "PUT", "PATCH", "DELETE"}
            or request.url.path in EXEMPT_PATHS
            or _holds_the_key(request, expected)
        ):
            return await call_next(request)

        # Middleware rather than a dependency on each route, so a write added later is
        # covered by default. Forgetting to opt in is the failure this cannot afford.
        if "text/html" not in request.headers.get("accept", ""):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "this deployment requires a write key; open /unlock?key=… "
                              "in a browser first, or send the precedent_write_key cookie"
                },
            )
        return HTMLResponse(status_code=403, content=(
            "<!doctype html><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Locked — Precedent</title>"
            "<style>body{font:16px/1.6 Iowan Old Style,Palatino,Georgia,serif;"
            "background:#F2F4F0;color:#1B2019;margin:0;padding:3rem 1.5rem;}"
            "div{max-width:34rem;margin:0 auto;}h1{font-size:1.6rem;margin:0 0 .5rem;}"
            "p{color:#67705F;}a{color:#1B2019;}</style>"
            "<div><h1>This copy is read-only</h1>"
            "<p>Browsing is open — the queue, any case, the corpus and the measurement are "
            "all readable. Recording a decision is not, because on a public URL one visitor "
            "working through the queue empties it for everyone after them, and every "
            "confirmation writes into a corpus that later cases are resolved from.</p>"
            "<p>If you were given a key, open "
            "<code>/unlock?key=…</code> once and this browser will remember it.</p>"
            "<p><a href='/'>Back to the queue</a></p></div>"
        ))

    @app.get("/unlock", include_in_schema=False)
    def unlock(request: Request, key: str = ""):
        """Exchange a key in the URL for a cookie, so it is sent once rather than typed.

        The redirect matters: leaving the key sitting in the address bar is how it ends up
        in a screenshot, a shared link, or a referrer header.
        """
        expected = configured_key()
        if not expected:
            return RedirectResponse("/", status_code=303)
        if not key or not secrets.compare_digest(key, expected):
            return HTMLResponse(status_code=403, content=(
                "<!doctype html><meta charset='utf-8'><title>Wrong key — Precedent</title>"
                "<style>body{font:16px/1.6 Georgia,serif;background:#F2F4F0;color:#1B2019;"
                "padding:3rem 1.5rem;}</style>"
                "<h1>That key was not right</h1><p><a href='/'>Back to the queue</a></p>"
            ))
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            COOKIE, expected,
            httponly=True,                       # never readable from script
            samesite="lax",                      # not sent on cross-site form posts
            secure=request.url.scheme == "https",
            max_age=60 * 60 * 24 * 7,
        )
        return response
