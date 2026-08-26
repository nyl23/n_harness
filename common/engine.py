#!/usr/bin/env python3
"""Shared state engine for the reusable live-map harness.

Cross-platform: Windows (msvcrt) and Unix (fcntl) file locking.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

# ---------------------------------------------------- 크로스플랫폼 파일 잠금
#
# Unix는 fcntl.flock(), Windows는 msvcrt.locking()을 사용한다.
# 잠금은 STATE.json의 동시 접근을 방지하는 데만 쓰인다.

if sys.platform == "win32":
    import msvcrt
    import time as _time

    @contextmanager
    def _file_lock(lock_path: Path) -> Iterator[None]:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            # 파일에 최소 1바이트가 있어야 msvcrt.locking이 작동한다.
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"\x00")
                lock.flush()
            lock.seek(0)
            deadline = _time.monotonic() + 10.0
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if _time.monotonic() > deadline:
                        raise
                    _time.sleep(0.02)
            try:
                yield
            finally:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass

else:
    import fcntl

    @contextmanager
    def _file_lock(lock_path: Path) -> Iterator[None]:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _run_root() -> Path:
    """Resolve the engagement root without silently writing state to the wrong folder."""
    configured = os.environ.get("REDTEAM_RUN_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    # 런처를 거치지 않고 실행된 경우: 기록이 엉뚱한 곳에 조용히 쌓이지 않도록
    # engagement 루트를 명시적으로 찾고, 못 찾으면 실패시킨다.
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).expanduser().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "runtime" / "STATE.json").exists():
            return candidate
        if candidate.name == "engagement":
            return candidate
    raise SystemExit(
        "REDTEAM_RUN_DIR이 설정되지 않았고 engagement 루트를 찾지 못했습니다. "
        "launcher.py로 실행하세요. 이 검사가 없으면 MAP·LEDGER가 "
        "엉뚱한 폴더에 조용히 생성됩니다."
    )


ROOT = _run_root()
HARNESS_DIR = ROOT / "runtime"
STATE_PATH = HARNESS_DIR / "STATE.json"
LOCK_PATH = HARNESS_DIR / "state.lock"
EVENTS_PATH = ROOT / "EVENTS.jsonl"
DECISIONS_PATH = ROOT / "DECISIONS.jsonl"
MAP_PATH = ROOT / "MAP.md"
LEDGER_PATH = ROOT / "LEDGER.md"
RAW_DIR = ROOT / "evidence" / "raw"
WORK_DIR = ROOT / "work"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def default_state() -> Dict[str, Any]:
    return {
        "schema": 4,
        "target": "미설정",
        "scope": "미설정",
        "goal": "미설정",
        "current_goal": "미설정",
        "targets": {},
        "next_target": 1,
        "current_stage": "stage1",
        "next_event": 1,
        "next_clue": 1,
        "next_branch": 2,
        "current_focus": "B-01",
        "branches": {
            "B-01": {
                "status": "FOCUS",
                "from_id": "START",
                "title": "초기 표면 탐색",
                "reason": "실행 초기화",
                "activity": 0,
                "recent_event": "없음",
                "agent": "main",
            }
        },
        "clues": [],
        "observations": [],
        "pending": {},
        "tool_map": {},
        "agents": {"main": {"branch": "B-01", "last_event": None}},
        "updated_at": utc_now(),
    }


def ensure_layout() -> None:
    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        HARNESS_DIR.chmod(0o700)
        RAW_DIR.chmod(0o700)
    except OSError:
        pass


def _load_unlocked() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()
    if not isinstance(value, dict):
        return default_state()
    base = default_state()
    for key, default in base.items():
        value.setdefault(key, default)
    return value


def dumps_safe(value: Any, **kwargs: Any) -> str:
    """서로게이트가 섞여도 utf-8로 쓸 수 있는 JSON 문자열을 돌려준다.

    json.dumps(ensure_ascii=False)는 서로게이트(\\uD800-\\uDFFF)에서 예외를 던지지
    않고 문자를 그대로 통과시킨다. 그래서 dumps만 try로 감싸면 크래시를 못 막고,
    실제 UnicodeEncodeError는 나중에 utf-8 인코딩(파일 write) 단계에서 터진다.
    Windows에서 surrogateescape로 읽힌 명령 출력·경로가 도구 결과에 섞이면 이
    경로로 훅이 죽는다. 여기서 실제 utf-8 인코딩 가능성까지 확인하고, 안 되면
    ensure_ascii=True로 서로게이트를 \\udXXX로 이스케이프해 순수 ASCII로 만든다.
    """
    text = json.dumps(value, ensure_ascii=False, **kwargs)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(value, ensure_ascii=True, **kwargs)
    return text


def _atomic_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 모든 텍스트 파일 쓰기의 단일 choke point. Windows에서 surrogateescape로 읽힌
    # 명령 출력·경로가 어느 호출부를 거쳐 서로게이트로 흘러들든, utf-8 write에서
    # 훅이 죽지 않도록 여기서 한 번 더 막는다. 인코딩 불가능한 문자만 U+FFFD로
    # 치환한다. JSON 라운드트립 파일(state.json 등)은 호출부에서 dumps_safe로 이미
    # 순수 ASCII라 이 치환에 영향받지 않는다.
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        content = content.encode("utf-8", "replace").decode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, mode)
        except OSError:
            pass  # Windows does not support Unix permissions
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _save_unlocked(state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    blob = dumps_safe(state, indent=2) + "\n"
    _atomic_text(STATE_PATH, blob)


@contextmanager
def locked_state() -> Iterator[Dict[str, Any]]:
    ensure_layout()
    with _file_lock(LOCK_PATH):
        state = _load_unlocked()
        yield state
        _save_unlocked(state)


def append_jsonl(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        line = dumps_safe(value, separators=(",", ":"))
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


# -------------------------------------------------------- 실행 텔레메트리
#
# 이 하네스는 같은 문제를 구성만 바꿔가며 여러 번 돌린 뒤 구성끼리 비교하는 데 쓴다.
# 그러려면 EVENTS.jsonl의 한 줄만 보고도 "어느 실행의, 어떤 구성에서, 어떤 코드
# 버전으로, 얼마나 데이터가 오갔는지"를 알 수 있어야 한다. 나중에 폴더 이름이나
# 실행 시각으로 유추하면 실행이 겹치거나 폴더를 옮기는 순간 조용히 어긋난다.

_RUN_META: Optional[Dict[str, str]] = None


def _clean_label(value: Any, limit: int = 80) -> str:
    return " ".join(str(value).split())[:limit]


def _detect_harness_rev() -> str:
    """공용 코드의 git 리비전. 워킹트리가 더러우면 -dirty를 붙인다.

    커밋 해시만 남기면 같은 rev로 묶인 두 실행이 실제로는 서로 다른 코드였을 수
    있고, 그러면 구성 비교 결과가 조용히 섞인다. 그 경우를 눈에 보이게 만든다.
    """
    repo = Path(__file__).resolve().parent.parent

    def git(*args: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ("git", "-C", str(repo)) + args,
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
    return rev + "-dirty" if git("status", "--porcelain") else rev


def _run_id_from_layout() -> str:
    """runs/<run_id>/engagement 구조라면 폴더 이름이 곧 실행 ID다."""
    parent = ROOT.parent
    if parent.parent.name == "runs" and parent.name:
        return parent.name
    return "unknown"


def run_meta() -> Dict[str, str]:
    """이번 실행을 식별하는 세 값. 런처가 넘긴 환경변수를 우선 쓴다.

    한 프로세스 안에서는 고정한다. 훅은 도구 호출마다 새 프로세스로 뜨므로 이
    캐시는 프로세스 수명만큼만 살고, 대신 git 조회가 이벤트마다 반복되지 않는다.
    """
    global _RUN_META
    if _RUN_META is None:
        _RUN_META = {
            "run_id": _clean_label(os.environ.get("REDTEAM_RUN_ID") or _run_id_from_layout()),
            "config_label": _clean_label(os.environ.get("REDTEAM_CONFIG_LABEL") or "default"),
            "harness_rev": _clean_label(
                os.environ.get("REDTEAM_HARNESS_REV") or _detect_harness_rev()
            ),
        }
    return dict(_RUN_META)


def payload_bytes(value: Any) -> int:
    """도구 입력·응답이 실제로 옮긴 바이트 수. 구성별 I/O 비교의 기준값이다."""
    if value is None:
        return 0
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    # surrogatepass: Windows surrogateescape로 읽힌 도구 출력에 섞인 서로게이트가
    # 그냥 encode("utf-8")를 크래시시킨다. 여기선 바이트 수만 세면 되므로 통과시킨다.
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogatepass"))
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8", "surrogatepass"))
    except (TypeError, ValueError, UnicodeEncodeError):
        return len(str(value).encode("utf-8", "surrogatepass"))


def append_event(value: Dict[str, Any]) -> None:
    """EVENTS.jsonl 한 줄.

    이벤트를 쓰는 자리가 훅·mapctl로 흩어져 있어 한 곳만 고치면 일부 줄에서
    실행 식별자가 빠진다. 그러면 집계에서 그 줄들이 통째로 사라지므로 모든
    이벤트 기록을 이 함수 하나로 모은다.
    """
    record = dict(value)
    record.setdefault("io_bytes", 0)
    record.update(run_meta())
    append_jsonl(EVENTS_PATH, record)


def event_id(number: int) -> str:
    return "E-{0:04d}".format(number)


def clue_id(number: int) -> str:
    return "C-{0:02d}".format(number)


def branch_id(number: int) -> str:
    return "B-{0:02d}".format(number)


def allocate_event(state: Dict[str, Any]) -> str:
    value = event_id(int(state["next_event"]))
    state["next_event"] = int(state["next_event"]) + 1
    return value


def allocate_clue(state: Dict[str, Any]) -> str:
    value = clue_id(int(state["next_clue"]))
    state["next_clue"] = int(state["next_clue"]) + 1
    return value


def allocate_branch(state: Dict[str, Any]) -> str:
    value = branch_id(int(state["next_branch"]))
    state["next_branch"] = int(state["next_branch"]) + 1
    return value


def target_id(number: int) -> str:
    return "T-{0:02d}".format(number)


def allocate_target(state: Dict[str, Any]) -> str:
    value = target_id(int(state["next_target"]))
    state["next_target"] = int(state["next_target"]) + 1
    return value


def approved_values(state: Dict[str, Any]) -> Set[str]:
    return {
        str(item.get("value"))
        for item in state.get("targets", {}).values()
        if item.get("status") == "approved" and item.get("value")
    }


def pending_targets(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for tid, item in sorted(state.get("targets", {}).items()):
        if item.get("status") == "pending":
            entry = dict(item)
            entry["id"] = tid
            result.append(entry)
    return result


SETTINGS_TEMPLATE = Path(__file__).resolve().parent / "settings.json"
HARNESS_COMMON_TOKEN = "__HARNESS_COMMON__"
PYTHON_TOKEN = "__PYTHON__"


def prepare_run(run_dir: Path) -> Dict[str, str]:
    """실행 폴더에 훅 설정과 실행 메타를 굳힌다.

    훅 경로를 절대 경로로 박는 이유: 실행 폴더가 runs/<run_id>/engagement로
    깊어지면서 `${CLAUDE_PROJECT_DIR}/../common` 같은 깊이 의존 배선은 폴더 구조를
    한 단계만 바꿔도 조용히 끊긴다. 이 저장소에서 실제로 그 회귀가 났고, 훅이
    전부 로드되지 않은 채로 테스트는 모두 통과했다. 깊이를 세지 않는다.

    RUN.json을 따로 남기는 이유: 이벤트가 하나도 없는 실행도 구성 비교에서는
    "그 구성으로 아무것도 하지 못했다"는 결과다. EVENTS.jsonl만 읽으면 그런
    실행이 집계에서 통째로 빠져 비교가 성공한 실행 쪽으로 치우친다.
    """
    run_dir = Path(run_dir).expanduser().resolve()
    common = str(SETTINGS_TEMPLATE.parent)
    template = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
    if HARNESS_COMMON_TOKEN not in template:
        raise SystemExit(
            "settings.json에 {0} 자리표시자가 없습니다. 훅 배선이 실행 폴더를 "
            "가리키게 되어 훅이 로드되지 않습니다.".format(HARNESS_COMMON_TOKEN)
        )
    # 경로에 따옴표·역슬래시가 있어도 JSON이 깨지지 않도록 문자열 리터럴 규칙으로 이스케이프한다.
    rendered = template.replace(HARNESS_COMMON_TOKEN, json.dumps(common)[1:-1])
    # Python 인터프리터 경로도 플랫폼에 맞게 치환한다.
    python_path = sys.executable.replace("\\", "/")
    rendered = rendered.replace(PYTHON_TOKEN, json.dumps(python_path)[1:-1])
    json.loads(rendered)  # 깨진 설정으로 실행되면 훅 없이 조용히 진행된다
    _atomic_text(run_dir / "settings.json", rendered)

    manifest = dict(run_meta(), started_at=utc_now())
    _atomic_text(run_dir / "RUN.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


def ensure_stage_workspace(stage: str, target: str = "") -> Path:
    """Stage별 작업 파일 자리를 만든다. 기록 파일과 섞이지 않게 work/ 아래에 둔다."""
    path = WORK_DIR / stage
    path.mkdir(parents=True, exist_ok=True)
    readme = path / "STAGE.md"
    if not readme.exists():
        _atomic_text(
            readme,
            "\n".join(
                [
                    "# {0} 작업 폴더".format(stage),
                    "",
                    "- 대상: {0}".format(target or "미설정"),
                    "- 생성: {0}".format(utc_now()),
                    "",
                    "이 Stage에서 새로 만든 스캔 결과·페이로드·임시 스크립트·메모를 여기에 둔다.",
                    "",
                    "`MAP.md`, `LEDGER.md`, `EVENTS.jsonl`, `DECISIONS.jsonl`, `runtime/`, `evidence/`는",
                    "상위 engagement 폴더에서 모든 Stage가 이어 쓴다. 그 파일들은 하네스가 생성하므로",
                    "직접 편집하지 않는다.",
                    "",
                ]
            ),
            mode=0o600,
        )
    return path


def next_stage_label(state: Dict[str, Any]) -> str:
    used = {item.get("stage") for item in state.get("targets", {}).values() if item.get("stage")}
    number = 1
    while "stage{0}".format(number) in used:
        number += 1
    return "stage{0}".format(number)


# ---------------------------------------------------------------- 범위 검사
#
# 이 검사의 위협 모델: 정직한 AI가 실수로 승인 범위를 벗어나는 것을 막는 가드레일이다.
# 우회하려고 작정한 실행자를 막는 샌드박스가 아니다. 셸 변수·파일 경유·호스트명처럼
# 문자열만 봐서는 알 수 없는 경로는 여전히 통과하므로, 범위 통제의 최종 책임은
# 프롬프트 §0의 안전 규칙과 사용자 승인에 있다.

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# URL로 적힌 호스트. 10진수·16진수 IP 표기는 오탐을 줄이려고 여기서만 해석한다.
# 호스트 문자만 받는다. 훅 입력은 JSON 문자열이라 따옴표까지 삼키면 파싱이 깨진다.
_URL_HOST_RE = re.compile(
    r"[a-z][a-z0-9+.\-]*://(?:[^/?#\s@\"']*@)?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._\-]+)",
    re.IGNORECASE,
)

# 대괄호 없이 적힌 IPv6 리터럴 후보. 콜론이 두 개 이상이어야 후보로 본다.
_IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[0-9A-Za-z_.\-]+)?"
)

# 10진수 한 덩어리를 IPv4로 해석할 최소값. 1.0.0.0 미만은 정수형 IP 표기로 보지 않는다.
_MIN_PACKED_IPV4 = 0x01000000

# 하네스 자신과 루프백은 언제나 허용한다. 대상이 아니라 도구이기 때문이다.
ALWAYS_ALLOWED = frozenset({"127.0.0.1", "0.0.0.0", "255.255.255.255"})

IPAddress = Any  # ipaddress.IPv4Address | IPv6Address


def extract_ipv4(text: str) -> Set[str]:
    """문자열에서 유효한 점 4개 IPv4 리터럴만 추출한다. 버전 문자열은 걸리지 않는다."""
    found: Set[str] = set()
    for candidate in _IPV4_RE.findall(text or ""):
        octets = candidate.split(".")
        if all(part.isdigit() and 0 <= int(part) <= 255 for part in octets):
            # leading-zero 옥텟(예: 192.168.001.5)을 정규화한다. 이렇게 하지 않으면
            # ipaddress.IPv4Address가 "Leading zeros are not permitted"로 예외를 던지고,
            # 그 예외가 훅을 죽여 도구가 범위 검사 없이 통과하는 fail-open이 된다.
            found.add(".".join(str(int(part)) for part in octets))
    return found


def _as_ip(text: str) -> Optional[IPAddress]:
    """호스트 문자열 하나를 IP로 해석한다. 10진수·16진수 표기도 받는다."""
    value = str(text).strip().strip("[]")
    if not value:
        return None
    value = value.split("%", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        pass
    lowered = value.lower()
    try:
        if lowered.startswith("0x"):
            number = int(lowered, 16)
        elif value.isdigit():
            number = int(value)
            if number < _MIN_PACKED_IPV4:
                return None  # 포트 번호·카운터를 IP로 오인하지 않는다
        else:
            return None
    except ValueError:
        return None
    if 0 <= number <= 0xFFFFFFFF:
        return ipaddress.IPv4Address(number)
    return None


def extract_targets(text: str) -> Set[IPAddress]:
    """명령·URL에서 대상 IP를 뽑는다. 점 4개 표기, IPv6 리터럴, URL 안의 정수형 표기."""
    text = text or ""
    found: Set[IPAddress] = set()
    for literal in extract_ipv4(text):
        found.add(ipaddress.IPv4Address(literal))
    for candidate in _IPV6_RE.findall(text):
        parsed = _as_ip(candidate)
        if parsed is not None and parsed.version == 6:
            found.add(parsed)
    for host in _URL_HOST_RE.findall(text):
        parsed = _as_ip(host)
        if parsed is not None:
            found.add(parsed)
    return found


def scope_enforced() -> bool:
    return str(os.environ.get("REDTEAM_SCOPE_ENFORCE", "1")).strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _approved_networks(state: Dict[str, Any]) -> List[Any]:
    """승인 값을 네트워크로 해석한다. 단일 IP는 /32(/128), 대역은 CIDR 그대로."""
    networks: List[Any] = []
    for value in approved_values(state):
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue  # 호스트명 등 IP가 아닌 승인 값은 IP 판정에 쓰지 않는다
    return networks


def _always_allowed(ip: IPAddress) -> bool:
    return str(ip) in ALWAYS_ALLOWED or ip.is_loopback or ip.is_unspecified


def unapproved_in(state: Dict[str, Any], text: str) -> List[str]:
    """승인되지 않은 대상 IP 목록. 도메인은 조사·연구를 막지 않도록 검사하지 않는다."""
    networks = _approved_networks(state)
    blocked = [
        str(ip)
        for ip in extract_targets(text)
        if not _always_allowed(ip) and not any(ip in network for network in networks)
    ]
    return sorted(set(blocked))


def register_initial_target(state: Dict[str, Any], value: str, stage: str = "stage1") -> str:
    """init 시점의 첫 대상을 승인 상태로 등록한다."""
    for tid, item in state.get("targets", {}).items():
        if item.get("value") == value:
            return tid
    tid = allocate_target(state)
    state["targets"][tid] = {
        "value": value,
        "status": "approved",
        "stage": stage,
        "evidence": None,
        "reason": "실행 시작 시 사용자가 지정한 최초 대상",
        "proposed_at": utc_now(),
        "decided_at": utc_now(),
    }
    state["current_stage"] = stage
    state["target"] = value
    ensure_stage_workspace(stage, value)
    return tid


def propose_target(value: str, evidence: Optional[str] = None, reason: str = "") -> Tuple[str, str, bool]:
    """새로 발견한 대상을 승인 대기로 올린다. 승인 전에는 접근이 차단된다."""
    value = " ".join(str(value).split())[:200]
    with locked_state() as state:
        for tid, item in sorted(state.get("targets", {}).items()):
            if item.get("value") == value:
                render_unlocked(state)
                return tid, str(item.get("status")), False
        tid = allocate_target(state)
        state["targets"][tid] = {
            "value": value,
            "status": "pending",
            "stage": None,
            "evidence": evidence,
            "reason": " ".join(str(reason).split())[:300] or "근거 미기재",
            "proposed_at": utc_now(),
            "decided_at": None,
        }
        append_jsonl(
            DECISIONS_PATH,
            {
                "ts_utc": utc_now(),
                "kind": "target-propose",
                "target_id": tid,
                "value": value,
                "evidence": evidence,
                "reason": state["targets"][tid]["reason"],
            },
        )
        render_unlocked(state)
    return tid, "pending", True


def decide_target(
    tid: str,
    decision: str,
    reason: str = "",
    stage: Optional[str] = None,
    agent: str = "main",
) -> Dict[str, Any]:
    """대상을 승인하거나 거부한다. 승인하면 새 Stage 라벨과 FOCUS 가지가 생긴다."""
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    with locked_state() as state:
        item = state.get("targets", {}).get(tid)
        if not isinstance(item, dict):
            raise KeyError(tid)
        item["status"] = decision
        item["decided_at"] = utc_now()
        item["decided_reason"] = " ".join(str(reason).split())[:300]
        result: Dict[str, Any] = {"id": tid, **item}
        if decision == "approved":
            if not item.get("stage"):
                item["stage"] = stage or next_stage_label(state)
            state["current_stage"] = item["stage"]
            state["target"] = item["value"]
            state["current_goal"] = "{0} 표면 탐색".format(item["value"])
            ensure_stage_workspace(item["stage"], item["value"])
            bid = allocate_branch(state)
            state["branches"][bid] = {
                "status": "FOCUS",
                "from_id": item.get("evidence") or "START",
                "title": "{0} 진입 ({1})".format(item["stage"], item["value"]),
                "reason": "사용자가 {0} 대상을 승인".format(tid),
                "activity": 0,
                "recent_event": item.get("evidence") or "없음",
                "agent": agent,
            }
            previous = state.get("current_focus")
            if (
                previous
                and previous != bid
                and previous in state["branches"]
                and state["branches"][previous].get("status") == "FOCUS"
            ):
                state["branches"][previous]["status"] = "OPEN"
            state["current_focus"] = bid
            state["agents"].setdefault(agent, {"last_event": None})["branch"] = bid
            result = {"id": tid, **item, "branch": bid}
        append_jsonl(
            DECISIONS_PATH,
            {
                "ts_utc": utc_now(),
                "kind": "target-decision",
                "target_id": tid,
                "value": item.get("value"),
                "decision": decision,
                "stage": item.get("stage"),
                "reason": item.get("decided_reason"),
            },
        )
        render_unlocked(state)
    return result


def ensure_agent_branch(state: Dict[str, Any], agent: str) -> str:
    agents = state["agents"]
    if agent in agents:
        return agents[agent]["branch"]
    branch = allocate_branch(state)
    agents[agent] = {"branch": branch, "last_event": None}
    state["branches"][branch] = {
        "status": "OPEN",
        "from_id": "START",
        "title": "병렬 에이전트 {0}".format(agent[:12]),
        "reason": "훅이 자동 생성한 실행 가지",
        "activity": 0,
        "recent_event": "없음",
        "agent": agent,
    }
    return branch


def safe_action_label(hook: Dict[str, Any]) -> str:
    tool_name = str(hook.get("tool_name") or "Tool")
    tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    description = tool_input.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()[:180]
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        name = Path(file_path).name
        if name.startswith(".env") or "key" in name.lower() or "secret" in name.lower():
            name = "[민감 파일]"
        return "{0} {1}".format(tool_name, name)[:180]
    return "{0} 실행".format(tool_name)


# ---------------------------------------------------- 하네스 자기 호출 판정
#
# 이 판정이 참이면 범위 검사와 분류 게이트를 건너뛰고 E-ID도 발급하지 않는다.
# 따라서 느슨하면 그대로 우회 통로가 된다. 예전처럼 명령 문자열 어딘가에
# "mapctl.py"가 있는지만 보면 주석·파일명·echo로 아무 명령이나 숨길 수 있었다.
# 이제는 실제로 실행되는 프로그램이 하네스 스크립트인 단일 명령일 때만 참이다.

_HARNESS_SCRIPTS = frozenset({"mapctl.py", "hook.py", "launcher.py"})
_PYTHON_NAMES = ("python", "python3", "python3.9", "python3.10", "python3.11", "python3.12", "python3.13")

# 명령 치환·백틱은 다른 명령을 숨길 수 있으므로 자기 호출로 인정하지 않는다.
_SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")
# shlex가 구두점으로 떼어내는 셸 제어 연산자.
_CONTROL_TOKENS = frozenset({";", "&", "&&", "|", "||", "(", ")", "<", ">", ">>", "<<"})


def _expand_common(command: str) -> str:
    """프롬프트가 쓰는 $REDTEAM_COMMON만 펼친다. 나머지 변수는 펼치지 않는다."""
    common = os.environ.get("REDTEAM_COMMON") or str(Path(__file__).resolve().parent)
    for form in ("${REDTEAM_COMMON}", "$REDTEAM_COMMON"):
        command = command.replace(form, common)
    return command


def _harness_script_arg(tokens: List[str]) -> Optional[str]:
    """실행 대상 위치에 하네스 스크립트가 있으면 그 경로를 돌려준다."""
    if not tokens:
        return None
    if Path(tokens[0]).name in _HARNESS_SCRIPTS:
        return tokens[0]
    if Path(tokens[0]).name in _PYTHON_NAMES and len(tokens) > 1:
        if Path(tokens[1]).name in _HARNESS_SCRIPTS:
            return tokens[1]
    return None


def _is_harness_path(text: str) -> bool:
    """common/ 또는 저장소 루트에 실제로 있는 하네스 스크립트인지 확인한다."""
    common = Path(__file__).resolve().parent
    try:
        candidate = Path(text).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return candidate.parent in {common, common.parent}


def is_internal_harness_call(hook: Dict[str, Any]) -> bool:
    if str(hook.get("tool_name") or "") != "Bash":
        return False
    tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    command = _expand_common(str(tool_input.get("command") or ""))
    if not command.strip() or _SUBSTITUTION_RE.search(command):
        return False
    if "\n" in command or "\r" in command:
        return False  # 여러 줄이면 단일 자기 호출이 아니다. 줄바꿈은 shlex가
        # 공백처럼 삼켜 제어 토큰을 남기지 않으므로 여기서 직접 막지 않으면
        # 첫 줄이 mapctl 호출인 블록 전체가 범위 검사를 건너뛴다(fail-open).
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if any(token in _CONTROL_TOKENS for token in tokens):
        return False  # 여러 명령이 붙어 있으면 단일 자기 호출이 아니다
    script = _harness_script_arg(tokens)
    return script is not None and _is_harness_path(script)


def record_private_evidence(eid: str, phase: str, hook: Dict[str, Any]) -> str:
    ensure_layout()
    path = RAW_DIR / "{0}-hook.json".format(eid)
    value: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value = loaded
        except (OSError, json.JSONDecodeError):
            value = {}
    value[phase] = hook
    value["event_id"] = eid
    content = dumps_safe(value, indent=2) + "\n"
    _atomic_text(path, content, mode=0o600)
    return str(path.relative_to(ROOT))


def _clue_line(clue: Dict[str, Any]) -> str:
    existence = "#" if clue.get("existence") == "confirmed" else "?"
    status_map = {"verified": "[v]", "progress": "[~]", "closed": "[x]"}
    status = status_map.get(str(clue.get("status")), "[~]")
    relation = str(clue.get("summary") or "관찰")
    if clue.get("door"):
        relation += " >> 문:" + str(clue["door"])
    return "{id} | {level} | {relation} | {existence} | {status} | ev:{event} | stage:{stage}".format(
        id=clue.get("id"),
        level=clue.get("level") or "현재",
        relation=relation,
        existence=existence,
        status=status,
        event=clue.get("event"),
        stage=clue.get("stage", "미지정"),
    )


def render_unlocked(state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    pending = list(state.get("pending", {}).values())
    pending.sort(key=lambda item: str(item.get("event_id")))
    clues = list(state.get("clues", []))
    branches = state.get("branches", {})

    targets = state.get("targets", {})
    waiting = pending_targets(state)
    approved_count = len(approved_values(state))

    map_lines: List[str] = [
        "# MAP — 구조적 실시간 침투 지도 V3",
        "",
        "대상: {0} ({1})".format(state.get("target", "미설정"), state.get("current_stage", "stage1")),
        "범위: {0}".format(state.get("scope", "미설정")),
        "최종 목표: {0}".format(state.get("goal", "미설정")),
        "현재 목표: {0}".format(state.get("current_goal", "미설정")),
        "승인 대상: {0}건 · 승인 대기: {1}건".format(approved_count, len(waiting)),
        "작업 폴더: work/{0}/ (이 Stage에서 새로 만드는 파일은 여기에 둔다)".format(
            state.get("current_stage", "stage1")
        ),
        "",
        "## 대상 범위 (T-*)",
        "",
        "```",
    ]
    if targets:
        for tid in sorted(targets):
            item = targets[tid]
            map_lines.append(
                "{tid} | [{status}] | {value} | {stage} | 근거:{evidence} | {reason}".format(
                    tid=tid,
                    status=item.get("status", "pending"),
                    value=item.get("value", "?"),
                    stage=item.get("stage") or "미배정",
                    evidence=item.get("evidence") or "없음",
                    reason=item.get("reason") or "",
                )
            )
    else:
        map_lines.append("_아직 등록된 대상 없음_")
    if waiting:
        map_lines.append("")
        map_lines.append(
            "!! 승인 대기 {0}건. 승인 전까지 해당 IP로 향하는 외부 행동은 훅이 차단한다.".format(len(waiting))
        )
    map_lines.extend(
        [
            "```",
            "",
            "## 실시간 행동 상태",
            "",
            "```",
        ]
    )
    if pending:
        for item in pending:
            map_lines.append(
                "{event_id} | [{status}] | {branch} | {action} | stage:{stage} | agent:{agent}".format(
                    event_id=item.get("event_id"),
                    status=item.get("status"),
                    branch=item.get("branch"),
                    action=item.get("action"),
                    stage=item.get("stage", "미지정"),
                    agent=item.get("agent"),
                )
            )
    else:
        map_lines.append("_분류 대기 행동 없음_")
    map_lines.extend(["```", "", "## 상승 경로", "", "```"])
    if clues:
        for clue in reversed(clues):
            marker = " <현재 위치>" if clue.get("id") == state.get("current_clue") else ""
            map_lines.append(_clue_line(clue) + marker)
    else:
        map_lines.append("_아직 승격된 단서 없음_")
    map_lines.extend(["```", "", "## 탐색 가지 (B-*)", "", "```"])
    for bid in sorted(branches):
        branch = branches[bid]
        map_lines.append(
            "{bid} | [{status}] | from:{from_id} | {title} | 근거:{reason} | 활동:{activity} | 최근변화:{recent_event}".format(
                bid=bid, **branch
            )
        )
    map_lines.extend(["```", "", "## 미승격 관찰", "", "```"])
    observations = state.get("observations", [])[-30:]
    if observations:
        for item in observations:
            map_lines.append(
                "{event} | [{state}] | {summary} | ev:{event} | stage:{stage}".format(
                    event=item.get("event"),
                    state=item.get("state"),
                    summary=item.get("summary"),
                    stage=item.get("stage", "미지정"),
                )
            )
    else:
        map_lines.append("_아직 없음_")
    map_lines.extend(
        [
            "```",
            "",
            "## 동기화 상태",
            "",
            "- 분류 대기: {0}".format(len(pending)),
            "- 마지막 구조 갱신: {0}".format(state.get("updated_at", utc_now())),
            "- MAP과 LEDGER는 하네스가 생성한다. 직접 편집하지 않는다.",
            "",
        ]
    )
    _atomic_text(MAP_PATH, "\n".join(map_lines), mode=0o600)

    ledger_lines: List[str] = [
        "# LEDGER — 구조화 단서 대장 V3",
        "",
        "대상: {0} ({1})".format(state.get("target", "미설정"), state.get("current_stage", "stage1")),
        "범위: {0}".format(state.get("scope", "미설정")),
        "목표: {0}".format(state.get("goal", "미설정")),
        "",
        "## 대상 범위 (T-*)",
        "",
    ]
    if targets:
        for tid in sorted(targets):
            item = targets[tid]
            ledger_lines.append(
                "- {tid} | {status} | {value} | stage:{stage} | 근거:{evidence} | {reason}".format(
                    tid=tid,
                    status=item.get("status", "pending"),
                    value=item.get("value", "?"),
                    stage=item.get("stage") or "미배정",
                    evidence=item.get("evidence") or "없음",
                    reason=item.get("reason") or "",
                )
            )
    else:
        ledger_lines.append("_아직 등록된 대상 없음_")
    ledger_lines.extend(["", "## 단서 (C-*)", ""])
    if clues:
        for clue in clues:
            ledger_lines.extend(
                [
                    "### {0} | {1}".format(clue.get("id"), clue.get("summary")),
                    "- 수준: {0}".format(clue.get("level")),
                    "- 존재: {0}".format(clue.get("existence")),
                    "- 상태: {0}".format(clue.get("status")),
                    "- 분기: {0}".format(clue.get("branch")),
                    "- Stage: {0}".format(clue.get("stage", "미지정")),
                    "- 관계: {0}".format(clue.get("relation")),
                    "- 문: {0}".format(clue.get("door") or "없음"),
                    "- 근거: ev:{0}".format(clue.get("event")),
                    "",
                ]
            )
    else:
        ledger_lines.extend(["_아직 없음_", ""])
    ledger_lines.extend(["## 정리 대장 (cleanup)", "", "_생성/변경 항목 없음_", ""])
    _atomic_text(LEDGER_PATH, "\n".join(ledger_lines), mode=0o600)


def bootstrap() -> None:
    with locked_state() as state:
        render_unlocked(state)
