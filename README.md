# Travel Operations Reporting Agent

AI agent for generating travel operations and tourism agency reports, built with Agentic Star.

> **Category**: Cat 2 (domain-specific document generation pipeline)
> **Industry**: Travel & Hospitality
> **Template ID**: TRV-C2-002

## Overview

Takes one accommodation operator's monthly performance figures — occupancy, RevPAR, the
per-channel revenue and booking split, and the same figures for the prior year — and
returns the monthly operations report a tourism authority filing is built from: the
headline indicators, the year-on-year movement on each, the share each sales channel
holds, and the channels whose share has fallen below the threshold the operator sets.
The document is assembled in the retention format the filing requires, with a header
block that identifies it as a retained record.

Two properties shape the pipeline. It is deterministic — the report is composed from the
figures themselves rather than written about them, so the same month always produces the
same document and every number in it can be traced back to the source datum it came from.
And it withholds rather than approximates: a figure that is not finite, not within its
declared bound, or not an inert label is refused outright, because a monthly filing that
quietly rounds a value nobody sent is worse than one that was never produced. Every
released report carries a disclaimer stating it is a draft to be checked before filing.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >= 3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Calling the agent

`POST /invoke` takes the reporting instruction with the month's figures embedded in it as a
JSON object:

```json
{
  "input": "月次運営報告書を作成してください {\"period\": \"2026年5月\", \"occupancy_rate\": 0.72, \"revpar\": 8500.0, \"channels\": {\"OTA\": {\"revenue\": 4200000, \"bookings\": 320}}, \"prior_period\": {\"occupancy_rate\": 0.68, \"revpar\": 7900.0}}",
  "session_id": "may-2026"
}
```

`occupancy_rate` is accepted as a fraction or as a percentage; `revpar` and the per-channel
revenue are JPY amounts; `period` and every channel name are labels rendered into the report
and are therefore restricted to an inert character set. The bounds each field is checked
against are declared in `config/config.yaml` and are read at run time, so changing one there
changes what the agent accepts.

In a standalone deployment the endpoint admits verified external callers only. Set
`INVOKE_AUTH_TOKEN` on the server environment and present it as `Authorization: Bearer …`;
a deployment that puts its own authenticating middleware in front can leave the variable
unset, in which case the trust the middleware establishes is used as-is.

## Project Structure

```
src/          agent implementation (nodes, graph, schemas)
tests/        unit and boundary tests
config/       agent manifest and runtime parameters
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/config.yaml` for your own thresholds and bounds.
2. Adapt the report sections in `src/nodes/narrative_generation_node.py` and the header block
   in `src/nodes/report_formatter_node.py` to the format your filing requires.
3. Review the release checks in `src/nodes/output_gate_node.py` against your own disclosure
   rules.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
