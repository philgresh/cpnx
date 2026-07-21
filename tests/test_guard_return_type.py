"""Tests for the construction-time CPN guard contract check ``Type[G(t)] = Bool``.

``cpnx.transitions._reject_non_bool_return`` enforces that a boolean-predicate
callable's *annotated* return type resolves to ``bool`` (or something that can be
``bool``, e.g. a union containing ``bool``) before it is wired into ``Transition.guard``
or ``OutputArc.expression``. It is deliberately conservative: unannotated callables
(all lambdas), ``Any``, bool-containing unions, and unresolvable annotations all pass;
only an unambiguous non-bool annotation raises ``TypeError``. It is NOT applied to
``InputArc.expression`` (which legitimately returns ``list[Token]``) nor to
``binding_priority_key``.

All annotated ``def`` guards/predicates are defined at module level (never nested
inside a test function) so ``verify_callable_purity``'s ``inspect.getsource`` call can
recover real source, per the certifier's requirements.
"""

from typing import Annotated, Any, Literal, Optional

import pytest

from cpnx.tokens import Token
from cpnx.transitions import InputArc, OutputArc, Transition

# ---------------------------------------------------------------------------
# Module-level annotated guards/predicates used across the acceptance cases.
# ---------------------------------------------------------------------------


def guard_bool(toks: list[Token]) -> bool:
    return bool(toks)


def guard_bool_or_none(toks: list[Token]) -> bool | None:
    return bool(toks) or None


def guard_optional_bool(toks: list[Token]) -> Optional[bool]:
    return bool(toks) or None


def guard_any(toks: list[Token]) -> Any:
    return bool(toks)


def guard_unresolvable_forward_ref(toks: list[Token]) -> "NotARealType":  # noqa: F821
    return bool(toks)


def guard_annotated_bool(toks: list[Token]) -> Annotated[bool, "doc"]:
    return bool(toks)


def guard_literal_true(toks: list[Token]) -> Literal[True]:
    return True


def guard_literal_false(toks: list[Token]) -> Literal[False]:
    return False


def output_predicate_unannotated(toks):
    return bool(toks)


def output_predicate_bool(toks: list[Token]) -> bool:
    return bool(toks)


def output_predicate_literal_true(toks: list[Token]) -> Literal[True]:
    return True


def ie(toks: list[Token]) -> list[Token]:
    return toks


# ---------------------------------------------------------------------------
# Module-level annotated guards/predicates used in the rejection cases.
# ---------------------------------------------------------------------------


def guard_int(toks: list[Token]) -> int:
    return len(toks)


def guard_str(toks: list[Token]) -> str:
    return "yes" if toks else "no"


def guard_none(toks: list[Token]) -> None:
    return None


def guard_list_tokens(toks: list[Token]) -> list[Token]:
    return toks


def guard_literal_int(toks: list[Token]) -> Literal[1]:
    return 1


def output_predicate_int(toks: list[Token]) -> int:
    return len(toks)


def _build_transition(guard=None, binding_priority_key=None) -> Transition:
    return Transition(
        name="t",
        inputs=[InputArc("a")],
        outputs=[OutputArc("b")],
        action=lambda toks: toks,
        guard=guard,
        binding_priority_key=binding_priority_key,
    )


class TestGuardReturnTypeAcceptance:
    """Constructions that must succeed (no raise) under the guard contract check."""

    def test_bool_annotated_guard_constructs(self):
        _build_transition(guard=guard_bool)

    def test_unannotated_lambda_guard_constructs(self):
        _build_transition(guard=lambda toks: bool(toks))

    def test_bool_or_none_union_guard_constructs(self):
        _build_transition(guard=guard_bool_or_none)

    def test_optional_bool_guard_constructs(self):
        _build_transition(guard=guard_optional_bool)

    def test_any_annotated_guard_constructs(self):
        _build_transition(guard=guard_any)

    def test_unresolvable_forward_ref_guard_constructs(self):
        # get_type_hints raises NameError internally (no "NotARealType" symbol exists);
        # the helper swallows that failure and lets construction proceed.
        _build_transition(guard=guard_unresolvable_forward_ref)

    def test_annotated_bool_guard_constructs(self):
        # typing.get_type_hints strips Annotated[bool, ...] down to bool.
        _build_transition(guard=guard_annotated_bool)

    def test_literal_true_guard_constructs(self):
        # Literal[True] is a valid boolean predicate return type.
        _build_transition(guard=guard_literal_true)

    def test_literal_false_guard_constructs(self):
        # Literal[False] is a valid boolean predicate return type.
        _build_transition(guard=guard_literal_false)


class TestOutputArcReturnTypeAcceptance:
    """`OutputArc.expression` acceptance mirrors the guard acceptance cases."""

    def test_unannotated_lambda_expression_constructs(self):
        OutputArc("b", expression=output_predicate_unannotated)

    def test_bool_annotated_expression_constructs(self):
        OutputArc("b", expression=output_predicate_bool)

    def test_literal_true_expression_constructs(self):
        # Literal[True] is a valid boolean predicate return type.
        OutputArc("b", expression=output_predicate_literal_true)


class TestGuardReturnTypeRejection:
    """Constructions that must raise `TypeError` with a `must return bool` message."""

    def test_int_annotated_guard_raises(self):
        with pytest.raises(TypeError, match="must return bool"):
            _build_transition(guard=guard_int)

    def test_str_annotated_guard_raises(self):
        with pytest.raises(TypeError, match="must return bool"):
            _build_transition(guard=guard_str)

    def test_none_annotated_guard_raises(self):
        with pytest.raises(TypeError, match="must return bool"):
            _build_transition(guard=guard_none)

    def test_list_tokens_annotated_guard_raises(self):
        with pytest.raises(TypeError, match="must return bool"):
            _build_transition(guard=guard_list_tokens)

    def test_literal_non_bool_guard_raises(self):
        # A Literal of non-bool values (e.g. Literal[1]) is not a boolean predicate.
        with pytest.raises(TypeError, match="must return bool"):
            _build_transition(guard=guard_literal_int)

    def test_reassignment_of_bad_guard_raises(self):
        transition = _build_transition(guard=guard_bool)
        with pytest.raises(TypeError, match="must return bool"):
            transition.guard = guard_int


class TestOutputArcReturnTypeRejection:
    """`OutputArc.expression` rejection mirrors the guard rejection cases."""

    def test_int_annotated_expression_raises(self):
        with pytest.raises(TypeError, match="OutputArc.expression must return bool"):
            OutputArc("b", expression=output_predicate_int)

    def test_reassignment_of_bad_expression_raises(self):
        arc = OutputArc("b", expression=output_predicate_bool)
        with pytest.raises(TypeError, match="OutputArc.expression must return bool"):
            arc.expression = output_predicate_int


class TestReturnTypeScope:
    """The check must not bleed into callables it is not meant to cover."""

    def test_input_arc_expression_returning_list_of_tokens_constructs(self):
        # InputArc.expression legitimately returns list[Token]; never bool-checked.
        InputArc("a", expression=ie)

    def test_bool_guard_with_valid_binding_priority_key_constructs(self):
        _build_transition(guard=guard_bool, binding_priority_key=lambda toks: 0)
