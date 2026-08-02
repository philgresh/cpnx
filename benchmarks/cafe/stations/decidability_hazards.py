"""⚠️ Decidability hazards — a gallery of DELIBERATE anti-patterns.

**Do not copy these inscriptions.** Every callable in this module is intentionally
wrong: it smuggles network I/O, a database read, or a clock/randomness read into a
transition's *enabling* logic (a guard, an arc `key`/`filter`, or a
`binding_priority_key`). Each one looks like a reasonable feature a shift lead
might ask for — "serve VIPs first", "don't grind if we're out of beans", "spot-check
one in ten drinks", "prioritise by time of day" — and each one quietly forfeits the
net's analysability, because enabling now depends on non-deterministic or external
state rather than on token colour alone.

Why this station exists
-----------------------
The base cafe is deterministic: its guards/keys/filters read only token payloads and
close over immutable config, so behaviour is reproducible and the best-effort linter
(:mod:`cpnx.linting`) is silent on it (see ``tests/test_cafe_lint.py``). This station
is the negative control: enabling it makes the linter *speak*, once per hazard,
naming exactly the trouble spot. It is the running, in-context counterpart to the
unit fixtures in ``tests/test_linting.py``.

Unlike the other opt-in stations — which add self-contained side rails — these hazards
deliberately tap **existing deep places** (``P_Ticket_Line``, ``P_Ground_Coffee``,
``P_Order_Tray``), so the anti-pattern sits *inside* the real processing flow rather
than off to the side. They remain default-off, so a bare ``build_cafe()`` is unchanged.

Grounding
---------
High-level Petri nets (ISO/IEC 15909-1:2019) enable transitions on data-dependent
inscriptions over token colours — a decidable core. Reaching outside that core for a
clock, a random draw, or a remote/DB read is the departure this module makes legible.
Sound *static* detection of such effects is impossible in Python (Eghbali, Burk &
Pradel, "DyLin: A Dynamic Linter for Python", FSE 2025), which is why the linter is
best-effort and advisory; this station is what a real net looks like when it trips it.

Runnability
-----------
The network hazard talks to a **local mock** loyalty endpoint (:func:`loyalty_stub`)
rather than a third-party service, so it is genuinely runnable — real sockets, real
(tunable) latency — without hammering anyone's API or requiring the network. The mock is
loopback-only on an OS-assigned port and torn down with its ``with`` block; it also
requires HTTP Basic auth (a self-describing, non-secret :data:`LOYALTY_DEMO_TOKEN`) to
model an authenticated upstream — not to secure a fixture that holds nothing. The other
three hazards run on the standard library alone. Detection does not depend on any of
this: the linter flags the ``http.client`` / ``sqlite3`` / ``random`` / ``time``
reference statically, at construction, whether or not the transition ever fires.
"""

import base64
import contextlib
import http.client
import json
import random
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cafe.support import with_work
from cpnx import BindingPolicy, InputArc, OutputArc, Place, SinkPlace, Token, Transition

# --- network hazard: a local mock "loyalty API" -------------------------------------

#: Address of the mock loyalty endpoint, set by :func:`loyalty_stub` while it runs.
#: ``None`` means "no server up" — the guard degrades to a safe default rather than
#: raising, so the station stays importable and lintable with nothing listening.
_LOYALTY_ADDR: tuple[str, int] | None = None

#: Loyalty tier at or above which a customer is treated as VIP (skips the queue).
VIP_TIER = 3

#: An **obviously fake, non-secret** bearer token the mock endpoint expects. It exists to
#: make the hazard realistic — real loyalty APIs are authenticated — not to protect a
#: loopback fixture that holds nothing worth protecting. The point it illustrates is the
#: *second* facet of the anti-pattern: authenticating a remote call from inside enabling
#: logic drags a credential into the guard too, which is strictly worse. Never put a real
#: token in source; this string is deliberately self-describing so a secret scanner can
#: tell it apart from one.
LOYALTY_DEMO_TOKEN = "demo-token-not-a-secret"  # noqa: S105 - intentional non-secret placeholder
_EXPECTED_AUTH = "Basic " + base64.b64encode(f"cafe:{LOYALTY_DEMO_TOKEN}".encode()).decode()


