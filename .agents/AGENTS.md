# Petriq Agent Instructions & Rules

These project-scoped guidelines apply to all AI agents working on the `cpnx` codebase.

---

## 1. Concurrency & Thread Safety Rules
*   **No Locks During Callbacks**: Never invoke user-supplied callbacks (such as `on_transition_fired`, `on_error`, or `on_token_deposited`) while holding the internal engine lock (`self._lock`). Doing so poses high risks of re-entrant deadlocks.
*   **Encapsulation Invariant**: Avoid direct accesses to private fields of places (e.g., `place._tokens` or `place._lock`) inside the engine code. Always delegate to thread-safe public interfaces like `len(place)` or explicit query methods.
*   **Lock Re-entrancy Safety**: The place and engine locks are standard `threading.Lock` instances (non-reentrant). Subclasses (like `PacedResourcePlace`) overriding retrieval/deposit methods must manipulate inner properties directly under their own lock instead of using nested `super()` calls that would attempt to acquire the lock a second time and deadlock.

---

## 2. Resource & Memory Safety Rules
*   **No Token Leaks**:
    *   Always wrap `self._executor.submit()` calls in try-except blocks to catch executor failures (e.g., pool shutdown) and restore consumed input tokens back to their source places.
    *   Surplus resource tokens remaining in transition queues must be returned to their original source places upon successful transition execution.
*   **Prune Cooldowns**: Ensure custom resource places (e.g., `PacedResourcePlace`) clean up internal cooldown mapping dictionaries when tokens are retrieved.

---

## 3. Formatting, Linting & Python Styling
*   **Compliance Checks**: Before proposing any change, always run `make format` and `make lint` to ensure compliance with Ruff formatting rules.
*   **Testing Requirement**: Always run `make test` and ensure all unit tests pass before concluding a task. Add new tests for any modified or new behavior.
*   **Python Target**: Code should align with Python 3.10+ conventions (Union typing `A | B` rather than `Union[A, B]`).
*   **Docstring Guidelines**: Follow PEP 257 docstring conventions for all public classes, methods, and functions.

---

## 4. Git & Commits Rules
*   **Conventional Commits**: When asked to commit something, always construct commit messages using the Conventional Commits specification (e.g. `feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`).
