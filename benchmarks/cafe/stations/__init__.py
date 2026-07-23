"""Opt-in cafe stations — one module per station, every one default-off.

Each module in this package is a **self-contained station**: its own places, its
own transitions, and whatever guards, keys, filters, and actions those need. A
station exists because there is some engine cost path the base topology never
touches, and each module's docstring names that path explicitly.

Station module contract
-----------------------
Every module here exposes exactly two entry points, so [`cafe.net.build_cafe`][cafe.net.build_cafe] can
wire any subset of them without knowing anything about a particular station:

```python
def places() -> list[Place]: ...
def transitions(*, work_secs: float = 0.0) -> list[Transition]: ...
```

A station may add further **keyword-only** parameters of its own — `cold_brew` takes
`key`, `knock_box` takes `min_pucks`, `cupping` takes `count` — and `build_cafe`
forwards them from correspondingly-named flags. Both functions must be callable with
no arguments at all, and neither may mutate anything outside its own return value. Nothing in this package
deposits tokens — a station only declares structure, and the benchmark that uses
it stocks the queue itself. That is what keeps a station's depth a property of
the *experiment* rather than of the fixture.

Why default-off
---------------
Every station here is structure-preserving when disabled: `build_cafe()` with no
flags returns exactly the base topology, so the long-standing benchmark numbers
stay comparable. Turning a station on adds places and transitions but changes
nothing about the ones already there.
"""
