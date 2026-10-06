"""AgentCore Platform v1.0"""

# TRV-C2-002 — OutputGateNode
# Inner domain node 5 (final): the release check for the assembled report.
#
# Three independent checks, each with its own audit event:
#
#   1. Credential scan. The framework's own detector is called first, so what
#      this gate blocks is a superset of what the framework blocks. A narrower
#      local pattern set is itself a bypass: a value the framework catches and
#      this gate misses makes the framework raise inside the node wrapper, and
#      the wrapper then discards this node's whole delta — including the
#      clearing below. A domain pattern set is layered on top for the operational
#      identifiers (connection markers, credential assignments) the framework's
#      list does not carry.
#
#   2. Release ceiling. A report longer than a monthly summary can be means the
#      pipeline is re-emitting its source payload rather than summarising it.
#
#   3. Source traceability. Each KPI figure present in the source data must
#      appear in the report body, so a report cannot be released having quietly
#      dropped the number it exists to communicate.
#
# On any violation the node returns an error status AND writes every
# output-bearing field empty. Returning the status alone is not containment:
# partial state updates are merged, so a key the node does not write keeps
# whatever value it already held, and the response builder falls back to the
# report field even on an error status. Clearing is what makes the withholding
# real.
#
# The disclaimer is appended to every released report and is not configurable.
#
# The release checks run INLINE in execute() via module-level helpers. The
# node's own output gate method is final on the base class and cannot be
# overridden; a hook method on this class would auto-wrap and produce a
# None state on .invoke().
#
# Wired by the inner DomainWorkflowGraph (final step).
# Returns only changed state keys (partial dict).
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - required_trust_level = TrustLevel.ANONYMOUS (inner domain node)
#  - No constructor arguments; runtime bounds arrive through inner state

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Appended to every released report. Not configurable.
_DISCLAIMER = (
    "\n\n---\n"
    "**【重要】免責事項**\n\n"
    "本レポートはAI支援により生成されたドラフトです。"
    "観光庁等への正式な提出・公表の前に、数値の正確性を担当者が確認してください。"
)

# Operational-credential shapes specific to hospitality systems, layered on top
# of the framework detector rather than replacing it.
_DOMAIN_CREDENTIAL_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # Property-management and channel-manager connection markers.
    ("connection_marker", re.compile(r"(?:jdbc|pms|channel_mgr):[^/\s]{4,}", re.IGNORECASE)),
    # Credential assignments in any form.
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
    # Publishable/API key shapes the framework list does not carry.
    ("api_key", re.compile(r"\b(?:pk|ak)-[A-Za-z0-9]{16,}")),
]

# Output-bearing inner state fields. Every one is written empty together on a
# violation; the tuple is the inventory a new field has to join deliberately
# rather than by being forgotten.
_OUTPUT_BEARING_FIELDS: Tuple[str, ...] = (
    "output_report",
    "formatted_report",
    "report_narrative",
    "collected_data",
    "analysis_result",
)


def _cleared_output_state() -> Dict[str, Any]:
    """Every output-bearing field, present and empty.

    Present matters as much as empty: an omitted key leaves the previous value
    in state, so a delta that simply does not mention a field withholds nothing.
    """
    return {field: "" for field in _OUTPUT_BEARING_FIELDS}


def _scan_credentials(report: str) -> Optional[str]:
    """Return a closed-set label if the report carries a credential shape.

    The framework detector runs first so this gate's block set is a superset of
    the framework's. Only the finding TYPE is returned — never the matched text,
    which is the value being withheld.
    """
    findings = detect_credentials(report)
    if findings:
        types = sorted({str(f.get("type", "credential")) for f in findings})
        return ",".join(types)
    for label, pattern in _DOMAIN_CREDENTIAL_PATTERNS:
        if pattern.search(report):
            return label
    return None


