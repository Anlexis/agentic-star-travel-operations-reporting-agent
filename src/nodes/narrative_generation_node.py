"""AgentCore Platform v1.0"""

# TRV-C2-002 — NarrativeGenerationNode
# Inner domain node 3: compose the monthly operations report narrative from the
# validated KPI figures. Every figure and year-on-year delta in the narrative is
# taken directly from collected_data / analysis_result, so the release check
# downstream can confirm that what the report says is what the source said.
#
# Composition is deterministic — no model call, so the same source data always
# produces the same document.
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
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json

logger = logging.getLogger(__name__)


def _fmt_pct(value: Optional[float], as_percent: bool = True) -> str:
    """Format a float as a percentage string, or '―' if None."""
    if value is None:
        return "―"
    pct = value * 100.0 if as_percent else value
    return f"{pct:.1f}%"


def _fmt_yen(value: Optional[float]) -> str:
    """Format a float as a JPY amount string, or '―' if None."""
    if value is None:
        return "―"
    return f"¥{value:,.0f}"


def _fmt_delta(value: Optional[float], unit: str = "pt") -> str:
    """Format a YoY delta with sign and unit, or '―' if None."""
    if value is None:
        return "―"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}{unit}"


def _build_narrative(
    period: str,
    occupancy: Optional[float],
    revpar: Optional[float],
    analysis: Dict[str, Any],
    channel_analysis: Dict[str, Dict[str, Any]],
) -> str:
    """Deterministic narrative composer.

    Every figure used in the narrative is taken directly from its source datum
    in collected_data / analysis_result, which is what lets the release check
    confirm the report reproduces the source.
    """
    lines: List[str] = []

    # Section 1: Reporting period
    lines.append("## 対象期間")
    lines.append(f"{period or '未指定'}")
    lines.append("")

    # Section 2: Key performance indicators (source-bound)
    lines.append("## 主要経営指標")
    lines.append(f"- **客室稼働率（OCC）**: {_fmt_pct(occupancy)}")

    yoy_occ_delta = analysis.get("yoy_occupancy_delta")
    yoy_occ_pct = analysis.get("yoy_occupancy_pct")
    if yoy_occ_delta is not None:
        yoy_occ_label = _fmt_delta(yoy_occ_pct, unit="%pt 前年同期比")
        lines.append(f"  - 前年同期比: {yoy_occ_label}")

    lines.append(f"- **RevPAR（Revenue per Available Room）**: {_fmt_yen(revpar)}")

    yoy_revpar_delta = analysis.get("yoy_revpar_delta")
    yoy_revpar_pct = analysis.get("yoy_revpar_pct")
    if yoy_revpar_delta is not None:
        yoy_revpar_label = _fmt_delta(yoy_revpar_pct, unit="% 前年同期比")
        lines.append(f"  - 前年同期比: {yoy_revpar_label} ({_fmt_delta(yoy_revpar_delta, unit='円 前年同期差')})")
    lines.append("")

    # Section 3: Channel breakdown (source-bound)
    if channel_analysis:
        lines.append("## 販売チャネル別分析")
        for ch_name, ch_data in channel_analysis.items():
            share = ch_data.get("share")
            share_str = f"{(share or 0) * 100:.1f}%" if share is not None else "―"
            ch_line = f"- **{ch_name}**: シェア {share_str}"
            yoy_ch_pct = ch_data.get("yoy_revenue_pct")
            if yoy_ch_pct is not None:
                ch_line += f" （前年同期比 {_fmt_delta(yoy_ch_pct, unit='%')}）"
            lines.append(ch_line)
        lines.append("")

    # Section 4: Underperforming channel flags
    underperforming = analysis.get("underperforming_channels") or []
    if underperforming:
        threshold = analysis.get("underperform_threshold", 0.05)
        lines.append("## 低パフォーマンスチャネル（要注視）")
        lines.append(
            f"以下のチャネルは総売上シェアが{_fmt_pct(threshold, as_percent=True)}未満であり、改善策の検討が推奨されます。"
        )
        for ch in underperforming:
            lines.append(f"- {ch}")
        lines.append("")

    return "\n".join(lines)


class NarrativeGenerationNode(FunctionNode):
    """Compose the monthly operations report narrative from the source figures.

    Input state keys:
        collected_data:  JSON STRING of normalized KPI data (DataCollectionNode)
        analysis_result: JSON STRING of analysis results (AnalysisNode)

    Output state keys (partial dict):
        report_narrative: the composed narrative text
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
        analysis: Dict[str, Any] = from_json(state.get("analysis_result"), {}) or {}

        period: str = collected.get("period") or ""
        occupancy: Optional[float] = collected.get("occupancy_rate")
        revpar: Optional[float] = collected.get("revpar")
        channel_analysis: Dict[str, Dict[str, Any]] = analysis.get("channel_analysis") or {}

        narrative = _build_narrative(period, occupancy, revpar, analysis, channel_analysis)

        # Audit the composition without repeating any source figure or label.
        emit_trace_event(
            "travel_narrative_generated",
            {
                "has_period": bool(period),
                "narrative_chars": len(narrative),
                "has_yoy": analysis.get("yoy_occupancy_delta") is not None,
            },
            state,
        )

        logger.info("NarrativeGenerationNode: composed %d characters", len(narrative))

        return {
            "report_narrative": narrative,
            "status": AgentStatus.SUCCESS.value,
        }
