"""AgentCore Platform v1.0"""

# TRV-C2-002 — DataCollectionNode
# Inner domain node 1: parse and normalize the KPI/operations source data
# carried in the validated payload, and enforce the caller-data contract.
#
# The caller owns every value this node reads, so nothing is trusted:
#  - every number goes through a finite + bounded parser. NaN and Infinity
#    parse cleanly through float() and arrive intact through JSON, and every
#    comparison against NaN is False — so an unchecked non-finite value does
#    not raise, it quietly turns a threshold decision into "no";
#  - every label that is rendered into the report is restricted to an inert
#    character set and a length cap, because a label is caller text that ends
#    up inside a document a reader treats as a record;
#  - the channel map is caller-sized, so it is capped.
#
# Rejections name the FIELD, never the value: an error line is a place a
# rejected value would otherwise be copied to verbatim.
#
# Wired by the inner DomainWorkflowGraph.
# Returns only changed state keys (partial dict).
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - required_trust_level = TrustLevel.ANONYMOUS (inner domain node)
#  - No constructor arguments; runtime bounds arrive through inner state

import json
import logging
import math
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Occupancy is accepted either as a 0–1 fraction or as a 0–100 percentage and is
# normalised to a fraction. Both forms share the same outer bound.
_OCCUPANCY_MAX_PCT = 100.0

# Characters a caller-supplied label may contain. Latin letters, digits, the two
# separators a period label needs, spaces, and the Japanese scripts real channel
# names are written in. Deliberately excludes newlines, markdown and markup
# characters, template markers and delimiters — a label carrying a newline plus
# a heading marker forges a section in the released document, and a label
# carrying markup travels into whatever renders it.
_LABEL_RE = re.compile(r"^[0-9A-Za-z 　_\-/.()々぀-ゟ゠-ヿ一-鿿０-ｚ]+$")

# Keys accepted for each figure, in preference order. Membership is checked
# explicitly rather than with `or`, so a legitimate zero is a value and not a
# missing field.
_OCCUPANCY_KEYS = ("occupancy_rate", "occupancy", "稼働率")
_REVPAR_KEYS = ("revpar", "RevPAR", "収益")
_PERIOD_KEYS = ("period", "対象期間")
_CHANNEL_KEYS = ("channels", "チャネル")
_PRIOR_KEYS = ("prior_period", "前年同期")
_REVENUE_KEYS = ("revenue", "売上", "amount")
_BOOKINGS_KEYS = ("bookings", "nights", "予約数")


