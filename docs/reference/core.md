# Core

The central object: build a net, wire places and transitions, then run it.

::: cpnx.PetriNet

::: cpnx.DriveResult

## Impact analysis

Declarative colour-domain **blast-radius** analysis: given a transition that mutates or side-effects a token colour domain, trace how far downstream its effect can spread. This is the sibling safety prong to the [side-effect linter](transitions.md#side-effect-linting) — the linter flags *which* transitions are hazards, the tracer bounds *how far* each one reaches (see [ADR 0008](https://github.com/philgresh/cpnx/blob/main/docs/adr/0008-color-domain-impact-analysis.md)). The forward walk is a sound, over-approximate cone-of-influence slice, gated by each place's `color_set`; annotate a transition with [`impacts_colors`][cpnx.Transition] to prune the trace to a declared colour domain.

::: cpnx.trace_impact

::: cpnx.ImpactMap

::: cpnx.risk_report
