# ☕ The Concurrency Cafe

A single-bar specialty coffee shop during the morning rush, modelled as a Coloured Petri
Net. Tickets pile up at the register, baristas share a small pool of scales, the grinders
need a breather after every dose, a barista won't grind an out-of-spec dose, and a drink
isn't done until both the shot and the milk land on the same tray.

It is a demo you can read end to end — but it is also `cpnx`'s **benchmark fixture**, and
that gives every station a second job. Each one is the only place in the corpus where some
particular engine cost path gets exercised, so each factory below documents two things: the
**cafe role** (what a barista would see) and the **net feature** it demonstrates.

!!! warning "Not a conservation-checked net"

    The cafe's transitions *transform* tokens — an order becomes grounds, then espresso,
    then part of a drink — rather than merely moving fixed colours between places. That is
    deliberate and idiomatic for `cpnx`, but it means token counts are not invariant across
    a run the way a strict place/transition conservation model's would be. Read the output
    as "a cafe served some drinks and binned some botched shots", not as an audited ledger.

Run it:

```bash
python benchmarks/concurrency_cafe.py
```

## The tour

::: cafe

## Building a net

::: cafe.net

## Core places

The base topology — everything you get from a bare `build_cafe()`.

::: cafe.places

## Core transitions

::: cafe.transitions

## Inscriptions — guards and binding keys

The net's predicates and orderings. These run **under the engine lock** and are
purity-verified, which is why they are kept apart from the actions.

::: cafe.inscriptions

## Actions

The work a barista actually does. Actions run on the thread pool, **outside** the lock,
and are the one part of a net explicitly allowed side effects.

::: cafe.actions

## Opt-in stations

Every station below is default-off and structure-preserving when off, so `build_cafe()`
with no flags is exactly the base topology and long-standing benchmark numbers stay
comparable. Each exists because there is some engine cost path the base net never touches.

::: cafe.stations

### 🧊 Cold-brew tower — a deep timed place

::: cafe.stations.cold_brew

### 📋 Rush-hour triage — a certified `InputArc.key` at depth

::: cafe.stations.batch_triage

### ☕ The decaf-only barista — `filter` without `key`

::: cafe.stations.decaf

### 🥁 The knock box — `consume_all` on a deep place

::: cafe.stations.knock_box

### 🧾 The specials board — an uncertified `key`

::: cafe.stations.specials_board

### 🚫 The 86 board — a certified `key` behind an uncertified `filter`

::: cafe.stations.eighty_six

### 🥄 The cupping table — `count > 1` and the candidate space

::: cafe.stations.cupping

### 🥐 The pastry case — a `SubstitutionTransition`

::: cafe.stations.pastry_case

## ⚠️ Decidability hazards — deliberate anti-patterns

Every station above is deterministic, so the base cafe is **lint-clean**: the best-effort
side-effect linter ([`cpnx.linting`](reference/transitions.md#side-effect-linting)) finds
nothing to warn about, and `tests/test_cafe_lint.py` locks that in. The station below is the
negative control — a gallery of guards/keys/filters that each smuggle network, database,
clock, or randomness reads into *enabling* logic. Enable it with `build_cafe(hazards=True)`
to watch the linter flag every one; `benchmarks/lint_cafe.py` prints the before/after report.
**Do not copy these inscriptions** — they exist to be detected.

::: cafe.stations.decidability_hazards

## Blast-radius / impact analysis

The linter tells you **which** transitions are hazards; it does not tell you **how far**
each one's effect can spread. That is the job of the colour-domain blast-radius tracer
([ADR 0008](https://github.com/philgresh/cpnx/blob/main/docs/adr/0008-color-domain-impact-analysis.md))
— the sibling safety prong to ADR 0007's linter. A transition declares the token colour domain it mutates via the
purely-declarative
[`impacts_colors`][cpnx.Transition] annotation, and
[`trace_impact`][cpnx.trace_impact] walks the static topology **forward** from that
transition's output places, pruning any downstream place whose `color_set` cannot carry a
declared colour (a cone-of-influence slice). The trace is a deliberate **over-approximation**:
it may over-name, but it never omits a genuinely-reachable node.

```python
net = build_cafe(hazards=True)

# How far does the inventory-gated grind reach downstream?
impact = net.trace_impact("T_Stock_Check_Grind")
print(sorted(impact.places), sorted(impact.transitions))

# Fuse the two prongs: lint every enabling inscription, and attach a blast
# radius for each transition that trips the linter OR declares impacts_colors.
report = net.risk_report()
#   report["findings"]      → which transitions are hazards (and why)
#   report["impact_maps"]   → how far each one's effect spreads

# Render the DOT with the blast radius shaded (impacted nodes filled, seed heavier).
dot = net.to_dot(highlight_impact_from="T_Stock_Check_Grind")
```

`risk_report()` is the single verification/debugging entry point that fuses linting and
tracing into one JSON-serialisable object. `benchmarks/impact_cafe.py` is the runnable
companion to `benchmarks/lint_cafe.py`: where the latter prints the linter's before/after on
`build_cafe(hazards=True)`, the former prints each hazard's blast radius.

## Shared helpers

::: cafe.support