def _first_present(payload: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[bool, Any]:
    """Return (present, value) for the first key that exists in the payload."""
    for key in keys:
        if key in payload:
            return True, payload[key]
    return False, None


def _finite_in_range(value: Any, low: float, high: float) -> Optional[float]:
    """Parse a caller number and return it only if finite and within bounds.

    Rejects booleans (bool is an int in Python, so `True` would otherwise read
    as 1), non-numeric text, NaN, ±Infinity, and out-of-range magnitudes.
    Returns None on rejection so every caller fails closed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").rstrip("%")
        if not cleaned:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if number < low or number > high:
        return None
    return number


def _parse_occupancy(value: Any) -> Optional[float]:
    """Normalise occupancy to a 0.0–1.0 fraction, or None if out of contract.

    Both the fractional form (0.72) and the percentage form (72) are accepted;
    the two overlap only at 0 and 1, where the fraction reading is used.
    """
    number = _finite_in_range(value, 0.0, _OCCUPANCY_MAX_PCT)
    if number is None:
        return None
    if number <= 1.0:
        return round(number, 6)
    return round(number / 100.0, 6)


def _parse_label(value: Any, max_chars: int) -> Optional[str]:
    """Return a caller label only if it is inert and within the length cap."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_chars:
        return None
    if not _LABEL_RE.match(text):
        return None
    return text


class DataCollectionNode(FunctionNode):
    """Parse and normalize KPI/operations source data under the caller contract.

    Input state keys:
        validated_input: reporting instruction + KPI payload (PreProcessNode)
        runtime_limits:  JSON STRING of the reporting bounds (inner graph seed)

    Output state keys (partial dict):
        collected_data: JSON STRING of the normalized KPI structure
                        (occupancy_rate in [0,1], revpar within bounds,
                        channels keyed by validated label)
    """

    # Inner domain node: the caller-facing trust decision is taken once, by the
    # backbone pre-process node.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        validated_input = state.get("validated_input") or state.get("user_input", "")
        limits: Dict[str, Any] = from_json(state.get("runtime_limits"), {}) or {}

        max_channels = int(limits.get("max_channels", 200))
        max_label_chars = int(limits.get("max_label_chars", 32))
        min_revpar = float(limits.get("min_revpar_jpy", 0))
        max_revpar = float(limits.get("max_revpar_jpy", 100000000))
        min_revenue = float(limits.get("min_revenue_jpy", 0))
        max_revenue = float(limits.get("max_revenue_jpy", 1000000000000))
        max_bookings = float(limits.get("max_bookings", 100000000))

        payload = _parse_payload(validated_input)
        rejected: List[str] = []

        period = ""
        period_present, raw_period = _first_present(payload, _PERIOD_KEYS)
        if period_present and raw_period is not None:
            parsed_period = _parse_label(raw_period, max_label_chars)
            if parsed_period is None:
                rejected.append("period")
            else:
                period = parsed_period

        occupancy: Optional[float] = None
        occ_present, raw_occupancy = _first_present(payload, _OCCUPANCY_KEYS)
        if occ_present and raw_occupancy is not None:
            occupancy = _parse_occupancy(raw_occupancy)
            if occupancy is None:
                rejected.append("occupancy_rate")

        revpar: Optional[float] = None
        revpar_present, raw_revpar = _first_present(payload, _REVPAR_KEYS)
        if revpar_present and raw_revpar is not None:
            revpar = _finite_in_range(raw_revpar, min_revpar, max_revpar)
            if revpar is None:
                rejected.append("revpar")
            else:
                revpar = round(revpar, 2)

        _, raw_channels = _first_present(payload, _CHANNEL_KEYS)
        channels, channel_rejects = _parse_channels(
            raw_channels,
            max_channels,
            max_label_chars,
            min_revenue,
            max_revenue,
            max_bookings,
            "channels",
        )
        rejected.extend(channel_rejects)

        prior: Dict[str, Any] = {}
        _, raw_prior = _first_present(payload, _PRIOR_KEYS)
        if isinstance(raw_prior, dict):
            prior_occ_present, raw_prior_occ = _first_present(raw_prior, _OCCUPANCY_KEYS)
            if prior_occ_present and raw_prior_occ is not None:
                prior_occ = _parse_occupancy(raw_prior_occ)
                if prior_occ is None:
                    rejected.append("prior_period.occupancy_rate")
                else:
                    prior["occupancy_rate"] = prior_occ
            prior_revpar_present, raw_prior_revpar = _first_present(raw_prior, _REVPAR_KEYS)
            if prior_revpar_present and raw_prior_revpar is not None:
                prior_revpar = _finite_in_range(raw_prior_revpar, min_revpar, max_revpar)
                if prior_revpar is None:
                    rejected.append("prior_period.revpar")
                else:
                    prior["revpar"] = round(prior_revpar, 2)
            _, raw_prior_channels = _first_present(raw_prior, _CHANNEL_KEYS)
            prior_channels, prior_rejects = _parse_channels(
                raw_prior_channels,
                max_channels,
                max_label_chars,
                min_revenue,
                max_revenue,
                max_bookings,
                "prior_period.channels",
            )
            rejected.extend(prior_rejects)
            prior["channels"] = prior_channels

        if rejected:
            # The field names are a closed set defined in this module; the
            # rejected values are not repeated anywhere.
            emit_trace_event(
                "travel_data_collection_rejected",
                {"fields": sorted(set(rejected))},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    "DataCollectionNode: source data rejected — fields outside the accepted "
                    f"contract: {', '.join(sorted(set(rejected)))}"
                ],
            }

        normalized: Dict[str, Any] = {
            "period": period,
            "occupancy_rate": occupancy,
            "revpar": revpar,
            "channels": channels,
        }
        if prior:
            normalized["prior_period"] = prior

        emit_trace_event(
            "travel_data_collected",
            {
                "has_period": bool(period),
                "has_occupancy": occupancy is not None,
                "has_revpar": revpar is not None,
                "channel_count": len(channels),
                "has_prior_period": bool(prior),
            },
            state,
        )

        logger.info(
            "DataCollectionNode: normalized %d channel(s); occupancy=%s revpar=%s",
            len(channels),
            occupancy is not None,
            revpar is not None,
        )

        return {
            "collected_data": to_json(normalized),
            "status": AgentStatus.SUCCESS.value,
        }


def _parse_channels(
    raw: Any,
    max_channels: int,
    max_label_chars: int,
    min_revenue: float,
    max_revenue: float,
    max_bookings: float,
    field_prefix: str,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Parse the per-channel breakdown under the caller contract.

    Returns the validated map plus the list of rejected field paths. A channel
    is identified in a rejection by its ordinal position, never by the label the
    caller supplied — the label is the thing being rejected.
    """
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, [field_prefix]
    if len(raw) > max_channels:
        return {}, [f"{field_prefix} (over {max_channels} entries)"]

    result: Dict[str, Dict[str, Any]] = {}
    rejected: List[str] = []
    for index, (label, data) in enumerate(raw.items()):
        name = _parse_label(label, max_label_chars)
        if name is None:
            rejected.append(f"{field_prefix}[{index}].name")
            continue
        if not isinstance(data, dict):
            rejected.append(f"{field_prefix}[{index}]")
            continue
        entry: Dict[str, Any] = {}
        revenue_present, raw_revenue = _first_present(data, _REVENUE_KEYS)
        if revenue_present and raw_revenue is not None:
            revenue = _finite_in_range(raw_revenue, min_revenue, max_revenue)
            if revenue is None:
                rejected.append(f"{field_prefix}[{index}].revenue")
                continue
            entry["revenue"] = round(revenue, 2)
        bookings_present, raw_bookings = _first_present(data, _BOOKINGS_KEYS)
        if bookings_present and raw_bookings is not None:
            bookings = _finite_in_range(raw_bookings, 0, max_bookings)
            if bookings is None:
                rejected.append(f"{field_prefix}[{index}].bookings")
                continue
            entry["bookings"] = int(bookings)
        result[name] = entry
    return result, rejected


def _parse_payload(text: str) -> Dict[str, Any]:
    """Extract the structured KPI object embedded in the validated text.

    Expects a JSON object with keys: period, occupancy_rate, revpar, channels
    (optional), prior_period (optional). Text without an embedded object yields
    an empty mapping; the caller handles the graceful-degradation path.
    """
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}
