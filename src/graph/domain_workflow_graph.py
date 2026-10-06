"""AgentCore Platform v1.0"""

# TRV-C2-002 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the monthly tourism-operations report generation workflow:
#
#   START
#     -> data_collection      (DataCollectionNode)      — validate + normalize KPI data
#     -> analysis             (AnalysisNode)            — YoY + underperforming channel flags
#     -> narrative_generation (NarrativeGenerationNode) — compose source-bound narrative
#     -> report_formatter     (ReportFormatterNode)     — structure to the filing schema
#     -> output_gate          (OutputGateNode)          — credential scan + release check
#     -> END
#
# Called by TravelReportGraphNode.get_subgraph() in graph.py.
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   - Inherits BaseGraph (fully custom topology — no forced backbone)
#   - Implements all 7 BaseGraph ABC methods
#   - register_nodes() does NOT call super() (abstract in BaseGraph)
#   - register_nodes() instantiates every domain node with NO ctor args
#   - Does NOT register initialize / finalize (outer backbone concerns)
#   - get_output() designed together with TravelReportGraphNode.merge_output()
#   - framework.* imports only
#   - Placed at src/graph/domain_workflow_graph.py (only accepted path)

import json
from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.analysis_node import AnalysisNode
from src.nodes.data_collection_node import DataCollectionNode
from src.nodes.narrative_generation_node import NarrativeGenerationNode
from src.nodes.output_gate_node import OutputGateNode
from src.nodes.report_formatter_node import ReportFormatterNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for TRV-C2-002.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by TravelReportGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          -> data_collection      (DataCollectionNode)
          -> analysis             (AnalysisNode)
          -> narrative_generation (NarrativeGenerationNode)
          -> report_formatter     (ReportFormatterNode)
          -> output_gate          (OutputGateNode)
          -> END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "trv_c2_002_travel_operations_report_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate the inner graph config before compilation.

        The reporting bounds arrive under config["configurable"]["reporting"].
        A wrong shape here is a deployment mistake, and it must fail loudly at
        compile time rather than degrade to defaults at request time — a graph
        that silently ignores its declared bounds looks healthy and enforces
        nothing.
        """
        configurable = self.config.get("configurable")
        if configurable is None:
            return
        if not isinstance(configurable, dict):
            raise ValueError(
                "DomainWorkflowGraph: config['configurable'] must be a mapping "
                f"when present, got {type(configurable).__name__}."
            )
        reporting = configurable.get("reporting")
        if reporting is not None and not isinstance(reporting, dict):
            raise ValueError(
                "DomainWorkflowGraph: config['configurable']['reporting'] must be a "
                f"mapping when present, got {type(reporting).__name__}."
            )

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the resolved reporting bounds into INNER state.

        The framework hands a nested graph only the input string, so the inner
        nodes have no other route to the declared configuration. Seeding here —
        rather than relying on the outer state — is what makes the channel cap,
        the KPI bounds, the underperformance threshold and the released-report
        ceiling read a key that exists in their own scope; a layer reading an
        outer key from inner state would compare against an empty mapping on
        every real invocation and be silently dead.
        """
        from src.graph.graph import reporting_limits

        configurable = self.config.get("configurable")
        declared = configurable.get("reporting") if isinstance(configurable, dict) else None
        limits = declared if isinstance(declared, dict) and declared else reporting_limits({})
        return {"runtime_limits": json.dumps(limits, ensure_ascii=False)}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.

        Every node is instantiated with NO constructor arguments; runtime values
        reach them through inner state, seeded by _extra_initial_state(). Every
        key registered here is referenced in add_edges().
        """
        self._nodes["data_collection"] = DataCollectionNode()
        self._nodes["analysis"] = AnalysisNode()
        self._nodes["narrative_generation"] = NarrativeGenerationNode()
        self._nodes["report_formatter"] = ReportFormatterNode()
        self._nodes["output_gate"] = OutputGateNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear report-generation domain topology.

        For this template the topology is intentionally linear — no conditional
        branching between domain nodes. route() is implemented as required by
        the ABC but add_conditional_edges() is not used.
        """
        self._sg.add_edge(START, "data_collection")
        self._sg.add_edge("data_collection", "analysis")
        self._sg.add_edge("analysis", "narrative_generation")
        self._sg.add_edge("narrative_generation", "report_formatter")
        self._sg.add_edge("report_formatter", "output_gate")
        self._sg.add_edge("output_gate", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Conditional routing — required by BaseGraph ABC.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime. Implemented to satisfy the ABC.
        Returns END on error so an unexpected call does not re-enter a node.

        Annotated with this graph's own State rather than the base state type:
        the graph library reads a path callable's annotation as its input schema
        and projects away fields the annotation does not declare, so a base-type
        annotation would hide every domain field from the decision.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_gate"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by TravelReportGraphNode.merge_output() in
        graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()  emits  -> "output_report", "status", ...
            Outer merge_output() reads -> sub_result.get("output_report"),
                                          sub_result.get("status")

        The report is surfaced only on a successful run. On any other terminal
        status the key is present and empty, so a partially built report cannot
        cross the layer boundary even if the caller of this method were to stop
        checking the status.
        """
        status = state.get("status")
        succeeded = status in (AgentStatus.SUCCESS, AgentStatus.SUCCESS.value)
        return {
            "output_report": state.get("output_report") if succeeded else "",
            # The reason must leave the subgraph or the outer graph cannot tell a
            # declined request from a run that simply produced nothing.
            "error_code": state.get("error_code"),
            "status": status,
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
