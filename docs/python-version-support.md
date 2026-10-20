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

### 2.2 PEP 798 unpacking in comprehensions (3.15) — `Starred` in `elt`. **Was a real hole; fixed.**

**This section previously concluded "benign". That was wrong, and the error is instructive.**

`[*a for a in rows]` parses to `ListComp(elt=Starred(...), generators=[...])`. The reasoning
was: the `Starred` lands in `elt`, `_check_comprehension` reads only `node.generators`, so the
bound on iteration is unaffected by what the element expression does. The second step does not
follow from the first. `[*xs for t in ts]` is sugar for `[x for t in ts for x in xs]` — `xs` is
**iterated**, and its length multiplies the result. So the unpacked expression is a second
iteration source that the bounds check simply never looked at:

```python
[x for _ in toks for x in _CONST_TUPLE]   # rejected: "iteration not bounded by the argument"
[*_CONST_TUPLE for _ in toks]            # was CERTIFIED — the same iteration, unchecked
```

Unpacking was therefore a way to iterate exactly what the `for`-clause check exists to forbid:
a module global, or a function local. The local case is the sharp one, because locals compose:

```python
def guard(toks):
    s1 = [*toks for _ in toks]   # len(toks) ** 2
    s2 = [*s1 for _ in toks]     # len(toks) ** 3
    s3 = [*s2 for _ in toks]     # len(toks) ** 4
```

`k` such lines iterate `O(len(toks) ** (k + 1))` — and certification grants the inline path,
*under the engine lock, with no timeout*. The pre-3.15 spelling of the same amplification
(`for x in s1`) was already rejected, so this was a regression in the termination guarantee
introduced purely by the new grammar, not a pre-existing gap.

**The fix** is `_unpacked_element`: `_check_comprehension` now bounds-checks the unpacked
expression against the same `params | targets` roots as every generator's `iter`, for all four
forms the PEP touches (`[*x …]`, `{*x …}`, `{**x …}`, `(*x …)`). A plain `elt` is still *not*
bounds-checked, because it is evaluated once per iteration rather than iterated.

**What the original testing got wrong**, since the lesson generalises: the test asserted the
rejection with `[*t.value for t in _ELSEWHERE]`, which puts `_ELSEWHERE` in the `iter`
position — the path that was already checked. It passed, and the passing test was read as
confirming the `elt` reasoning. Writing a test for the position you reasoned about is not the
same as writing one for the position you are worried about. The pin is now
`[*_ELSEWHERE for t in tokens]`.

Separately, and still true: the element expression *is* walked, so
`[*open(t.value) for t in tokens]` is rejected by both certification and the purity sandbox.
Admitting the container never stopped `ast.walk` descending into it. That part of the original
claim held; the bounds claim did not.

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

### 2.3 PEP 810 lazy imports (3.15) — the `is_lazy` field is benign; the **runtime proxy was not**.

The AST half of this feature is benign, and was the only half the first pass looked at. The
runtime half — what a deferred import leaves *in the module* — broke both inspecting modules.

#### 2.3.1 The AST half (benign)

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

#### 2.3.2 The runtime half — `types.LazyImportType` in `__globals__`. **Was broken; fixed.**

`lazy import m` leaves a `types.LazyImportType` proxy in the module dict. CPython resolves it
when `LOAD_GLOBAL` executes — **not** when a dict lookup hands it to Python. Both inspecting
modules read names through `certification._resolve_name`, i.e. `func.__globals__.get(name)`,
at *construction* time, before the callable has ever run. So they saw the proxy, and all three
consequences were silent:

| | eager `import sqlite3` | `lazy import sqlite3` (before the fix) |
| --- | --- | --- |
| `lint_callable` on a guard calling `sqlite3.connect` | `[('database', 'sqlite3.connect')]` | `[]` |
| `certify` of a guard calling a lazily-imported helper | `Verdict(True)` | `Verdict(False, "call to non-function 'helper'")` |
| `certify` of a guard reading a lazily-imported constant | `Verdict(True)` | `Verdict(False, "reads mutable/uncertified external 'MAX_BATCH'")` |

The linter column is the dangerous one: `lazy import` silently switched off hazard detection,
because the proxy resolves to no defining package, so the "resolve the symbol to the package
that defines it" strategy found `builtins` or nothing. A guard doing `sqlite3.connect()` in
enabling logic stopped being reported at all — a false negative in the advisory safety layer,
not merely a missed optimisation.

The certifier columns are false *rejections*, which are safe but expensive: they drop the
callable off the inline path (~90× per evaluation) and, for an arc `key`/`filter`, off its
place's key index. Worst of the three, though, was that **the verdict depended on call order**:
invoking the guard once resolved the proxy via `LOAD_GLOBAL`, so an identical callable
certified `False` before and `True` after. A safety verdict that is a function of unrelated
history is indefensible, and is why the fix resolves eagerly rather than reporting "unresolved".

**The fix** is `_force_lazy_import`, applied to both lookup paths in `_resolve_name` (globals
*and* closure cells). Getting at the proxy is awkward, and the awkwardness is the point: the
proxy is **deliberately transparent**, so there is no way to test for one —

```python
isinstance(v, types.LazyImportType)   # False, even as the first touch
type(v) is types.LazyImportType       # False
v.resolve()                           # AttributeError, from the *resolved* object
repr(v)                               # "<lazy_import 'h.MAX'>"  <- the only non-invasive read
```

