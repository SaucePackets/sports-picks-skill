"""Installed-artifact integration tests for the agent peer protocol.

The Hermes installer (`scripts/install-hermes.sh`) copies `skills/*` into
`$HERMES_HOME/skills/sports/`. These tests replicate that exact copy step and
verify the installed bundle actually carries the anti-loop safeguard: the
guard module, the protocol contract, and the SKILL.md wiring that tells the
installed agent to load them. This is what keeps the safeguard connected to
installed behavior instead of living only at the repo top level.
"""

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
INSTALLER = REPO_ROOT / "scripts" / "install-hermes.sh"


def install_bundle(dest: Path) -> Path:
    """Replicate the installer's skills copy: cp -R repo/skills/* dest/."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in SKILLS_DIR.iterdir():
        target = dest / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)
    return dest


class InstallerScriptTests(unittest.TestCase):
    def test_installer_copies_the_whole_skills_tree(self):
        # The safeguard ships inside skills/sports-picks; if the installer
        # ever stops copying skills/* wholesale, this wiring silently breaks.
        script = INSTALLER.read_text()
        self.assertIn('cp -R "$TMP_DIR/repo/skills/"*', script)

    def test_guard_and_contract_live_inside_the_installed_tree(self):
        self.assertTrue(
            (SKILLS_DIR / "sports-picks" / "scripts" / "agent_peer_protocol.py").is_file()
        )
        self.assertTrue(
            (SKILLS_DIR / "sports-picks" / "references" / "agent-collaboration.md").is_file()
        )


class InstalledBundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.installed = install_bundle(Path(self._tmp.name) / "skills" / "sports")

    def tearDown(self):
        self._tmp.cleanup()

    def test_installed_bundle_contains_the_safeguard_files(self):
        base = self.installed / "sports-picks"
        self.assertTrue((base / "scripts" / "agent_peer_protocol.py").is_file())
        self.assertTrue((base / "references" / "agent-collaboration.md").is_file())

    def test_installed_skill_wires_the_protocol_in(self):
        skill = (self.installed / "sports-picks" / "SKILL.md").read_text()
        self.assertIn("references/agent-collaboration.md", skill)
        self.assertIn("agent-peer-protocol-v1", skill)
        self.assertIn("scripts/agent_peer_protocol.py", skill)
        contract = (
            self.installed / "sports-picks" / "references" / "agent-collaboration.md"
        ).read_text()
        self.assertIn("agent-peer-protocol-v1", contract)
        self.assertIn("Acks are terminal", contract)

    def test_installed_guard_module_enforces_terminal_ack(self):
        # Import the guard from the INSTALLED location and run the core
        # anti-loop invariant end to end: ack recorded, no reply permitted,
        # peer-initiated delegation refused.
        module_path = (
            self.installed / "sports-picks" / "scripts" / "agent_peer_protocol.py"
        )
        spec = importlib.util.spec_from_file_location(
            "agent_peer_protocol_installed", module_path
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["agent_peer_protocol_installed"] = mod
        spec.loader.exec_module(mod)

        alice = mod.PeerProtocolGuard(self_pubkey="a" * 64)
        bob = mod.PeerProtocolGuard(self_pubkey="b" * 64)
        request = alice.open_request(peer="b" * 64, event_id="ev-req", now=0.0)
        accepted = bob.classify_incoming(
            request, 1.0, signer_pubkey=request["sender"]
        )
        self.assertEqual(accepted.action, "handle_request")
        ack = bob.build_ack(request["request_id"], "ev-ack", now=2.0)
        decision = alice.classify_incoming(ack, 3.0, signer_pubkey=ack["sender"])
        self.assertEqual(decision.action, "accept_ack")
        self.assertFalse(decision.may_reply)
        with self.assertRaises(PermissionError):
            bob.open_request(
                peer="c" * 64,
                event_id="ev-fwd",
                now=4.0,
                turn_origin=mod.PEER_ORIGIN,
            )


if __name__ == "__main__":
    unittest.main()
