# Protocol Documentation Standard

## Document status

- **Authority:** Normative documentation standard
- **Audience:** Authors and reviewers of project protocols
- **Read when:** Creating or materially changing a protocol document

Every normative protocol document MUST use the following structure or explain why a section does not apply:

1. Status and scope
2. Ownership boundaries
3. Terminology
4. Typed event inventory, grouped by direction
5. State inventory
6. Complete transition table
7. Invariants
8. Timeouts and cancellation
9. Failure and recovery
10. Normal sequences
11. Invalid sequences
12. Observability requirements
13. Implementation and test references
14. Compatibility policy
15. Explicitly unresolved decisions

## Requirement language

- **MUST** and **MUST NOT** identify requirements necessary for conformance.
- **SHOULD** and **SHOULD NOT** identify strong defaults whose exceptions require a documented reason.
- **MAY** identifies optional behavior.

Avoid using requirement words casually in descriptive text.

## Names and boundaries

Every named item MUST be identified as one of:

- protocol event;
- protocol state;
- internal implementation state;
- illustrative sequence step.

Event inventories MUST identify direction, fields, field constraints, sender, receiver, and valid states. Conceptual milestones MUST NOT be formatted as though they were typed events.

Each protocol MUST name the component that owns every state transition, timeout, retry, cancellation, and recovery decision. Device- or transport-specific behavior must remain behind its abstract interface.

## Completeness

A transition table MUST cover every event in every state. Each combination must be classified as:

- a valid transition;
- a valid event without a state transition;
- a protocol violation;
- or inapplicable because the endpoint is already terminal.

Examples MUST satisfy all documented invariants. Appendices MUST NOT present only a convenient subset of the real event vocabulary.

## Traceability

Stable requirements SHOULD have identifiers that can be referenced by tests, for example `CP-MESSAGE-001` or `MP-VISUAL-001`.

Every normative protocol MUST link to:

- its abstract interfaces and message definitions;
- components implementing it;
- conformance tests;
- related protocols or mapping documents.

A protocol change MUST update its conformance tests in the same change. An implementation change that intentionally alters a protocol MUST update and approve the normative document first.

## Size and presentation

Prefer tables for event vocabularies and transition matrices, short numbered sequences for examples, and prose for rationale. Keep rationale separate from requirements. Link to implementation instead of embedding large code excerpts.

Split a document when independent ownership boundaries would otherwise be obscured. Do not split one state machine across multiple documents unless one document is explicitly the normative base and the others are named extensions.
