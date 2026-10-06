# Test Specification — TRV-C2-002

## Strategy

Every test is deterministic: no model call, no network, no clock dependence. The suite is
organised around the three things that can be true of a green suite and false of the agent.

1. **The deployed entry point.** The checks in `tests/unit/test_caller_contract.py` that
   drive the agent go through the real ASGI application in `src/api/server.py`, at the trust
   level `config/agent.yaml` declares. A suite that only calls `execute()` and `invoke()`
   cannot see an entry point that admits callers at a level the first node refuses — which
   is a defect that makes every request fail while every test passes.
2. **The caller contract.** Every caller-controlled number is run against a non-finite
   matrix (`NaN`, `±Infinity` as raw floats and as JSON text, booleans, non-numeric text)
   on every field that accepts one. Every caller-supplied label is run against the inert
   alphabet. Both directions are checked: attack forms refused, and real domain values —
   Japanese channel names, `2026年5月`, a zero occupancy month — accepted.
3. **The release boundary.** The withholding checks assert that each output-bearing field
   is present in the returned update and empty. State updates are merged, so an omitted key
   keeps its previous value: an assertion that only checks falsiness passes on a gate that
   clears nothing at all.

The two ways a request stops (§6.8 of the design) are asserted apart throughout, and neither
predicate is allowed to satisfy the other. A value the caller can correct is checked for
success **with** a reason code — the status alone would also hold for a run that quietly
answered. Content this agent refuses, and a release-gate withholding, keep asserting error
status with nothing carried forward.

Node-level security checks call `execute()` directly. The framework's own input gate refuses
some payloads before a node body runs, so an end-to-end assertion cannot distinguish "the
template refused" from "the framework refused first", and the template owns its guarantee
wherever that gate is absent or configured off.

## Framework compliance

| TC-ID | Test | Expected result | Where |
|-------|------|-----------------|-------|
| TC-01 | State is a flat TypedDict of primitives | Type check passes; no model objects | `tests/proof_of_boundary/test_state_safety.py` |
| TC-02 | Invalid caller input does not produce a report | A value the caller can correct: success status **with** a reason code, nothing carried forward. Asserting the status alone would pass on a run that quietly answered | `test_caller_contract.py::TestCallerNumbers` |
| TC-03 | No credential literal in state or repository | Credential gate: 0 findings | `scripts/check_credentials.py` |
| TC-04 | Invocation context reaches nodes through the graph only | Direct access refused | `tests/proof_of_boundary/test_pb_invoke_order.py` |
| TC-05 | No duplicate lifecycle events inside `execute()` | Framework emits them; nodes do not | `scripts/check_audit_trace.py` |
| TC-06 | The default input gate is not overridden | `TypeError` at class definition if overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-07 | The default output gate is not overridden | `TypeError` at class definition if overridden | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` is declared and enforced | Insufficient trust → refused | `scripts/check_trust_level.py` |
| TC-09 | Domain input screening is non-trivial | Control tokens and directive phrases terminate the run (error status, no `validated_input`); ordinary domain text passes | `test_caller_contract.py::TestInjectionScreen` |
| TC-10 | Domain output screening is non-trivial | Credential shapes, ceiling and traceability enforced | `test_caller_contract.py::TestReleaseBoundary` |
| TC-11 | Each node emits a domain audit event | ≥1 reachable emit per node | `scripts/check_audit_trace.py` |

## Proof-of-boundary

| PB-ID | Boundary | Test | Expected result |
|-------|----------|------|-----------------|
| PB-1 | Node → audit sink | A domain event fires on every invocation path | No silent failures |
| PB-2 | State serialization | Post-invoke state holds primitives only | No model objects |
| PB-4 | Import isolation | The template imports the framework only | Syntax-tree scan: 0 violations |
| PB-5 | Checkpoint safety | No credential or model object in a checkpoint | Inspection passes |
| PB-6 | Invoke execution order | The backbone runs initialize → pre-process → main → post-process → finalize | Order verified |
| PB-7 | Human-review interrupt propagation | The interrupt contract holds, or the template declares it unused | Verified against `config/config.yaml` |

## Business logic

| TC-ID | Test | Input | Expected result |
|-------|------|-------|-----------------|
| BL-01 | A month's figures produce the report | Occupancy, RevPAR, three channels, prior year | Report carries `72.0%`, `¥8,500`, each channel's share and the year-on-year movement |
| BL-02 | The underperformance threshold is a live declared value | The same request at 0.05 and at 0.50 | Different channels flagged; the report states the threshold it used |
| BL-03 | A bound declared on the agent reaches the inner pipeline | `max_channels: 1` with three channels | No report; the run completes with the invalid-value sentence as the body — the declared bound is what declines it |
| BL-04 | A non-finite figure is declined, not absorbed | `NaN` / `Infinity` on any numeric field | Success status carrying a reason code; no `collected_data` |
| BL-05 | An out-of-range figure is declined | Occupancy 101, negative RevPAR, RevPAR 1e300 | Success status carrying a reason code; no `collected_data` |
| BL-06 | Zero is a value, not an absence | `occupancy_rate: 0`, `revpar: 0` | Report states 0.0% and ¥0 |
| BL-07 | A label that would forge report structure is declined | Channel name containing a newline and a heading marker | Success status carrying a reason code; `error_log` names the position, not the label |
| BL-08 | Real domain labels are accepted | `直販`, `楽天トラベル`, `Booking.com`, `2026年5月` | Report released |
| BL-09 | A credential shape in the report withholds it | Every pattern the framework detector carries, plus the domain set | Error status; the value appears nowhere in the update |
| BL-10 | A withheld report clears every output-bearing field | Any release violation | Each field present in the update and empty |
| BL-11 | A declined request carries its reason and no report text | Declined request through `/invoke` | Success status; the body is the fixed reason sentence and nothing else — no reason code in the envelope, no figures, no traceback, no source paths |
| BL-12 | The clean path still answers | The same request without the fault | Report released with the disclaimer |

## Execution

Run the suite with `python -m pytest tests/ -v`. Verification is done against the framework
wheel the pipeline installs; a run against a stub is not a run.
