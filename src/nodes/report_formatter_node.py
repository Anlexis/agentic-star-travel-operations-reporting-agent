"""AgentCore Platform v1.0"""

# TRV-C2-002 — ReportFormatterNode
# Inner domain node 4: structure the composed narrative into the monthly
# operations report schema and the electronic-record retention format
# (Markdown with a structured header block).
#
# Wired by the inner DomainWorkflowGraph.
# Returns only changed state keys (partial dict).
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - required_trust_level = TrustLevel.ANONYMOUS (inner domain node)
#  - No constructor arguments

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

logger = logging.getLogger(__name__)

# Structural sections a conforming monthly operations report must contain.
_REQUIRED_SECTIONS = [
    "対象期間",
    "主要経営指標",
    "販売チャネル別分析",
]

# Retention-conforming document header. `period` is written from the validated
# source data, so it is one of the labels the collection node has already
# restricted to an inert character set.
_RETENTION_HEADER_TEMPLATE = """\
---
document_type: 観光庁月次運営報告書
retention_class: 電子帳簿保存法対象
format_version: "1.0"
generated_by: TravelOperationsReportGeneratorAgent / TRV-C2-002
period: {period}
session_id: {session_id}
---
"""

_REPORT_TITLE = "# 月次運営報告書（観光庁向け）\n"


class ReportFormatterNode(FunctionNode):
    """Structure the narrative into the monthly operations report format.

    Adds the retention-conforming document header block, checks that every
    mandatory section is present in the narrative, and assembles the final
    structured Markdown document.

    Input state keys:
        report_narrative: composed narrative text (NarrativeGenerationNode)
        collected_data:   JSON STRING of KPI data (for the header period field)

    Output state keys (partial dict):
        formatted_report: the assembled monthly report (Markdown)
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

        narrative = state.get("report_narrative") or ""
        collected: Dict[str, Any] = from_json(state.get("collected_data"), {}) or {}

        period = collected.get("period") or "未指定"
        session_id = state.get("session_id") or ""

        missing_sections: List[str] = [s for s in _REQUIRED_SECTIONS if s not in narrative]
        if missing_sections:
            logger.warning(
                "ReportFormatterNode: narrative missing required sections: %s",
                missing_sections,
            )
            # Non-fatal: a section with no source data is stated as such rather
            # than left out, so a reader can tell "no data" from "not reported".
            stub_lines = [f"\n## {section}\n（データなし）\n" for section in missing_sections]
            narrative = narrative + "\n".join(stub_lines)

        header = _RETENTION_HEADER_TEMPLATE.format(period=period, session_id=session_id)
        formatted = header + _REPORT_TITLE + "\n" + narrative

        emit_trace_event(
            "travel_report_formatted",
            {
                "formatted_chars": len(formatted),
                "missing_section_count": len(missing_sections),
            },
            state,
        )

        logger.info(
            "ReportFormatterNode: assembled %d characters (%d section stub(s))",
            len(formatted),
            len(missing_sections),
        )

        return {
            "formatted_report": formatted,
            "status": AgentStatus.SUCCESS.value,
        }
