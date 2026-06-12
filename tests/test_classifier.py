"""Tests for the tool classifier (Phase 1)."""

from __future__ import annotations

from policy.classifier import classify_and_filter, classify_tool, is_mutation_schema


def _tool(name: str, description: str = "", *, with_ns: bool = True) -> dict:
    props = {"namespace": {"type": "string"}} if with_ns else {}
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": props, "required": []},
    }


# ── classify_tool ───────────────────────────────────────────────────────────


class TestClassifyTool:
    def test_pure_read_is_soft(self, base_cfg) -> None:
        assert classify_tool(_tool("list_pods", "List pods in a namespace"), base_cfg) == "soft"

    def test_get_is_soft(self, base_cfg) -> None:
        assert classify_tool(_tool("get_pod", "Get a pod"), base_cfg) == "soft"

    def test_describe_is_soft(self, base_cfg) -> None:
        assert classify_tool(_tool("describe_pod", "Describe a pod"), base_cfg) == "soft"

    def test_query_is_soft(self, base_cfg) -> None:
        assert classify_tool(_tool("query_metrics", "Run a PromQL query"), base_cfg) == "soft"

    def test_patch_is_hard(self, base_cfg) -> None:
        assert classify_tool(_tool("patch_deployment", "Patch a deployment"), base_cfg) == "hard"

    def test_scale_is_hard(self, base_cfg) -> None:
        assert classify_tool(_tool("scale_deployment", "Scale a deployment"), base_cfg) == "hard"

    def test_restart_is_hard(self, base_cfg) -> None:
        assert classify_tool(_tool("restart_pod", "Restart a pod"), base_cfg) == "hard"

    def test_apply_is_hard(self, base_cfg) -> None:
        assert classify_tool(_tool("apply_manifest", "Apply a manifest"), base_cfg) == "hard"

    def test_cordon_is_hard(self, base_cfg) -> None:
        assert classify_tool(_tool("cordon_node", "Cordon a node"), base_cfg) == "hard"

    def test_delete_is_violent(self, base_cfg) -> None:
        assert classify_tool(_tool("delete_pod", "Delete a pod"), base_cfg) == "violent"

    def test_destroy_is_violent(self, base_cfg) -> None:
        assert classify_tool(_tool("destroy_cluster", "Destroy"), base_cfg) == "violent"

    def test_drop_is_violent(self, base_cfg) -> None:
        assert classify_tool(_tool("drop_database", "Drop a database"), base_cfg) == "violent"

    def test_purge_is_violent(self, base_cfg) -> None:
        assert classify_tool(_tool("purge_queue", "Purge"), base_cfg) == "violent"

    def test_wipe_is_violent(self, base_cfg) -> None:
        assert classify_tool(_tool("wipe_data", "Wipe data"), base_cfg) == "violent"

    def test_force_evict_violent(self, base_cfg) -> None:
        # description mentions evict --force
        assert (
            classify_tool(_tool("evict_pod", "evict --force"), base_cfg) == "violent"
        )

    def test_force_kill_violent(self, base_cfg) -> None:
        assert classify_tool(_tool("forcekill", "force-kill grace=0"), base_cfg) == "violent"

    def test_ambiguous_defaults_hard(self, base_cfg) -> None:
        # No verb matches → default hard (safe-by-default).
        assert classify_tool(_tool("weird_internal_op", "does something custom"), base_cfg) == "hard"

    def test_cluster_scoped_hard_escalates_to_violent(self, base_cfg) -> None:
        # A patch tool without a `namespace` property is cluster-shaped → violent.
        t = _tool("patch_clusterrole", "Patch a clusterrole", with_ns=False)
        assert classify_tool(t, base_cfg) == "violent"

    def test_violent_match_wins_over_hard(self, base_cfg) -> None:
        # 'delete' mentioned in description should override patch verb.
        t = _tool("patch_or_delete_resource", "Delete a resource")
        assert classify_tool(t, base_cfg) == "violent"

    def test_description_only_match(self, base_cfg) -> None:
        # Verb in description, name uninformative.
        t = _tool("operation_42", "Restart a deployment")
        assert classify_tool(t, base_cfg) == "hard"


# ── is_mutation_schema ──────────────────────────────────────────────────────


def test_is_mutation_schema_when_no_namespace() -> None:
    assert is_mutation_schema(_tool("foo", with_ns=False)) is True


def test_is_mutation_schema_when_namespace_present() -> None:
    assert is_mutation_schema(_tool("foo", with_ns=True)) is False


# ── classify_and_filter ──────────────────────────────────────────────────────


class TestClassifyAndFilter:
    def test_violent_tools_filtered(self, base_cfg) -> None:
        tools = [
            _tool("list_pods", "List pods"),
            _tool("delete_pod", "Delete a pod"),
            _tool("destroy_namespace", "Destroy"),
        ]
        kept, filtered = classify_and_filter(tools, base_cfg)
        kept_names = [t["name"] for t in kept]
        filtered_names = [t["name"] for t in filtered]
        assert "list_pods" in kept_names
        assert "delete_pod" not in kept_names
        assert "destroy_namespace" not in kept_names
        assert set(filtered_names) == {"delete_pod", "destroy_namespace"}

    def test_action_class_attached(self, base_cfg) -> None:
        tools = [_tool("get_pod", "Get a pod"), _tool("scale_dep", "Scale")]
        kept, _ = classify_and_filter(tools, base_cfg)
        classes = {t["name"]: t["_action_class"] for t in kept}
        assert classes == {"get_pod": "soft", "scale_dep": "hard"}

    def test_audit_called_for_classifications(self, base_cfg) -> None:
        events: list[tuple[str, dict]] = []

        class FakeAudit:
            def write(self, e, p):
                events.append((e, p))

        tools = [_tool("list_pods"), _tool("delete_pod", "Delete a pod")]
        classify_and_filter(tools, base_cfg, audit_writer=FakeAudit())
        classified = [e for e in events if e[0] == "tool_classified"]
        filtered = [e for e in events if e[0] == "tool_filtered"]
        assert len(classified) == 2
        assert len(filtered) == 1
        assert filtered[0][1]["tool"] == "delete_pod"
        assert filtered[0][1]["reason"] == "violent-classified"
