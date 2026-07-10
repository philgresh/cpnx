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
*   **Docstring Guidelines**: Every public class, method, function, and module constant (anything in `cpnx.__all__` and their public members) is rendered on the docs site by [mkdocstrings](https://mkdocstrings.github.io/), so docstrings are API documentation — keep them accurate and complete.
    *   **Style**: Google-style docstrings (`Args:`, `Returns:`, `Raises:`, `Attributes:`, `Example:`). The first line must be a specific, self-contained summary of *that* member in the imperative mood — not a generic restatement. Document every parameter (meaning, units, default), the return value, and anything raised. For subclass overrides, state how the behavior differs from the base method.
    *   **Markdown, not reStructuredText**: docstring bodies are rendered as Markdown. Do **not** use Sphinx roles (`` :class:`X` ``, `` :meth:`X` ``, `` :attr:`X` ``, `` :exc:`X` `` …) — they render literally. Instead:
        *   Cross-reference a **public** symbol with an autorefs link: `` [`PetriNet`][cpnx.PetriNet] `` or `` [`step`][cpnx.PetriNet.step] ``.
        *   Use plain backticks (`` `last_deposit_time` ``) for instance attributes, private members (`_on_deposit`), and stdlib/builtin types. When unsure a cross-reference resolves, prefer plain backticks — a broken one fails the strict build.
    *   **Formatting**: use valid Markdown list markers (`-` or `1.`) with a blank line before the list; `A.`/`B.`/`C.` are **not** list markers and collapse into one paragraph. Use fenced ```` ```python ```` blocks for examples.
    *   **Verify**: run `make docs-build` (`mkdocs build --strict`) after touching docstrings — it fails on unresolved cross-references and broken rendering. Requires the docs toolchain: `pip install -e ".[docs]"`.
    *   `sandbox.py` is not part of the public API and is not rendered, so its docstrings are exempt from the cross-reference rules.

---

## 4. Git & Commits Rules
*   **Conventional Commits**: When asked to commit something, always construct commit messages using the Conventional Commits specification (e.g. `feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`).
