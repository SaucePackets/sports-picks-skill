# Agent Peer Protocol (agent-peer-protocol-v1)

Bounded request/response contract for agent-to-agent collaboration around
this skill on Buzz. Written after an acknowledgment loop between two agents
in a feature channel: an ack was read as a fresh message that deserved its
own ack, and the pair ping-ponged. This contract makes that loop — and
delegation chains, replays, and duplicate work — structurally impossible.

Reference implementation: `scripts/agent_peer_protocol.py` in this skill
(installed alongside this file; repo path
`skills/sports-picks/scripts/agent_peer_protocol.py`). Regression tests:
`tests/test_agent_peer_protocol.py` and `tests/test_install_bundle.py` in the
repo. This file is installed with the skill: any agent running sports-picks
carries this contract, and `SKILL.md` requires loading it before any
agent-to-agent messaging.

This contract governs agent-to-agent messaging only. It does not change the
human feature workflow, and it does not weaken any authorization: the
human-only merge gate stands (only the repo owner merges), and owner/sender
authorization checks elsewhere in this repo are untouched.

## Message types

Every peer message is an `agent-peer-message-v1` envelope with exactly one
type:

| Type | Sent by | Meaning |
|------|---------|---------|
| `request` | requester | Ask one peer to do one bounded task |
| `ack` | responder | Single pickup acknowledgment: "working on it" |
| `result` | responder | Single completion message with the deliverable |
| `blocker` | responder | Single terminal message: cannot complete, here's why |
| `expired` | requester | Request TTL elapsed with no result; request is closed |

Required envelope fields: `schema`, `type`, `request_id` (unique per
request), `event_id` (relay event id), `reply_to_event_id` (every non-request
must name the request's `event_id`), `sender`, `recipient`, `hop_depth`,
`created_at`, `expires_at`. A malformed envelope is dropped, never answered.

## Lifecycle

```text
requester                       responder
   |---- request (id R) --------->|   accept once; dedupe by R + event id
   |<--------- ack (R) -----------|   at most one; TERMINAL for ack phase
   |                              |   ... bounded work ...
   |<---- result | blocker (R) ---|   at most one; closes R
   |                              |
   |-- expired (R) if TTL hits -->|   closes R on both sides
```

## Invariants

1. **Acks are terminal.** The requester records an ack and sends nothing in
   reply — never an ack-of-ack, never any automatic response. In code,
   `Decision.may_reply` is `false` for every accepted ack.
2. **Nothing but a `request` is ever a request.** `ack`, `result`,
   `blocker`, and `expired` can never be classified as work to pick up,
   regardless of wording. Unknown types drop.
3. **Correlation is mandatory.** A reply is accepted only if its
   `request_id` matches an open request we originated, its
   `reply_to_event_id` names that request's event id, and its `sender` is
   the exact peer the request was addressed to. Anything else drops as
   unsolicited, forged, or mis-correlated.
4. **Identity is authenticated, not asserted.** Every classification
   requires the verified Nostr signer of the carrying relay event
   (`signer_pubkey`), and the envelope's self-asserted `sender` must equal
   it — a third party signing its own event while claiming a peer's
   identity drops (`signer_mismatch`) even with perfect correlation. When
   the relay event id is available it must match the envelope's `event_id`.
5. **Dedupe.** Relay event ids are processed at most once; a re-sent request
   with a known `request_id` is dropped and never re-acked.
6. **One ack, one result.** The responder can physically build only one ack
   and one result/blocker per request; the requester drops duplicates of
   either.
7. **Depth-1 only.** Requests originate only from human-initiated turns.
   A peer-initiated turn has no delegation capability: it cannot open a new
   peer request (`open_request` raises), so A→B→C chains cannot form and no
   peer edge is ever created by another peer edge. An inbound request is
   accepted only at `hop_depth` exactly 1 — depth 0 (a peer request claiming
   human origin) is as invalid as depth 2+.
8. **A→B→A replay is dead on arrival.** An incoming request whose
   `request_id` matches one we originated drops (`own_request_replayed`);
   our own echoed output drops (`self_trigger_own_output`); messages
   addressed to someone else drop.
9. **Bounded expiry.** Every request carries `expires_at` (default 6 h,
   hard maximum 24 h on both sides). Non-finite timestamps (NaN, infinity)
   are malformed and drop; a finite expiry beyond the 24 h horizon drops.
   Late acks/results drop on the requester side; the responder refuses to
   build a result for an expired request; the requester emits exactly one
   `expired` notice, which also closes the responder side. Closed is closed
   — nothing reopens a request.
10. **Fail closed.** Any classification doubt ends in a silent drop with a
   recorded reason. A drop never generates an outbound message.

## Human-channel discipline (unchanged, restated)

- One pickup ack per assignment, then silence until the single result or
  blocker. Never acknowledge an acknowledgment.
- The feature workflow is unchanged: one canonical `feat/<slug>` branch,
  review at the exact tip SHA, and merges are performed by the human repo
  owner only. Nothing in this protocol lets an agent approve, merge, or
  authorize on a human's behalf.
