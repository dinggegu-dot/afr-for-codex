from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from afr_agent import semantic, workflow
from afr_agent.intake import digest
from afr_agent.library import RequestError
from afr_share import watch
from afr_share.config import Store, read_policy, update_policy


class FakeLaunchctl:
    def __init__(self):
        self.jobs = {}
        self.fail_bootstrap = False
        self.calls = []
        self.before_bootstrap = lambda: None

    def __call__(self, action, target, *args):
        self.calls.append((action, target, *args))
        if action == "print":
            if target in self.jobs:
                return subprocess.CompletedProcess([], 0, "   state = running\n", "")
            return subprocess.CompletedProcess([], 113, "", 'Bad request.\nCould not find service')
        if action == "bootstrap":
            self.before_bootstrap()
            if self.fail_bootstrap:
                return subprocess.CompletedProcess([], 5, "", "Bootstrap failed")
            plist = plistlib.loads(args[0].read_bytes())
            self.jobs[f"{target}/{plist['Label']}"] = plist
        elif action == "bootout":
            self.jobs.pop(target)
        else:
            raise AssertionError(action)
        return subprocess.CompletedProcess([], 0, "", "")


class WatchTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "share")
        self.config = self.store.configure()
        self.launchctl = FakeLaunchctl()
        self.enterContext(patch.object(watch.sys, "platform", "darwin"))
        self.enterContext(patch.object(watch, "_launchctl", self.launchctl))
        self.enterContext(patch.object(watch.codex, "find_codex", return_value="/opt/homebrew/bin/codex"))

    def start(self, **kwargs):
        config = self.config
        return watch.start(config.state_dir, config.inbox, config.vault, config.approvals, **kwargs)

    def source(self, name):
        path = self.config.inbox / f"{name}.txt"
        text = f"{name}\n这是合成研究材料，样本用于验证本地收录和逐版本批准。\n"
        path.write_text(text, encoding="utf-8")
        return digest(path.read_bytes())

    def refresh(self, *, authorize_new):
        identity = workflow.inbox_identity(self.config.inbox)
        real_cycle = workflow.cycle

        def settled_cycle(*args, **kwargs):
            return real_cycle(*args, **kwargs, settle_seconds=0)

        with patch.object(workflow, "cycle", settled_cycle):
            return watch._cycle(self.store, identity, authorize_new)

    def test_local_opt_in_is_isolated_and_idempotent(self):
        root = self.config.state_dir
        self.assertEqual(watch.status(root)["state"], "stopped")
        status = self.start()
        self.assertEqual(status["state"], "running")
        self.assertFalse(status["authorize_new"])
        self.assertFalse((root / watch.BASELINE).exists())
        target = watch._target(root)
        job = self.launchctl.jobs[target]
        self.assertEqual(job["ProgramArguments"][0], sys.executable)
        self.assertEqual(job["ProgramArguments"][-1], str(root))
        self.assertNotIn("--authorize-new", job["ProgramArguments"])
        self.assertTrue(job["RunAtLoad"])
        self.assertEqual((root / "watch.plist").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.start(), status)
        with self.assertRaisesRegex(RequestError, "watch_already_loaded"):
            self.start(authorize_new=True)
        self.assertEqual(sum(call[0] == "bootstrap" for call in self.launchctl.calls), 1)

        second = Store(root.parent / "separate").configure()
        self.assertNotEqual(watch._target(second.state_dir), target)
        self.launchctl.jobs[watch._target(second.state_dir)] = {}
        self.assertEqual(watch.stop(root)["state"], "stopped")
        self.assertNotIn(target, self.launchctl.jobs)
        self.assertIn(watch._target(second.state_dir), self.launchctl.jobs)

    def test_failed_start_keeps_automatic_authorization_off(self):
        old = self.source("existing")
        root = self.config.state_dir
        self.launchctl.fail_bootstrap = True
        with self.assertRaisesRegex(RequestError, "watch_start_failed"):
            self.start(authorize_new=True)
        self.assertFalse(read_policy(root)["allow_new_articles"])
        sealed = workflow.load_baseline(
            self.config.vault, self.config.inbox, self.config.approvals, baseline_path=root / watch.BASELINE
        )
        self.assertEqual(sealed, {old})

    def test_each_start_reseals_and_preserves_manual_grants_and_denials(self):
        config = self.config
        root = config.state_dir
        old = self.source("existing")
        self.launchctl.before_bootstrap = lambda: self.assertFalse(read_policy(root)["allow_new_articles"])
        self.assertTrue(self.start(authorize_new=True)["authorize_new"])
        self.assertFalse(watch.stop(root)["authorize_new"])
        (config.inbox / "existing.txt").unlink()
        stopped = self.source("added-while-stopped")
        self.assertTrue(self.start(authorize_new=True)["authorize_new"])
        self.source("existing")
        newer = self.source("added-after-start")
        denied = self.source("denied")
        update_policy(root, lambda policy: policy.update(sha256=[stopped], denied_sha256=[denied]))
        with patch.object(semantic, "process", side_effect=RequestError("codex_unavailable")) as process:
            summary = self.refresh(authorize_new=True)
        self.assertEqual({call.args[1] for call in process.call_args_list}, {newer, stopped})
        self.assertEqual(summary["records"], 4)
        self.assertEqual(summary["local_only"], 2)
        self.assertEqual(summary["article_failures"], 2)
        self.assertEqual(len(list(config.inbox.glob("*.txt"))), 4)
        self.assertFalse((config.vault / ".afr/automatic-baseline.json").exists())
        self.assertEqual(workflow.load_baseline(config.vault, config.inbox, config.approvals,
                                                baseline_path=root / watch.BASELINE), {old, stopped})
        watch.stop(root)
        self.assertEqual(read_policy(root)["sha256"], [stopped])
        self.assertEqual(read_policy(root)["denied_sha256"], [denied])

    def test_local_watch_collects_with_no_model_call(self):
        self.source("existing")
        self.start()
        with patch.object(semantic, "process") as process:
            summary = self.refresh(authorize_new=False)
        process.assert_not_called()
        self.assertEqual(summary["collected"], 1)
        self.assertEqual(summary["local_only"], 1)
        self.assertEqual(summary["processed"], 0)


if __name__ == "__main__":
    unittest.main()
