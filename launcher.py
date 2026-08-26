#!/usr/bin/env python3
"""Cross-platform launcher for the unified pentest harness.

Replaces start-redteam.command (zsh). Works on Windows and macOS/Linux.
Each run is isolated under runs/<run_id>/engagement/.
"""

from __future__ import annotations

import atexit
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


ROOT_DIR = Path(__file__).resolve().parent
COMMON_DIR = ROOT_DIR / "common"

# ------------------------------------------------------------------ run ID

def _generate_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "{0}-{1}".format(ts, secrets.token_hex(2))


def _validate_run_id(run_id: str) -> None:
    if not run_id or "/" in run_id or "\\" in run_id or ".." in run_id:
        print(
            "REDTEAM_RUN_ID는 폴더 이름 하나여야 한다 (/, \\, .. 금지): " + run_id,
            file=sys.stderr,
        )
        sys.exit(1)


# ------------------------------------------------------------ harness rev

def _detect_harness_rev() -> str:
    def git(*args: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(ROOT_DIR)] + list(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").strip()

    rev = git("rev-parse", "--short", "HEAD")
    if not rev:
        return "unknown"
    porcelain = git("status", "--porcelain")
    return rev + "-dirty" if porcelain else rev


# --------------------------------------------------------- find claude CLI

def _find_claude() -> str:
    """Locate the claude CLI executable."""
    # Explicit override
    env_claude = os.environ.get("CLAUDE_BIN")
    if env_claude:
        return env_claude

    # Try common names
    names = ["claude"]
    if sys.platform == "win32":
        names = ["claude.cmd", "claude.exe", "claude"]

    for name in names:
        found = shutil.which(name)
        if found:
            return found

    print(
        "claude CLI를 찾을 수 없습니다. PATH에 추가하거나 CLAUDE_BIN 환경변수를 설정하세요.",
        file=sys.stderr,
    )
    sys.exit(1)


# ------------------------------------------------------------------- main

def main() -> None:
    run_id = os.environ.get("REDTEAM_RUN_ID") or _generate_run_id()
    _validate_run_id(run_id)

    config_label = os.environ.get("REDTEAM_CONFIG_LABEL", "default")
    harness_rev = os.environ.get("REDTEAM_HARNESS_REV") or _detect_harness_rev()
    viewer_port = os.environ.get("REDTEAM_PORT", "8765")

    run_dir = ROOT_DIR / "runs" / run_id
    engagement_dir = run_dir / "engagement"
    settings_file = run_dir / "settings.json"
    viewer_log = engagement_dir / "runtime" / "viewer.log"

    if engagement_dir.exists():
        print(
            "이미 있는 실행 폴더다. 다른 REDTEAM_RUN_ID를 쓰거나 폴더를 지워라: "
            + str(engagement_dir),
            file=sys.stderr,
        )
        sys.exit(1)

    engagement_dir.joinpath("runtime").mkdir(parents=True, exist_ok=True)

    # Set environment for child processes
    env = os.environ.copy()
    env["REDTEAM_COMMON"] = str(COMMON_DIR)
    env["REDTEAM_RUN_DIR"] = str(engagement_dir)
    env["REDTEAM_RUN_ID"] = run_id
    env["REDTEAM_CONFIG_LABEL"] = config_label
    env["REDTEAM_HARNESS_REV"] = harness_rev
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    python = sys.executable

    # Bootstrap
    subprocess.run(
        [python, str(COMMON_DIR / "hook.py"), "bootstrap"],
        env=env,
        stdin=subprocess.DEVNULL,
        check=True,
    )

    # Prepare run (renders settings.json with absolute paths)
    subprocess.run(
        [python, str(COMMON_DIR / "hook.py"), "prepare-run", str(run_dir)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        check=True,
    )

    print("실행 ID: {0} | 구성: {1} | 하네스: {2}".format(run_id, config_label, harness_rev))
    print("기록 폴더: " + str(engagement_dir))

    # Copy SCOPE.yaml into engagement if it exists
    scope_src = ROOT_DIR / "SCOPE.yaml"
    if scope_src.exists():
        shutil.copy2(str(scope_src), str(engagement_dir / "SCOPE.yaml"))

    # Start map_viewer in background
    viewer_log.parent.mkdir(parents=True, exist_ok=True)
    viewer_log_handle = open(str(viewer_log), "w", encoding="utf-8")
    viewer_proc = subprocess.Popen(
        [
            python,
            str(COMMON_DIR / "map_viewer.py"),
            str(engagement_dir / "MAP.md"),
            "--label", "ENGAGEMENT",
            "--port", viewer_port,
        ],
        env=env,
        stdout=viewer_log_handle,
        stderr=viewer_log_handle,
        stdin=subprocess.DEVNULL,
    )

    def cleanup_viewer() -> None:
        try:
            viewer_proc.terminate()
            viewer_proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                viewer_proc.kill()
            except OSError:
                pass
        try:
            viewer_log_handle.close()
        except OSError:
            pass

    atexit.register(cleanup_viewer)

    # Wait for viewer to start and show its address
    for _ in range(20):
        try:
            text = viewer_log.read_text(encoding="utf-8", errors="replace")
            if "실시간 지도:" in text:
                for line in text.splitlines():
                    if "실시간 지도:" in line:
                        print(line)
                        break
                break
        except OSError:
            pass
        time.sleep(0.2)

    # Launch claude CLI
    claude_bin = _find_claude()
    prompt_file = str(COMMON_DIR / "unified-prompt.md")

    claude_args = [
        claude_bin,
        "--settings", str(settings_file),
        "--append-system-prompt-file", prompt_file,
        "--name", "pentest-harness",
    ]

    try:
        proc = subprocess.run(claude_args, env=env, cwd=str(engagement_dir))
        sys.exit(proc.returncode)
    except KeyboardInterrupt:
        print("\n세션 종료.")
        sys.exit(0)
    finally:
        cleanup_viewer()


if __name__ == "__main__":
    main()
