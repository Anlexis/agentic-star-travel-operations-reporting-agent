"""AgentCore Platform v1.0"""

# TRV-C2-002 — PostProcessNode (outer post_process backbone slot)
#
# Reads the released report from state["result"] — written by
# TravelReportGraphNode.merge_output() from the inner graph's output_report —
# and surfaces it as formatted_output.
#
# The release checks (credential scan, release ceiling, source traceability,
# disclaimer) belong to OutputGateNode inside the inner workflow. This node is
# the outer backbone formatting step, and it runs only on the success path: the
# backbone routes any non-success status straight to finalize.
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from mediator/, api/, or other agents

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG
from src.schemas.state import REPORT_WITHHELD_NOTICE


# Reason code -> the sentence the caller reads. A code with no entry falls back
# to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Surface the released report as the caller-facing output.

    Outer backbone post_process slot. Reads state["result"] and emits it as
    formatted_output for the finalize step. An empty result here means the
    workflow produced nothing to release, so the fixed withheld notice takes
    its place: an empty formatted_output would send the response builder back
    to result, which is exactly the field that has nothing in it.
    """

    # Backbone formatting slot; the caller-facing trust decision is taken by the
    # pre-process node ahead of it.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # Checked BEFORE the no-report branch below. A declined request produced
        # no report, so that branch would fire and turn a completed, actionable
        # refusal back into a bare withheld-output error - undoing the whole
        # point of settling a reason upstream.
        marker = state.get("error_code")
        if marker:
            emit_trace_event("travel_report_not_produced", {"reason": marker}, state)
            return {
                "result": "",
                "formatted_output": _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED),
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }

        result = state.get("result") or ""

        if not result.strip():
            emit_trace_event(
                "travel_report_not_released",
                {"reason": "no_report"},
                state,
            )
            return {
                "result": "",
                "formatted_output": REPORT_WITHHELD_NOTICE,
                "status": AgentStatus.ERROR.value,
            }

        emit_trace_event(
            "travel_report_released",
            {"report_chars": len(result)},
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }
