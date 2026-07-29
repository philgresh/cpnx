# Transitions

Transitions fire work over the thread pool; arcs connect them to places.

::: cpnx.Transition

::: cpnx.SubstitutionTransition

An `InputArc` with `test=True` is a **non-consuming test/read arc**: it gates a transition on
token *presence* (at least `count` available) without consuming anything, so many transitions can
test the same place concurrently. This is how a
[`CircuitBreakerPlace`](places.md) health gate disables its dependent transitions.

::: cpnx.InputArc

::: cpnx.OutputArc

`BindingPolicy` selects how a transition resolves which input tokens bind it — the legacy leading-token check, or a deterministic-complete binding search.

::: cpnx.BindingPolicy
