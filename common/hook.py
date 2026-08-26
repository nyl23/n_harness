#!/usr/bin/env python3
"""Shared Claude Code hook: record the active Stage without sharing run state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from engine import (
        allocate_event,
        append_event,
        bootstrap,
        ensure_agent_branch,
        is_internal_harness_call,
        locked_state,
        payload_bytes,
        prepare_run,
        record_private_evidence,
        render_unlocked,
        safe_action_label,
        scope_enforced,
        unapproved_in,
        utc_now,
    )
except BaseException as error:  # noqa: BLE001 - engine은 루트를 못 찾으면 SystemExit을 던진다
    # 여기서 죽으면 PreToolUse가 판단 없이 사라져 도구가 그대로 실행된다.
    # 원인을 기억해 두었다가 pre 모드에서 차단 사유로 돌려준다.
    IMPORT_ERROR: str | None = "{0}: {1}".format(type(error).__name__, error)
else:
    IMPORT_ERROR = None


MAPCTL_PATH = Path(__file__).resolve().with_name("mapctl.py")


def read_input() -> Dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


_EMITTED = False


def emit(value: Dict[str, Any]) -> None:
    global _EMITTED
    _EMITTED = True
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except UnicodeEncodeError:
        text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    # stdout 코덱이 cp1252/cp949 같은 non-UTF8이면 sys.stdout.write가 한글에서
    # UnicodeEncodeError를 던진다. pre 훅에서는 이게 fail-open(도구가 판단 없이
    # 실행)으로 이어지므로 코덱에 상관없이 UTF-8 바이트로 직접 내보낸다.
    data = (text + "\n").encode("utf-8", errors="replace")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        sys.stdout.flush()


def agent_key(hook: Dict[str, Any]) -> str:
    return str(hook.get("agent_id") or "main")


def stage_key(state: Dict[str, Any] | None = None) -> str:
    """Stage는 이제 승인 흐름이 정하는 상태 라벨이다. 환경변수는 초기값으로만 쓴다."""
    if isinstance(state, dict):
        current = state.get("current_stage")
        if current:
            return str(current)[:80]
    return str(os.environ.get("REDTEAM_STAGE") or "stage1")[:80]


def requires_classification(hook: Dict[str, Any]) -> bool:
    tool_name = str(hook.get("tool_name") or "")
    return tool_name in {"Bash", "WebFetch", "WebSearch"} or tool_name.startswith("mcp__")


def reachable_text(hook: Dict[str, Any]) -> str:
    """외부로 나가는 도구 입력만 문자열로 모은다."""
    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    try:
        return json.dumps(tool_input, ensure_ascii=False)
    except (TypeError, ValueError, UnicodeEncodeError):
        # Windows에서 surrogateescape로 읽힌 경로/출력에 서로게이트 문자(\uD800-\uDFFF)가
        # 섞이면 json.dumps(ensure_ascii=False)가 UnicodeEncodeError를 던진다.
        # 서로게이트를 제거하고 재시도한다. IP 추출에는 ASCII만 필요하므로 손실 없다.
        try:
            cleaned = json.dumps(tool_input, ensure_ascii=True)
            return cleaned
        except (TypeError, ValueError, UnicodeEncodeError):
            return str(tool_input).encode("utf-8", errors="replace").decode("utf-8")


# 실패 훅과 성공 훅은 결과를 담는 키가 다르다. 먼저 있는 키 하나를 결과로 본다.
_RESPONSE_KEYS = ("tool_response", "tool_result", "error")


def response_bytes(hook: Dict[str, Any]) -> int:
    """도구가 돌려준 결과의 바이트 수. 구성별 I/O 비교에서 응답 쪽 값이 된다."""
    for key in _RESPONSE_KEYS:
        if key in hook:
            return payload_bytes(hook[key])
    return 0


def deny_for_scope(blocked: list[str]) -> None:
    mapctl = str(MAPCTL_PATH)
    ids = ", ".join(blocked)
    reason = (
        "승인되지 않은 대상입니다: {0}. 이 IP는 아직 사용자가 범위에 넣지 않았습니다. "
        "새 경계를 발견했다면 먼저 근거와 함께 승인 요청을 올리세요: "
        "`python3 {1} target-propose --value {2} --evidence E-xxxx --reason '무엇을 근거로 다음 대상이라 판단했는지'`. "
        "사용자가 대시보드에서 승인하면 새 Stage가 열리고 차단이 풀립니다. "
        "이는 탐색 방향 제한이 아니라 승인 범위 밖 행위를 막는 안전장치입니다."
    ).format(ids, mapctl, blocked[0])
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def deny_for_pending(hook: Dict[str, Any], pending_ids: list[str]) -> None:
    mapctl = str(MAPCTL_PATH)
    ids = ", ".join(pending_ids)
    reason = (
        "실시간 지도 동기화 대기: {0}. 결과를 판단한 뒤 mapctl로 분류하세요. "
        "변화가 없으면 `python3 {1} resolve --event {2} "
        "--outcome no-change --summary '짧은 판단'`; 단서면 같은 도구의 clue 명령을 사용하세요. "
        "이는 탐색 방향 제한이 아니라 기록 원자성 보장입니다."
    ).format(ids, mapctl, pending_ids[0])
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def handle_pre(hook: Dict[str, Any]) -> None:
    if is_internal_harness_call(hook):
        return
    agent = agent_key(hook)
    with locked_state() as state:
        awaiting = [
            eid
            for eid, item in state["pending"].items()
            if item.get("agent") == agent and item.get("status") == "AWAITING_CLASSIFICATION"
        ]
        if awaiting and requires_classification(hook):
            deny_for_pending(hook, sorted(awaiting))
            return
        if requires_classification(hook) and scope_enforced():
            blocked = unapproved_in(state, reachable_text(hook))
            if blocked:
                deny_for_scope(blocked)
                return
        branch = ensure_agent_branch(state, agent)
        eid = allocate_event(state)
        parent = state["agents"][agent].get("last_event")
        action = safe_action_label(hook)
        started = utc_now()
        item = {
            "event_id": eid,
            "status": "RUNNING",
            "branch": branch,
            "action": action,
            "agent": agent,
            "stage": stage_key(state),
            "parent_event": parent,
            "started_at": started,
            "tool_name": str(hook.get("tool_name") or "Tool"),
            "tool_use_id": str(hook.get("tool_use_id") or ""),
            "requires_classification": requires_classification(hook),
        }
        state["pending"][eid] = item
        if item["tool_use_id"]:
            state["tool_map"][item["tool_use_id"]] = eid
        state["agents"][agent]["last_event"] = eid
        state["branches"][branch]["activity"] = int(state["branches"][branch].get("activity", 0)) + 1
        state["branches"][branch]["recent_event"] = eid
        evidence_path = record_private_evidence(eid, "pre", hook)
        append_event(
            {
                "event_id": eid,
                "ts_utc": started,
                "action_type": item["tool_name"],
                "action_label": action,
                "parent_event": parent,
                "branch_id": branch,
                "agent_id": agent,
                "stage_id": item["stage"],
                "scope_ref": action,
                "evidence_path": evidence_path,
                "phase": "start",
                # start 줄의 io_bytes는 도구로 들어간 입력 크기다. finish 줄은 응답 크기다.
                "io_bytes": payload_bytes(hook.get("tool_input")),
            }
        )
        render_unlocked(state)


def _finish(hook: Dict[str, Any], failed: bool) -> None:
    if is_internal_harness_call(hook):
        return
    agent = agent_key(hook)
    tool_use_id = str(hook.get("tool_use_id") or "")
    with locked_state() as state:
        eid = state["tool_map"].get(tool_use_id)
        if not eid or eid not in state["pending"]:
            branch = ensure_agent_branch(state, agent)
            eid = allocate_event(state)
            action = safe_action_label(hook)
            state["pending"][eid] = {
                "event_id": eid,
                "status": "RUNNING",
                "branch": branch,
                "action": action,
                "agent": agent,
                "stage": stage_key(state),
                "parent_event": state["agents"][agent].get("last_event"),
                "started_at": utc_now(),
                "tool_name": str(hook.get("tool_name") or "Tool"),
                "tool_use_id": tool_use_id,
                "requires_classification": requires_classification(hook),
            }
        item = state["pending"][eid]
        strict = bool(item.get("requires_classification"))
        item["status"] = "AWAITING_CLASSIFICATION" if strict else "AUTO_CLASSIFIED"
        status = "failed" if failed else "success"
        evidence_path = record_private_evidence(eid, "failure" if failed else "post", hook)
        append_event(
            {
                "event_id": eid,
                "ts_utc": utc_now(),
                "duration_ms": hook.get("duration_ms"),
                "action_type": item.get("tool_name"),
                "action_label": item.get("action"),
                "parent_event": item.get("parent_event"),
                "branch_id": item.get("branch"),
                "agent_id": item.get("agent"),
                "stage_id": item.get("stage"),
                "status": status,
                "exit_code": None,
                "evidence_path": evidence_path,
                "observation_summary": item.get("action"),
                "promotion_state": "unreviewed" if strict else "closed",
                "clue_ids": [],
                "map_changed": True,
                "phase": "finish",
                "io_bytes": response_bytes(hook),
            }
        )
        if not strict:
            del state["pending"][eid]
        render_unlocked(state)
    if not strict:
        return
    event_name = "PostToolUseFailure" if failed else "PostToolUse"
    context = (
        "{0} 자동 기록 완료. 다음 외부 행동 전에 이 결과를 mapctl로 no-change/candidate/closed 또는 clue로 분류하세요. "
        "MAP은 코드가 즉시 재생성하므로 직접 편집하지 마세요."
    ).format(eid)
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            }
        }
    )


def deny_for_hook_failure(detail: str) -> None:
    """훅이 판단을 마치지 못했으면 통과가 아니라 차단으로 끝낸다."""
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "하네스 훅이 범위·동기화 판단을 마치지 못했습니다 ({0}). "
                    "판단하지 못한 행동은 승인 범위 안이라고 간주하지 않으므로 차단합니다. "
                    "launcher.py로 실행 중인지, common/ 코드가 정상인지 확인하세요. "
                    "원인을 알고도 계속해야 한다면 REDTEAM_SCOPE_ENFORCE=0으로 강제를 끌 수 있습니다."
                ).format(detail),
            }
        }
    )


def run_pre_fail_closed(hook: Dict[str, Any]) -> None:
    """pre 훅의 실패는 fail-open이 된다. 이 하네스에서 가장 위험한 실패 양식이다.

    훅이 예외로 죽으면 Claude Code는 그 도구 호출을 그대로 실행한다. 즉 범위
    차단이 조용히 사라진다. 실제로 leading-zero IP 하나가 파서를 크래시시켜
    승인 안 된 대상이 통과한 적이 있다. 그래서 어떤 이유로 죽든 차단으로 닫는다.
    """
    if IMPORT_ERROR is not None:
        deny_for_hook_failure(IMPORT_ERROR)
        return
    try:
        handle_pre(hook)
    except BaseException as error:  # noqa: BLE001 - 어떤 실패든 통과시키지 않는다
        if not _EMITTED:
            deny_for_hook_failure("{0}: {1}".format(type(error).__name__, error))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "session"
    hook = read_input()
    if mode == "pre":
        run_pre_fail_closed(hook)
        return
    if IMPORT_ERROR is not None:
        # pre가 아닌 모드는 기록용이다. 차단할 대상이 없으므로 원인만 드러낸다.
        raise SystemExit("engine 로드 실패: " + IMPORT_ERROR)
    if mode in ("session", "bootstrap"):
        bootstrap()
    elif mode == "prepare-run":
        if len(sys.argv) < 3:
            raise SystemExit("prepare-run: 실행 폴더 경로가 필요합니다")
        from engine import dumps_safe as _dumps_safe
        print(_dumps_safe(prepare_run(Path(sys.argv[2]))))
    elif mode == "post":
        _finish(hook, failed=False)
    elif mode == "failure":
        _finish(hook, failed=True)
    else:
        raise SystemExit("unknown hook mode: " + mode)


if __name__ == "__main__":
    main()
