"""
Tests for graph/review_graph.py — pure helper functions only.
No LLM calls, no graph compilation.
"""
from graph.review_graph import _merge_results, _node_name
from config import AGENT_CONFIGS


class TestMergeResults:
    def test_merges_two_disjoint_dicts(self):
        a = {"Injection Expert": "result_a"}
        b = {"Auth Expert": "result_b"}
        merged = _merge_results(a, b)
        assert merged == {"Injection Expert": "result_a", "Auth Expert": "result_b"}

    def test_b_overwrites_a_on_same_key(self):
        a = {"Injection Expert": "old"}
        b = {"Injection Expert": "new"}
        assert _merge_results(a, b)["Injection Expert"] == "new"

    def test_all_five_agents_merge_correctly(self):
        agents = ["Injection Expert", "Auth Expert", "Secrets Expert",
                  "Performance Expert", "Code Quality Expert"]
        result = {}
        for name in agents:
            result = _merge_results(result, {name: f"result_{name}"})
        assert len(result) == 5
        for name in agents:
            assert name in result


class TestNodeName:
    def test_spaces_become_underscores(self):
        assert _node_name("Injection Expert") == "injection_expert"

    def test_all_agent_names_produce_valid_node_ids(self):
        for name in AGENT_CONFIGS:
            node = _node_name(name)
            assert node, f"Empty node name for: {name}"
            assert node == node.lower(), f"Uppercase in node name: {node}"
            assert " " not in node, f"Space in node name: {node}"
            assert node.replace("_", "").isalnum(), f"Invalid chars in: {node}"

    def test_agent_names_produce_unique_node_ids(self):
        node_ids = [_node_name(name) for name in AGENT_CONFIGS]
        assert len(node_ids) == len(set(node_ids)), "Duplicate node IDs detected"
