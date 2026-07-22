# cpnx

[![PyPI version](https://img.shields.io/pypi/v/cpnx.svg)](https://pypi.org/project/cpnx/)
[![Python versions](https://img.shields.io/pypi/pyversions/cpnx.svg)](https://pypi.org/project/cpnx/)
[![CI status](https://github.com/philgresh/cpnx/actions/workflows/ci.yml/badge.svg)](https://github.com/philgresh/cpnx/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Docs](https://img.shields.io/badge/docs-philgresh.github.io%2Fcpnx-blue.svg)](https://philgresh.github.io/cpnx/)

**cpnx** is a Coloured Petri Net (CPN) executor for concurrent Python pipelines — zero dependencies, stdlib-only threading.

📖 **Documentation:** [philgresh.github.io/cpnx](https://philgresh.github.io/cpnx/) — including the full [API reference](https://philgresh.github.io/cpnx/latest/reference/core/).

---

## Motivation

Python has excellent Petri net modeling libraries (like [SNAKES](https://snakes.ibisc.univ-evry.fr/) for formal analysis and [pm4py](https://pm4py.fit.fraunhofer.de/) for process mining) but lacks a lightweight concurrent runtime executor. Developers managing resource-constrained workflows (GPU slots, API rate limits, database connection pools) often stitch together `threading.Semaphore`, `ThreadPoolExecutor`, and `queue.Queue` by hand — ad-hoc wiring that is hard to visualise and impossible to formally reason about.

**cpnx** fills this gap: it models your concurrent pipeline as a Coloured Petri Net where transitions execute real work on thread pools, resource tokens are returned atomically on failure, and the net's structure makes resource contention a mathematical property rather than scattered locking code.

The execution model is aligned with Jensen's CPN formalism (see [Theoretical Foundation](#theoretical-foundation)), so the net you write is also amenable to formal analysis with standard CPN tools.

---

## Install

```bash
pip install cpnx
```

---

## Quickstart

A pool of 2 GPU slots shared across 10 concurrent training jobs:

```python
"""examples/gpu_pipeline.py — GPU slot management with cpnx."""

import time
from cpnx import InputArc, OutputArc, PetriNet, Place, ResourcePlace, Token, Transition


def train_model(tokens: list[Token]) -> list[Token]:
    data = tokens[0]
    time.sleep(0.5)  # simulate GPU work
    # Tokens are immutable — produce a new one with updated payload
    return [data.evolve(payload_updates={"trained": True})]


net = PetriNet(max_workers=4)

net.add_place(Place("raw_data"))
net.add_place(Place("trained_models"))
net.add_place(ResourcePlace("gpu_slots", capacity=2))

net.add_transition(Transition(
    name="train",
    inputs=[InputArc("raw_data"), InputArc("gpu_slots")],
    outputs=[OutputArc("trained_models"), OutputArc("gpu_slots")],
    action=train_model,
))

for i in range(10):
    net.deposit("raw_data", Token(payload={"model_id": i}))

net.run(deadline=time.monotonic() + 30)

print(f"Trained:            {len(net.places['trained_models'].tokens)}")
print(f"GPU slots returned: {len(net.places['gpu_slots'].tokens)}")
# Trained:            10
# GPU slots returned: 2
```

---

## Core Concepts

A CPN consists of **places** (token containers), **transitions** (processing steps), and **arcs** (directed connections). Tokens carry a **colour** that determines which places they may occupy and which transitions may consume them.

```mermaid
graph LR
    raw_data(("Place: raw_data")) --> train[Transition: train]
    gpu_slots(("ResourcePlace: gpu_slots\n(capacity=2)")) --> train
    train --> trained_models(("Place: trained_models"))
    train --> gpu_slots

    style raw_data fill:#e1f5fe,stroke:#0288d1,stroke-width:2px
    style trained_models fill:#e1f5fe,stroke:#0288d1,stroke-width:2px
    style gpu_slots fill:#efebe9,stroke:#5d4037,stroke-width:2px
    style train fill:#fffde7,stroke:#fbc02d,stroke-width:2px
```

### Tokens

Tokens are **immutable**. Their `payload` is a [`FrozenDict`](#frozendict) — a hashable, recursively-immutable mapping. To produce a token with updated data, use `token.evolve()`:

```python
result = token.evolve(payload_updates={"score": 0.92})
```

Each token carries a `color: str | None` field — the CPN colour. `None` means an uncoloured data token; `"resource"` is the built-in colour for permit tokens. You can define your own colours for domain-typed nets.

### Places

All places are thread-safe.

| Type | Behaviour |
|---|---|
| `Place` | Unbounded FIFO queue for data/work items |
| `ResourcePlace(capacity)` | Pre-filled bounded pool of `"resource"` permit tokens; returned on transition completion or failure |
| `PacedResourcePlace(capacity, pacing_secs)` | Like `ResourcePlace`, but returned tokens cool down for `pacing_secs` before becoming reusable (rate-limiting) |
| `ThresholdPlace(threshold)` | Tokens only consumable once the queue depth reaches `threshold` (batch accumulation) |
| `SinkPlace(keep_last)` | Absorbing terminal place; counts stats and evicts oldest tokens (streaming-native terminus) |

### Transitions

A transition is **enabled** when all input places contain sufficient tokens and any guard expression evaluates to `True`. When fired, it consumes input tokens, executes the action on a thread pool, and deposits output tokens.

**Atomic Rollback & Bounded Retry:** if a transition action raises, the engine catches the exception. Resource tokens are returned to their original source places immediately. Data tokens are rolled back to their source places (with a delay and incremented `attempts` counter) up to `max_retries` times (default 5). Once the retry limit is exhausted, data tokens are routed to `error_place` (default `"failed"`), allowing the net to safely quiesce. Callbacks (`on_error`, `on_token_dead_lettered`) fire for observability. The `error_place` can be configured as a `SinkPlace` to avoid memory leaks from failure accumulation in long-lived streaming nets.

### Canonical Error Handling (Colour Routing)

The most robust and mathematically sound way to handle errors in Coloured Petri Nets is to catch exceptions inside the action and return an **error-coloured token**. Output arcs can then use `condition` predicates to route success tokens to normal places and error tokens to an error sink. This preserves token conservation (1-in-1-out) and keeps the firing rules pure.

```python
from cpnx import PetriNet, Place, Token, Transition, InputArc, OutputArc, ERROR_COLOR

def process_job(tokens: list[Token]) -> list[Token]:
    data = tokens[0]
    try:
        # Perform fragile work
        if data.payload.get("should_fail"):
            raise ValueError("fragile operation failed")
        return [data.evolve(color="success")]
    except Exception as exc:
        # Catch and return an error-coloured token
        return [data.evolve(color=ERROR_COLOR, payload_updates={"error": str(exc)})]

net = PetriNet()
net.add_place(Place("jobs"))
net.add_place(Place("completed"))
net.add_place(Place("failed"))  # Custom error destination

net.add_transition(Transition(
    name="process",
    inputs=[InputArc("jobs")],
    outputs=[
        OutputArc.on_color("success", "completed"),
        OutputArc.on_color(ERROR_COLOR, "failed"),
    ],
    action=process_job,
))
```

### Arc Selection: `key`, `filter`, `condition`

`InputArc` accepts two **per-token** callables that together select which tokens a firing consumes: `key` orders the eligible tokens (ascending, min-first — negate the key for descending order) and `filter` decides which tokens are eligible at all (rejected tokens simply stay in the place). `OutputArc` accepts `condition` — a predicate over the action's output tokens that gates whether the arc deposits anything:

```python
# Consume the highest-priority lead first (higher score first, so negate for descending)
InputArc("leads", count=1, key=lambda t: -t.payload.get("score", 0))

# Only pull tokens that are actually ready for processing
InputArc("leads", count=1, filter=lambda t: not t.payload.get("on_hold", False))

# Only deposit to the output place if processing produced any tokens
OutputArc("results", condition=lambda tokens: bool(tokens))
```

### Marking

The **marking** is the complete distribution of tokens across all places at a given moment — the formal CPN state:

```python
m = net.marking           # dict[str, list[Token]]
dead = net.is_dead()      # True if no transition can fire in this marking
quiet = net.is_quiescent()  # True if dead AND no in-flight transitions
```

---

## API at a Glance

A quick-look cheatsheet of the public surface. For the complete, auto-generated
reference, see the [full API reference](https://philgresh.github.io/cpnx/latest/reference/core/).

### `Token`

[→ Full API reference](https://philgresh.github.io/cpnx/latest/reference/tokens/#cpnx.Token)

```python
@dataclass(frozen=True)
class Token:
    id: str              # 16-char hex, auto-generated
    payload: FrozenDict  # immutable enrichment data; use .evolve() to update
    created_at: float    # monotonic creation timestamp
    color: str | None    # CPN colour; None = uncoloured, "resource" = permit token
    available_at: float  # timed CPN: earliest time this token may be consumed
    attempts: int        # number of failed firings this token has been rolled back from

    def evolve(self, payload_updates: dict | None = None, **field_updates) -> Token: ...
    @property
    def is_resource(self) -> bool: ...  # shorthand for color == "resource"
```

### `FrozenDict`

[→ Full API reference](https://philgresh.github.io/cpnx/latest/reference/tokens/#cpnx.FrozenDict)

An immutable, hashable mapping. Nested dicts and lists are frozen recursively at construction time.

```python
fd = FrozenDict({"x": 1, "tags": ["a", "b"]})
fd["x"]          # 1
fd.as_dict()     # {"x": 1, "tags": ["a", "b"]}  — plain dict, JSON-serialisable
fd.set("y", 2)   # returns a new FrozenDict — fd is unchanged
```

### Places

[→ Full API reference](https://philgresh.github.io/cpnx/latest/reference/places/#cpnx.Place)

```python
Place(name: str, bound: int | None = None, color_set: set[str] | None = None,
      initial_marking: list[Token] | None = None)

ResourcePlace(name: str, capacity: int)
PacedResourcePlace(name: str, capacity: int, pacing_secs: float)
ThresholdPlace(name: str, threshold: int)
SinkPlace(name: str, *, keep_last: int = 0, color_set: set[str] | None = None)
```

- `bound` — k-bounded place; raises if a deposit would exceed capacity (standard CPN)
- `color_set` — if set, `deposit()` rejects tokens whose `color` is not in the set
- `initial_marking` — tokens deposited at construction time
- `keep_last` — number of most recent tokens to keep in a ring buffer for inspection (defaults to `0`)

### Arcs

[→ Full API reference](https://philgresh.github.io/cpnx/latest/reference/transitions/#cpnx.InputArc)

```python
InputArc(place: str, count: int = 1, consume_all: bool = False,
         settle_secs: float = 0.0, *,
         key: Callable[[Token], object] | None = None,
         filter: Callable[[Token], bool] | None = None)

OutputArc(place: str, count: int = 1,
          condition: Callable[[list[Token]], bool] | None = None)

# Helper for color-routed error handling
OutputArc.on_color(color: str, place: str, count: int = 1) -> OutputArc
```

### `Transition`

[→ Full API reference](https://philgresh.github.io/cpnx/latest/reference/transitions/#cpnx.Transition)

```python
@dataclass
class Transition:
    name: str
    inputs: list[InputArc]
    outputs: list[OutputArc]
    action: Callable[[list[Token]], list[Token]]
    guard: Callable[[list[Token]], bool] | str | None = None  # transition guard
    priority: int = 10  # lower fires first among equally-enabled transitions
    action_timeout_secs: float | None = None
    max_retries: int | None = 5  # default 5. None = infinite retry.
```

### `PetriNet`

[→ Full API reference](https://philgresh.github.io/cpnx/latest/reference/core/#cpnx.PetriNet)

```python
class PetriNet:
    def __init__(self, max_workers: int = 4, error_place: str = "failed",
                 places: list[Place] | None = None,
                 transitions: list[Transition] | None = None,
                 cooldown_interval: float = 0.05, timeout_secs: float = 1.0,
                 expr_timeout_secs: float = 0.1, retry_delay: float = 1.0): ...

    def add_place(self, place: Place) -> None: ...
    def add_transition(self, transition: Transition) -> None: ...
    def deposit(self, place_name: str, token: Token) -> None: ...

    def step(self) -> bool: ...                  # fire one enabled transition; False if none
    def run(self, deadline: float | None = None, *, stop_event: threading.Event | None = None) -> None: ...

    @property
    def marking(self) -> dict[str, list[Token]]: ...  # current CPN marking
    def is_dead(self) -> bool: ...                # no transition enabled in current marking
    def is_quiescent(self) -> bool: ...           # dead AND no in-flight transitions
    def advance_time(self, t: float) -> None: ... # advance timed CPN model clock
    def snapshot(self) -> dict: ...               # JSON-serialisable marking snapshot
    def to_dot(self) -> str: ...                  # Graphviz DOT representation

    # Callback hooks
    on_transition_fired: Callable[[str, float], None] | None         # (name, duration_secs)
    on_token_deposited: Callable[[str, Token], None] | None          # (place_name, token)
    on_token_dead_lettered: Callable[[str, Token], None] | None      # (transition_name, token)
    on_error: Callable[[str, Exception, Token | None], None] | None  # (name, exc, token)
```

---

## Examples

- [examples/gpu_pipeline.py](https://github.com/philgresh/cpnx/blob/main/examples/gpu_pipeline.py) — GPU slot pool; shows concurrent throttling
- [examples/api_rate_limit.py](https://github.com/philgresh/cpnx/blob/main/examples/api_rate_limit.py) — paced resource tokens enforce external API rate limits
- [examples/etl_pipeline.py](https://github.com/philgresh/cpnx/blob/main/examples/etl_pipeline.py) — multi-stage ETL using `ThresholdPlace` for batch accumulation

---

## Guards & Arc Selectors: Certification and the Inline Fast Path

Guards and arc selectors (`InputArc.key`, `InputArc.filter`, `OutputArc.condition`) are Python callables — `def`s or lambdas — full stop. Passing a `str` to `guard=`, `key=`, `filter=`, or `condition=` raises `TypeError` at construction. This replaced an earlier string-expression surface; cpnx now has one authoring surface and one security model to reason about, not two.

Removing strings doesn't mean removing the fast path they enabled. Instead, cpnx **certifies** callables: a callable that draws only on a fixed, library-controlled vocabulary (a small whitelist of builtins and methods), iterates only over its own argument, calls only helpers that themselves certify, and closes over nothing mutable is *closed-world and provably terminating* — so it's safe to call **inline**, under the engine lock, with no timeout. A callable that can't be certified falls back to the **timeout-bounded executor** (`expr_timeout_secs`, default 100 ms), exactly as before. Certification never changes *whether* a callable is allowed — only *how* it runs. The difference matters on the hot path: guards are re-evaluated per transition on every `step()`, and per candidate binding under `RANDOM`/`PRIORITY` binding-selection — the executor round-trip costs roughly 90x the inline call.

**The guard contract.** A guard is a *pure predicate over a candidate binding, evaluated an unbounded number of times, possibly under the engine lock.* Certification proves closed-world-ness and termination, not purity — Python offers no way to enforce that mechanically. A side-effecting guard can certify and run inline with no timeout, speculatively and repeatedly. Keep side effects out of guards; that's what actions are for.

To parameterise a guard, close over a value captured at construction time rather than reaching for module state:

```python
threshold = config["max_weight"]                       # read once, at net-build time
guard = lambda toks: toks[0].payload["w"] <= threshold  # closes over an immutable float → certifies
```

Finally, an *annotated* `guard`, `OutputArc.condition`, or `InputArc.filter` (`-> bool`) is checked at construction time to actually return `bool`; unannotated callables (including all lambdas) pass through unchecked. `InputArc.key` is **not** bool-checked — it returns an arbitrary comparable sort key, not a boolean. See [ADR 0002](https://github.com/philgresh/cpnx/blob/main/docs/adr/0002-guard-type-checking-scope.md) for the exact rules.

### Modelling external state

Two shapes handle the cases a pure, closed-over guard can't reach on its own — treat these as the recommended patterns, not the uncertified-executor path as a goal in itself:

**(a) Per-token external data → reify upstream in an action.** If a guard needs to look something up per token — a DB row, an API response — do that lookup in an upstream transition's `action` (side effects are explicitly allowed there, and actions run off the lock) and attach the result to the token, e.g. via `payload`. The downstream guard then reads a plain token attribute and stays pure.

**(b) Shared/global state → model it as a token in a place.** If firing should depend on mutable state shared across transitions — a feature flag, a pool count — represent that state as a token in a `Place` and consume/return it through an input arc, rather than closing a guard over a module global. `examples/api_rate_limit.py` and `examples/gpu_pipeline.py` already do this for capacity: `PacedResourcePlace`/`ResourcePlace` model paced and pooled resources as tokens, not external counters — the same resource-as-token shape generalises to other shared state.

---

## FAQ

### Why not Airflow or Celery?

Airflow and Celery are excellent for distributed, long-running DAGs. They require external brokers (Redis, Postgres) and add deployment complexity. cpnx is an in-process threading library for fine-grained resource control within a single Python process — no infrastructure required.

### Why not asyncio?

ML/AI pipelines, CPU-bound parsing, and legacy database integrations use synchronous libraries. Thread pools let synchronous code run concurrently without rewriting blocking calls to async.

### Can it prevent deadlocks?

Structurally, yes — as long as resource tokens are always returned (which the Resource Return Invariant enforces). Beyond that, CPNs are amenable to formal reachability analysis: expressing constraints as explicit token structures rather than scattered locks makes deadlock-freedom properties checkable with standard CPN tools.

---

## Theoretical Foundation

cpnx's execution model is aligned with **Coloured Petri Nets (CPNs)** as formalised by Kurt Jensen's group at Aarhus University. The key CPN concepts — colour sets, transition guards, formal markings, and k-bounded places — map directly onto cpnx's API. The classic CPN *arc inscription* is what cpnx splits into its two honest per-token halves: `InputArc`'s `key` (order) and `filter` (eligibility) on the input side, and `OutputArc`'s `condition` (activation) on the output side.

**References:**

- Jensen, K. et al. — *CPN Group at Aarhus University* — [cs.au.dk/cpnets](https://cs.au.dk/cpnets)  
  The canonical reference for CPN theory, tools (CPN Tools), and formalism.

- Winkler, T. et al. — *CPN-Py: A Python Framework for Coloured Petri Nets* (2025) — [arxiv.org/html/2506.12238v1](https://arxiv.org/html/2506.12238v1)  
  The closest Python CPN library; cpnx differs by targeting concurrent **execution** rather than sequential **simulation** and formal state-space analysis.

**Where cpnx intentionally diverges from standard CPN theory:**

| cpnx feature | Status |
|---|---|
| `PacedResourcePlace`, `settle_secs` | Pragmatic concurrency extensions; no CPN equivalent |
| `expr_timeout_secs`, certification (`cpnx.certification`) | Pragmatic purity-checking / certification of callable expressions; no CPN equivalent |
| `is_quiescent()` | Dead marking AND no in-flight threads; no single CPN term |
| `ResourcePlace`, `ThresholdPlace` | CPN patterns expressed as typed place shorthands |
| `Place.bound` | Standard CPN: k-bounded place |
| `Token.color`, `Place.color_set` | Standard CPN: colours and colour sets |

---

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](https://github.com/philgresh/cpnx/blob/main/CONTRIBUTING.md)
for development setup, the `make` targets CI runs, and the docstring/documentation conventions.

---

## License

MIT — see [LICENSE](https://github.com/philgresh/cpnx/blob/main/LICENSE).
