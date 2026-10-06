# PB-6 — Invoke-Order Boundary: a full agent.invoke() must execute the fixed
# AgentBaseGraph backbone in order.
#
# The Cat 2 backbone is fixed and is NEVER overridden by the template
# (add_edges() belongs to the framework):
#
#     START -> initialize -> pre_process -> main -> {route} -> post_process
#           -> finalize -> END
#
# The framework records every executed node in `node_history` (an AgentState
# field whose reducer is operator.add, so entries accumulate in execution
# order). Each entry is the node's CLASS NAME — appended by BaseNode.__call__.
#
# For TRV-C2-002 (Cat 2, two-layer nested) the `main` slot is TravelReportGraphNode
# (a GraphNode subclass) that delegates to the inner DomainWorkflowGraph. The
# inner graph runs with its own state; its inner node_history is NOT merged back
# into the outer state (merge_output() maps only output_report / result / status),
# so the OUTER node_history contains exactly the five backbone slots — never the
# inner domain nodes.
#
# This test drives a real end-to-end Graph().invoke() over a valid domain
# payload and asserts the surfaced node_history matches the canonical backbone
# order. A SUCCESS terminal status is required: on any non-SUCCESS status
# route() short-circuits main -> finalize and the post_process slot is
# skipped — which this test would catch.
#
# CRITICAL: The invoke MUST use InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
# — NEVER InvocationContext.for_internal(). The inner nodes admit any caller, so
# an internal-context invoke passes whether or not the outer trust gate is wired
# correctly. Verified-external is the level a real external caller arrives at, and
# the only one that exercises that gate.
#
# docs/03_test_spec.md, proof-of-boundary section.
# Deterministic — no LLM, no network. framework.* / src.* imports only.

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph, TravelOperationsReportGeneratorAgent

# --- TEMPLATE-SPECIFIC -------------------------------------------------------
# The `main`-slot GraphNode class name for TRV-C2-002. A sibling template
# mirroring this file changes ONLY this one entry; the other four backbone slot
# names come from the framework and are identical across every template.
_MAIN_SLOT_NODE = "TravelReportGraphNode"

# A valid, non-injected payload that drives the full domain workflow to a
# SUCCESS terminal status via all 5 inner domain nodes. Occupancy + RevPAR
# must be present so the source-traceability check in the output gate passes.
# The values (occupancy_rate=0.72 → 72.0%, revpar=8500.0 → ¥8,500) appear in
# the narrative and are confirmed by that check in OutputGateNode.
_VALID_PAYLOAD = json.dumps(
    {
        "period": "2026年5月",
        "occupancy_rate": 0.72,
        "revpar": 8500.0,
    },
    ensure_ascii=False,
)
# --- END TEMPLATE-SPECIFIC ---------------------------------------------------

# Canonical AgentBaseGraph backbone execution order, by node class name as
# recorded in node_history. Four entries come from the framework and are
# identical for every template; only _MAIN_SLOT_NODE is template-specific.
_EXPECTED_ORDER = [
    "InitializeNode",  # framework default   (initialize slot)
    "PreProcessNode",  # request screen      (pre_process slot, VERIFIED_EXTERNAL)
    _MAIN_SLOT_NODE,  # TEMPLATE-SPECIFIC   (main slot GraphNode)
    "PostProcessNode",  # outer post_process  (post_process slot, ANONYMOUS)
    "FinalizeNode",  # framework default   (finalize slot)
]


def _run() -> dict:
    """Run a full, externally-trusted end-to-end invocation.

    Uses InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    — the same trust path a real external caller takes. NEVER uses
    for_internal(): an internal context masks inner-node trust-trap bugs.
    """
    ctx = InvocationContext(
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="pb6-test-suite",
    )
    return Graph().invoke(_VALID_PAYLOAD, ctx=ctx)


class TestInvokeOrderBoundary:
    """PB-6: full agent.invoke() executes the backbone in the fixed order."""

    def test_invoke_reaches_success(self):
        """The full run must terminate SUCCESS — otherwise route() short-circuits
        main -> finalize and the post_process slot never runs, which is itself
        an invoke-order violation this test catches."""
        result = _run()
        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"Expected status='success', got {result.get('status')!r}. " f"result={result!r}"
        )

    def test_output_is_non_empty(self):
        """A successful run must surface a non-empty output."""
        output = _run().get("output")
        assert isinstance(output, str) and output.strip(), f"invoke() surfaced an empty or missing output: {output!r}"

    def test_node_history_is_populated(self):
        """node_history must be a non-empty list of node class-name strings."""
        history = _run().get("node_history")
        assert isinstance(history, list) and history, f"node_history must be a non-empty list, got {history!r}"
        assert all(isinstance(n, str) for n in history), f"node_history entries must be strings, got {history!r}"

    def test_backbone_slot_order(self):
        """Core invoke-order boundary: pre_process runs before main, main before
        post_process — as a strict ordered subsequence of node_history."""
        history = _run().get("node_history", [])
        ordered_slots = ["PreProcessNode", _MAIN_SLOT_NODE, "PostProcessNode"]
        for name in ordered_slots:
            assert name in history, f"Expected backbone slot {name!r} in node_history, got {history!r}"
        positions = [history.index(name) for name in ordered_slots]
        assert positions == sorted(positions), (
            f"Backbone slots executed out of order: {ordered_slots} at " f"{positions}. node_history={history!r}"
        )

    def test_full_backbone_sequence(self):
        """The complete AgentBaseGraph backbone order:
        initialize -> pre_process -> main -> post_process -> finalize.
        This is the non-negotiable 5-node sequence for every Cat 1/Cat 2 template."""
        history = _run().get("node_history", [])
        assert history == _EXPECTED_ORDER, (
            "node_history does not match the canonical backbone order.\n"
            f"  expected: {_EXPECTED_ORDER}\n"
            f"  actual:   {history}"
        )

    def test_graph_alias(self):
        """Graph back-compat alias must point at the real agent class."""
        assert Graph is TravelOperationsReportGeneratorAgent
