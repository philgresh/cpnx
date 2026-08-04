# Visualization

Introspect a running net or export it to Graphviz DOT.

::: cpnx.snapshot

::: cpnx.to_dot

## Highlighting a blast radius

Both [`to_dot`][cpnx.to_dot] and [`PetriNet.to_dot`][cpnx.PetriNet.to_dot] accept an optional
`highlight_impact_from=<transition name>` argument. When given, the transition's forward
colour-domain blast radius (see [`trace_impact`][cpnx.trace_impact]) is shaded into the
rendering:

| Element | Fill | Meaning |
|---|---|---|
| Impacted places and transitions | `#ffd9d9` (light red) | reachable within the traced colour domain |
| The seed transition (`origin`) | `#ff8080` (heavier red) + thicker border | the transition the trace started from |

`highlight_impact_from=None` (the default) leaves the rendering exactly as it was — no styling
is added. See [ADR 0008](https://github.com/philgresh/cpnx/blob/main/docs/adr/0008-color-domain-impact-analysis.md)
for the underlying impact-analysis model.