class _LoyaltyHandler(BaseHTTPRequestHandler):
    """Return ``{"tier": N}`` after a small delay — a stand-in for a real loyalty API.

    Requires HTTP Basic auth and answers ``401`` without it. On a loopback fixture this
    protects nothing; it is here to model an *authenticated* upstream so the hazard reads
    like the real mistake it warns against (see :data:`LOYALTY_DEMO_TOKEN`).

    The tier is a genuine random draw, so the lookup is really non-deterministic — the
    whole point of the hazard. :func:`loyalty_stub` seeds :attr:`rng` when reproducibility
    is wanted (tests, a repeatable demo) and leaves it on system entropy otherwise.
    """

    #: Artificial latency (seconds) so the hazard exhibits real round-trip cost.
    delay = 0.02
    #: Source of tier draws; replaced per-stub by :func:`loyalty_stub`.
    rng = random.Random()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        time.sleep(self.delay)
        if self.headers.get("Authorization") != _EXPECTED_AUTH:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="loyalty"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        # A real draw per lookup: the same card can come back a different tier, which is
        # exactly why reading this in a selection callable is non-deterministic.
        tier = type(self).rng.randint(0, 4)
        body = json.dumps({"tier": tier}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # silence the default stderr access log
        pass


@contextlib.contextmanager
def loyalty_stub(delay: float = 0.02, seed: int | None = None):
    """Run a local mock loyalty endpoint for the lifetime of the ``with`` block.

    Binds ``127.0.0.1`` on an OS-assigned port, publishes the address in
    :data:`_LOYALTY_ADDR` so :func:`loyalty_priority` can reach it, and tears the
    server down on exit. Using a mock we control — rather than a third-party service —
    keeps the runnable demo reproducible, offline-friendly, and free of any external
    rate-limit or fair-use concern, while still exercising a real socket round-trip.

    The endpoint returns a genuinely random tier. Pass *seed* to make that draw
    reproducible (same seed + same request order → same tiers); leave it ``None`` for
    system-entropy non-determinism. For *true* external entropy and real WAN latency, see
    :func:`fetch_randomorg_outcomes` — an opt-in, one-shot path, not this local mock.
    """
    global _LOYALTY_ADDR
    _LoyaltyHandler.delay = delay
    _LoyaltyHandler.rng = random.Random(seed)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoyaltyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _LOYALTY_ADDR = server.server_address
    try:
        yield server.server_address
    finally:
        _LOYALTY_ADDR = None
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def loyalty_priority(tokens: list[Token]) -> int:
    """⚠️ ``binding_priority_key`` HAZARD (network): rank a shot by a remote loyalty lookup.

    Cafe role:
        "VIPs skip the line." Before deciding whose grounds to pull next, phone the
        loyalty service for the customer's tier and let VIPs sort ahead.

    Why it's a hazard:
        The binding *priority* — and therefore which token the transition consumes —
        now depends on a network round-trip. Firing is non-deterministic (the service
        can change its answer, time out, or fail), latency-bound, and impossible to
        reason about statically. The right home for a loyalty lookup is the transition's
        **action** (after the binding is chosen), or a value stamped onto the token
        upstream — never the enabling decision. :mod:`cpnx.linting` flags the
        ``http.client`` reference as ``network``.

        A second facet, since the endpoint is authenticated: the call must carry a
        credential, so a bearer token now lives inside the selection callable too. Secret
        handling migrating into enabling logic is strictly worse than the network read
        alone — one more reason this belongs in the action, not the guard/key.
    """
    if _LOYALTY_ADDR is None:
        return 1  # no endpoint up: everyone is a walk-in
    host, port = _LOYALTY_ADDR
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        conn.request(
            "GET",
            f"/loyalty?card={tokens[0].payload.get('card', '')}",
            headers={"Authorization": _EXPECTED_AUTH},  # credential dragged into selection logic
        )
        response = conn.getresponse()
        if response.status != 200:
            return 1  # auth failed / service unhappy → treat as a walk-in
        tier = json.load(response).get("tier", 0)
    finally:
        conn.close()
    return 0 if tier >= VIP_TIER else 1  # min-first ordering → VIPs sort ahead


# --- optional: true external entropy + real WAN latency from random.org --------------
#
# The local mock above is non-deterministic but its entropy and latency are local. This
# section is the opt-in flourish: one well-behaved, batched call to random.org for *true*
# atmospheric entropy over a *real* internet round-trip, used only by the one-shot demo
# (gated on CPNX_DEMO_RANDOM_ORG). It is deliberately NOT wired into the net, tests, or
# any drive-to-quiescence path — at "once per candidate binding" call rates it would
# exhaust random.org's per-IP quota, trip its rate limiter, and make runs non-reproducible
# and network-dependent. Batching N draws into a single request keeps us within quota.

#: random.org host and an identifying User-Agent (their automated-client guidelines ask
#: callers to identify themselves; we point at the repo rather than a personal address).
RANDOMORG_HOST = "www.random.org"
_RANDOMORG_USER_AGENT = "cpnx-demo (+https://github.com/philgresh/cpnx)"

#: Map a small random integer to a simulated loyalty-service outcome. The range is kept
#: tiny (0..3, i.e. 2 bits/draw) on purpose: fewer bits drawn is fewer bits charged
#: against random.org's daily per-IP quota.
_OUTCOME_BY_INT = {
    0: "accepted (2xx, loyalty applied)",
    1: "rejected (2xx, no loyalty)",
    2: "unauthorized (simulated 401)",
    3: "server-error (simulated 5xx)",
}
#: random.org throttling us is itself a first-class scenario — a net that depends on an
#: external service must model the service saying "not now".
RATE_LIMITED = "rate-limited (random.org 429/503)"


def map_outcome(value: int) -> str:
    """Map a 0..3 draw to a simulated loyalty HTTP outcome (pure; offline-testable)."""
    return _OUTCOME_BY_INT.get(value, f"unknown ({value})")


def fetch_randomorg_quota(*, timeout: float = 5.0) -> int | None:
    """Return remaining bits in today's per-IP quota, or ``None`` if unavailable.

    random.org's guidelines ask automated clients to check quota before drawing; this is
    that check. Never raises — a network problem yields ``None`` and the caller skips.
    """
    conn = http.client.HTTPSConnection(RANDOMORG_HOST, timeout=timeout)
    try:
        conn.request("GET", "/quota/?format=plain", headers={"User-Agent": _RANDOMORG_USER_AGENT})
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        return int(resp.read().decode().strip())
    except (OSError, ValueError):
        return None
    finally:
        conn.close()


def fetch_randomorg_outcomes(count: int = 16, *, timeout: float = 5.0) -> tuple[list[str], str]:
    """One batched, well-behaved call to random.org → simulated outcomes + a status note.

    Draws *count* integers in ``[0, 3]`` in a single request (one round-trip, charged
    once) and maps each to :data:`_OUTCOME_BY_INT`. A ``429``/``503`` from random.org is
    returned as the :data:`RATE_LIMITED` scenario rather than an error, so the caller can
    treat "the entropy service throttled us" as a valid net outcome. Never raises.

    Returns ``(outcomes, note)`` where *note* is ``"ok"``, :data:`RATE_LIMITED`, or a
    short diagnostic; *outcomes* is empty unless *note* is ``"ok"``.
    """
    query = f"/integers/?num={count}&min=0&max=3&col=1&base=10&format=plain&rnd=new"
    conn = http.client.HTTPSConnection(RANDOMORG_HOST, timeout=timeout)
    try:
        conn.request("GET", query, headers={"User-Agent": _RANDOMORG_USER_AGENT})
        resp = conn.getresponse()
        if resp.status in (429, 503):
            resp.read()
            return [], RATE_LIMITED
        if resp.status != 200:
            return [], f"random.org returned HTTP {resp.status}"
        draws = [int(token) for token in resp.read().decode().split()]
        return [map_outcome(value) for value in draws], "ok"
    except (OSError, ValueError) as exc:
        return [], f"random.org unavailable ({type(exc).__name__})"
    finally:
        conn.close()


# --- database hazard: an inventory lookup -------------------------------------------

#: Path to an inventory database, set by a demo/test that seeds one. ``None`` means the
#: guard reads a throwaway in-memory database and defaults to "in stock".
_INVENTORY_DB: str | None = None


def stock_check_guard(tokens: list[Token]) -> bool:
    """⚠️ ``guard`` HAZARD (database): gate grinding on a live inventory query.

    Cafe role:
        "Don't grind if we're out of that bean." Look the ticket's bean up in the
        stock database and only enable the grind when quantity remains.

    Why it's a hazard:
        Whether the transition is enabled now depends on **external mutable state**.
        Two identical markings can enable or not depending on what a separate system
        wrote to the database, so reachability and liveness cannot be reasoned about
        from the net. A stock level belongs on the token (stamped upstream) or checked
        inside the action, not in the guard. :mod:`cpnx.linting` flags ``sqlite3`` as
        ``database``.
    """
    conn = sqlite3.connect(_INVENTORY_DB or ":memory:")
    try:
        bean = tokens[0].payload.get("bean", "house")
        row = conn.execute("SELECT qty FROM stock WHERE bean = ?", (bean,)).fetchone()
    except sqlite3.OperationalError:
        return True  # no stock table (nothing seeded) → assume in stock
    finally:
        conn.close()
    return bool(row) and row[0] > 0


# --- randomness hazard: a quality spot-check ----------------------------------------


def qc_spot_check(token: Token) -> bool:
    """⚠️ ``filter`` HAZARD (randomness): divert ~1 drink in 10 for a quality tasting.

    Cafe role:
        "Spot-check one in ten." A quietly reasonable-looking QC rule that pulls a
        random sample of finished drinks off the tray for a taste test.

    Why it's a hazard:
        The most innocent-looking of the four, and the most corrosive: token
        *eligibility* is decided by a coin flip, so which drinks a transition may
        consume is different on every run and no invariant over the marking holds.
        Sampling should be an action side effect on a token that *would* be served, not
        an eligibility predicate. :mod:`cpnx.linting` flags ``random`` as
        ``nondeterminism``.
    """
    return random.random() < 0.1


# --- clock hazard: time-of-day priority ---------------------------------------------


def happy_hour_key(token: Token) -> int:
    """⚠️ ``key`` HAZARD (clock): order the serve queue by the wall clock.

    Cafe role:
        "During happy hour, prioritise the discounted orders." Sort the tray by a value
        derived from the current time of day.

    Why it's a hazard:
        Consumption *order* now depends on when the check happens to run, so the same
        marking drains in different orders across runs and the net never has a
        reproducible fixed point. Time-of-day belongs on the token at intake, not read
        live in the selection key. :mod:`cpnx.linting` flags ``time`` as
        ``nondeterminism``.
    """
    # Happy hour (16:00–18:00) sorts discounted orders first; otherwise arrival order.
    hour = time.localtime(time.time()).tm_hour
    return 0 if 16 <= hour < 18 and token.payload.get("discount") else 1


# --- station contract ----------------------------------------------------------------


def places() -> list[Place]:
    """The one extra place the hazards need: a terminal bench for QC-sampled drinks."""
    return [SinkPlace("P_QC_Bench", keep_last=8)]


def transitions(*, work_secs: float = 0.0) -> list[Transition]:
    """Four hazard transitions, each tapping an existing deep place (all default-off).

    - ``T_Loyalty_Pull`` — network: a loyalty-ranked pull off ``P_Ground_Coffee``.
    - ``T_Stock_Check_Grind`` — database: an inventory-gated grind off ``P_Ticket_Line``.
    - ``T_Quality_Hold`` — randomness: a random QC sample off ``P_Order_Tray``.
    - ``T_Happy_Hour_Serve`` — clock: a time-of-day-ordered serve off ``P_Order_Tray``.
    """

    def _pass(tokens: list[Token]) -> list[Token]:
        return list(tokens)

    return [
        Transition(
            name="T_Loyalty_Pull",
            inputs=[InputArc("P_Ground_Coffee", count=1)],
            outputs=[OutputArc("P_Order_Tray", count=1)],
            action=with_work(work_secs, _pass),
            binding_policy=BindingPolicy.PRIORITY,
            binding_priority_key=loyalty_priority,
        ),
        Transition(
            name="T_Stock_Check_Grind",
            inputs=[InputArc("P_Ticket_Line", count=1)],
            outputs=[OutputArc("P_Ground_Coffee", count=1)],
            action=with_work(work_secs, _pass),
            guard=stock_check_guard,
        ),
        Transition(
            name="T_Quality_Hold",
            inputs=[InputArc("P_Order_Tray", count=1, filter=qc_spot_check)],
            outputs=[OutputArc("P_QC_Bench", count=1)],
            action=with_work(work_secs, _pass),
        ),
        Transition(
            name="T_Happy_Hour_Serve",
            inputs=[InputArc("P_Order_Tray", count=1, key=happy_hour_key)],
            outputs=[OutputArc("P_Served", count=1)],
            action=with_work(work_secs, _pass),
        ),
    ]
