"""AgentCore Platform v1.0"""

# TRV-C2-002 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# TravelOperationsReportGeneratorAgent (Cat 2 domain workflow — document
# generation pattern).
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#                                             |  (RETRY)
#                                             -> pre_process
#
#   `main` slot is a GraphNode subclass (TravelReportGraphNode) that delegates
#   the full report-generation domain workflow to DomainWorkflowGraph (inner
#   BaseGraph at src/graph/domain_workflow_graph.py).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (multi-step topology)
#
# Rules enforced:
#   - TravelOperationsReportGeneratorAgent inherits AgentBaseGraph (direct)
#   - super().register_nodes() called first (fills initialize + finalize)
#   - TravelReportGraphNode assigned to self._nodes["main"]
#   - merge_output() returns only changed keys
#   - add_edges() NOT overridden on the outer graph
#   - framework.* imports only

from pathlib import Path
from typing import Any, ClassVar, Dict, Tuple, cast

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.utils.config_loader import load_agent_config
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import REPORT_WITHHELD_NOTICE, State

# Repo root: src/graph/graph.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors the `reporting` block of config/config.yaml so the bounds are never
# empty in a deployment layout where the file cannot be read.
_FALLBACK_REPORTING: Dict[str, Any] = {
    "underperform_threshold": 0.05,
    "max_channels": 200,
    "max_label_chars": 32,
    "max_report_chars": 200000,
    "min_revpar_jpy": 0,
    "max_revpar_jpy": 100000000,
    "min_revenue_jpy": 0,
    "max_revenue_jpy": 1000000000000,
    "max_bookings": 100000000,
}


def runtime_config() -> Dict[str, Any]:
    """Load config/config.yaml — the live runtime parameters.

    The agent registry loads this file and hands it to the graph constructor;
    the standalone HTTP entry point does the same, so the retry budget and the
    reporting bounds are live in both deployments rather than declared in a
    file nothing reads.

    Reading config/agent.yaml here instead would return nothing usable: that
    file carries identity and compile-time requirements only, and a reader
    pointed at it degrades silently to hard-coded defaults.
    """
    loaded = load_agent_config(_REPO_ROOT)
    return dict(loaded) if isinstance(loaded, dict) else {}


