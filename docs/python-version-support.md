# Python version support — 3.14 and 3.15

Engineering record for the release that promoted **3.14** to a blocking CI entry and added
**3.15** as the experimental one. Not part of the rendered documentation site (see
`exclude_docs` in `mkdocs.yml`); the user-facing claim is the trove classifier list in
`pyproject.toml`.

`requires-python = ">=3.10"` and `[tool.ruff] target-version = "py310"` are **unchanged** —
the floor did not move, and nothing below was adopted into shipped code.

## 1. Why a new Python matters more to cpnx than to most libraries

cpnx has **no runtime dependencies**, so the usual dependency-compatibility risk is zero.
The exposure is elsewhere: three modules introspect Python's own syntax, and until this
release there was **no `sys.version_info` gating anywhere in `src/`**.

| module | AST surface | other introspection |
| --- | --- | --- |
| `cpnx/certification.py` | 34 distinct `ast.*` names, 46 references | `inspect.getsourcelines`, `getsourcefile`, `func.__code__` |
| `cpnx/sandbox.py` | 17 distinct `ast.*` names | `inspect.getsource{,file,lines}` |
| `cpnx/linting.py` | 7 distinct `ast.*` names | `inspect.currentframe` + `f_back` walk, `code.co_filename`, `ast.unparse` |

All three decide by matching on node **classes**. That makes exactly two kinds of release
change interesting:

1. **A new node class** — unmatched, so it falls through to the permissive default.
2. **A new field on an existing class** — invisible to a class-based match.

Both are *safe* in the sense of not crashing, and that is precisely the hazard:
`cpnx.certification` is a safety boundary where a construct that is silently **admitted**
as inline-safe is worse than one that raises. A false "certified" grants a callable the
inline path — under the engine lock, with no timeout.

So the question for each release is not "does the suite pass" but "did the grammar grow a
way to smuggle unbounded iteration or an escape past the walk".

## 2. What actually changed in the AST

Measured by enumerating `ast.AST` subclasses and their `_fields` on each interpreter, then
diffing — not read off a changelog:

```text
3.13.16 -> 3.14.6
  NEW node classes: ['Interpolation', 'TemplateStr']      # PEP 750 t-strings
  REMOVED node classes: ['_ast_Ellipsis']                 # private alias, never referenced

3.14.6 -> 3.15.0
  FIELDS CHANGED Import:     ['names'] -> ['names', 'is_lazy']                        # PEP 810
  FIELDS CHANGED ImportFrom: ['module','names','level'] -> [...,'is_lazy']            # PEP 810
  (no new or removed node classes)
```

The headline is counter-intuitive: **the grammar risk is concentrated in 3.14, not 3.15.**
3.14 introduced two genuinely new node classes; 3.15 added two fields and no classes at all.
3.15's other syntax feature (PEP 798) reuses `Starred` in a new position rather than adding
a node. Since 3.14 was the version still running `continue-on-error`, its failures were
invisible — which is why it was the larger of the two gaps.

### 2.1 PEP 750 template strings (3.14) — new node classes. Benign.

`t'{x}'` parses to `TemplateStr(values=[Interpolation(...)])`. Neither class is matched by
`_check_structural`, so a t-string guard falls through to `Verdict(True)`.

That is the right answer, and for a reason worth stating: a t-string **defers** formatting
(it builds a `string.templatelib.Template`) instead of calling `__format__` eagerly. So it is
strictly tamer than the f-string (`JoinedStr`/`FormattedValue`) that cpnx already admits —
an f-string invokes payload `__format__` inline, which is the documented residual assumption
in `certification`'s module docstring; a t-string does not even do that.

The load-bearing half is that admitting the *container* must not stop the walk reaching
inside it. Verified: a guard whose interpolation calls `open()` is rejected by **both**
`certify` (`call to unresolved name 'open'`) and `verify_callable_purity` (`PermissionError`).
`ast.walk` descends into `Interpolation.value` normally. Pinned by
`tests/test_new_grammar_support.py`.

### 2.2 PEP 798 unpacking in comprehensions (3.15) — `Starred` in `elt`. Benign.

`[*a for a in rows]` parses to `ListComp(elt=Starred(...), generators=[...])`. The `Starred`
lands in `elt`, a position the certifier does not inspect: `_check_comprehension` reads only
`node.generators`, checking each `iter` is rooted at a parameter or an in-comprehension
target. The bound on iteration is unaffected by what the element expression does, so the
termination proof still holds.

