"""使用现有 Codex 登录执行隔离的结构化语义任务。"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .library import RequestError


def find_codex():
    configured = os.environ.get("AFR_FOR_CODEX_CLI")
    if configured:
        binary = Path(configured)
        if binary.is_absolute() and binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
        raise RequestError("codex_unavailable")
    binary = shutil.which("codex")
    if binary:
        return binary
    package = Path("ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    candidates = (
        Path("/opt/homebrew/bin/codex"),
        Path("/usr/local/bin/codex"),
        Path.home() / ".local/bin/codex",
        Path("/Applications") / package,
        Path.home() / "Applications" / package,
    )
    for binary in candidates:
        if binary.is_file() and os.access(binary, os.X_OK):
            return str(binary)
    raise RequestError("codex_unavailable")


def run_structured(instructions, payload, schema, *, timeout=600):
    binary = find_codex()
    model = os.environ.get("AFR_FOR_CODEX_MODEL", "")
    if model and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", model):
        raise RequestError("invalid_codex_model")
    model_args = ["--model", model] if model else []
    with tempfile.TemporaryDirectory(prefix="afr-codex-") as temporary:
        base = Path(temporary)
        schema_file = base / "schema.json"
        schema_file.write_text(json.dumps(schema))
        output = base / "result.json"
        argv = [binary, "exec", *model_args, "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "-c", 'approval_policy="never"',
                "-c", "mcp_servers={}", "-c", 'web_search="disabled"', "-c", 'model_provider="openai"',
                "-c", 'model_reasoning_effort="medium"']
        for feature in ("shell_tool", "unified_exec", "multi_agent", "multi_agent_v2", "hooks", "apps",
                        "plugins", "browser_use", "browser_use_external", "in_app_browser",
                        "skill_mcp_dependency_install"):
            argv.extend(["--disable", feature])
        argv.extend(["--output-schema", str(schema_file), "--output-last-message", str(output), "-"])
        prompt = instructions + "\n" + json.dumps(payload, ensure_ascii=False)
        # 复用本机登录，隔离项目配置、第三方端点和其他凭据。
        environment = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL",
                       "TMPDIR", "USER", "LOGNAME", "CODEX_HOME") if key in os.environ}
        try:
            proc = subprocess.run(argv, cwd=base, env=environment, input=prompt.encode(),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RequestError("codex_timeout") from None
        if proc.returncode or not output.is_file():
            error = proc.stderr.decode(errors="replace").lower()
            if any(word in error for word in ("401", "unauthorized", "authentication", "log in", "login required")):
                raise RequestError("codex_authentication")
            if "model" in error and any(word in error for word in ("not supported", "not available", "does not exist")):
                raise RequestError("codex_model_unavailable")
            raise RequestError("codex_failed")
        try:
            result = json.loads(output.read_text())
        except (UnicodeError, ValueError):
            raise RequestError("invalid_codex_output") from None
        if not isinstance(result, dict):
            raise RequestError("invalid_codex_output")
        return result
