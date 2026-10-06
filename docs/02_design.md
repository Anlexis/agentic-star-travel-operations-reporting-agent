# Design — TRV-C2-002 TravelOperationsReportGeneratorAgent

## 1. Template overview

| Field | Value |
|---|---|
| **Template ID** | `TRV-C2-002` |
| **Template Name** | `TravelOperationsReportGeneratorAgent` |
| **Category** | Cat 2 (document generation pipeline) |
| **Industry** | TRV (Travel) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| **Pattern** | Cat 2 nested: outer `AgentBaseGraph` + a `GraphNode` in the `main` slot → inner `BaseGraph` |

## 2. Purpose

TRV-C2-002 takes one accommodation operator's monthly performance figures — occupancy,
RevPAR, the per-channel revenue and booking split, and the same figures for the prior year —
and produces the monthly operations report a tourism authority filing is built from:
headline indicators, year-on-year movement, per-channel share, the channels below the
operator's underperformance threshold, and a disclaimer stating the document is a draft to
be checked before filing. Every figure in the report is taken from a validated source
datum; the report is composed from the data rather than written about it, so the same month
always produces the same document.

## 3. Architecture

### 3.1 Layer model

```
L1 Base (AgentBaseGraph)            <- TravelOperationsReportGeneratorAgent inherits here
│
└─ Outer backbone (fixed 5 slots):
     START -> initialize -> pre_process -> main -> post_process -> finalize -> END
                                          |  (RETRY)
                                          -> pre_process

       main slot = TravelReportGraphNode(GraphNode)
                                          │
                                          └─ Inner DomainWorkflowGraph(BaseGraph)
                                               START -> data_collection
                                                     -> analysis
                                                     -> narrative_generation
                                                     -> report_formatter
                                                     -> output_gate
                                                     -> END
```

### 3.2 Outer graph (`src/graph/graph.py`)

**Class:** `TravelOperationsReportGeneratorAgent(AgentBaseGraph)`.

| Slot | Node class | Trust level | Role |
|---|---|---|---|
| `pre_process` | `PreProcessNode` | `VERIFIED_EXTERNAL` | Request validation, size cap, injection screen |
| `main` | `TravelReportGraphNode(GraphNode)` | (delegates) | Wraps `DomainWorkflowGraph` |
| `post_process` | `PostProcessNode` | `ANONYMOUS` | Surface the released report |
| `initialize` | `InitializeNode` (framework) | (framework) | Session and schema initialisation |
| `finalize` | `FinalizeNode` (framework) | (framework) | Response metadata |

`add_edges()` is **not overridden** — backbone wiring is the framework's responsibility.

`TravelReportGraphNode` sets `error_strategy = "contain"` and overrides `on_subgraph_error()`.
The default strategy re-raises a failed inner run, and the base node wrapper then turns the
exception into a bare error update whose log carries the raised text — for a nested graph, a
traceback with absolute source paths — and clears none of the output-bearing fields.
Containing the failure is what lets the caller be told the report was withheld without being
told anything about the data or the code that withheld it.

### 3.3 Inner graph (`src/graph/domain_workflow_graph.py`)

**Class:** `DomainWorkflowGraph(BaseGraph)` — all seven abstract methods implemented.

```
START
  -> data_collection      DataCollectionNode       validate + normalize the caller's figures
  -> analysis             AnalysisNode             year-on-year movement + underperformance flags
  -> narrative_generation NarrativeGenerationNode  compose the report from the validated figures
  -> report_formatter     ReportFormatterNode      structure + retention header block
  -> output_gate          OutputGateNode           release checks + disclaimer
  -> END
```

The topology is linear; `route()` is implemented to satisfy the abstract base but no
conditional edge uses it. It is annotated with this graph's own `State` rather than the base
state type, because the graph library reads a path callable's annotation as its input schema
and projects away fields the annotation does not declare.

Every inner node is instantiated with no constructor arguments. Runtime values reach them
through inner state (§5), not through a per-call config argument: the base node wrapper calls
`execute(state)` with the state alone, so a node reading a config parameter always sees the
hard-coded default and every declaration in `config/config.yaml` would be inert.

