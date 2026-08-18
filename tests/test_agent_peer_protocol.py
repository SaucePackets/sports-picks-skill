import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "sports-picks"
    / "scripts"
    / "agent_peer_protocol.py"
)
spec = importlib.util.spec_from_file_location("agent_peer_protocol_test", SCRIPT_PATH)
assert spec is not None
app = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["agent_peer_protocol_test"] = app
spec.loader.exec_module(app)


ALICE = "a" * 64  # requester pubkey
BOB = "b" * 64  # responder pubkey
CAROL = "c" * 64  # third agent / impostor
T0 = 1_000_000.0


def deliver(guard, message, now, signer=None, relay_event_id=None):
    """Deliver a message as if the relay authenticated its author.

    Defaults the authenticated signer to the envelope's claimed sender —
    the honest case. Impersonation tests pass an explicit mismatched signer.
    """
    if signer is None:
        signer = message.get("sender", "f" * 64) if isinstance(message, dict) else "f" * 64
    return guard.classify_incoming(
        message, now, signer_pubkey=signer, relay_event_id=relay_event_id
    )


def open_pair(now=T0, ttl=3600.0):
    """Return (alice_guard, bob_guard, request_envelope) with the request
    already accepted on Bob's side."""
    alice = app.PeerProtocolGuard(self_pubkey=ALICE)
    bob = app.PeerProtocolGuard(self_pubkey=BOB)
    request = alice.open_request(
        peer=BOB, event_id="ev-req-1", now=now, ttl_seconds=ttl
    )
    decision = deliver(bob, request, now)
    assert decision.action == "handle_request", decision
    return alice, bob, request


def reissue(message, event_id):
    """Same payload re-sent as a new relay event (retry, not exact dup)."""
    copy = dict(message)
    copy["event_id"] = event_id
    return copy


class DuplicateRequestTests(unittest.TestCase):
    def test_exact_duplicate_event_drops(self):
        _, bob, request = open_pair()
        decision = deliver(bob, request, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "duplicate_event")

    def test_resent_request_same_request_id_drops(self):
        _, bob, request = open_pair()
        decision = deliver(bob, reissue(request, "ev-req-1b"), T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "duplicate_request")

    def test_duplicate_request_never_yields_second_ack(self):
        _, bob, request = open_pair()
        bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        deliver(bob, reissue(request, "ev-req-1b"), T0 + 2)
        with self.assertRaises(ValueError):
            bob.build_ack(request["request_id"], "ev-ack-2", now=T0 + 3)


class DuplicateAckTests(unittest.TestCase):
    def test_second_ack_drops(self):
        alice, bob, request = open_pair()
        ack = bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        first = deliver(alice, ack, T0 + 2)
        self.assertEqual(first.action, "accept_ack")
        second = deliver(alice, reissue(ack, "ev-ack-1b"), T0 + 3)
        self.assertTrue(second.dropped)
        self.assertEqual(second.reason, "duplicate_ack")

    def test_responder_cannot_build_second_ack(self):
        _, bob, request = open_pair()
        bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        with self.assertRaises(ValueError):
            bob.build_ack(request["request_id"], "ev-ack-2", now=T0 + 2)


class DuplicateResultTests(unittest.TestCase):
    def test_second_result_drops(self):
        alice, bob, request = open_pair()
        result = bob.build_result(request["request_id"], "ev-res-1", now=T0 + 2)
        first = deliver(alice, result, T0 + 3)
        self.assertEqual(first.action, "accept_result")
        second = deliver(alice, reissue(result, "ev-res-1b"), T0 + 4)
        self.assertTrue(second.dropped)
        self.assertEqual(second.reason, "request_already_closed")

    def test_responder_cannot_build_second_result(self):
        _, bob, request = open_pair()
        bob.build_result(request["request_id"], "ev-res-1", now=T0 + 2)
        with self.assertRaises(ValueError):
            bob.build_result(request["request_id"], "ev-res-2", now=T0 + 3)

    def test_ack_after_result_drops(self):
        alice, bob, request = open_pair()
        ack = bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        result = bob.build_result(request["request_id"], "ev-res-1", now=T0 + 2)
        deliver(alice, result, T0 + 3)
        late_ack = deliver(alice, ack, T0 + 4)
        self.assertTrue(late_ack.dropped)
        self.assertEqual(late_ack.reason, "request_already_closed")


