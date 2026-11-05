import pytest

from gateadapt import FeatureSpec, Task, evaluate, infer_spec, parse, validate
from gateadapt.scope import dumps, from_jsonable, to_jsonable

SPEC = FeatureSpec(
    fields={"tags.type": str, "tags.domain": str, "tags.n": int, "tags.flag": bool}
)


def feats(**tags):
    return Task(id="t", tags=tags).features


class TestEvaluate:
    def test_all(self):
        assert evaluate(("all",), feats())

    def test_eq_dotted(self):
        f = feats(type="refusal")
        assert evaluate(("eq", "tags.type", "refusal"), f)
        assert not evaluate(("eq", "tags.type", "answer"), f)

    def test_missing_field_is_false(self):
        f = feats()
        assert not evaluate(("eq", "tags.type", "refusal"), f)
        assert evaluate(("neq", "tags.type", "refusal"), f)
        assert not evaluate(("ge", "tags.n", 3), f)

    def test_numeric_ops(self):
        f = feats(n=4)
        assert evaluate(("ge", "tags.n", 4), f)
        assert evaluate(("gt", "tags.n", 3), f)
        assert not evaluate(("lt", "tags.n", 4), f)
        assert evaluate(("le", "tags.n", 4.0), f)

    def test_in(self):
        f = feats(domain="acme")
        assert evaluate(("in", "tags.domain", ("acme", "nimbus")), f)
        assert not evaluate(("in", "tags.domain", ("other",)), f)

    def test_bool_vs_numeric(self):
        # bool is not numeric: ordered ops must not fire on flags
        f = feats(flag=True)
        assert evaluate(("eq", "tags.flag", True), f)
        assert not evaluate(("ge", "tags.flag", 1), f)

    def test_and_or_not(self):
        f = feats(type="refusal", domain="acme")
        assert evaluate(("and", ("eq", "tags.type", "refusal"), ("eq", "tags.domain", "acme")), f)
        assert not evaluate(
            ("and", ("eq", "tags.type", "answer"), ("eq", "tags.domain", "acme")), f
        )
        assert evaluate(("or", ("eq", "tags.type", "answer"), ("eq", "tags.domain", "acme")), f)
        assert not evaluate(("not", ("eq", "tags.domain", "acme")), f)


class TestValidate:
    def test_ok(self):
        assert validate(("and", ("eq", "tags.type", "refusal"), ("ge", "tags.n", 2)), SPEC) == []

    def test_unknown_field_rejected(self):
        errs = validate(("eq", "tags.mystery", "x"), SPEC)
        assert errs and "unknown feature" in errs[0]

    def test_ordered_op_on_str_rejected(self):
        errs = validate(("ge", "tags.type", "a"), SPEC)
        assert errs and "non-numeric" in errs[0]

    def test_wrong_type_rejected(self):
        errs = validate(("eq", "tags.type", 3), SPEC)
        assert errs

    def test_numeric_fields_accept_int_and_float(self):
        assert validate(("ge", "tags.n", 2.5), SPEC) == []

    def test_in_needs_tuple_of_field_type(self):
        assert validate(("in", "tags.type", ("a", "b")), SPEC) == []
        errs = validate(("in", "tags.type", ("a", 3)), SPEC)
        assert errs

    def test_compound_errors_propagate(self):
        errs = validate(("and", ("eq", "tags.type", "x"), ("eq", "bogus", 1)), SPEC)
        assert errs


class TestDSL:
    @pytest.mark.parametrize(
        "text",
        [
            "all",
            'tags.type == "refusal"',
            "tags.domain != 'acme'",
            'and(tags.type == "refusal", or(tags.domain == "acme", tags.n >= 4))',
            "not(tags.flag == true)",
            'tags.domain in ["acme", "nimbus"]',
        ],
    )
    def test_parse_roundtrip_eval(self, text):
        ast = parse(text)
        assert isinstance(ast, tuple)
        # deterministic serialization round-trips
        assert from_jsonable(to_jsonable(ast)) == ast
        assert isinstance(evaluate(ast, feats(type="refusal", domain="acme", n=5, flag=False)), bool)

    def test_parse_eq(self):
        assert parse('tags.type == "refusal"') == ("eq", "tags.type", "refusal")

    def test_parse_in(self):
        assert parse('tags.domain in ["acme", "nimbus"]') == (
            "in",
            "tags.domain",
            ("acme", "nimbus"),
        )

    def test_parse_errors(self):
        for bad in ["", "tags.type", "tags.type ==", "and()", "and(x)" "]", 'type == "a",']:
            with pytest.raises(Exception):
                parse(bad)

    def test_dumps_deterministic(self):
        a = parse('and(tags.type == "a", tags.n >= 2)')
        assert dumps(a) == dumps(a)


class TestInferSpec:
    def test_infers_tag_fields(self):
        tasks = make = [
            Task(id="1", tags={"type": "refusal", "n": 3}, data={"p": 0.5, "meta": {"x": 1}}),
            Task(id="2", tags={"type": "answer", "n": 1}, data={"p": 0.7}),
        ]
        spec = infer_spec(tasks)
        assert spec.fields["tags.type"] is str
        assert spec.fields["tags.n"] is int
        assert spec.fields["p"] is float
        assert spec.fields["meta.x"] is int
