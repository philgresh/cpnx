"""☕ The Concurrency Cafe — a whimsical, illustrative `cpnx` reference topology.

Picture a single-bar specialty coffee shop during the morning rush. Tickets pile up
at the register, baristas share a small pool of digital scales, the grinders need a
breather after every dose, a barista won't grind a ticket whose declared dose misses
spec (it goes back for a re-dose instead), and a finished drink is only "done" once
*both* the espresso shot and the steamed milk have landed on the same tray. That
whole scene maps almost one-to-one onto `cpnx`'s vocabulary of places, resources,
thresholds, guards, and sinks — which is why it makes a good end-to-end tour of the
library.

It is also the fixture every benchmark in `benchmarks/` runs against, so each station
carries a second job: to be the *only* place in the corpus where some particular
engine cost path is exercised. Each factory's docstring names both — the cafe role
and the net feature.

Layout
------
| Module | Holds |
| --- | --- |
| [`cafe.places`][cafe.places] | The core places, one documented factory each |
| [`cafe.transitions`][cafe.transitions] | The core transitions, one documented factory each |
| [`cafe.inscriptions`][cafe.inscriptions] | Guards and binding keys — pure, run under the engine lock |
| [`cafe.actions`][cafe.actions] | Transition actions — side effects allowed, run off the lock |
| [`cafe.stations`][cafe.stations] | Opt-in stations, one self-contained module per station |
| [`cafe.net`][cafe.net] | `build_cafe`, which just chooses among the above |
| [`cafe.support`][cafe.support] | Shared constants and the `work_secs` wrapper |

Warning:
    This is an **illustrative benchmark/demo, not a conservation-checked CPN**. Its
    transitions *transform* tokens (an order token is consumed and becomes a
    ground-coffee token, then an espresso token, then part of a drink token) rather
    than merely moving fixed colours between places. That is deliberate and idiomatic
    for `cpnx`, but it means you should not expect the total token count, or any
    single colour's count, to be invariant across a run the way it would be in a
    strict place/transition conservation model. Treat what this prints as "a cafe
    served some drinks and binned some botched shots", not as an audited ledger.

Token colours in play
---------------------
- `None` (order tickets) — an uncoloured data token carrying the customer's order as
  its `payload`: `ratio`, `weight_g`, `dairy_free`, `mobile_pickup`.
- `"resource"` — permit tokens pre-filled into [`ResourcePlace`][cpnx.ResourcePlace] and
  [`PacedResourcePlace`][cpnx.PacedResourcePlace] instances (scales, grinders, group heads, wands). The engine
  returns these automatically once consumed; action code never hands them back.
- `"ground_coffee"` / `"milk_ticket"` — intermediate work-in-progress tokens produced
  by the grind step, one feeding the espresso line and one the milk line.
- `"espresso"` / `"oat_milk"` / `"dairy_milk"` — finished component tokens that
  accumulate on the order tray.
- `"cold_brew"` — a batch steeping in the opt-in cold-brew tower.
- `"drink"` — the final assembled beverage, deposited into the `P_Served` sink.

Base topology (always present)
------------------------------
| Place | cpnx type | Cafe role |
| --- | --- | --- |
| `P_Ticket_Line` | [`Place`][cpnx.Place] | Unbounded FIFO of incoming order tickets |
| `P_Digital_Scales` | [`ResourcePlace`][cpnx.ResourcePlace] | Shared pool of 3 scales |
| `P_Burr_Grinder` | [`PacedResourcePlace`][cpnx.PacedResourcePlace] | Grinders, each with a cooldown |
| `P_Ground_Coffee` | [`Place`][cpnx.Place] | Grounds awaiting a shot |
| `P_Milk_Queue` | [`Place`][cpnx.Place] | Milk tickets awaiting steaming |
| `P_Espresso_Machine` | [`ResourcePlace`][cpnx.ResourcePlace] | Two group heads |
| `P_Steam_Wand` | [`ResourcePlace`][cpnx.ResourcePlace] | Two steam wands |
| `P_Order_Tray` | [`ThresholdPlace`][cpnx.ThresholdPlace] | Shot + milk rendezvous; counter fits 6 cups |
| `P_Served` | [`SinkPlace`][cpnx.SinkPlace] | Terminal place for completed drinks |
| `P_Trash_Can` | [`SinkPlace`][cpnx.SinkPlace] | Dead-letter bin (also the net's `error_place`) |

Opt-in stations
---------------
All default to off, and all are structure-preserving when off — `build_cafe()` with
no flags is exactly the table above. See [`cafe.stations`][cafe.stations] for the module contract.

| Flag | Station | Exercises |
| --- | --- | --- |
| `cold_brew` | 🧊 Cold-brew tower | A deep **timed** place |
| `cold_brew_key` | ↳ with a keyed arc | The timed×key residual (#25) |
| `batch_triage` | 📋 Rush-hour triage | A certified [`InputArc.key`][cpnx.InputArc] at depth |

Run it directly:

    python benchmarks/concurrency_cafe.py
"""

from cafe.net import build_cafe

__all__ = ["build_cafe"]
