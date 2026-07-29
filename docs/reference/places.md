# Places

Places hold tokens. Beyond the plain `Place`, several variants model resource
pools, pacing, thresholds, sinks, and dependency-health gating.

::: cpnx.Place

::: cpnx.ResourcePlace

::: cpnx.PacedResourcePlace

::: cpnx.ThresholdPlace

::: cpnx.SinkPlace

`CircuitBreakerPlace` disables the transitions that depend on a shared external service while
it is down, holding their input tokens in place and resuming automatically once a probe confirms
recovery. Dependent transitions gate on it with a non-consuming [test arc](transitions.md)
(`InputArc(..., test=True)`); a transition whose failures should trip it names it via
`Transition.breaker`. See the design record in
[`docs/adr/0007-dependency-health-circuit-breaker.md`](https://github.com/philgresh/cpnx/blob/main/docs/adr/0007-dependency-health-circuit-breaker.md).

::: cpnx.CircuitBreakerPlace