class UnsolicitedPeerMessageTests(unittest.TestCase):
    def test_reply_to_unknown_request_drops(self):
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        stray = {
            "schema": app.PROTOCOL_SCHEMA,
            "type": "result",
            "request_id": "nobody-asked",
            "event_id": "ev-stray-1",
            "reply_to_event_id": "ev-none",
            "sender": BOB,
            "recipient": ALICE,
            "hop_depth": 1,
            "created_at": T0,
            "expires_at": T0 + 3600,
        }
        decision = deliver(alice, stray, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "unsolicited_reply_unknown_request")

    def test_reply_from_wrong_peer_drops(self):
        alice, _, request = open_pair()
        impostor = app.PeerProtocolGuard(self_pubkey=CAROL)
        forged = {
            "schema": app.PROTOCOL_SCHEMA,
            "type": "result",
            "request_id": request["request_id"],
            "event_id": "ev-forged-1",
            "reply_to_event_id": request["event_id"],
            "sender": CAROL,
            "recipient": ALICE,
            "hop_depth": 1,
            "created_at": T0,
            "expires_at": request["expires_at"],
        }
        del impostor
        decision = deliver(alice, forged, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "unexpected_peer_identity")

    def test_wrong_reply_correlation_drops(self):
        alice, bob, request = open_pair()
        result = bob.build_result(request["request_id"], "ev-res-1", now=T0 + 2)
        result["reply_to_event_id"] = "ev-some-other-event"
        decision = deliver(alice, result, T0 + 3)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "correlation_mismatch")

    def test_message_addressed_elsewhere_drops(self):
        _, bob, request = open_pair()
        misaddressed = reissue(request, "ev-req-x")
        misaddressed["recipient"] = CAROL
        decision = deliver(bob, misaddressed, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "not_addressed_to_us")


class SelfTriggerTests(unittest.TestCase):
    def test_own_output_drops(self):
        alice, _, request = open_pair()
        echoed = reissue(request, "ev-echo-1")
        echoed["recipient"] = ALICE
        decision = deliver(alice, echoed, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "self_trigger_own_output")

    def test_cannot_open_request_to_self(self):
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        with self.assertRaises(ValueError):
            alice.open_request(peer=ALICE, event_id="ev-self", now=T0)


class AckEchoTests(unittest.TestCase):
    def test_ack_is_terminal_no_reply_permitted(self):
        alice, bob, request = open_pair()
        ack = bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        decision = deliver(alice, ack, T0 + 2)
        self.assertEqual(decision.action, "accept_ack")
        self.assertFalse(decision.may_reply)

    def test_ack_echoed_back_at_responder_drops(self):
        alice, bob, request = open_pair()
        ack = bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        deliver(alice, ack, T0 + 2)
        # Requester (or relay) reflects the ack back at the responder.
        echo = reissue(ack, "ev-ack-echo-1")
        echo["sender"] = ALICE
        echo["recipient"] = BOB
        decision = deliver(bob, echo, T0 + 3)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "unsolicited_reply_unknown_request")

    def test_ack_never_classified_as_request(self):
        alice, bob, request = open_pair()
        ack = bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        decision = deliver(alice, ack, T0 + 2)
        self.assertNotEqual(decision.action, "handle_request")


