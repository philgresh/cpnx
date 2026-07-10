# Contributing to cpnx

Thanks for your interest in improving cpnx! This guide covers the essentials; the
full project conventions (including the concurrency/thread-safety invariants that
matter most in this codebase) live in [`.agents/AGENTS.md`](.agents/AGENTS.md).

## Development setup

cpnx has no runtime dependencies. Install the dev toolchain into a virtualenv:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Requires Python 3.10+ (use `A | B` union syntax, not `Union[A, B]`).

## Before you open a PR

Run the same checks CI runs:

```bash
make format   # ruff format + autofix
make lint     # ruff check (no autofix)
make test     # pytest
```

Add or update tests for any behavior you change.

## Documentation

Public API docstrings are rendered on the [docs site](https://philgresh.github.io/cpnx/)
by [mkdocstrings](https://mkdocstrings.github.io/), so **docstrings are API
documentation**. When you touch a public class, method, function, or constant:

- Write **Google-style** docstrings (`Args:`, `Returns:`, `Raises:`, `Attributes:`,
  `Example:`) with a specific, self-contained first line.
- Bodies render as **Markdown, not reStructuredText**. Do not use Sphinx roles like
  `` :meth:`x` ``; cross-reference public symbols with autorefs
  (`` [`step`][cpnx.PetriNet.step] ``) and use plain backticks for everything else.
- Verify locally:

  ```bash
  pip install -e ".[docs]"
  make docs-serve   # live preview at http://127.0.0.1:8000/cpnx/
  make docs-build   # mkdocs build --strict — fails on broken cross-refs/rendering
  ```

`make docs-build` runs in CI and must pass. See the *Docstring Guidelines* in
[`.agents/AGENTS.md`](.agents/AGENTS.md) for the complete rules.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
`docs:`, `chore:`, …). Releases are cut by pushing a `vX.Y.Z` tag, which publishes to
PyPI and deploys the versioned docs.
