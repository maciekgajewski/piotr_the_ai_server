# Project Documentation Index

## Document status

- **Authority:** Index and navigation
- **Audience:** Agents and project maintainers
- **Read when:** Starting documentation, architecture, protocol, firmware, or operational work

This index is the entry point for project documentation. A document's authority determines how it may be used:

- **Normative:** defines an approved contract or architecture. Implementation drift is a defect.
- **Draft normative:** intended to become normative but not yet approved. Known contradictions must be resolved through the linked design task, not silently interpreted.
- **Operational:** commands and procedures for operating or configuring the current system.
- **Component reference:** focused guidance for one code or tool area.
- **Plan:** proposed work and acceptance criteria; not a current runtime contract.
- **Historical:** investigation record or superseded design; evidence only.

Normative protocol documents follow [the protocol documentation standard](protocol-documentation-standard.md).

## Architecture and protocols

| Document | Authority | Audience and required use | Implementation and tests |
|---|---|---|---|
| [AI Server Conversation Bridge Protocol](ai-server-conversation-protocol.md) | Normative; approved 2026-07-19; implemented by T-004 | Read before changing conversation core, scoped interfaces, input supervision, Agent factories, or application Conversation shutdown | `ai_server/conversations/`, active AgentConversation implementations, input supervision, fatal containment, and bridge tests |
| [Websocket Conversation Protocol](websocket-conversation-protocol.md) | Normative external binding approved 2026-07-19 and implemented by T-004; T-005 client-ownership reconciliation drafted with fresh review pending | Read before changing websocket admission, JSON events, any websocket client, heartbeat, capacity, follow-up gating, or resource leases | Strict JSON binding, websocket adapter/messages, required bounds, admission, gate/lease, and transport tests |
| [Websocket Client Behavior](websocket-client-behavior.md) | Draft normative repository-client contract; amended 2026-07-20; fresh independent review required before Captain approval | Read before changing the repository interactive or batch websocket clients, client presentation, local follow-up timing, or exit behavior | T-005 shared client behavior, interactive and batch presentations, and client conformance tests |
| [Microphone Protocol](microphone-protocol.md) | Normative | Read before changing manager-to-driver behavior, capture, playback, cues, visual state, timeouts, recovery, microphone drivers, or satellite firmware | `ai_server/microphones/`, satellite firmware; microphone and driver conformance tests |
| [Microphone-Conversation Mapping](microphone-conversation-mapping.md) | Normative; approved 2026-07-19; implemented by T-004 | Read before changing how accepted speech, follow-up, bounded rendering, termination, or re-arm crosses the microphone and bridge protocols | Voice InputSession/InputConversation adapter, bounded assistant sink, manager-owned presentation/timing, and mapping tests; Microphone Protocol remains normative |
| [Protocol Conformance Catalogue](protocol-conformance-catalogue.md) | Normative conformance plan; T-004 sections approved 2026-07-19 | Read before implementing or reviewing protocol requirements and tests | Requirement-to-owner and requirement-to-evidence traceability |
| [Orchestrator and DSA Architecture](orchestrator-dsa-architecture.md) | Normative | Read before changing orchestrator or DSA planning, routing, ownership, or prompt architecture | `ai_server/orchestrator/`, DSA implementations; `tests/test_orchestrator_agent.py`, `orchestrator_and_dsa_tests/` |
| [Project-Standard Satellite Behavior](project-standard-satellite-behavior.md) | Normative device behavior, subject to the future Microphone Protocol | Read before changing shared satellite behavior, firmware services, wake words, cues, or controls | `firmware/esphome/`; firmware validation and device checks |

## Plans

| Document | Authority | Purpose |
|---|---|---|
| [T-001 Protocol and Documentation Cleanup](tasks/T-001-protocol-and-documentation-cleanup.md) | Active plan with partially superseded scope | Records the original three-stage cleanup, completed protocol/microphone work, and outstanding hardware verification. Its conversation-core and websocket redesign scope is superseded by T-004. |
| [T-002 Fix Open-Mic Audio Progress Correlation](tasks/T-002-box3-open-mic-audio-progress-correlation.md) | Completed defect-remediation record | Records the fix and live verification for the ESPHome driver failure where inter-segment open-mic audio attempted to emit `AudioProgress` without an active `utterance_id`. |
| [T-003 Bound Open-Mic Pre-Roll and Eliminate the Accepted-Turn Stop Race](tasks/T-003-box3-open-mic-preroll-stop-race.md) | Active defect-remediation task | Fixes unbounded idle pre-roll and stale capture events that can produce `SpeechStarted` in `STOPPING`, leave a satellite in `ERROR`, and prevent an accepted reply. |
| [T-004 Conversation Bridge Protocol Redesign](tasks/T-004-conversation-bridge-protocol-redesign.md) | Active verification task | Defines the approved per-conversation bridge architecture and normative protocol suite. The atomic runtime cutover, independent closure review, 707-test automated checkpoint, and 45-scenario behavioral suite are complete; manual hardware closure remains. |
| [T-005 Websocket Client UX and Protocol Redesign](tasks/T-005-websocket-client-ux-protocol-redesign.md) | Active plan; normative client contract amended 2026-07-20 and awaiting fresh independent review | Replaces duplicated repository websocket-client loops with one typed engine, a `prompt_toolkit` terminal presenter, strict client-side protocol validation, deterministic follow-up timing, and consistent UX/exit behavior. |
| [ESP32-S3-BOX-3 Satellite Plan](esp32-s3-box-3-satellite.md) | Historical plan | Records the original satellite direction. Use current firmware and normative satellite behavior for present requirements. |

## Operational guides

| Document | Authority | Read when |
|---|---|---|
| [Home Assistant Voice PE Bedroom Setup](home-assistant-voice-pe-bedroom-setup.md) | Operational | Building, flashing, configuring, or checking the bedroom Voice Preview satellite |
| [Managing Docker Compose Services](managing-docker-compose-services.md) | Operational | Inspecting or controlling the Compose services |
| [Ollama Cloud Models](ollama-cloud-models.md) | Operational | Signing in, pulling, selecting, or testing Ollama cloud models |
| [ESP32-S3-BOX-3 Tools](../tools/README.md) | Component reference and operational | Using repository tools for Box3, audio, wake-word, TTS, STT, deployment, or model operations |
| [Wake Word Training](../wakeword/README.md) | Component reference and operational | Training or evaluating custom wake words |
| [Agent Loop](../ai_server/agent_loop/README.md) | Component reference | Changing the Ollama agent loop or its tool conventions |

## Historical records

| Document | Authority | Use |
|---|---|---|
| [Setting Up ESP32-S3-BOX-3](../notes/setting-up-esp-box.md) | Historical | Timestamped experiment and setup record. Do not use as a current protocol or operational procedure without verification. |
| [Open-Mic Streaming Protocol](open-mic-protocol.md) | Superseded historical protocol | Redirects agents to the normative Microphone Protocol and mapping; retained as a stable old link |

## Agent reading rules

1. Start with `AGENTS.md`, then use this index to locate documents for the touched subsystem.
2. Read all applicable normative documents before planning a behavioral or architectural change.
3. If a draft normative document contradicts code or another document, record and resolve the design decision; do not silently select one source.
4. Treat operational guides as current procedures only within their declared scope.
5. Treat plans and historical notes as context, not runtime contracts.
