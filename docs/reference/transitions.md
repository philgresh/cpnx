# Transitions

Transitions fire work over the thread pool; arcs connect them to places.

::: cpnx.Transition

::: cpnx.SubstitutionTransition

::: cpnx.InputArc

::: cpnx.OutputArc

`BindingPolicy` selects how a transition resolves which input tokens bind it — the deterministic-complete binding search (`FIRST`, the default), a selection *among* the satisfying bindings (`RANDOM`, `PRIORITY`), or the deprecated head-only check (`LEGACY`, which warns and is removed in 0.7.0).

::: cpnx.CpnxDeprecationWarning

::: cpnx.BindingPolicy

## Retry scheduling

`RetryPolicy` says how long a rolled-back data token waits before each retry: capped exponential backoff with optional full jitter, the shape recommended by the AWS Builders' Library's [Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/). Attach one per transition ([`Transition.retry_policy`][cpnx.Transition]) — retry behaviour belongs to the dependency a transition talks to, not to the net — and a transition that declares none inherits [`PetriNet.retry_policy`][cpnx.PetriNet]. How *many* times it retries before dead-lettering stays [`Transition.max_retries`][cpnx.Transition].

::: cpnx.RetryPolicy

## Side-effect linting

Guards, arc `key`/`filter` selectors, and output-arc conditions should be deterministic and side-effect-free — they are evaluated to decide *enabling*, so network I/O, database reads, or clock/randomness use in them make the net's behaviour non-reproducible and undermine static analysability (see [ADR 0007](https://github.com/philgresh/cpnx/blob/main/docs/adr/0007-best-effort-side-effect-linting.md)). `cpnx` runs a best-effort AST linter at construction time that **warns** ([`CpnxLintWarning`][cpnx.CpnxLintWarning]) when it detects these trouble spots. Set `CPNX_LINT_STRICT=1` or call [`set_strict`][cpnx.set_strict] to promote warnings to errors for CI gating, or call [`lint_callable`][cpnx.lint_callable] directly to inspect a callable programmatically.

::: cpnx.lint_callable

::: cpnx.LintFinding

::: cpnx.CpnxLintWarning

::: cpnx.set_strict