def _check_traceability(report: str, collected: Dict[str, Any]) -> bool:
    """True when every KPI figure in the source data appears in the report."""
    occupancy = collected.get("occupancy_rate")
    if isinstance(occupancy, (int, float)) and not isinstance(occupancy, bool):
        occ_pct = float(occupancy) * 100.0
        if not any(v in report for v in (f"{occ_pct:.1f}", f"{occ_pct:.0f}")):
            return False

    revpar = collected.get("revpar")
    if isinstance(revpar, (int, float)) and not isinstance(revpar, bool):
        value = float(revpar)
        if not any(v in report for v in (f"{value:,.0f}", f"{value:.0f}")):
            return False

    return True


class OutputGateNode(FunctionNode):
    """Release check for the assembled report, with the disclaimer.

    Final inner domain node. Runs the credential scan, the release ceiling and
    the source-traceability check on formatted_report, then appends the
    disclaimer. On any violation the report is withheld: the node returns an
    error status and every output-bearing field is written empty.

    Input state keys:
        formatted_report: the assembled report (ReportFormatterNode)
        collected_data:   JSON STRING of KPI data (for the traceability check)
        runtime_limits:   JSON STRING of the reporting bounds (inner graph seed)

    Output state keys (partial dict):
        output_report: the released report with the disclaimer appended, or
                       every output-bearing field empty when withheld
    """

    # Inner domain node: the caller-facing trust decision is taken once, by the
    # backbone pre-process node.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on source data that was already
        # declined. Without this the node reports its own precondition failure
        # and the specific, actionable reason is replaced by a vaguer one.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        formatted_report = state.get("formatted_report") or ""
        collected: Dict[str, Any] = from_json(state.get("collected_data"), {}) or {}
        limits: Dict[str, Any] = from_json(state.get("runtime_limits"), {}) or {}
        max_report_chars = int(limits.get("max_report_chars", 200000))

        if not formatted_report.strip():
            emit_trace_event(
                "travel_output_gate_withheld",
                {"reason": "no_report"},
                state,
            )
            return {
                **_cleared_output_state(),
                "status": AgentStatus.ERROR.value,
                "error_log": ["OutputGateNode: report withheld — no report was assembled"],
            }

        credential_label = _scan_credentials(formatted_report)
        if credential_label:
            logger.error("OutputGateNode: report withheld — credential shape (%s)", credential_label)
            emit_trace_event(
                "travel_output_gate_withheld",
                {"reason": "credential_shape", "finding_types": credential_label},
                state,
            )
            return {
                **_cleared_output_state(),
                "status": AgentStatus.ERROR.value,
                "error_log": [f"OutputGateNode: report withheld — credential shape detected ({credential_label})"],
            }

        if len(formatted_report) > max_report_chars:
            logger.error("OutputGateNode: report withheld — over the release ceiling")
            emit_trace_event(
                "travel_output_gate_withheld",
                {"reason": "over_release_ceiling", "limit": max_report_chars},
                state,
            )
            return {
                **_cleared_output_state(),
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"OutputGateNode: report withheld — over the {max_report_chars}-character " "release ceiling"
                ],
            }

        if not _check_traceability(formatted_report, collected):
            logger.error("OutputGateNode: report withheld — source figure missing from the report")
            emit_trace_event(
                "travel_output_gate_withheld",
                {"reason": "source_figure_missing"},
                state,
            )
            return {
                **_cleared_output_state(),
                "status": AgentStatus.ERROR.value,
                "error_log": ["OutputGateNode: report withheld — a source figure is missing from the " "report body"],
            }

        gated_report = formatted_report + _DISCLAIMER

        emit_trace_event(
            "travel_output_gate_released",
            {"report_chars": len(gated_report), "has_disclaimer": True},
            state,
        )

        logger.info("OutputGateNode: released %d characters", len(gated_report))

        return {
            "output_report": gated_report,
            "status": AgentStatus.SUCCESS.value,
        }