## 4. Configuration

Two files, with different jobs.

`config/agent.yaml` is the static manifest read by the agent registry: identity, category,
entry-point class, the trust level the agent requires, and the compile-time `requires`
block. `requires.secrets` is empty — no code path calls `ctx.secrets.require()`, and
declaring a secret that is not provisioned makes the agent fail at compile time.

`config/config.yaml` holds the runtime parameters and is passed to the graph constructor by
the registry. The standalone entry point loads the same file through `runtime_config()` and
constructs the agent the same way, so the values are live in both deployments.

| Key | Meaning |
|---|---|
| `max_retry` | Backbone retry budget |
| `timeout_s` | Request timeout honoured by framework service clients |
| `reporting.underperform_threshold` | Revenue share below which a channel is flagged |
| `reporting.max_channels` | Cap on caller-supplied channel entries |
| `reporting.max_label_chars` | Cap on any caller label rendered into the report |
| `reporting.max_report_chars` | Release ceiling for the assembled report |
| `reporting.min_revpar_jpy` / `max_revpar_jpy` | Inclusive bounds for RevPAR |
| `reporting.min_revenue_jpy` / `max_revenue_jpy` | Inclusive bounds for a per-channel revenue figure |
| `reporting.max_bookings` | Upper bound on a per-channel booking count |

Path of a declared value: `config/config.yaml` → the agent constructor → the agent's
`register_nodes()`, which hands its configuration to `TravelReportGraphNode` →
`_parent_config()` → the inner graph constructor → `_extra_initial_state()`, which seeds the
resolved block into **inner** state as `runtime_limits` → the inner nodes read it there.

Seeding into inner state is deliberate. The framework hands a nested graph only the input
string, so a node reading an outer-state key from inner state compares against an empty
mapping on every real invocation: the layer is dead, it looks healthy, and its tests pass.

## 5. State schema (`src/schemas/state.py`)

Flat `TypedDict` extending the framework state. Structured fields are stored as JSON strings
via `to_json()` / `from_json()` — checkpoints are serialised with msgpack and bare containers
are not round-trippable there.

| Field | Type | Producer | Consumer | Notes |
|---|---|---|---|---|
| `validated_input` | `Optional[str]` | `PreProcessNode` | `DataCollectionNode` | Screened request text |
| `runtime_limits` | `Optional[str]` | `DomainWorkflowGraph._extra_initial_state()` | All inner nodes | JSON: the `reporting` block |
| `collected_data` | `Optional[str]` | `DataCollectionNode` | `AnalysisNode` | JSON: normalized figures |
| `analysis_result` | `Optional[str]` | `AnalysisNode` | `NarrativeGenerationNode` | JSON: movement + flags |
| `report_narrative` | `Optional[str]` | `NarrativeGenerationNode` | `ReportFormatterNode` | Composed narrative |
| `formatted_report` | `Optional[str]` | `ReportFormatterNode` | `OutputGateNode` | Assembled document |
| `output_report` | `Optional[str]` | `OutputGateNode` | `merge_output()` | Released document |
| `result` | `Optional[str]` | `merge_output()` | `PostProcessNode` | Outer alias for `output_report` |
| `trace_id` | `Optional[str]` | (framework) | — | Tracing |
| `correlation_id` | `Optional[str]` | (framework) | — | Request correlation |
| `error_code` | `Optional[str]` | `PreProcessNode`, `DataCollectionNode` | `PostProcessNode`, `merge_output()`, every downstream node | Reason marker for a run stopped on a correctable value; never published in the caller envelope (§6.8) |

## 6. Node design

### 6.1 PreProcessNode (`src/nodes/pre_process_node.py`)

Trust level `VERIFIED_EXTERNAL` — the caller-facing decision for the whole graph is taken
once, here.

Stops empty and non-string input and anything over the 200,000-character cap — values the
caller can correct, so the run completes carrying the reason (§6.8). Screens the
request for two classes: directive phrases, and chat-template control tokens (`<|…|>`,
`[INST]`, `<<SYS>>`, `<|system|>`). A screen built only from phrases misses the token class
entirely, and the token form is the more effective attack because it addresses the model's
turn structure rather than its reading of a sentence.

