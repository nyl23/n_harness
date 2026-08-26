#!/usr/bin/env python3
"""실행별 EVENTS.jsonl을 구성(config_label)별로 묶어 비교 집계를 만든다.

한 번의 실행을 채점하는 도구가 아니다. 같은 하네스를 구성만 바꿔 여러 번 돌린 뒤
"이 구성이 저 구성과 실제로 무엇이 달랐는가"를 같은 축 위에서 보기 위한 것이다.

표준 라이브러리만 쓰고 engine을 임포트하지 않는다. engine은 임포트 시점에
REDTEAM_RUN_DIR을 요구하므로, 실행이 모두 끝난 뒤 밖에서 돌리는 이 도구가
특정 실행 폴더에 묶이면 안 된다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"


# ------------------------------------------------------------------ 읽기


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def read_events(path: Path) -> List[Dict[str, Any]]:
    """dict인 줄만 읽는다.

    EVENTS.jsonl은 append-only이고 실행이 도중에 끊기면 마지막 줄이 잘려 있을 수
    있다. 그 한 줄 때문에 실행 전체를 버리면 비교에서 그 실행이 사라진다.
    """
    records: List[Dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _read_manifest(run_dir: Path) -> Dict[str, Any]:
    try:
        value = json.loads((run_dir / "RUN.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _first(events: Sequence[Dict[str, Any]], key: str) -> Optional[str]:
    for event in events:
        value = event.get(key)
        if value:
            return str(value)
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------ 실행 단위 집계


def summarize_run(run_dir: Path) -> Dict[str, Any]:
    """실행 폴더 하나를 비교 가능한 숫자들로 요약한다."""
    manifest = _read_manifest(run_dir)
    events = read_events(run_dir / "engagement" / "EVENTS.jsonl")

    starts = {
        str(event.get("event_id"))
        for event in events
        if event.get("phase") == "start" and event.get("event_id")
    }
    finishes = [event for event in events if event.get("phase") == "finish"]
    success = sum(1 for event in finishes if event.get("status") == "success")
    failed = sum(1 for event in finishes if event.get("status") == "failed")

    clues = {
        str(cid)
        for event in events
        for cid in (event.get("clue_ids") or [])
        if cid
    }
    stages = {str(event.get("stage_id")) for event in events if event.get("stage_id")}

    io_in = sum(_as_int(e.get("io_bytes")) for e in events if e.get("phase") == "start")
    io_out = sum(_as_int(e.get("io_bytes")) for e in events if e.get("phase") == "finish")
    io_total = sum(_as_int(e.get("io_bytes")) for e in events)

    stamps = [ts for ts in (_parse_ts(e.get("ts_utc")) for e in events) if ts]
    duration_s = (max(stamps) - min(stamps)).total_seconds() if len(stamps) > 1 else 0.0

    return {
        "run_id": manifest.get("run_id") or _first(events, "run_id") or run_dir.name,
        "config_label": manifest.get("config_label") or _first(events, "config_label") or "unknown",
        "harness_rev": manifest.get("harness_rev") or _first(events, "harness_rev") or "unknown",
        "path": str(run_dir),
        "actions": len(starts),
        "success": success,
        "failed": failed,
        "clues": len(clues),
        "stages": len(stages),
        "io_in": io_in,
        "io_out": io_out,
        "io_total": io_total,
        "duration_s": round(duration_s, 1),
    }


def collect_runs(runs_dir: Path) -> List[Dict[str, Any]]:
    if not runs_dir.is_dir():
        return []
    runs = [summarize_run(child) for child in sorted(runs_dir.iterdir()) if child.is_dir()]
    return sorted(runs, key=lambda run: run["run_id"])


# ------------------------------------------------------------ 구성 단위 집계


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def group_by_config(runs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """같은 config_label을 한 그룹으로 묶는다."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for run in runs:
        groups.setdefault(run["config_label"], []).append(run)

    result: List[Dict[str, Any]] = []
    for label in sorted(groups):
        items = groups[label]
        attempts = sum(run["success"] + run["failed"] for run in items)
        result.append(
            {
                "config_label": label,
                "runs": len(items),
                # 이벤트가 하나도 없는 실행은 "그 구성으로 아무것도 못 했다"는 결과다.
                # 평균에 0으로 들어가되, 몇 건인지 따로 보여야 해석을 오도하지 않는다.
                "empty_runs": sum(1 for run in items if run["actions"] == 0),
                "harness_revs": sorted({run["harness_rev"] for run in items}),
                "actions_mean": round(_mean([run["actions"] for run in items]), 1),
                "actions_median": round(_median([run["actions"] for run in items]), 1),
                "clues_mean": round(_mean([run["clues"] for run in items]), 1),
                "stages_max": max(run["stages"] for run in items),
                # 실행별 성공률의 평균이 아니라 그룹 전체를 모은 비율이다. 행동 수가
                # 제각각인 실행들을 같은 무게로 평균 내면 짧은 실행이 과대 대표된다.
                "success_rate": round(sum(run["success"] for run in items) / attempts, 3)
                if attempts
                else None,
                "io_total_mean": round(_mean([run["io_total"] for run in items])),
                "io_total_median": round(_median([run["io_total"] for run in items])),
                "duration_s_mean": round(_mean([run["duration_s"] for run in items]), 1),
            }
        )
    return result


