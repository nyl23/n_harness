#!/usr/bin/env python3
"""Small structured interface used by Claude to classify events and steer the map."""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from engine import (
    DECISIONS_PATH,
    allocate_branch,
    allocate_clue,
    append_event,
    append_jsonl,
    bootstrap,
    decide_target,
    locked_state,
    propose_target,
    register_initial_target,
    render_unlocked,
    utc_now,
)


def short(value: str, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def pending_item(state: Dict[str, Any], eid: str) -> Dict[str, Any]:
    item = state.get("pending", {}).get(eid)
    if not isinstance(item, dict):
        raise SystemExit("분류 대기 중인 이벤트가 아닙니다: " + eid)
    return item


def append_classification(
    eid: str,
    item: Dict[str, Any],
    summary: str,
    promotion_state: str,
    clue_ids: list[str],
) -> None:
    value = {
        "event_id": eid,
        "ts_utc": utc_now(),
        "action_type": "classification",
        "parent_event": item.get("parent_event"),
        "branch_id": item.get("branch"),
        "agent_id": item.get("agent"),
        "stage_id": item.get("stage"),
        "status": "classified",
        "observation_summary": summary,
        "promotion_state": promotion_state,
        "clue_ids": clue_ids,
        "map_changed": True,
        "phase": "classification",
        # 분류는 하네스 안에서만 일어난다. 외부로 오간 바이트가 없으므로 0이다.
        "io_bytes": 0,
    }
    append_event(value)
    append_jsonl(DECISIONS_PATH, value)


def command_init(args: argparse.Namespace) -> None:
    with locked_state() as state:
        target = short(args.target)
        state["target"] = target
        state["scope"] = short(args.scope, 400)
        state["goal"] = short(args.goal, 400)
        state["current_goal"] = state["goal"]
        tid = register_initial_target(state, target, args.stage)
        render_unlocked(state)
    print("하네스 초기화 완료 ({0} = {1}, {2})".format(tid, target, args.stage))


def command_target_propose(args: argparse.Namespace) -> None:
    tid, status, created = propose_target(args.value, args.evidence, args.reason)
    if not created:
        print(
            json.dumps(
                {"id": tid, "value": args.value, "status": status, "created": False},
                ensure_ascii=False,
            )
        )
        return
    print(
        json.dumps(
            {
                "id": tid,
                "value": args.value,
                "status": status,
                "created": True,
                "note": "승인 대기. 대시보드에서 승인하거나 사용자가 승인하기 전까지 이 대상으로의 외부 행동은 차단된다.",
            },
            ensure_ascii=False,
        )
    )


def command_target_approve(args: argparse.Namespace) -> None:
    try:
        result = decide_target(args.id, "approved", args.reason or "", args.stage)
    except KeyError:
        raise SystemExit("등록되지 않은 대상입니다: " + args.id)
    print(json.dumps(result, ensure_ascii=False))


def command_target_reject(args: argparse.Namespace) -> None:
    try:
        result = decide_target(args.id, "rejected", args.reason or "")
    except KeyError:
        raise SystemExit("등록되지 않은 대상입니다: " + args.id)
    print(json.dumps(result, ensure_ascii=False))


def command_target_list(_args: argparse.Namespace) -> None:
    with locked_state() as state:
        targets = {tid: dict(item) for tid, item in state.get("targets", {}).items()}
        current = state.get("current_stage")
    print(json.dumps({"current_stage": current, "targets": targets}, ensure_ascii=False, indent=2))


def command_resolve(args: argparse.Namespace) -> None:
    with locked_state() as state:
        item = pending_item(state, args.event)
        summary = short(args.summary)
        if args.outcome in ("candidate", "closed"):
            state["observations"].append(
                {
                    "event": args.event,
                    "state": args.outcome,
                    "summary": summary,
                    "stage": item.get("stage", "미지정"),
                }
            )
        promotion = "closed" if args.outcome == "no-change" else args.outcome
        del state["pending"][args.event]
        append_classification(args.event, item, summary, promotion, [])
        render_unlocked(state)
    print("{0} -> {1}".format(args.event, args.outcome))


def command_clue(args: argparse.Namespace) -> None:
    with locked_state() as state:
        item = pending_item(state, args.event)
        cid = allocate_clue(state)
        branch = args.branch or item.get("branch") or state.get("current_focus")
        clue = {
            "id": cid,
            "event": args.event,
            "branch": branch,
            "stage": item.get("stage", "미지정"),
            "summary": short(args.summary),
            "level": short(args.level, 80),
            "existence": args.existence,
            "status": args.status,
            "relation": args.relation,
            "door": short(args.door, 160) if args.door else None,
            "created_at": utc_now(),
        }
        state["clues"].append(clue)
        state["current_clue"] = cid
        if branch in state["branches"]:
            state["branches"][branch]["from_id"] = cid
            state["branches"][branch]["recent_event"] = args.event
            state["branches"][branch]["reason"] = short(args.summary, 160)
        if args.door:
            state["current_goal"] = short(args.door, 240)
        del state["pending"][args.event]
        append_classification(args.event, item, clue["summary"], "promoted", [cid])
        render_unlocked(state)
    print(cid)


def command_branch(args: argparse.Namespace) -> None:
    with locked_state() as state:
        bid = args.branch
        if not bid:
            bid = "B-{0:02d}".format(int(state["next_branch"]))
            state["next_branch"] = int(state["next_branch"]) + 1
        current = state["branches"].get(bid, {})
        current.update(
            {
                "status": args.status,
                "from_id": args.from_id or current.get("from_id", "START"),
                "title": short(args.title or current.get("title", "탐색 가지"), 160),
                "reason": short(args.reason or current.get("reason", "사용자/AI 초점 조정"), 180),
                "activity": int(current.get("activity", 0)),
                "recent_event": args.event or current.get("recent_event", "없음"),
                "agent": args.agent,
            }
        )
        if args.status == "FOCUS":
            old = state.get("current_focus")
            if old and old != bid and old in state["branches"] and state["branches"][old].get("status") == "FOCUS":
                state["branches"][old]["status"] = "OPEN"
            state["current_focus"] = bid
            state["agents"].setdefault(args.agent, {"last_event": None})["branch"] = bid
        state["branches"][bid] = current
        decision = {
            "ts_utc": utc_now(),
            "kind": "branch",
            "branch_id": bid,
            "status": args.status,
            "title": current["title"],
            "reason": current["reason"],
        }
        append_jsonl(DECISIONS_PATH, decision)
        render_unlocked(state)
    print(bid)


def command_status(_args: argparse.Namespace) -> None:
    with locked_state() as state:
        render_unlocked(state)
        result = {
            "target": state.get("target"),
            "current_stage": state.get("current_stage"),
            "current_goal": state.get("current_goal"),
            "focus": state.get("current_focus"),
            "pending": sorted(state.get("pending", {}).keys()),
            "pending_targets": [
                {"id": tid, "value": item.get("value"), "reason": item.get("reason")}
                for tid, item in sorted(state.get("targets", {}).items())
                if item.get("status") == "pending"
            ],
            "approved_targets": [
                {"id": tid, "value": item.get("value"), "stage": item.get("stage")}
                for tid, item in sorted(state.get("targets", {}).items())
                if item.get("status") == "approved"
            ],
            "clues": len(state.get("clues", [])),
            "branches": len(state.get("branches", {})),
        }
    print(json.dumps(result, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="공용 구조화 지도 제어기")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--target", required=True)
    init.add_argument("--scope", required=True)
    init.add_argument("--goal", required=True)
    init.add_argument("--stage", default="stage1")
    init.set_defaults(func=command_init)

    propose = sub.add_parser("target-propose", help="새로 발견한 대상을 승인 대기로 올린다")
    propose.add_argument("--value", required=True, help="발견한 IP 또는 호스트")
    propose.add_argument("--evidence", help="근거 이벤트/단서 ID (예: E-0087 또는 C-14)")
    propose.add_argument("--reason", default="", help="이 대상이 다음 경계라고 판단한 근거")
    propose.set_defaults(func=command_target_propose)

    approve = sub.add_parser("target-approve", help="사용자 승인. 새 Stage와 FOCUS 가지가 생긴다")
    approve.add_argument("--id", required=True)
    approve.add_argument("--stage", help="생략하면 다음 stage 번호가 자동 배정된다")
    approve.add_argument("--reason", default="")
    approve.set_defaults(func=command_target_approve)

    reject = sub.add_parser("target-reject", help="대상 거부. 이후에도 계속 차단된다")
    reject.add_argument("--id", required=True)
    reject.add_argument("--reason", default="")
    reject.set_defaults(func=command_target_reject)

    target_list = sub.add_parser("target-list")
    target_list.set_defaults(func=command_target_list)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--event", required=True)
    resolve.add_argument("--outcome", choices=("no-change", "candidate", "closed"), required=True)
    resolve.add_argument("--summary", required=True)
    resolve.set_defaults(func=command_resolve)

    clue = sub.add_parser("clue")
    clue.add_argument("--event", required=True)
    clue.add_argument("--summary", required=True)
    clue.add_argument("--level", default="현재")
    clue.add_argument("--existence", choices=("confirmed", "hypothesis"), default="confirmed")
    clue.add_argument("--status", choices=("verified", "progress", "closed"), default="verified")
    clue.add_argument("--relation", choices=("child", "door", "alternate"), default="child")
    clue.add_argument("--door")
    clue.add_argument("--branch")
    clue.set_defaults(func=command_clue)

    branch = sub.add_parser("branch")
    branch.add_argument("--branch")
    branch.add_argument("--status", choices=("FOCUS", "OPEN", "PARKED", "CLOSED"), required=True)
    branch.add_argument("--title")
    branch.add_argument("--reason")
    branch.add_argument("--from-id")
    branch.add_argument("--event")
    branch.add_argument("--agent", default="main")
    branch.set_defaults(func=command_branch)

    status = sub.add_parser("status")
    status.set_defaults(func=command_status)
    return parser


def main() -> None:
    bootstrap()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