class ReplayTests(unittest.TestCase):
    def test_a_b_a_request_replay_drops(self):
        alice, _, request = open_pair()
        replay = reissue(request, "ev-replay-1")
        replay["sender"] = BOB
        replay["recipient"] = ALICE
        decision = deliver(alice, replay, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "own_request_replayed")

    def test_hop_depth_beyond_one_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        deep = {
            "schema": app.PROTOCOL_SCHEMA,
            "type": "request",
            "request_id": "req-deep",
            "event_id": "ev-deep-1",
            "reply_to_event_id": None,
            "sender": CAROL,
            "recipient": BOB,
            "hop_depth": 2,
            "created_at": T0,
            "expires_at": T0 + 3600,
        }
        decision = deliver(bob, deep, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "hop_depth_invalid")

    def test_hop_depth_zero_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        shallow = {
            "schema": app.PROTOCOL_SCHEMA,
            "type": "request",
            "request_id": "req-shallow",
            "event_id": "ev-shallow-1",
            "reply_to_event_id": None,
            "sender": ALICE,
            "recipient": BOB,
            "hop_depth": 0,
            "created_at": T0,
            "expires_at": T0 + 3600,
        }
        decision = deliver(bob, shallow, T0 + 1)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "hop_depth_invalid")


class DelegationCapabilityTests(unittest.TestCase):
    def test_peer_initiated_turn_cannot_delegate(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        self.assertFalse(bob.delegation_allowed(app.PEER_ORIGIN))
        with self.assertRaises(PermissionError):
            bob.open_request(
                peer=CAROL,
                event_id="ev-forward-1",
                now=T0,
                turn_origin=app.PEER_ORIGIN,
            )

    def test_human_initiated_turn_can_delegate(self):
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        self.assertTrue(alice.delegation_allowed(app.HUMAN_ORIGIN))
        request = alice.open_request(peer=BOB, event_id="ev-req-1", now=T0)
        self.assertEqual(request["type"], "request")
        self.assertEqual(request["hop_depth"], 1)


class ExpiryTests(unittest.TestCase):
    def test_expired_request_dropped_by_responder(self):
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        request = alice.open_request(
            peer=BOB, event_id="ev-req-1", now=T0, ttl_seconds=10.0
        )
        decision = deliver(bob, request, T0 + 11)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "request_already_expired")

    def test_late_result_drops_and_expiry_notice_emitted_once(self):
        alice, bob, request = open_pair(ttl=10.0)
        notices = alice.expire_due(now=T0 + 11)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["type"], "expired")
        self.assertEqual(notices[0]["request_id"], request["request_id"])
        self.assertEqual(alice.expire_due(now=T0 + 12), [])
        with self.assertRaises(ValueError):
            bob.build_result(request["request_id"], "ev-res-late", now=T0 + 12)

    def test_expired_notice_closes_responder_side(self):
        alice, bob, request = open_pair(ttl=10.0)
        notice = alice.expire_due(now=T0 + 11)[0]
        decision = deliver(bob, notice, T0 + 12)
        self.assertEqual(decision.action, "accept_expired")
        with self.assertRaises(ValueError):
            bob.build_result(request["request_id"], "ev-res-late", now=T0 + 13)


class SignerBindingTests(unittest.TestCase):
    def test_carol_signed_message_claiming_bob_drops(self):
        # Carol signs the relay event but the envelope claims sender=Bob and
        # copies the correct request correlation. Envelope fields alone would
        # pass every check; the authenticated signer is what kills it.
        alice, bob, request = open_pair()
        result = bob.build_result(request["request_id"], "ev-res-1", now=T0 + 2)
        forged = dict(result)
        decision = deliver(alice, forged, T0 + 3, signer=CAROL)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "signer_mismatch")
        # The genuine Bob-signed result is still accepted afterward.
        genuine = deliver(alice, reissue(result, "ev-res-1-real"), T0 + 4)
        self.assertEqual(genuine.action, "accept_result")

    def test_carol_signed_request_claiming_alice_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        request = alice.open_request(peer=BOB, event_id="ev-req-1", now=T0)
        decision = deliver(bob, request, T0 + 1, signer=CAROL)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "signer_mismatch")

    def test_missing_signer_identity_drops(self):
        _, bob, request = open_pair()
        decision = bob.classify_incoming(
            reissue(request, "ev-req-1c"), T0 + 1, signer_pubkey=""
        )
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "missing_signer_identity")

    def test_relay_event_id_mismatch_drops(self):
        alice, bob, request = open_pair()
        ack = bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        decision = deliver(
            alice, ack, T0 + 2, relay_event_id="ev-something-else"
        )
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "relay_event_id_mismatch")


