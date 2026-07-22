"""Deep keyed-drain A/B harness — runs identically on cpnx 0.3.2 and on the PR 2 branch.

The workload is one deep place drained one token per firing through an arc that orders by
an ascending per-token key. Only the *arc construction* differs between versions, because
that is exactly the API that changed:

    0.3.2   InputArc("in", expression=lambda tokens: sorted(tokens, key=k))
    PR 2    InputArc("in", key=k)

Everything else — token count, payload distribution, seed, worker count, consumption order
— is shared code, so a timing difference can only come from the engine.

Emits one JSON line per (variant, n, repeat) so the caller can interleave runs and
aggregate. Also emits the consumption order digest, which MUST match across variants or the
comparison is meaningless.
"""

import hashlib
import json
import random
import sys
import time

from cpnx import InputArc, OutputArc, PetriNet, Place, Token, Transition

SEED = 20260722


def _priority(token):
    """The per-token key. Identical on both sides; only how it is *passed* differs."""
    return token.payload["p"]


def _build_arc(api):
    """The one construction that differs between the two APIs."""
    if api == "expression":  # cpnx 0.3.2: opaque list[Token] -> list[Token]
        return InputArc("in", expression=lambda tokens: sorted(tokens, key=_priority))
    return InputArc("in", key=_priority)  # PR 2: per-token key


def run_once(n, api):
    rnd = random.Random(SEED)
    order = []
    net = PetriNet(max_workers=1)
    net.add_place(Place("in"))
    net.add_place(Place("out"))
    net.add_transition(
        Transition(
            name="drain",
            inputs=[_build_arc(api)],
            outputs=[OutputArc("out")],
            action=lambda toks: order.append(toks[0].payload["id"]) or toks,
        )
    )
    # Deposit the whole marking up front so the place is genuinely deep from firing one.
    for i in range(n):
        net.deposit("in", Token(payload={"p": rnd.randrange(0, n * 3), "id": i}))

    started = time.perf_counter()
    net.run(deadline=time.monotonic() + 3600)
    elapsed = time.perf_counter() - started

    digest = hashlib.sha256(",".join(map(str, order)).encode()).hexdigest()[:16]
    return {
        "n": n,
        "drained": len(order),
        "left": len(net.places["in"]),
        "ms": round(elapsed * 1000, 2),
        "us_per_order": round(elapsed * 1e6 / n, 1),
        "order_sha": digest,
    }


def _disable_key_index():
    """Force the PR 2 build onto its own fallback path.

    Isolates the key-index from every *other* change since 0.3.2 (#24 callables-only,
    #26 the linearized store, #28 the API split): this variant runs today's engine with
    only the index switched off, so `pr2 - pr2_noindex` is the index's contribution and
    `pr2_noindex - v032` is everything else's.
    """
    import cpnx.places as places

    places._TokenStore.peek_by_key = lambda self, index_id, k, predicate=None: None


if __name__ == "__main__":
    api, variant = sys.argv[1], sys.argv[2]
    if "noindex" in variant:
        _disable_key_index()
    for n in (int(x) for x in sys.argv[3].split(",")):
        result = run_once(n, api)
        result["variant"] = variant
        print(json.dumps(result), flush=True)