The original analysis of this feature stopped at the list-comprehension case and left the
conclusion resting on a reasoning step ("a `Starred` in `elt` cannot smuggle anything past
the walk") rather than an observation. Both gaps are now closed:

* **Every form PEP 798 touches** is checked, not just `ListComp` — the PEP also covers set
  comprehensions, dict comprehensions (`**`), generator expressions and async generator
  expressions. All stay correctly bounded.
* **The reasoning step is now an observation.** `[*t.value for t in _ELSEWHERE]` is rejected
  (`iteration not bounded by the argument`), and `[*open(t.value) for t in tokens]` is
  rejected by both certification and the purity sandbox. The element expression *is* walked.

One asymmetry found along the way and worth recording, because it would trip anyone adding a
`DictComp` branch later: `{**d for d in ds}` parses with the unpacked expression in **`key`**
and **`value` set to `None`** —

```python
{**d for d in ds}  # DictComp(key=Name('d'), value=None, generators=[...])
{**d}              # Dict(keys=[None], values=[Name('d')])   <- the inverse convention
```

so `DictComp.value` can now be `None` where it was always an expression node. cpnx is
unaffected (nothing reads `DictComp.value`, and `ast.walk`/`iter_child_nodes` skip `None`
children), but the two unpacking spellings put the `None` on opposite sides. Pinned.

### 2.3 PEP 810 lazy imports (3.15) — the `is_lazy` field. Benign.

`ast.Import` and `ast.ImportFrom` gained `is_lazy`. Both cpnx call sites match on the node
class, so the field is invisible to them:

* `certification.py:336` — `isinstance(node, (ast.Import, ast.ImportFrom))` → `Verdict(False, "import")`
* `sandbox.py:97` — `case ast.Import() | ast.ImportFrom():` (a class pattern with no
  sub-patterns, so added fields do not affect the match)

Rejecting a lazy import is also the correct answer on the merits: a deferred import in
enabling logic is still an import.

**A correction to the earlier research.** PEP 810 permits `lazy` only at **module scope** —
inside a function, class body, or `try`/`except`/`finally` it is a `SyntaxError`, as is
`lazy from ... import *`. The 3.15.0b4 parser does **not** enforce this (it accepts all four
cases), so a beta-based probe gives a misleading picture. The consequence is practical: a
regression test shaped as `ast.parse("def guard():\n    lazy import json\n")` would raise
`SyntaxError` on real 3.15.0 rather than yield a `Verdict`. The test therefore pins
`_check_structural` against a **module-scope** lazy import node, plus the case that actually
reaches cpnx — a lazy import in a guard's *file*, which certification parses whole and must
still isolate the guard from.

`lazy` is a **soft** keyword: `lazy = 1`, `def f(lazy)`, `lazy.foo = 2` and `import lazy` all
still parse. No existing identifier breaks.

## 3. Breaking changes — audited against cpnx

Verified against `Doc/whatsnew/3.15.rst` at tag `v3.15.0` in the CPython repository (the
source of the published page; `docs.python.org` is unreachable from the CI/dev container, and
secondary blog summaries proved unreliable on at least the PEP 810 scoping point above).

**Removals: none reachable.** cpnx imports none of `sre_compile`/`sre_constants`/`sre_parse`,
`http.server` (CGI handler, `--cgi`), `datetime.utcnow`, `typing.no_type_check_decorator`,
`typing.NamedTuple` keyword form, `typing.ByteString`, `collections.abc.ByteString`,
`ctypes.SetPointerType`, `platform.java_ver`, `importlib.resources.read_text`/`read_binary`,
`glob.glob0`/`glob1`, `pathlib.PurePath.is_reserved`, `Loader.load_module`,
`zipimport.load_module`, `codeobject.co_lnotab`, or the removed `wave` marker methods. The
removed `ast` aliases (`Num`, `Str`, `Bytes`, `NameConstant`, `Ellipsis`) went in **3.14** and
are not referenced either — cpnx already uses `ast.Constant`.

One near-miss worth naming: `datetime.utcnow` **was** removed in 3.15, and `utcnow` does
appear in `linting.py` — but only as a *string* in the nondeterminism detection table
(`"datetime": frozenset({"now", "utcnow", "today"})`). It is a symbol cpnx looks for in
*user* code, never one it calls. The entry stays correct and useful: cpnx supports 3.10+, so
user guards on those versions can still call it.

**Deprecations: none triggered.** The one with cpnx's name on it is
`ast.AST`/`ast.expr` instantiation (deprecated 3.15, an error in 3.20). Every such use in
cpnx is a **type annotation** — `certification.py` (12 sites), `linting.py` (3),
`sandbox.py` (6) — which is never evaluated at runtime. The only node cpnx *constructs* is
`ast.Constant(value=None)` in `_strip_annotations` (`certification.py:326`), which is a
concrete class with its required field supplied. This also covers the related 3.15 change
that AST constructors now raise `TypeError` (was `DeprecationWarning` since 3.13) on a
missing required argument or an unknown keyword.

Proven rather than argued: the full suite passes under
`-W error::DeprecationWarning -W error::SyntaxWarning -W error::PendingDeprecationWarning
-W error::EncodingWarning` on both 3.14.6 and 3.15.0b4. All 48 warnings the suite emits are
cpnx's **own** `CpnxLintWarning` (8 distinct call sites), identical in count *and category*
across every interpreter — so no test silently changed category, got skipped, or started
tripping a Python-level deprecation.

**Behaviour changes: none reachable.**

* **UTF-8 as the default encoding (PEP 686).** All three `open()` calls already pass
  `encoding="utf-8"` explicitly (`certification.py:126`, `linting.py:373`,
  `sandbox.py:116`), so cpnx's behaviour is identical with `PYTHONUTF8=0`. The suite is clean
  under `-W error::EncodingWarning`.
* **PEP 765 — control flow in `finally`** now raises `SyntaxWarning`. cpnx has one `finally`
  block (`places.py:135`) and its `return` is *after* the block, not inside it. No warning.
* **Frame-walk attribution.** `linting._assignment_stacklevel()` returns the same value on
  3.13, 3.14 and 3.15 (`1` from a non-internal caller; `_FALLBACK_STACKLEVEL = 3`), and
  `_PACKAGE_ROOT` resolves identically. `tests/test_linting.py` passes on all three.
* **PEP 649/749 deferred annotation evaluation (3.14).** cpnx strips annotations at the *AST*
  level in `_strip_annotations`, before any evaluation, so lazy evaluation of annotations
  cannot change a verdict. Unaffected.
* **`unittest.assertWarns` no longer swallows non-matching warnings.** cpnx's suite uses
  `pytest.warns`, not `assertWarns`.

## 4. Evidence

Full gate on five interpreter configurations. `881` is the pre-existing suite; the `10` added
tests are version-gated (3 at 3.14+, 7 at 3.15+), which is why the totals differ by version
rather than indicating a discrepancy.

| configuration | `pytest tests/` | `ruff check` | radon `--min C` | radon average |
| --- | --- | --- | --- | --- |
| 3.13.16 (baseline) | 881 passed, 10 skipped, 48 warnings | clean | clean | A (3.709) |
| 3.14.6 | 884 passed, 7 skipped, 48 warnings | clean | clean | A (3.709) |
| 3.14.6 free-threaded | 884 passed, 7 skipped, 48 warnings | clean | clean | A (3.709) |
| 3.15.0b4 | 891 passed, 48 warnings | clean | clean | A (3.709) |
| 3.15.0b4 free-threaded | 891 passed, 48 warnings | clean | clean | A (3.709) |

### Confirmed on the real 3.15.0 in CI

The local 3.15 runs above used `3.15.0b4`, the newest interpreter `uv 0.11.32`'s download
index offers. That was strong evidence rather than proof, and deliberately so: b4 is past
feature freeze, but it is demonstrably *not* behaviourally identical to the release, since it
fails to enforce PEP 810's module-scope restriction (§2.3).

CI closes that gap. `actions/setup-python@v5` resolves `python-version: "3.15"` to the newest
**stable** entry in its version manifest, which is **3.15.0 final** (`"3.14"` likewise
resolves to 3.14.8 — newer than the 3.14.6 tested locally). On that interpreter the suite
reports **891 passed, 48 warnings**, identical to the local b4 run and with the same eight
`CpnxLintWarning` call sites, and `ruff` and the radon gate are clean.

Worth knowing when reading CI for this matrix: the 3.15 job runs under `continue-on-error`,
which reports a **failing** pytest step as `conclusion: success`. The job's conclusion field
therefore cannot be used to judge the experimental entry — the step's log has to be read for
the actual pytest summary line. The result above is from the log, not the conclusion.

3.15 stays the `experimental` entry despite being green on the real release: the point of
that slot is that a *future* 3.15.x patch release regressing cpnx should not block an
unrelated PR, and 3.15 has had one patch release's worth of field exposure. Promote it to
blocking on the same reasoning that promoted 3.14 — once it has been stable long enough that
silent failures are the bigger risk.

### Reproducing

```bash
uv python install 3.15.0b4 3.14.6            # check `uv python list | grep 3.15` for final first
uv venv --python 3.15.0b4 /tmp/venv315
uv pip install --python /tmp/venv315/bin/python -e ".[dev]"   # the venv has no pip; use uv
/tmp/venv315/bin/python -m pytest tests/ -q
/tmp/venv315/bin/python -m ruff check src/ tests/
/tmp/venv315/bin/python -m radon cc src/cpnx --min C -s       # empty output = pass
```

## 5. New features assessed for cpnx — and why none were adopted

The floor is 3.10, so anything version-specific costs a `sys.version_info` branch plus a
fallback path in modules that currently have **neither**. That bar is deliberately high:
`certification.py` and `sandbox.py` are the proof surface, and a second code path through
them doubles what has to be argued correct. Each feature below is judged against it.

**`frozendict` builtin (PEP 814, 3.15) — not a replacement for `cpnx.tokens.FrozenDict`.**
The obvious candidate, and it does not fit. cpnx's `FrozenDict` freezes **recursively**
(nested `dict`/`Mapping` → `FrozenDict`, nested `list` → `tuple`) and hashes eagerly at
construction so an unhashable value is rejected at the boundary rather than at first `hash()`.
PEP 814's `frozendict` is **shallow**: `frozendict({"a": {"b": 1}})` still holds a mutable
`dict`, which is precisely the aliasing a token payload must not have. It is also not a
`dict` subclass. So it is not a drop-in, and the deep-freeze and eager-hash semantics are the
load-bearing parts. Revisit only if a leaf-only fast path is ever measured to matter.

**Free-threading (PEP 779 official in 3.14) — the most relevant item, and the one worth
following up.** cpnx is a threading library: a `ThreadPoolExecutor`, an engine lock, cooling
heaps. It is the natural beneficiary of a GIL-free build, and also what such a build is most
likely to break. Tested rather than assumed: the full suite passes on **both** free-threaded
builds (`Py_GIL_DISABLED=1`), with the same 884/891 counts and the same 48 warnings as the
GIL builds. That is real evidence the lock discipline holds without the GIL's incidental
protection.

It is deliberately **not** claimed here. A passing suite under free-threading is not the same
as a free-threading support guarantee: the suite was written against GIL semantics, and a
data race it does not exercise would go unseen. Adding `3.14t`/`3.15t` matrix entries is the
right next step and belongs in its own PR, where a failure means something specific.

**Not adopted, with reasons:**

* **PEP 810 lazy imports** — cpnx has zero runtime dependencies, so there is no import cost
  to defer. `linting.py` is already imported lazily *inside a function* to avoid import-time
  coupling, which the comment at its call site explains; `lazy import` would express the same
  thing more clearly but only on 3.15+, replacing a portable idiom with a gated one.
* **PEP 798 unpacking in comprehensions** — readability sugar. Using it anywhere in `src/`
  would raise the floor to 3.15 for a cosmetic gain.
* **PEP 750 t-strings** — a deferred-formatting template type; cpnx's warning and error
  messages are built eagerly and f-strings are correct for that.
* **PEP 661 `sentinel` builtin** — cpnx uses two module-level sentinels
  (`certification._UNRESOLVED`, `engine._DRAIN`). `object()` is two lines, works on 3.10, and
  the builtin's advantages (a nicer `repr`, picklability) do not apply to values that never
  escape the module.
* **`ast.parse(..., module_name=...)` (3.15)** — lets a syntax warning be attributed to a
  module name. cpnx's three `ast.parse` calls read *user* source for inspection, do not emit
  syntax warnings, and already pass `filename=`.
* **`profiling.sampling` / Tachyon (PEP 799, 3.15)** — potentially useful against
  `benchmarks/`, but a developer tool choice, not a library change. Note `profile` is
  deprecated for removal in 3.17; cpnx imports neither it nor `cProfile`.
* **`ast.dump(color=True)`, `inspect.getdoc` parameters, `json` `array_hook`,
  `slice` genericity, `bytearray.take_bytes`** — no call site in cpnx.

## 6. Residual risk

The gap this release does not close: certification's correctness under a *future* grammar
rests on the same structural argument each time — that an unmatched node class is inert and
that `ast.walk` still reaches the expressions inside it. Both held for 3.14 and 3.15, and
both are now pinned by tests, so the next release that breaks either fails loudly instead of
silently widening what counts as inline-safe. What is *not* pinned is a construct nobody has
thought of: a future node that is both unmatched **and** capable of binding or iterating
something, in a position no existing test covers, would reproduce the same shape of gap. The
mitigation is the one applied here — enumerate `_fields` across versions and diff, rather
than read the changelog and trust it.