class ExpiryBoundsTests(unittest.TestCase):
    def _request(self, **overrides):
        message = {
            "schema": app.PROTOCOL_SCHEMA,
            "type": "request",
            "request_id": "req-bounds",
            "event_id": "ev-bounds-1",
            "reply_to_event_id": None,
            "sender": ALICE,
            "recipient": BOB,
            "hop_depth": 1,
            "created_at": T0,
            "expires_at": T0 + 3600,
        }
        message.update(overrides)
        return message

    def test_infinite_ttl_rejected_outbound(self):
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        with self.assertRaises(ValueError):
            alice.open_request(
                peer=BOB, event_id="ev-1", now=T0, ttl_seconds=float("inf")
            )

    def test_nan_and_nonpositive_ttl_rejected_outbound(self):
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        for ttl in (float("nan"), 0, -60, app.MAX_TTL_SECONDS + 1):
            with self.assertRaises(ValueError):
                alice.open_request(
                    peer=BOB, event_id="ev-1", now=T0, ttl_seconds=ttl
                )

    def test_nonfinite_now_rejected_outbound(self):
        alice = app.PeerProtocolGuard(self_pubkey=ALICE)
        with self.assertRaises(ValueError):
            alice.open_request(peer=BOB, event_id="ev-1", now=float("nan"))

    def test_infinite_expires_at_inbound_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        decision = deliver(
            bob, self._request(expires_at=float("inf")), 10**30
        )
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "malformed:invalid_timestamp:expires_at")

    def test_nan_expires_at_inbound_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        decision = deliver(bob, self._request(expires_at=float("nan")), T0)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "malformed:invalid_timestamp:expires_at")

    def test_nan_created_at_inbound_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        decision = deliver(bob, self._request(created_at=float("nan")), T0)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "malformed:invalid_timestamp:created_at")

    def test_finite_but_unbounded_horizon_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        far = T0 + app.MAX_TTL_SECONDS + 1
        decision = deliver(bob, self._request(expires_at=far), T0)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "expiry_horizon_exceeded")

    def test_horizon_at_max_ttl_accepted(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        edge = T0 + app.MAX_TTL_SECONDS
        decision = deliver(bob, self._request(expires_at=edge), T0)
        self.assertEqual(decision.action, "handle_request")


class MalformedMessageTests(unittest.TestCase):
    def test_missing_field_drops(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        decision = deliver(bob, {"schema": app.PROTOCOL_SCHEMA}, T0)
        self.assertTrue(decision.dropped)
        self.assertTrue(decision.reason.startswith("malformed:"))

    def test_reply_without_correlation_drops(self):
        alice, bob, request = open_pair()
        ack = bob.build_ack(request["request_id"], "ev-ack-1", now=T0 + 1)
        ack["reply_to_event_id"] = None
        decision = deliver(alice, ack, T0 + 2)
        self.assertTrue(decision.dropped)
        self.assertEqual(decision.reason, "malformed:reply_missing_correlation")

    def test_unknown_type_never_handled_as_request(self):
        bob = app.PeerProtocolGuard(self_pubkey=BOB)
        weird = {
            "schema": app.PROTOCOL_SCHEMA,
            "type": "ping",
            "request_id": "req-weird",
            "event_id": "ev-weird-1",
            "reply_to_event_id": None,
            "sender": ALICE,
            "recipient": BOB,
            "hop_depth": 1,
            "created_at": T0,
            "expires_at": T0 + 3600,
        }
        decision = deliver(bob, weird, T0)
        self.assertTrue(decision.dropped)


if __name__ == "__main__":
    unittest.main()