`isinstance` and `type()` report the eventual object, and any attribute access resolves and
forwards. The fix therefore calls the **unbound** `types.LazyImportType.resolve` descriptor,
which bypasses that forwarding; a `TypeError` means the value was never a proxy (the common
case), and an `ImportError` from a deferred import that cannot be satisfied is reported as
`_UNRESOLVED` rather than raising on a construction path that must not raise. After the fix,
every cell in the table above matches the eager column exactly, and the order dependence is
gone — both pinned against an eager control in `tests/test_new_grammar_support.py`.

Two honest caveats. **This defeats the deferral** for names a guard actually mentions:
`_resolve_name` is only reached for names in the callable's own AST, and a guard referencing a
module needs it imported when it fires anyway, so the cost is narrow and deliberate — but it
is real, and a user who marked an import lazy specifically to avoid importing it will still pay
for it if a guard names it. **And this is the part least verifiable from 3.15.0b4**: the proxy
mechanics above are beta observations, and b4 already proved divergent from the release once
(§2.3.1). The fix is written defensively — no `types.LazyImportType`, no behaviour change; a
`TypeError` is treated as "not a proxy" — so the worst case on a changed final release is that
resolution silently stops happening and the pre-fix symptoms return, rather than an exception
on a construction path. CI on the real 3.15.0 is the authoritative check.

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
* **PEP 649/749 deferred annotation evaluation (3.14).** No behaviour change, but the earlier
  wording here ("cpnx strips annotations at the AST level, so lazy evaluation cannot change a
  verdict") was too broad: it describes `certification._strip_annotations` and overlooked that
  `transitions._reject_non_bool_return` calls `typing.get_type_hints(value)` at *runtime*, on
  every `Transition.guard`, `InputArc.filter` and `OutputArc.condition` assignment. Under PEP
  649 that call is now what triggers `value.__annotate__()`, so cpnx is the thing forcing
  evaluation rather than merely reading an already-evaluated `__annotations__`. `sandbox.py`
  and `linting.py` do not strip annotations at all; they do not need to, since neither reads
  an annotation's *value* — only certification walks names that an annotation could introduce.

  One consequence is worth stating because it looks like a bug: an unresolvable parameter
  annotation (a `TYPE_CHECKING`-only import, a forward reference) makes `get_type_hints` raise
  `NameError` before it can inspect `"return"`, so a genuinely wrong `-> int` on that callable
  is *not* rejected. That is the documented, deliberate contract — `_reject_non_bool_return`
  lists "Unresolvable → pass" among its conservative cases, because the annotation is advisory
  and must never break construction. It is also **not** a 3.14 change: `get_type_hints` raises
  the same `NameError` on 3.13, 3.14 and 3.15 for the same callable, so PEP 649 moves *when*
  annotations are evaluated without changing this outcome.
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

**`frozendict` is now *recognised*, though, which is a different question from adopting it.**
`certification._is_immutable` decides what a certified callable may close over, and it accepted
only scalars, `frozenset` and recursive `tuple`s — so a guard closing over a frozen lookup
table was rejected as reading "mutable/uncertified external" state. That hit the idiom the
`InputArc`/`Transition` docs actively recommend (read immutable data once at construction
time), and the penalty was invisible: the callable silently lost the inline path, and an arc
`key`/`filter` silently lost its place's key index. Both `FrozenDict` and, on 3.15+, builtin
`frozendict` are now accepted — **by recursing into keys and values, not by trusting the
type**, because neither is deeply immutable on its own: `frozendict` is shallow by design, and
`FrozenDict` freezes nested `dict`/`list` values but admits *any hashable* value, so an object
with mutable attributes and a `__hash__` passes straight through. Recursion keeps the
rejections that matter (verified, not just asserted: a shallow `frozendict` over a live `dict`,
and either type holding a hashable-but-mutable object, are all still rejected).

One inconsistency this leaves, flagged rather than changed: `frozenset` is still trusted as a
leaf, and a `frozenset` can hold that same hashable-but-mutable object. Tightening it would be
the consistent move, but it would de-certify callables that pass today, so it belongs in its
own change rather than riding along here.

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

Certification's correctness under a *future* grammar rests on the same structural argument
each time — that an unmatched node class is inert, and that `ast.walk` still reaches the
expressions inside it. Both held for 3.14 and 3.15 and are now pinned by tests. What is *not*
pinned is a construct nobody has thought of: a future node that is both unmatched **and**
capable of binding or iterating something, in a position no existing test covers, reproduces
the same shape of gap.

That is not hypothetical, which is the main lesson of this release. PEP 798 **was** exactly
such a construct, and the first pass through this document called it benign (§2.2). Two things
produced that miss, and both are worth naming because the diff-the-`_fields` method does not
catch either:

1. **A node can iterate without being a generator.** The audit asked "does any node class or
   field change?", saw only `Starred` in a new position, and stopped. The question that
   mattered was "does anything new get *iterated*?" — and `_fields` diffing cannot answer it,
   because the node class was old and the field was old. Only reading the PEP's desugaring
   (`[*xs for t in ts]` → `for x in xs`) answers it.
2. **A test can confirm the wrong position.** The pin was written for the `iter` position, which
   was already checked, and it passed — which read as confirmation of a claim about the `elt`
   position. A test that exercises the path you reasoned *about* rather than the path you are
   *worried about* is worse than no test, because it converts an open question into a settled
   one.

So the honest statement of residual risk is: the mechanical checks here (diff `_fields`, run
the suite on every interpreter, `-W error`) are necessary and were not sufficient. They
established that nothing *crashes*; they could not establish that nothing new is *admitted*.
For a whitelist proof, every release still needs the question asked deliberately, per feature:
*can this construct bind a name, iterate a sequence, or defer a lookup?* PEP 798 could iterate
and PEP 810 could defer a lookup; both slipped through a pass that only looked for new node
classes.
