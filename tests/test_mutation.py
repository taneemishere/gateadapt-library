from gateadapt import Mutation, Task
from gateadapt.mutation import apply_patch, deep_merge, effective_config, expand_patch


class TestPatch:
    def test_dotted_keys_expand(self):
        assert expand_patch({"retrieval.min_score": 0.35}) == {
            "retrieval": {"min_score": 0.35}
        }

    def test_multi_level_dotted(self):
        assert expand_patch({"a.b.c": 1, "a.b.d": 2}) == {"a": {"b": {"c": 1, "d": 2}}}

    def test_deep_merge_preserves_siblings(self):
        base = {"retrieval": {"top_k": 4, "min_score": 0.15}, "other": True}
        out = apply_patch(base, {"retrieval.min_score": 0.35})
        assert out == {"retrieval": {"top_k": 4, "min_score": 0.35}, "other": True}
        assert base["retrieval"]["min_score"] == 0.15  # base untouched

    def test_scalar_overwrites(self):
        assert deep_merge({"a": {"x": 1}}, {"a": 5}) == {"a": 5}


class TestMutation:
    def test_scope_parsing(self):
        m = Mutation(id="m", patch={"x": 1}, scope='tags.type == "refusal"')
        assert m.scope_ast == ("eq", "tags.type", "refusal")
        assert m.fires(Task(id="a", tags={"type": "refusal"}))
        assert not m.fires(Task(id="b", tags={"type": "answer"}))

    def test_callable_scope(self):
        m = Mutation(id="m", patch={"x": 1}, scope=lambda t: t.tags.get("flag") is True)
        assert m.fires(Task(id="a", tags={"flag": True}))
        assert not m.fires(Task(id="b"))

    def test_ast_scope(self):
        m = Mutation(id="m", patch={"x": 1}, scope=("in", "tags.d", ("a", "b")))
        assert m.fires(Task(id="a", tags={"d": "b"}))

    def test_effective_config_scoped(self):
        committed = [Mutation(id="c", patch={"contract": True}, scope='tags.type == "refusal"')]
        ref = Task(id="r", tags={"type": "refusal"})
        ans = Task(id="a", tags={"type": "answer"})
        assert effective_config({}, committed, ref) == {"contract": True}
        assert effective_config({}, committed, ans) == {}

    def test_apply_callable(self):
        m = Mutation(
            id="m",
            scope="all",
            apply=lambda cfg, task: {**cfg, "model": "big"} if task.tags.get("hard") else cfg,
        )
        hard = Task(id="h", tags={"hard": True})
        assert effective_config({"model": "small"}, [m], hard) == {"model": "big"}

    def test_serialization_roundtrip(self):
        m = Mutation(id="m", kind="retrieval_rule", patch={"a.b": 1}, scope='tags.d == "x"', family="f")
        m2 = Mutation.from_dict(m.to_dict())
        assert m2.id == m.id and m2.kind == m.kind and m2.patch == m.patch
        assert m2.scope_ast == m.scope_ast and m2.family == "f"

    def test_callable_scope_marked_unserializable(self):
        m = Mutation(id="m", patch={"x": 1}, scope=lambda t: True)
        d = m.to_dict()
        assert d["serializable"] is False and d["scope"] is None
