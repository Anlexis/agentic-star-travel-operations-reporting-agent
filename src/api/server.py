"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# On the hosted platform the gateway calls agent.invoke() directly instead.

import os
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials_in_value
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event
from src.graph.graph import TravelOperationsReportGeneratorAgent, runtime_config

app = FastAPI(title="Agent")

# The registry loads config/config.yaml and constructs the graph as
# Graph(config=...); this standalone entry point does the same, so the retry
# budget and the reporting bounds are live in both deployments rather than
# declared in a file nothing reads.
agent = TravelOperationsReportGeneratorAgent(config=runtime_config())
agent.compile()
# Namespace and agent name match the manifest values.
agent.provision_secrets(secrets_factory(namespace="trv", agent_name="TravelOperationsReportGeneratorAgent"))

# Upper bound on the request body handled here. The payload is caller-sized, so
# it is capped at the boundary rather than inside the graph.
_MAX_INPUT_CHARS = 200000


class InvokeRequest(BaseModel):
    """Request body for /invoke.

    `input` carries the reporting instruction with the period KPI document
    embedded in it as a JSON object — period, occupancy_rate, revpar, an
    optional per-channel breakdown and an optional prior_period block. The
    collection node validates every field against the declared bounds.

    There is deliberately no structured-context channel on this endpoint. The
    backbone's first node returns whatever arrives on that channel verbatim into
    its own result, where the framework's mandatory output scan reads it; a
    credential-shaped value there fails the first node of the graph before any
    template code runs. Declaring a narrow contract would not close that,
    because a validator ignores undeclared keys rather than dropping them. Not
    exposing the channel does.
    """

    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller authentication. When INVOKE_AUTH_TOKEN is set on the
    # server environment, a caller that no upstream middleware vouched for must
    # present it as a bearer token and then runs as a verified external caller.
    # Trust established upstream is never demoted.
    #
    # Required here specifically: the pre-process node admits verified external
    # callers only, and nothing else sets request.state.trust_level in a
    # standalone deployment. Without this boundary every request arrives
    # anonymous, the trust gate denies it, and the agent answers every call with
    # an error status and no document at all.
    #
    # This is a deployment-level caller credential rather than an agent secret,
    # so the secrets provider does not apply — no invocation context exists
    # before authentication.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would surface as a 500 instead of
        # the generic 401 below.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was
            # absent, malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    if len(req.input) > _MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"input exceeds the {_MAX_INPUT_CHARS}-character limit for one request.",
        )

    # Credential screen on the raw payload, before the graph sees it.
    #
    # The framework's mandatory output scan checks every value of every node
    # result and raises when it finds a credential shape, and the pre-process
    # node carries the caller's text into its own result — so a credential-shaped
    # value in the payload fails a node before any reporting happens, and the
    # caller receives an error status that names nothing they can act on. The
    # request cannot succeed either way, so refusing it here converts an opaque
    # failure into an actionable one.
    #
    # The screen calls the same detector the framework's scan calls, so what this
    # adapter refuses and what the framework blocks are one set by construction,
    # with no local pattern list that could drift from it. The field is named;
    # the value never is.
    if detect_credentials_in_value(req.input):
        emit_trace_event(
            "input_credential_refused",
            {"field": "input"},
            {"session_id": req.session_id},
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "input contains a credential-shaped value. Remove API keys, tokens and "
                "connection strings from the reporting request and retry."
            ),
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "TravelOperationsReportGeneratorAgent"}
