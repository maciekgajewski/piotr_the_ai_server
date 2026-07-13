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
| [AI Server Conversation Protocol](ai-server-conversation-protocol.md) | Normative | Read before changing sessions, conversation endpoints, websocket protocol, websocket clients, or microphone-to-session mapping | `ai_server/messages.py`, `ai_server/interfaces.py`, `ai_server/sessions.py`, `ai_server/websocket_server.py`; focused Session, message, and websocket tests |
| [Microphone Protocol](microphone-protocol.md) | Normative | Read before changing manager-to-driver behavior, capture, playback, cues, visual state, timeouts, recovery, microphone drivers, or satellite firmware | `ai_server/microphones/`, satellite firmware; microphone and driver conformance tests |
| [Microphone-Conversation Mapping](microphone-conversation-mapping.md) | Normative | Read before changing how accepted speech, follow-up, processing, playback, termination, or re-arm crosses the two protocols | `ai_server/microphones/manager.py`, microphone Session endpoint; mapping tests |
| [Protocol Conformance Catalogue](protocol-conformance-catalogue.md) | Normative conformance plan | Read before implementing or reviewing protocol requirements and tests | Requirement-to-owner and requirement-to-test traceability |
| [Orchestrator and DSA Architecture](orchestrator-dsa-architecture.md) | Normative | Read before changing orchestrator or DSA planning, routing, ownership, or prompt architecture | `ai_server/orchestrator/`, DSA implementations; `tests/test_orchestrator_agent.py`, `orchestrator_and_dsa_tests/` |
| [Project-Standard Satellite Behavior](project-standard-satellite-behavior.md) | Normative device behavior, subject to the future Microphone Protocol | Read before changing shared satellite behavior, firmware services, wake words, cues, or controls | `firmware/esphome/`; firmware validation and device checks |

## Plans

| Document | Authority | Purpose |
|---|---|---|
| [T-001 Protocol and Documentation Cleanup](tasks/T-001-protocol-and-documentation-cleanup.md) | Active plan | Defines the three-stage documentation, protocol-design, and implementation migration. It records known protocol-document drift and the approval gate before implementation. |
| [T-002 Fix Open-Mic Audio Progress Correlation](tasks/T-002-box3-open-mic-audio-progress-correlation.md) | Completed defect-remediation record | Records the fix and live verification for the ESPHome driver failure where inter-segment open-mic audio attempted to emit `AudioProgress` without an active `utterance_id`. |
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