def reporting_limits(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the reporting bounds from a graph config, falling back to file.

    A graph constructed with no config at all still runs against the shipped
    values rather than against numbers hard-coded here that could drift from
    config/config.yaml.
    """
    source = config if config else runtime_config()
    declared = source.get("reporting")
    merged = dict(_FALLBACK_REPORTING)
    if isinstance(declared, dict):
        merged.update(declared)
    return merged


class TravelReportGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of the outer agent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()       — instantiate DomainWorkflowGraph with the resolved bounds
      extract_input()      — pull validated_input from outer state
      merge_output()       — map sub_result fields into outer state delta (changed keys only)
      on_subgraph_error()  — contained error envelope (see error_strategy below)
    """

    # "contain": a failed inner run is turned into a contained error envelope by
    # on_subgraph_error() below rather than re-raised. Re-raising loses the
    # delta: the base wrapper then returns a bare error partial whose error_log
    # carries the raised exception text — for a nested graph, a traceback with
    # absolute source paths — and clears none of the output-bearing fields.
    # Containing here is what lets the caller be told the report was withheld
    # without being told anything about the data or the code that withheld it.
    error_strategy: ClassVar[str] = "contain"

    # False: any human-review interrupt is handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    # Outer state fields that can carry report text. Cleared together on every
    # non-success path so a checkpoint reader cannot pick up an ungated report.
    _OUTPUT_BEARING: ClassVar[Tuple[str, ...]] = ("result", "output_report")

    # Runtime configuration of the agent that registered this node. Assigned in
    # TravelOperationsReportGeneratorAgent.register_nodes(); empty when the node
    # is built on its own, in which case the shipped file is read instead.
    graph_config: Dict[str, Any] = {}

    def _parent_config(self) -> Dict[str, Any]:
        """Runtime bounds handed down to the inner graph.

        The inner graph is constructed per invocation and has no other route to
        the declared configuration, so the resolved bounds travel through its
        constructor. Without this the inner graph would build against an empty
        mapping and every value in config/config.yaml would be inert.

        The configuration the agent was constructed with wins over the shipped
        file: reading only the file would make a deployment that overrides a
        bound at construction time run against the file's value instead, which
        is the same silent-default failure one layer up. `graph_config` is set
        by the agent in register_nodes(); a node built outside a graph — a unit
        test — falls back to the file.
        """
        source = getattr(self, "graph_config", None) or runtime_config()
        return {"configurable": {"reporting": reporting_limits(source)}}

    def get_subgraph(self) -> BaseGraph:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time. The only constructor argument
        is the graph configuration the base class already accepts.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def execute(self, state: AgentState) -> Dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A declined request has no validated text to collect data from, so
        running the workflow would only reach the first domain node, fail its own
        precondition, and replace the specific, actionable reason already settled
        with a vaguer one.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        return cast(Dict[str, Any], super().execute(state))

    def extract_input(self, state: AgentState) -> str:
        """Return the string input passed into inner_graph.invoke().

        PreProcessNode validates the payload and writes it to validated_input.
        Prefer that; fall back to user_input if absent (e.g. in unit tests that
        bypass the outer backbone).
        """
        value = state.get("validated_input") or state.get("user_input", "")
        return str(value) if value else ""

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  -> "output_report", "status", ...
          This merge_output() reads -> sub_result.get("output_report"),
                                        sub_result.get("status")

        The report is surfaced only when the inner run reports success. On any
        other terminal status the output-bearing fields are written empty and
        the fixed withheld notice takes their place, so a partially built report
        cannot travel outward through the response fallback that prefers
        formatted_output and then result.
        """
        # Checked BEFORE the success branch below. A declined request completes
        # with SUCCESS, so that branch would publish an EMPTY report and the
        # reason would be gone by the time the formatting slot ran. A reason
        # settled OUTSIDE the subgraph is preferred: the inner graph never ran,
        # so its own marker is blank.
        marker = state.get("error_code") or sub_result.get("error_code")
        if marker:
            delta = self._withheld_delta(AgentStatus.SUCCESS.value)
            delta["error_code"] = marker
            delta["error_log"] = ["TravelReportGraphNode: no report produced — the request was not accepted"]
            return delta

        status = sub_result.get("status")
        succeeded = status in (AgentStatus.SUCCESS, AgentStatus.SUCCESS.value)
        if not succeeded:
            return self._withheld_delta(status)

        report = sub_result.get("output_report")
        return {
            "output_report": report,
            # PostProcessNode reads state.get("result") — map output_report here.
            "result": report,
            "status": status,
        }

    def on_subgraph_error(self, state: AgentState, error: Exception) -> Dict[str, Any]:
        """Contained error envelope for a failed inner run.

        The base implementation puts str(error) into error_log. For a nested
        graph that string is the inner failure text — which may quote source
        figures — or a traceback carrying absolute file paths. Neither belongs
        in a response, so the reason is recorded as a fixed label instead.
        """
        return self._withheld_delta(AgentStatus.ERROR.value)

    def _withheld_delta(self, status: Any) -> Dict[str, Any]:
        """Outer state delta for every non-success path.

        Every output-bearing key is PRESENT and empty — omitting a key would
        leave whatever the state already held, because partial deltas are merged
        rather than replacing state. formatted_output carries a non-empty
        notice: an empty string there re-opens the fallback to result.
        """
        delta: Dict[str, Any] = {field: "" for field in self._OUTPUT_BEARING}
        delta["formatted_output"] = REPORT_WITHHELD_NOTICE
        delta["status"] = status if status else AgentStatus.ERROR.value
        delta["error_log"] = ["TravelReportGraphNode: report withheld — reporting workflow did not complete"]
        return delta


class TravelOperationsReportGeneratorAgent(AgentBaseGraph):
    """Outer graph for TRV-C2-002 (Cat 2).

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    TravelReportGraphNode (main slot), which delegates to DomainWorkflowGraph
    (inner BaseGraph at src/graph/domain_workflow_graph.py).

    Backbone (fixed):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode (input validation + injection screen;
                                       required_trust_level = VERIFIED_EXTERNAL)
      - main:         TravelReportGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (format + audit; required_trust_level = ANONYMOUS)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "TravelOperationsReportGeneratorAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        main = TravelReportGraphNode()
        # The main node forwards the resolved bounds into the inner graph, so it
        # is handed this agent's configuration. A node reading only the shipped
        # file would ignore a bound overridden at construction time.
        main.graph_config = dict(self.config or {})

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = main
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat alias — src/api/server.py and the boundary tests import `Graph`.
Graph = TravelOperationsReportGeneratorAgent
