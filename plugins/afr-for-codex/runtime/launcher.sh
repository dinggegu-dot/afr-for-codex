#!/bin/bash
set -euo pipefail

runtime_dir="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != Darwin ]]; then
  printf '%s\n' 'AFR for Codex 0.1 支持 macOS。' >&2
  exit 1
fi

for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 "$HOME/.local/bin/python3" \
  "$HOME"/.local/bin/python3.1[1-9] python3.11; do
  if python_executable="$("$candidate" -I -B -c '
import pathlib
import sys
if sys.version_info < (3, 11):
    raise SystemExit(1)
print(pathlib.Path(sys.executable).resolve())
' 2>/dev/null)"; then
    exec "$python_executable" -I -B "$runtime_dir/bootstrap.py" "$@"
  fi
done

printf '%s\n' 'AFR for Codex 需要 Python 3.11 或更新版本。安装后重新启动 Codex。' >&2
exit 1
