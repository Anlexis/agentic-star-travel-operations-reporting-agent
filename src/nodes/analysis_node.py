"""AgentCore Platform v1.0"""

# TRV-C2-002 — AnalysisNode
# Inner domain node 2: compute the year-on-year comparison and flag channels
# whose share of revenue falls below the configured threshold. Produces the
# structured analysis the report narrative is written from.
#
# The threshold is a declared runtime value, read from the reporting bounds the
# inner graph seeds into its own state. Reading it from a per-call config
# argument would be reading a channel the node is never handed: the base node
# wrapper calls execute(state) with the state alone, so such a value is always
# the hard-coded default and the declaration in config/config.yaml is inert.
#
# Wired by the inner DomainWorkflowGraph.
# Returns only changed state keys (partial dict).
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - required_trust_level = TrustLevel.ANONYMOUS (inner domain node)
#  - No constructor arguments; runtime bounds arrive through inner state

import logging
import math
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Used only when the reporting bounds cannot be read at all — mirrors the
# underperform_threshold value in config/config.yaml.
_FALLBACK_UNDERPERFORM_THRESHOLD = 0.05


def _resolve_threshold(limits: Dict[str, Any]) -> float:
    """Resolve the underperformance threshold, rejecting an unusable value.

    A threshold that is not a finite fraction would make every share comparison
    meaningless — against NaN the comparison is False, so nothing is ever
    flagged and the report reads as if every channel performed.
    """
    declared = limits.get("underperform_threshold", _FALLBACK_UNDERPERFORM_THRESHOLD)
    if isinstance(declared, bool) or not isinstance(declared, (int, float)):
        return _FALLBACK_UNDERPERFORM_THRESHOLD
    value = float(declared)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        return _FALLBACK_UNDERPERFORM_THRESHOLD
    return value


def _yoy_delta(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    """Year-on-year absolute delta; None if either side is missing."""
    if current is None or prior is None:
        return None
    return round(current - prior, 6)


def _yoy_pct_change(current: Optional[float], prior: Optional[float]) -> Optional[float]:
    """Year-on-year percentage change; None if the prior value is zero/missing."""
    if current is None or prior is None or prior == 0.0:
        return None
    return round((current - prior) / abs(prior) * 100.0, 2)


class AnalysisNode(FunctionNode):
    """Compute the year-on-year comparison and flag underperforming channels.

    Input state keys:
        collected_data: JSON STRING of normalized KPI data (DataCollectionNode)
        runtime_limits: JSON STRING of the reporting bounds (inner graph seed)

    Output state keys (partial dict):
        analysis_result: JSON STRING of the analysis, including the deltas and
                         the list of channels flagged for review
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

        collected: Dict[str, Any] = from_json(state.get("collected_data"), {}) or {}
        limits: Dict[str, Any] = from_json(state.get("runtime_limits"), {}) or {}
        threshold = _resolve_threshold(limits)

        if not collected:
            emit_trace_event(
                "travel_analysis_skipped_no_data",
                {"reason": "no_source_data"},
                state,
            )
            return {
                "analysis_result": to_json(
                    {
                        "yoy_occupancy_delta": None,
                        "yoy_occupancy_pct": None,
                        "yoy_revpar_delta": None,
                        "yoy_revpar_pct": None,
                        "underperforming_channels": [],
                        "channel_analysis": {},
                        "underperform_threshold": threshold,
                    }
                ),
                "status": AgentStatus.SUCCESS.value,
            }

        occupancy: Optional[float] = collected.get("occupancy_rate")
        revpar: Optional[float] = collected.get("revpar")
        channels: Dict[str, Dict[str, Any]] = collected.get("channels") or {}
        prior: Dict[str, Any] = collected.get("prior_period") or {}

        prior_occupancy: Optional[float] = prior.get("occupancy_rate")
        prior_revpar: Optional[float] = prior.get("revpar")
        prior_channels: Dict[str, Dict[str, Any]] = prior.get("channels") or {}

        yoy_occupancy_delta = _yoy_delta(occupancy, prior_occupancy)
        yoy_revpar_delta = _yoy_delta(revpar, prior_revpar)
        yoy_occupancy_pct = _yoy_pct_change(occupancy, prior_occupancy)
        yoy_revpar_pct = _yoy_pct_change(revpar, prior_revpar)

        # Every revenue figure reaching this point passed the finite + bounded
        # parser, so the total is a real number and the shares below are real
        # fractions of it.
        total_revenue = sum(float(ch.get("revenue") or 0) for ch in channels.values())

        channel_analysis: Dict[str, Dict[str, Any]] = {}
        underperforming: List[str] = []

        for ch_name, ch_data in channels.items():
            revenue = float(ch_data.get("revenue") or 0)
            share = (revenue / total_revenue) if total_revenue > 0 else 0.0
            ch_entry: Dict[str, Any] = {"share": round(share, 4)}

            prior_ch = prior_channels.get(ch_name) or {}
            prior_revenue = float(prior_ch.get("revenue") or 0)
            if prior_revenue > 0:
                ch_entry["yoy_revenue_delta"] = round(revenue - prior_revenue, 2)
                ch_entry["yoy_revenue_pct"] = round((revenue - prior_revenue) / prior_revenue * 100.0, 2)

            channel_analysis[ch_name] = ch_entry

            if share < threshold:
                underperforming.append(ch_name)

        analysis: Dict[str, Any] = {
            "yoy_occupancy_delta": yoy_occupancy_delta,
            "yoy_occupancy_pct": yoy_occupancy_pct,
            "yoy_revpar_delta": yoy_revpar_delta,
            "yoy_revpar_pct": yoy_revpar_pct,
            "underperforming_channels": underperforming,
            "channel_analysis": channel_analysis,
            "underperform_threshold": threshold,
        }

        emit_trace_event(
            "travel_analysis_completed",
            {
                "underperforming_channel_count": len(underperforming),
                "has_yoy_occupancy": yoy_occupancy_delta is not None,
                "has_yoy_revpar": yoy_revpar_delta is not None,
                "channel_count": len(channels),
                "threshold": threshold,
            },
            state,
        )

        logger.info(
            "AnalysisNode: %d channel(s) below the %.4f share threshold",
            len(underperforming),
            threshold,
        )

        return {
            "analysis_result": to_json(analysis),
            "status": AgentStatus.SUCCESS.value,
        }