Both classes are checked against the raw text and against the parsed payload, keys included:
an escaped token is invisible in the raw text and plain once the JSON is parsed. The text is
also screened with markup removed, so a directive spliced with tags is caught after the strip
re-assembles it. The directive patterns require a full phrase shape rather than a suggestive
verb, because an unanchored fragment matches ordinary reporting instructions.

A screen refusal terminates the run: spliced instructions are not a value a caller can
correct by rewording, and completing the run would make a refusal read like an ordinary
declined value. It names a class label and never the text that produced it, and it writes a
fixed notice into the caller-facing field — the rest of the backbone is skipped, so this is
the only chance to say anything at all.

### 6.2 DataCollectionNode (`src/nodes/data_collection_node.py`)

Trust level `ANONYMOUS` — inner node.

Owns the caller-data contract. Extracts the JSON object embedded in the request and:

- runs every number through `_finite_in_range`, which rejects booleans, non-numeric text,
  NaN, ±Infinity and out-of-range magnitudes. NaN and Infinity parse cleanly through
  `float()` and travel through JSON intact, and every comparison against NaN is False — so an
  unchecked non-finite value does not raise, it quietly answers "no" to the underperformance
  question the report exists to ask;
- checks every label rendered into the report (`period`, each channel name) against an inert
  character set and a length cap. A label carrying a newline and a heading marker forges a
  section in the released document;
- caps the channel map, which is caller-sized;
- treats key presence, not truthiness, as the test for whether a figure was supplied, so a
  legitimate zero is a value rather than a missing field.

Every bound here is a value the caller can correct, so a breach stops the run carrying the
reason rather than terminating it (§6.8). Rejections name the field — or, for a channel, its
ordinal position — and never the value.

### 6.3 AnalysisNode (`src/nodes/analysis_node.py`)

Trust level `ANONYMOUS`. Computes year-on-year absolute and percentage movement for occupancy
and RevPAR, each channel's share of total revenue, and the set of channels below
`reporting.underperform_threshold`, read from `runtime_limits`. A threshold that is not a
finite fraction falls back to the shipped default rather than being used: against a non-finite
threshold every comparison is False, so nothing is ever flagged and the report reads as if
every channel performed.

### 6.4 NarrativeGenerationNode (`src/nodes/narrative_generation_node.py`)

Trust level `ANONYMOUS`. Deterministic composition — no model call. Every figure is taken
directly from `collected_data` / `analysis_result`, which is what lets the release check
confirm the report reproduces the source.

### 6.5 ReportFormatterNode (`src/nodes/report_formatter_node.py`)

Trust level `ANONYMOUS`. Adds the retention-conforming header block, states any section with
no source data as such rather than omitting it, and assembles the document.

### 6.6 OutputGateNode (`src/nodes/output_gate_node.py`)

Trust level `ANONYMOUS`. The release boundary — three independent checks, each with its own
audit event:

1. **Credential scan.** The framework's own `detect_credentials` runs first, so this gate's
   block set is a superset of the framework's. A narrower local set is itself a bypass: a
   value the framework catches and this gate misses makes the framework raise inside the node
   wrapper, and the wrapper discards this node's whole update — including the clearing below.
   A domain set is layered on top for the operational identifiers (connection markers,
   credential assignments) the framework's list does not carry.
2. **Release ceiling.** A report longer than `reporting.max_report_chars` means the pipeline
   is re-emitting its source payload rather than summarising it.
3. **Source traceability.** Each figure present in the source data must appear in the report
   body, so a report cannot be released having dropped the number it exists to communicate.

On violation the node returns an error status **and** writes every output-bearing field
empty. Returning the status alone is not containment: state updates are merged, so a key the
node does not write keeps whatever value it held, and the response builder resolves the
caller-facing value as formatted-output-or-result with no status check. The cleared set is a
module-level inventory with a test pinning it, so a new report-carrying field has to join it
deliberately rather than by being forgotten.

