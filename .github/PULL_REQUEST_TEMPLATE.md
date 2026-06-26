## Description

<!-- Provide a clear summary of what this PR does, the problem it solves, and why it is needed. -->

## Related Issue(s)

<!-- Link the issue(s) here, e.g., Closes #123, Resolves #456. -->

## Type of Change

- [ ] **Fix**: Bug fix (non-breaking change which fixes an issue)
- [ ] **Feat**: New feature (non-breaking change which adds functionality)
- [ ] **Refactor**: Code reorganization, styling, or performance improvement
- [ ] **Docs**: Documentation update
- [ ] **Chore**: Build configuration, CI, tool setup, or repository maintenance
- [ ] **Breaking Change**: Fix or feature that would cause existing functionality to not work as expected

## Verification & Testing

### Test Plan
<!-- Describe how you tested your changes and how a reviewer can verify them. -->

### Checklist
- [ ] Added new unit tests covering the modified/new behavior.
- [ ] Verified that all unit tests pass locally (`make test`).
- [ ] Run style checks and formatter (`make format && make lint`) to ensure compliance with Ruff/formatting standards.

## Library-Specific Checklist

### Concurrency & Thread Safety
- [ ] **No locks during callbacks**: User-supplied callbacks (e.g., `on_transition_fired`, `on_error`, or `on_token_deposited`) are not invoked while holding the internal engine lock (`self._lock`).
- [ ] **Encapsulation invariant**: Private properties of places (e.g., `place._tokens` or `place._lock`) are not accessed directly in engine code (use thread-safe public interfaces like `len(place)` instead).
- [ ] **Lock re-entrancy safety**: Standard non-reentrant locks are handled safely to prevent self-deadlocks (e.g., avoided nested lock acquisitions or recursive `super()` calls that acquire the same lock).

### Resource & Memory Safety
- [ ] **No token leaks**: Wrapped executor submissions in try-except blocks to catch executor failures and restore consumed input tokens to their source places.
- [ ] **Surplus tokens**: Returned surplus resource tokens in transition queues to their original source places upon successful transition execution.
- [ ] **Prune cooldowns**: Ensured custom resource places clean up internal cooldown mapping dictionaries when tokens are retrieved.

### Python Coding Style & Git
- [ ] **Python target**: Used Python 3.10+ typing conventions (e.g., `A | B` rather than `Union[A, B]`).
- [ ] **Docstring guidelines**: Followed PEP 257 docstring conventions for all public classes, methods, and functions.
- [ ] **Conventional commits**: Commits follow the Conventional Commits specification (e.g., `feat: ...`, `fix: ...`, `chore: ...`).
