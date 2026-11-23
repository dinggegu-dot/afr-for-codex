"""Use the public stdio server and isolated local files with no remote model requests."""

import hashlib
import json
import os
from pathlib import Path
from shlex import quote
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlparse


RUNTIME = Path(__file__).resolve().parents[1] / "plugins/afr-for-codex/runtime"
MARKER = "这一行是未授权的合成材料正文。"


class ShareMcpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.state = self.base / "state"
        self.cli = self.base / "fixture-codex"
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "afr_share.server", "--state-dir", str(self.state)],
            cwd=RUNTIME, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env={**os.environ, "AFR_FOR_CODEX_CLI": str(self.cli)},
        )
        self.addCleanup(self._kill)
        self.identifier = 0
        result = self.rpc("initialize", {"protocolVersion": "2025-11-25"})
        self.assertEqual(result["serverInfo"]["name"], "afr-for-codex")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _kill(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def _send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params=None):
        self.identifier += 1
        self._send({"jsonrpc": "2.0", "id": self.identifier, "method": method, "params": params or {}})
        line = self.proc.stdout.readline()
        if not line and self.proc.poll() is not None:
            self.fail(f"MCP closed with status {self.proc.returncode}: {self.proc.stderr.read()}")
        result = json.loads(line)
        self.assertEqual(result["id"], self.identifier)
        return result["result"]

    def tool(self, name, args=None, error=None):
        result = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        value = result["content"][0]["text"]
        if error is not None:
            self.assertTrue(result.get("isError"), result)
            self.assertEqual(value, error)
            return
        self.assertFalse(result.get("isError"), value)
        return json.loads(value)

    def upload(self, filename, payload):
        file = self.state / "Inbox" / filename
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()

    def complete_job(self, sha):
        for _ in range(75):
            status = self.tool("afr_status", {"article_sha": sha})
            if status["job"]["state"] not in ("queued", "running"):
                return status
            time.sleep(0.03)
        self.fail("Background job did not reach a terminal state")

    def test_local_intake_approval_and_revocation(self):
        specs = {entry["name"]: entry for entry in self.rpc("tools/list")["tools"]}
        self.assertTrue({"afr_setup", "afr_status", "afr_scan", "afr_process", "read_article"}.issubset(specs))
        self.assertTrue(specs["afr_process"]["annotations"]["openWorldHint"])
        self.assertTrue(specs["afr_watch_start"]["annotations"]["openWorldHint"])
        self.assertIn("confirm", specs["update_question"]["inputSchema"]["required"])
        self.assertFalse(self.tool("afr_status")["configured"])
        self.tool("afr_setup", error="invalid_request")
        status = self.tool("afr_setup", {"confirm": True})
        self.assertEqual(status["vault"], str(self.state / "Library"))
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / "approvals.json").stat().st_mode & 0o777, 0o600)
        sha = self.upload("资料/测试.md", f"# 城市治理案例\n\n{MARKER}\n".encode())
        other = self.upload("另一份.md", b"# Other\n\nAnother synthetic report.\n")
        scan = self.tool("afr_scan", {"filename": "资料/测试.md"})
        self.assertEqual(scan["versions"], [{"sha256": sha, "readable": True}])
        self.assertNotIn(MARKER, json.dumps(scan, ensure_ascii=False))
        self.assertEqual(len(self.tool("afr_scan")["versions"]), 2)
        self.tool("afr_scan", {"filename": "../settings.json"}, error="invalid_path")
        self.assertEqual(self.tool("list_articles")["articles"], [])
        read = {"article_sha": sha, "start": 1, "limit": 10}
        confirm = {"article_sha": sha, "confirm": True}
        self.tool("read_article", read, error="local_only")
        self.tool("afr_process", confirm, error="local_only")
        self.tool("afr_authorize", {"article_sha": sha, "confirm": False}, error="invalid_request")
        self.tool("afr_authorize", confirm)
        self.assertIn(MARKER, repr(self.tool("read_article", read)))
        self.tool("read_article", {**read, "article_sha": other}, error="local_only")
        self.tool("afr_revoke", confirm)
        self.tool("read_article", read, error="local_only")
        policy = json.loads((self.state / "approvals.json").read_text())
        self.assertEqual(policy["sha256"], [])
        self.assertEqual(policy["denied_sha256"], [sha])
        self.assertFalse(policy["allow_new_articles"])

    def test_refuses_foreign_managed_vault(self):
        vault = self.base / "formal-vault"
        (vault / ".afr").mkdir(parents=True)
        marker = vault / ".afr/intake.sqlite"
        marker.write_bytes(b"independent vault state")
        self.tool("afr_setup", {"vault": str(vault), "confirm": True}, error="vault_already_managed")
        self.assertEqual(marker.read_bytes(), b"independent vault state")
        self.assertFalse(self.tool("afr_status")["configured"])

    def test_background_job_reports_real_failure(self):
        self.tool("afr_setup", {"confirm": True})
        sha = self.upload("sample.md", b"# Synthetic case\n\nThis is temporary local material.\n")
        self.tool("afr_scan")
        args = {"article_sha": sha, "confirm": True}
        self.tool("afr_authorize", args)
        queued = self.tool("afr_process", args)
        self.assertEqual(queued["state"], "queued")
        status = self.complete_job(sha)
        self.assertEqual(status["job"]["id"], queued["id"])
        self.assertEqual(status["job"]["state"], "failed")
        self.assertEqual(status["job"]["error"], "codex_unavailable")
        self.assertEqual(status["article"]["error"], "codex_unavailable")
        self.assertNotIn("files", status["article"])

    def _install_fixture_cli(self):
        stub = self.base / "codex_fixture.py"
        stub.write_text(r"""import json
from pathlib import Path
import sys

schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
payload = json.loads(sys.stdin.read().rsplit('\n', 1)[1])
if 'passed' in schema['properties']:
    result = {'passed': True, 'issues': []}
elif 'clusters' in schema['properties']:
    result = {'clusters': [dict(
        question_id='', new_question='样本口径不同会影响配电表之间的比较吗？',
        links=[dict(sha256=article['sha256'], topic_id=article['topics'][0]['topic_id'],
                    relation='QUALIFIES', reason='样本来源说明了比较口径。')
               for article in payload['articles']])]}
else:
    evidence = payload['evidence']
    result = {'groups': [
        dict(start=evidence[0]['n'], end=evidence[-1]['n'], title='配电样本',
             branch='配电样本', argument='本样本记录同一市场的合成资料。', summary='合成配电样本。')
    ]}
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(result))
""", encoding="utf-8")
        self.cli.write_text(f"#!/bin/sh\nexec {quote(sys.executable)} {quote(str(stub))} \"$@\"\n")
        self.cli.chmod(0o700)

    def test_background_job_publishes_signed_canvas_and_source(self):
        self._install_fixture_cli()
        self.tool("afr_setup", {"confirm": True})
        sha = self.upload("配电样本.md", f"# 配电样本\n\n{MARKER}\n".encode())
        self.tool("afr_scan")
        args = {"article_sha": sha, "confirm": True}
        self.tool("afr_authorize", args)
        queued = self.tool("afr_process", args)
        ready = self.complete_job(sha)
        self.assertEqual(ready["job"]["id"], queued["id"])
        self.assertEqual(ready["job"]["state"], "ready", ready["job"])
        files = ready["article"]["files"]
        self.assertIn(MARKER, Path(files["reading"]["path"]).read_text())
        self.assertTrue(json.loads(Path(files["canvas"]["path"]).read_text())["nodes"])
        uri = urlparse(files["canvas"]["obsidian"])
        self.assertEqual((uri.scheme, uri.netloc), ("obsidian", "open"))
        self.assertEqual(parse_qs(uri.query)["path"], [files["canvas"]["path"]])
        self.tool("afr_revoke", args)
        self.assertNotIn("files", self.tool("afr_status", {"article_sha": sha})["article"])
        self.assertTrue(Path(files["reading"]["path"]).is_file())

    def test_candidate_promotion_is_explicit_and_preserves_all_fixed_links(self):
        self._install_fixture_cli()
        self.tool("afr_setup", {"confirm": True})
        sources = [
            self.upload("配电样本甲.md", b"# One synthetic report\n\nFirst limited test sample.\n"),
            self.upload("配电样本乙.md", b"# Other synthetic report\n\nSecond limited test sample.\n"),
        ]
        self.tool("afr_scan")
        for sha in sources:
            args = {"article_sha": sha, "confirm": True}
            self.tool("afr_authorize", args)
            self.tool("afr_process", args)
            self.assertEqual(self.complete_job(sha)["job"]["state"], "ready")
        self.tool("afr_review_questions", {"confirm": True})
        status = self.complete_job(sources[0])
        self.assertEqual(status["job"]["state"], "ready", status["job"])
        self.assertEqual(status["research"]["candidate_count"], 1)
        self.assertTrue(Path(status["research"]["candidate_artifact"]["path"]).is_file())
        candidates = self.tool("afr_list_question_candidates", {"limit": 1})
        candidate = candidates["candidates"][0]
        self.assertIsNone(candidates["next_offset"])
        self.assertEqual({link["sha256"] for link in candidate["sources"]}, set(sources))
        for link in candidate["sources"]:
            self.assertTrue(link["topic_revision"])
            self.assertIn("#^topic-", link["path"])
        self.assertEqual(self.tool("search_questions", {"query": "配电", "limit": 2})["hits"], [])

        selection = {k: candidate[k] for k in ("batch_id", "candidate_id")}
        self.tool("afr_promote_question_candidate", selection, error="invalid_request")
        self.tool("afr_revoke", {"article_sha": sources[1], "confirm": True})
        self.assertEqual(self.tool("afr_list_question_candidates")["candidates"], [])
        self.assertNotIn("candidate_count", self.tool("afr_status")["research"])
        self.assertNotIn("candidate_artifact", self.tool("afr_status")["research"])
        self.tool("afr_promote_question_candidate", {**selection, "confirm": True}, error="local_only")
        self.tool("afr_authorize", {"article_sha": sources[1], "confirm": True})
        receipt = self.tool("afr_promote_question_candidate", {**selection, "confirm": True})
        self.assertEqual(receipt["title"], candidate["title"])
        self.assertEqual(receipt["added_links"], 2)
        note = Path(receipt["path"])
        self.assertTrue(note.is_file())
        with note.open("a") as stream:
            stream.write("人工补充的观察。\n")
        question = self.tool("read_question", {"id": receipt["id"]})
        self.assertEqual({link["sha256"] for link in question["links"]}, set(sources))
        again = self.tool("afr_promote_question_candidate", {**selection, "confirm": True})
        self.assertEqual((again["id"], again["revision"], again["added_links"]),
                         (receipt["id"], receipt["revision"], 0))
        self.assertIn("人工补充的观察。", note.read_text())
        self.assertEqual(self.tool("afr_list_question_candidates")["candidates"], [])
        self.assertEqual(self.tool("afr_status")["research"]["candidate_count"], 0)

    def test_stale_automatic_permission_does_not_apply_without_watcher(self):
        self.tool("afr_setup", {"confirm": True})
        approvals = self.state / "approvals.json"
        policy = json.loads(approvals.read_text())
        policy["allow_new_articles"] = True
        approvals.write_text(json.dumps(policy))
        sha = self.upload("sample.md", b"# Synthetic case\n\nA previously unset new file.\n")
        self.tool("afr_scan")
        status = self.tool("afr_status", {"article_sha": sha})
        self.assertFalse(status["automatically_authorize_new"])
        self.assertFalse(status["article"]["authorized"])
        self.tool("read_article", {"article_sha": sha, "start": 1, "limit": 10}, error="local_only")

    def test_foreign_nonempty_state_directory_remains_untouched(self):
        foreign = self.base / "foreign-state"
        foreign.mkdir(mode=0o700)
        note = foreign / "user-settings.txt"
        note.write_text("unrelated configuration")
        rejected = subprocess.run(
            [sys.executable, "-B", "-m", "afr_share.server", "--state-dir", str(foreign)],
            cwd=RUNTIME, input=b"", capture_output=True, timeout=5,
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(rejected.stderr.strip(), b"unsafe_state_directory")
        self.assertEqual(list(foreign.iterdir()), [note])

    def test_discussion_write_requires_opt_in_and_blocks_embed_markup(self):
        self.tool("afr_setup", {"confirm": True})
        args = {"id": "q-" + "f" * 32, "expected_revision": 1, "confirm": True}
        valid = "## 笔记\n\n参见 [[研究问题索引]]、[来源](https://example.com/) 和 <https://example.com/>。"
        before = self.rpc("tools/list")["tools"]
        self.tool("update_question", {**args, "body": valid}, error="write_disabled")
        self.tool("afr_discussion_write", {"confirm": True, "enabled": True})
        self.assertEqual(before, self.rpc("tools/list")["tools"])
        self.tool("update_question", {**args, "body": valid}, error="not_found")
        for markup in ("![图片](https://example.com/)", "![引用式][图]", "![[本地图片.png]]",
                       '<iframe src="https://example.com/"></iframe>'):
            self.tool("update_question", {**args, "body": markup}, error="unsafe_discussion_markup")


if __name__ == "__main__":
    unittest.main()
