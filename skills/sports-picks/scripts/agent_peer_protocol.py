#!/usr/bin/env python3
"""Bounded Buzz agent peer request/response protocol guard.

Implements ``agent-peer-protocol-v1`` (see ``references/agent-collaboration.md``):
a bounded, correlation-checked request/response protocol for agent-to-agent
collaboration around this skill. It exists to make acknowledgment loops and
delegation chains structurally impossible, not merely discouraged.

Message types: ``request``, ``ack``, ``result``, ``blocker``, ``expired``.

Hard invariants enforced here:

- Every request carries a unique ``request_id`` and every reply must
  correlate (same ``request_id`` AND ``reply_to_event_id`` naming the
  request's relay event id) and come from the expected peer.
- Identity is authenticated, not asserted: ``classify_incoming`` requires
  the verified Nostr signer of the carrying relay event, and the envelope's
  ``sender`` must equal it.
- A pickup ``ack`` is terminal for the acknowledgment phase: the requester
  never replies to an ack, and no ``ack``/``result``/``blocker``/``expired``
  is ever interpreted as a fresh request.
- One ack and one result (or one blocker) per request; duplicates drop.
- Depth-1 only: requests may originate only from human-initiated turns and
  are accepted only at ``hop_depth`` exactly 1. A peer-initiated turn
  cannot open a new request, so A->B->C chains and A->B->A reflections
  cannot form.
- Bounded expiry: every request carries a finite ``expires_at`` (default
  6 h, hard maximum 24 h); non-finite timestamps drop as malformed, late
  replies drop, and the requester emits a single ``expired`` notice.
- Fail closed: malformed, unaddressed, self-sent, replayed, or unsolicited
  messages drop with a reason and never produce an automatic reply.

Pure stdlib. Callers supply ``now`` explicitly so behavior is deterministic.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

PROTOCOL_SCHEMA = "agent-peer-message-v1"

MESSAGE_TYPES = ("request", "ack", "result", "blocker", "expired")

# Reply types that close a request. ``ack`` is progress, never closure.
TERMINAL_REPLY_TYPES = ("result", "blocker", "expired")

# The only turn origin allowed to open a new peer request.
HUMAN_ORIGIN = "human"
PEER_ORIGIN = "peer"

# The one legal hop depth for a peer request: a human told agent A to ask
# agent B. Depth 0 would be a peer request claiming human origin; depth 2+
# would be a chain. Both are invalid — the peer edge is exactly depth 1.
REQUEST_HOP_DEPTH = 1

DEFAULT_TTL_SECONDS = 6 * 60 * 60

# Hard ceiling on any request's lifetime, inbound or outbound. A request
# whose expiry sits further out than this is unbounded in practice and is
# rejected/dropped rather than tracked forever.
MAX_TTL_SECONDS = 24 * 60 * 60

_REQUIRED_FIELDS = (
    "schema",
    "type",
    "request_id",
    "event_id",
    "sender",
    "recipient",
    "hop_depth",
    "created_at",
    "expires_at",
)


@dataclass(frozen=True)
class Decision:
    """Outcome of classifying one incoming message.

    ``action`` is what the caller may do; ``drop`` means do nothing at all.
    ``may_reply`` is the ONLY authorization to send anything back, and it is
    never true for an ack — that is the terminal-ack rule in code.
    """

    action: str  # handle_request | accept_ack | accept_result |
    #              accept_blocker | accept_expired | drop
    reason: str
    may_reply: bool = False

    @property
    def dropped(self) -> bool:
        return self.action == "drop"


def _drop(reason: str) -> Decision:
    return Decision(action="drop", reason=reason, may_reply=False)


def new_request_id() -> str:
    return str(uuid.uuid4())


def _well_formed(message: dict) -> str | None:
    """Return a drop reason if the envelope is malformed, else None."""
    if not isinstance(message, dict):
        return "not_an_object"
    for key in _REQUIRED_FIELDS:
        if key not in message:
            return f"missing_field:{key}"
    if message["schema"] != PROTOCOL_SCHEMA:
        return "unknown_schema"
    if message["type"] not in MESSAGE_TYPES:
        return "unknown_type"
    if not isinstance(message["hop_depth"], int) or message["hop_depth"] < 0:
        return "invalid_hop_depth"
    for key in ("created_at", "expires_at"):
        value = message[key]
        # bool is an int subclass; NaN and +/-inf survive ordinary
        # comparisons, which is exactly how an unbounded TTL sneaks in.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return f"invalid_timestamp:{key}"
    for key in ("request_id", "event_id", "sender", "recipient"):
        if not isinstance(message[key], str) or not message[key]:
            return f"invalid_field:{key}"
    if message["type"] != "request" and not message.get("reply_to_event_id"):
        return "reply_missing_correlation"
    return None


@dataclass
class _OutboundRequest:
    """Requester-side state for one request we originated."""

    request_id: str
    event_id: str
    peer: str
    expires_at: float
    acked: bool = False
    closed: bool = False


@dataclass
class _InboundRequest:
    """Responder-side state for one request we accepted."""

    request_id: str
    event_id: str
    peer: str
    expires_at: float
    ack_sent: bool = False
    result_sent: bool = False


@dataclass
class PeerProtocolGuard:
    """Per-agent guard over every peer message in and out.

    One instance per agent identity. Both roles live here because the same
    agent can be requester on one request and responder on another; the
    request ledgers are what let us tell a legitimate reply from a replay.
    """

    self_pubkey: str
    _seen_event_ids: set = field(default_factory=set)
    _outbound: dict = field(default_factory=dict)  # request_id -> _OutboundRequest
    _inbound: dict = field(default_factory=dict)  # request_id -> _InboundRequest

    # ---------------- capability gate ----------------

    def delegation_allowed(self, turn_origin: str) -> bool:
        """Whether the current turn may open a new peer request.

        Only human-initiated turns may delegate. A turn triggered by any
        peer message (request, ack, result, blocker, expired) gets no
        delegation capability, which is what keeps the graph depth-1.
        """
        return turn_origin == HUMAN_ORIGIN

    # ---------------- requester side ----------------

    def open_request(
        self,
        peer: str,
        event_id: str,
        now: float,
        turn_origin: str = HUMAN_ORIGIN,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> dict:
        """Create a new outbound request envelope, or raise if forbidden."""
        if not self.delegation_allowed(turn_origin):
            raise PermissionError(
                "peer-initiated turns cannot open new peer requests"
            )
        if peer == self.self_pubkey:
            raise ValueError("cannot open a request to self")
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("now must be a finite timestamp")
        if (
            not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
            or ttl_seconds > MAX_TTL_SECONDS
        ):
            raise ValueError(
                "ttl_seconds must be finite, positive, and at most "
                f"{MAX_TTL_SECONDS} seconds"
            )
        request_id = new_request_id()
        self._outbound[request_id] = _OutboundRequest(
            request_id=request_id,
            event_id=event_id,
            peer=peer,
            expires_at=now + ttl_seconds,
        )
        return {
            "schema": PROTOCOL_SCHEMA,
            "type": "request",
            "request_id": request_id,
            "event_id": event_id,
            "reply_to_event_id": None,
            "sender": self.self_pubkey,
            "recipient": peer,
            "hop_depth": REQUEST_HOP_DEPTH,
            "created_at": now,
            "expires_at": now + ttl_seconds,
        }

    def expire_due(self, now: float) -> list:
        """Close overdue outbound requests; return one expired notice each.

        Idempotent: a request expires at most once, then stays closed, so
        late acks/results afterward drop and no second notice is emitted.
        """
        notices = []
        for req in self._outbound.values():
            if req.closed or now < req.expires_at:
                continue
            req.closed = True
            notices.append(
                {
                    "schema": PROTOCOL_SCHEMA,
                    "type": "expired",
                    "request_id": req.request_id,
                    "event_id": f"expired-{req.request_id}",
                    "reply_to_event_id": req.event_id,
                    "sender": self.self_pubkey,
                    "recipient": req.peer,
                    "hop_depth": REQUEST_HOP_DEPTH,
                    "created_at": now,
                    "expires_at": req.expires_at,
                }
            )
        return notices

    # ---------------- responder side ----------------

    def build_ack(self, request_id: str, event_id: str, now: float) -> dict:
        """Build the single pickup ack for an accepted inbound request."""
        req = self._require_inbound(request_id)
        if req.ack_sent:
            raise ValueError("ack already sent for this request")
        req.ack_sent = True
        return self._reply_envelope("ack", req, event_id, now)

    def build_result(
        self, request_id: str, event_id: str, now: float, blocker: bool = False
    ) -> dict:
        """Build the single result (or blocker) closing an inbound request."""
        req = self._require_inbound(request_id)
        if req.result_sent:
            raise ValueError("result already sent for this request")
        if now >= req.expires_at:
            raise ValueError("request expired; result would be dropped")
        req.result_sent = True
        return self._reply_envelope(
            "blocker" if blocker else "result", req, event_id, now
        )

    def _require_inbound(self, request_id: str) -> _InboundRequest:
        req = self._inbound.get(request_id)
        if req is None:
            raise KeyError(f"no accepted inbound request {request_id!r}")
        return req

    def _reply_envelope(
        self, msg_type: str, req: _InboundRequest, event_id: str, now: float
    ) -> dict:
        return {
            "schema": PROTOCOL_SCHEMA,
            "type": msg_type,
            "request_id": req.request_id,
            "event_id": event_id,
            "reply_to_event_id": req.event_id,
            "sender": self.self_pubkey,
            "recipient": req.peer,
            "hop_depth": REQUEST_HOP_DEPTH,
            "created_at": now,
            "expires_at": req.expires_at,
        }

    # ---------------- classification ----------------

    def classify_incoming(
        self,
        message: dict,
        now: float,
        *,
        signer_pubkey: str,
        relay_event_id: str | None = None,
    ) -> Decision:
        """Classify one incoming peer message. Fail closed on anything odd.

        ``signer_pubkey`` is REQUIRED and must be the authenticated author of
        the relay event that carried this envelope (the verified Nostr event
        pubkey) — never a value read from the envelope itself. The envelope's
        self-asserted ``sender`` is only accepted when it matches the signer,
        so a third party cannot impersonate a peer by claiming its identity
        and copying the correlation fields. When the caller has the relay
        event id, pass it as ``relay_event_id`` and it must match the
        envelope's ``event_id``.
        """
        malformed = _well_formed(message)
        if malformed is not None:
            return _drop(f"malformed:{malformed}")

        if not isinstance(signer_pubkey, str) or not signer_pubkey:
            return _drop("missing_signer_identity")
        if message["sender"] != signer_pubkey:
            return _drop("signer_mismatch")
        if relay_event_id is not None and relay_event_id != message["event_id"]:
            return _drop("relay_event_id_mismatch")

        if message["sender"] == self.self_pubkey:
            return _drop("self_trigger_own_output")
        if message["recipient"] != self.self_pubkey:
            return _drop("not_addressed_to_us")

        event_id = message["event_id"]
        if event_id in self._seen_event_ids:
            return _drop("duplicate_event")
        self._seen_event_ids.add(event_id)

        if message["type"] == "request":
            return self._classify_request(message, now)
        return self._classify_reply(message, now)

    def _classify_request(self, message: dict, now: float) -> Decision:
        request_id = message["request_id"]
        if message["hop_depth"] != REQUEST_HOP_DEPTH:
            # Depth 0 (a peer request claiming human origin) is as invalid
            # as depth 2+ (a chain): the only legal peer edge is exactly 1.
            return _drop("hop_depth_invalid")
        if now >= message["expires_at"]:
            return _drop("request_already_expired")
        if message["expires_at"] - now > MAX_TTL_SECONDS:
            return _drop("expiry_horizon_exceeded")
        if request_id in self._outbound:
            # Our own request came back at us: A->B->A replay.
            return _drop("own_request_replayed")
        if request_id in self._inbound:
            return _drop("duplicate_request")
        self._inbound[request_id] = _InboundRequest(
            request_id=request_id,
            event_id=message["event_id"],
            peer=message["sender"],
            expires_at=message["expires_at"],
        )
        return Decision(
            action="handle_request",
            reason="new_request_accepted",
            may_reply=True,
        )

    def _classify_reply(self, message: dict, now: float) -> Decision:
        request_id = message["request_id"]
        req = self._outbound.get(request_id)
        if req is None:
            # A requester may cancel work with an expired notice; accept it
            # on the responder side only from the original requester with
            # correct correlation, and mark the inbound request closed.
            if message["type"] == "expired":
                inbound = self._inbound.get(request_id)
                if (
                    inbound is not None
                    and message["sender"] == inbound.peer
                    and message["reply_to_event_id"] == inbound.event_id
                    and not inbound.result_sent
                ):
                    inbound.result_sent = True
                    return Decision(
                        action="accept_expired", reason="request_cancelled"
                    )
            # Includes acks/results echoed at a responder: a responder holds
            # no outbound entry, so a reflected ack can never re-trigger it.
            return _drop("unsolicited_reply_unknown_request")
        if message["sender"] != req.peer:
            return _drop("unexpected_peer_identity")
        if message["reply_to_event_id"] != req.event_id:
            return _drop("correlation_mismatch")
        if req.closed:
            return _drop("request_already_closed")

        msg_type = message["type"]
        if msg_type == "ack":
            if now >= req.expires_at:
                return _drop("ack_after_expiry")
            if req.acked:
                return _drop("duplicate_ack")
            req.acked = True
            # Terminal ack: record it and do nothing. may_reply stays False,
            # so no ack-of-ack can ever be produced through this guard.
            return Decision(action="accept_ack", reason="ack_recorded")

        if msg_type in ("result", "blocker"):
            if now >= req.expires_at:
                return _drop("reply_after_expiry")
            req.closed = True
            return Decision(
                action=f"accept_{msg_type}", reason=f"{msg_type}_recorded"
            )

        if msg_type == "expired":
            req.closed = True
            return Decision(action="accept_expired", reason="expired_recorded")

        return _drop("unknown_reply_type")
