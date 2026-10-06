"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic model. Graph checkpoints
# are serialised with msgpack, and model instances corrupt silently there.
# Extend the framework state with agent-specific fields only. Do NOT add
# credentials, secrets, or model objects.
#
# Structured fields (dict / list[dict]) are stored as JSON STRINGS, not bare
# Python containers: a bare container in a checkpointed field is not
# round-trippable. Producers serialise with to_json() on write; consumers
# deserialise with from_json() on read.
#
# TRV-C2-002 — TravelOperationsReportGeneratorAgent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner domain
# workflow (BaseGraph). The fields below cover both layers.
#
# Confidentiality note: the operational and financial figures (RevPAR,
# occupancy, per-channel breakdowns) live only inside the inner domain nodes.
# OutputGateNode is the last node of that workflow and either releases the
# assembled report or writes every one of these fields empty; the outer graph
# surfaces the report only on the success path.

import json
from typing import Any, Optional

from framework.schemas.agent_state import AgentState

# Fixed text returned to the caller when no report is released. Constant by
# design: it names no source datum, no field value and no internal location, so
# it carries nothing of the request that produced it.
REPORT_WITHHELD_NOTICE = (
    "レポートは出力されませんでした。入力データが検証に通らなかったか、"
    "出力チェックで公開が保留されました。数値と項目名を確認のうえ再送してください。\n"
    "(No report was released: the source data failed validation, or the output "
    "check withheld it. Review the figures and labels, then resubmit.)"
)


def to_json(value: Any) -> Optional[str]:
    """Serialise a dict/list state field to a JSON string.

    None passes through unchanged so an 'unset' field stays distinguishable
    from an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialise a JSON-string state field back to its dict/list.

    None / empty / malformed input returns the supplied ``default`` so a missing
    or corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for TRV-C2-002.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from the framework state.
    """

    # ------------------------------------------------------------------
    # Outer layer — PreProcessNode / TravelReportGraphNode.merge_output
    # ------------------------------------------------------------------

    # Validated and screened request text produced by PreProcessNode. The raw
    # request is not persisted beyond that node.
    validated_input: Optional[str]

    # The released report, mapped from the inner graph's output_report by
    # merge_output() and surfaced as formatted_output by PostProcessNode.
    # Written empty on every non-success path.
    result: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # Reporting bounds seeded into inner state by
    # DomainWorkflowGraph._extra_initial_state(). JSON STRING of the `reporting`
    # block of config/config.yaml. Read by DataCollectionNode (caps and KPI
    # bounds), AnalysisNode (underperformance threshold) and OutputGateNode
    # (release ceiling). Seeded into INNER state deliberately: a node reading an
    # outer-state key from inner state would compare against an empty mapping on
    # every real invocation and be silently dead.
    runtime_limits: Optional[str]

    # DataCollectionNode output
    # JSON STRING (to_json) of the normalized KPI/operations data. Shape:
    # {
    #   "period": str,                  # validated inert label
    #   "occupancy_rate": float | None, # e.g. 0.72 (72%)
    #   "revpar": float | None,         # e.g. 8500.0 (JPY)
    #   "channels": {                   # per-channel breakdown, validated labels
    #     "<channel>": {"revenue": float, "bookings": int}
    #   },
    #   "prior_period": {               # year-on-year comparison source (optional)
    #     "occupancy_rate": float,
    #     "revpar": float,
    #     "channels": {...}
    #   }
    # }
    # Consumers (AnalysisNode) read it back via from_json().
    collected_data: Optional[str]

    # AnalysisNode output
    # JSON STRING (to_json) of the analysis. Shape:
    # {
    #   "yoy_occupancy_delta": float | None,
    #   "yoy_occupancy_pct": float | None,
    #   "yoy_revpar_delta": float | None,
    #   "yoy_revpar_pct": float | None,
    #   "underperforming_channels": [str],
    #   "channel_analysis": {<channel>: {"share": float, "yoy_revenue_pct": float | None}},
    #   "underperform_threshold": float
    # }
    # Consumers (NarrativeGenerationNode) read it back via from_json().
    analysis_result: Optional[str]

    # NarrativeGenerationNode output
    # The composed narrative; every figure in it is bound to its source datum.
    # Deterministic composition — no model call.
    report_narrative: Optional[str]

    # ReportFormatterNode output
    # The assembled monthly report (Markdown with the retention header block).
    formatted_report: Optional[str]

    # OutputGateNode output
    # The released report with the disclaimer appended. This is the final
    # inner-graph output, surfaced via get_output() -> merge_output().
    output_report: Optional[str]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    # Set when a run COMPLETES without carrying out the request, because the
    # caller sent a value they can correct. A closed set of codes, never caller
    # content. Nodes downstream of the one that set it do no work and pass it on.
    error_code: Optional[str]
    # node_history is inherited from the framework state; listed here for clarity
    # node_history: Optional[List[str]]