# ------------------------------------------------------------------ 출력


def _width(text: str) -> int:
    """한글은 터미널에서 두 칸을 차지한다. 문자 수로 맞추면 표가 어긋난다."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [_width(head) for head in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _width(cell))
    lines = ["  ".join(_pad(head, widths[i]) for i, head in enumerate(headers))]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def _bytes(value: float) -> str:
    number = float(value)
    for unit in ("B", "K", "M", "G"):
        if number < 1024 or unit == "G":
            return "{0:.0f}{1}".format(number, unit) if unit == "B" else "{0:.1f}{1}".format(number, unit)
        number /= 1024
    return str(value)


def format_report(runs: Sequence[Dict[str, Any]], configs: Sequence[Dict[str, Any]], per_run: bool) -> str:
    blocks: List[str] = []

    rows = [
        [
            config["config_label"],
            str(config["runs"]),
            str(config["empty_runs"]),
            "{0} ({1})".format(config["actions_mean"], config["actions_median"]),
            str(config["clues_mean"]),
            str(config["stages_max"]),
            "-" if config["success_rate"] is None else "{0:.0%}".format(config["success_rate"]),
            "{0} ({1})".format(_bytes(config["io_total_mean"]), _bytes(config["io_total_median"])),
            "{0}s".format(config["duration_s_mean"]),
        ]
        for config in configs
    ]
    blocks.append("## 구성별 비교 (평균, 괄호는 중앙값)\n")
    blocks.append(
        _table(
            ["config", "runs", "empty", "actions", "clues", "stages", "ok%", "io", "duration"],
            rows,
        )
    )

    mixed = [config for config in configs if len(config["harness_revs"]) > 1]
    if mixed:
        blocks.append("")
        blocks.append("!! 한 구성 안에서 하네스 코드가 섞였다. 이 행의 비교는 코드 차이와 구성 차이를 구분하지 못한다.")
        for config in mixed:
            blocks.append("   {0}: {1}".format(config["config_label"], ", ".join(config["harness_revs"])))

    if per_run:
        blocks.append("")
        blocks.append("## 실행별 상세\n")
        blocks.append(
            _table(
                ["run_id", "config", "rev", "actions", "ok", "fail", "clues", "stages", "in", "out", "duration"],
                [
                    [
                        run["run_id"],
                        run["config_label"],
                        run["harness_rev"],
                        str(run["actions"]),
                        str(run["success"]),
                        str(run["failed"]),
                        str(run["clues"]),
                        str(run["stages"]),
                        _bytes(run["io_in"]),
                        _bytes(run["io_out"]),
                        "{0}s".format(run["duration_s"]),
                    ]
                    for run in runs
                ],
            )
        )

    return "\n".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser(description="구성별 실행 비교 집계")
    parser.add_argument(
        "--runs-dir",
        default=str(DEFAULT_RUNS_DIR),
        help="실행 폴더들의 상위 경로 (기본: 저장소의 runs/)",
    )
    parser.add_argument("--per-run", action="store_true", help="실행별 상세 표도 함께 출력한다")
    parser.add_argument("--json", action="store_true", help="표 대신 JSON으로 출력한다")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir).expanduser().resolve()
    runs = collect_runs(runs_dir)
    configs = group_by_config(runs)

    if args.json:
        print(json.dumps({"runs_dir": str(runs_dir), "runs": runs, "configs": configs}, ensure_ascii=False, indent=2))
        return

    if not runs:
        print("집계할 실행이 없다: {0}".format(runs_dir))
        return
    print(format_report(runs, configs, args.per_run))


if __name__ == "__main__":
    main()