The violation message is a closed-set label — the check that fired — never the matched value.

### 6.7 PostProcessNode (`src/nodes/post_process_node.py`)

Trust level `ANONYMOUS`. Surfaces the released report. An empty result here means nothing was
produced, so the fixed withheld notice takes its place: an empty caller-facing value would
send the response builder back to the field that is empty.

A reason settled upstream is read BEFORE that branch. Without that order the no-report branch
would fire on a declined request and turn a completed, actionable reason back into a bare
withheld-output error — undoing the point of settling a reason upstream. On that path the
node publishes the reason's sentence as the caller-facing value instead.

### 6.8 Two ways a request stops

No report reaches the caller on either path. The paths differ only in how the stop is
REPORTED back.

| Path | What takes it | How the run ends | What the caller reads |
|---|---|---|---|
| completes carrying a reason | a value the caller can correct: an empty or non-string request, one over the character cap, or source data outside its contract (a non-finite or out-of-range figure, a non-inert or over-long label, more channel entries than the declared cap) | `status = success`, a reason code held in State | the sentence for that reason, as the whole body |
| terminates | content this agent refuses, and a failure of its own release checks: directive phrases or chat-template control tokens in the request, and a release-gate withholding (credential shape, over the release ceiling, a source figure missing from the report, no report assembled) | `status = error`, every output-bearing field cleared and the fixed withheld notice in their place | the withheld notice, no report |

A correctable value completes rather than terminating because terminating ends the calling
surface's turn and surfaces only a status, leaving the reason reachable solely from the audit
trail; completing with a sentence lets the caller fix the value and resubmit on the same
conversation. The release-gate withholdings are not caller values at all — they are this
agent's own checks on an assembled report, and there is nothing for the caller to correct.

| Reason code | The sentence names |
|---|---|
| `EMPTY_INPUT` | that nothing was received to work on |
| `QUESTION_TOO_LONG` | that the request is too long and should be shortened |
| `INVALID_REQUEST` | that a value could not be accepted and should be checked against the documented format |

The code itself never leaves the process. It is a State marker: `PostProcessNode` reads it to
pick the sentence, and the caller envelope carries the sentence as the body and no reason
code — so neither a field path nor an internal log reaches the caller. A code with no entry
in that table falls back to the generic sentence rather than leaking the code.

Once a reason is settled, the main slot does not run the inner graph, `merge_output()` reads
the marker BEFORE its success branch (which would otherwise publish an empty report and lose
the reason), and each inner node passes the marker through untouched instead of reporting its
own precondition failure — so a second, vaguer reason can never replace the specific,
actionable one.

## 7. Output invariant

The report renders exact figures. There is deliberately **no rounding grid**: a filing built
from these numbers needs them as reported, and snapping a monetary aggregate to a coarser
scale would corrupt the deliverable rather than protect it. The invariant the release
boundary enforces instead is that every figure and every label in a released report came from
a validated source datum — which is enforced upstream by refusing anything that is not finite,
not within bounds, or not an inert label, and confirmed at the boundary by the traceability
check.

## 8. Caller interface

`POST /invoke` accepts `input` (the reporting instruction with the figures embedded as JSON)
and an optional `session_id`.

There is deliberately no structured-context channel. The backbone's first node returns
whatever arrives on that channel verbatim into its own result, where the framework's mandatory
output scan reads it — so a credential-shaped value there fails the first node of the graph
before any template code runs. Declaring a narrow contract would not close that, because a
validator ignores undeclared keys rather than dropping them. Not exposing the channel does.

The entry point screens the raw payload with the same detector the framework's scan calls, and
refuses a credential-shaped request with a 400 naming the field. The request cannot succeed
either way; refusing here turns a node failure the caller cannot act on into one they can.

In a standalone deployment the endpoint requires a bearer token (`INVOKE_AUTH_TOKEN`) and
promotes an otherwise-anonymous caller to verified external. Without that boundary every
request arrives anonymous, the trust gate on the pre-process node denies it, and the agent
answers every call with an error and no document. Trust established by upstream middleware is
used as-is and never demoted.
