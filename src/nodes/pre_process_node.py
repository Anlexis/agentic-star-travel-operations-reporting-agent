"""AgentCore Platform v1.0"""

# TRV-C2-002 — PreProcessNode (outer pre_process backbone slot)
#
# Input validation: reject empty or malformed input before the inner domain
# workflow runs.
#
# Injection screen: the reporting instruction is natural language written by the
# caller and the KPI payload travels inside it, so both are screened before
# validated_input is written to state. The screen covers two classes:
#
#   1. directive phrases  — "ignore all previous instructions" and relatives;
#   2. chat-template control tokens — <|...|>, [INST], <<SYS>>, <|system|>.
#      A screen built only from phrases misses the token class entirely, and a
#      token is the more effective attack because it addresses the model's own
#      turn structure rather than its reading of a sentence.
#
# Both classes are checked against the raw text AND against the parsed payload,
# keys included: a \u-escaped token is invisible in the raw text and plain once
# the JSON is parsed, so a raw-only screen is evaded by escaping.
#
# The template owns this guarantee. A test that asserts "the framework refused"
# passes only where the framework gate is active; where it is absent the payload
# reaches the answer path and the run succeeds — the wrong direction to fail in.
#
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from mediator/, api/, or other agents

import json
import re
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress
from src.schemas.state import REPORT_WITHHELD_NOTICE

# Upper bound on the request text. The payload is caller-sized, so it is capped
# before anything walks it.
_MAX_INPUT_CHARS = 200000

# Directive-phrase screen. Each pattern requires the full phrase shape rather
# than a single suggestive verb: an unanchored fragment matches ordinary domain
# text — "act as a booking agent" and "insert into the report" are things a
# reporting instruction legitimately says — and refusing real work is the
# failure direction that stops the template being usable.
_DIRECTIVE_PATTERNS = [
    re.compile(
        r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+)?"
        r"(?:previous|prior|above|earlier|preceding)\s+"
        r"(?:instruction|instructions|prompt|prompts|rule|rules)",
        re.I,
    ),
    re.compile(
        r"\bact\s+as\b[^\n]{0,120}?(?:jailbreak|DAN\b|unrestricted|unfiltered|"
        r"no\s+restrictions|without\s+restrictions)",
        re.I,
    ),
    re.compile(
        r"\b(?:you\s+are\s+now|from\s+now\s+on\s+you\s+are)\b[^\n]{0,60}?"
        r"(?:developer\s+mode|jailbreak|unrestricted|unfiltered)",
        re.I,
    ),
    re.compile(
        r"(?:reveal|print|show|output)\s+(?:your\s+|the\s+)?"
        r"(?:system\s+prompt|initial\s+instructions|hidden\s+instructions)",
        re.I,
    ),
]

# Control-token screen. These are the turn delimiters chat templates use; none
# of them occurs in a KPI report request, so the class is refused outright
# rather than pattern-matched inside a sentence.
_CONTROL_TOKEN_PATTERNS = [
    re.compile(r"<\|[^|>\n]{1,40}\|>"),  # <|im_start|>, <|system|>, <|endoftext|>
    re.compile(r"<<\s*/?\s*SYS\s*>>", re.I),  # <<SYS>> / <</SYS>>
    re.compile(r"\[/?\s*INST\s*\]", re.I),  # [INST] / [/INST]
    re.compile(r"</?\s*(?:system|assistant|user)\s*>", re.I),
    re.compile(r"\{\{.*?\}\}|\$\{.*?\}", re.S),  # template markers
]

# Markup a sanitiser would strip. The text is screened both as submitted and
# with these removed: a token is caught before the strip could delete it, and a
# directive spliced with markup ("ig<b>nore all previous instructions") is
# caught after the strip re-assembles it.
_MARKUP_RE = re.compile(r"<[^<>\n]{0,80}>")


def _screen_text(text: str) -> Optional[str]:
    """Return a closed-set class label if the text is refused, else None."""
    stripped_markup = _MARKUP_RE.sub("", text)
    for pattern in _CONTROL_TOKEN_PATTERNS:
        if pattern.search(text):
            return "control_token"
    for pattern in _DIRECTIVE_PATTERNS:
        if pattern.search(text) or pattern.search(stripped_markup):
            return "directive_phrase"
    return None


def _screen_parsed(value: Any, depth: int = 0) -> Optional[str]:
    """Screen a parsed payload depth-first, keys included.

    A field NAME is caller data as much as a field value, and JSON \\u escapes
    are only resolved once the document is parsed — so the walk has to happen
    after parsing and has to cover both sides of every pair.
    """
    if depth > 12:
        return "nesting_depth"
    if isinstance(value, str):
        return _screen_text(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                found = _screen_text(key)
                if found:
                    return found
            found = _screen_parsed(item, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = _screen_parsed(item, depth + 1)
            if found:
                return found
    return None


def _refusal_output() -> Dict[str, Any]:
    """Caller-facing fields for a refused request.

    A refusal here short-circuits the rest of the backbone, so the formatting
    node never runs and this is the only chance to say anything. The notice is
    non-empty on purpose: an empty value sends the response builder back to the
    report field, which on this path holds nothing.
    """
    return {
        "validated_input": "",
        "result": "",
        "formatted_output": REPORT_WITHHELD_NOTICE,
    }


def _embedded_json(text: str) -> Any:
    """Return the JSON object embedded in the text, or None if there is none."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


class PreProcessNode(FunctionNode):
    """Validate and screen the caller request before the domain workflow.

    The backbone pre_process node. Checks that user_input is a non-empty string
    within the size cap and screens it for injection, then writes the stripped
    text to validated_input so the inner domain nodes can rely on it having
    passed the gate.
    """

    # This backbone node takes the caller-facing trust decision for the whole
    # graph; the external-facing level is the one the manifest declares.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")

        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            # Nothing was sent. The caller can fix that, so the run COMPLETES
            # carrying the reason rather than terminating and leaving them only
            # an exception type.
            emit_progress(INPUT_REJECTED)
            return {
                **_refusal_output(),
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: request text is empty or missing"],
            }

        if len(user_input) > _MAX_INPUT_CHARS:
            emit_trace_event(
                "travel_report_input_rejected",
                {"reason": "over_size_cap"},
                state,
            )
            # The whole request is oversized, not one field of it; shortening
            # it is a change the caller can make.
            emit_progress(INPUT_REJECTED)
            return {
                **_refusal_output(),
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [f"PreProcessNode: request text exceeds the {_MAX_INPUT_CHARS}-character cap"],
            }

        stripped = user_input.strip()

        # Screen the text as submitted, then the payload as parsed.
        refusal = _screen_text(stripped)
        if refusal is None:
            refusal = _screen_parsed(_embedded_json(stripped))
        if refusal:
            # The label is one of a closed set defined in this module; the text
            # that produced it is not repeated.
            emit_trace_event(
                "travel_report_input_rejected",
                {"reason": refusal},
                state,
            )
            # Terminal, unlike the two rejections above. Spliced instructions
            # are not a value the caller can correct by rewording, and completing
            # the run would make a refusal read like an ordinary declined value.
            return {
                **_refusal_output(),
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: request refused — {refusal}"],
            }

        emit_trace_event(
            "travel_report_request_accepted",
            {"input_chars": len(stripped)},
            state,
        )

        return {
            "validated_input": stripped,
            "status": AgentStatus.SUCCESS.value,
        }
