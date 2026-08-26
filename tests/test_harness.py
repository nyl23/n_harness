#!/usr/bin/env python3
"""하네스 핵심 흐름 검증: 대상 승인, 범위 차단, Stage 전이, 기록 원자성.

Cross-platform: Windows (msvcrt) and Unix (fcntl).
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "common"

# engine은 임포트 시점에 루트를 확정하므로 그 전에 임시 engagement를 지정한다.
_TEMP = tempfile.TemporaryDirectory()
os.environ["REDTEAM_RUN_DIR"] = _TEMP.name
os.environ["REDTEAM_SCOPE_ENFORCE"] = "1"
sys.path.insert(0, str(COMMON))

import engine  # noqa: E402
import hook  # noqa: E402


def pre(tool_name: str, tool_input: dict) -> dict | None:
    """PreToolUse 훅을 직접 호출하고 훅이 낸 결정을 돌려준다."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        hook.handle_pre(
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": "tu-" + str(len(buffer.getvalue())),
                "agent_id": "main",
            }
        )
    raw = buffer.getvalue().strip()
    return json.loads(raw) if raw else None


def decision_of(result: dict | None) -> str | None:
    if not result:
        return None
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def clear_pending() -> None:
    """분류 대기 게이트가 범위 검사 테스트를 가리지 않도록 비운다."""
    with engine.locked_state() as state:
        state["pending"] = {}


class TargetApprovalFlow(unittest.TestCase):
    def setUp(self) -> None:
        shutil.rmtree(engine.WORK_DIR, ignore_errors=True)
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            state["scope"] = "단일 호스트"
            state["goal"] = "다음 경계 확인"
            engine.render_unlocked(state)

    def test_initial_target_is_approved(self) -> None:
        with engine.locked_state() as state:
            self.assertEqual(engine.approved_values(state), {"192.0.2.10"})
            self.assertEqual(state["current_stage"], "stage1")

    def test_approved_target_is_allowed(self) -> None:
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "curl http://192.0.2.10/"})))

    def test_unapproved_ip_is_denied(self) -> None:
        clear_pending()
        result = pre("Bash", {"command": "nmap 203.0.113.55"})
        self.assertEqual(decision_of(result), "deny")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("203.0.113.55", reason)
        self.assertIn("target-propose", reason)

    def test_loopback_and_versions_are_not_blocked(self) -> None:
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "curl http://127.0.0.1:8765/"})))
        clear_pending()
        # 버전 문자열은 IPv4가 아니므로 걸리지 않아야 한다.
        self.assertIsNone(decision_of(pre("Bash", {"command": "echo nginx/1.27.5 grafana 11.2.0"})))

    def test_octet_range_is_validated(self) -> None:
        self.assertEqual(engine.extract_ipv4("999.1.1.1 and 203.0.113.9"), {"203.0.113.9"})

    def test_propose_then_approve_opens_next_stage(self) -> None:
        tid, status, created = engine.propose_target("203.0.113.55", "C-14", "설정 파일에서 발견")
        self.assertTrue(created)
        self.assertEqual(status, "pending")

        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 203.0.113.55"})), "deny")

        result = engine.decide_target(tid, "approved", "테스트 승인")
        self.assertEqual(result["stage"], "stage2")

        with engine.locked_state() as state:
            self.assertEqual(state["current_stage"], "stage2")
            self.assertIn("203.0.113.55", engine.approved_values(state))
            focus = state["branches"][state["current_focus"]]
            self.assertEqual(focus["status"], "FOCUS")
            self.assertIn("203.0.113.55", focus["title"])
            # 이전 대상 가지는 닫히지 않고 남아 있어야 되돌아갈 수 있다.
            self.assertNotIn("CLOSED", [b["status"] for b in state["branches"].values()])

        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "nmap 203.0.113.55"})))

    def test_stage_workspace_is_created(self) -> None:
        stage1 = engine.WORK_DIR / "stage1"
        self.assertTrue(stage1.is_dir(), "최초 대상 등록 시 work/stage1이 생겨야 한다")
        self.assertTrue((stage1 / "STAGE.md").exists())

        tid, _, _ = engine.propose_target("203.0.113.55", "C-14", "설정 파일에서 발견")
        self.assertFalse((engine.WORK_DIR / "stage2").exists(), "승인 전에는 만들지 않는다")

        engine.decide_target(tid, "approved", "테스트 승인")
        stage2 = engine.WORK_DIR / "stage2"
        self.assertTrue(stage2.is_dir())
        self.assertIn("203.0.113.55", (stage2 / "STAGE.md").read_text(encoding="utf-8"))

    def test_workspace_note_is_not_overwritten(self) -> None:
        note = engine.WORK_DIR / "stage1" / "STAGE.md"
        note.write_text("사용자가 적어둔 메모", encoding="utf-8")
        engine.ensure_stage_workspace("stage1", "192.0.2.10")
        self.assertEqual(note.read_text(encoding="utf-8"), "사용자가 적어둔 메모")

    def test_map_points_to_current_workspace(self) -> None:
        text = engine.MAP_PATH.read_text(encoding="utf-8")
        self.assertIn("작업 폴더: work/stage1/", text)

    def test_reject_keeps_blocking(self) -> None:
        tid, _, _ = engine.propose_target("203.0.113.99", "C-20", "가능성만 있음")
        engine.decide_target(tid, "rejected", "범위 밖")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "curl 203.0.113.99"})), "deny")

    def test_duplicate_proposal_is_idempotent(self) -> None:
        first, _, created_first = engine.propose_target("203.0.113.77", "C-30", "최초")
        second, status, created_second = engine.propose_target("203.0.113.77", "C-31", "중복")
        self.assertEqual(first, second)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(status, "pending")

    def test_harness_own_commands_are_not_scope_checked(self) -> None:
        clear_pending()
        command = 'python3 "$REDTEAM_COMMON/mapctl.py" target-propose --value 203.0.113.55'
        self.assertIsNone(decision_of(pre("Bash", {"command": command})))

    def test_map_renders_target_section(self) -> None:
        tid, _, _ = engine.propose_target("203.0.113.55", "C-14", "설정 파일에서 발견")
        text = engine.MAP_PATH.read_text(encoding="utf-8")
        self.assertIn("## 대상 범위 (T-*)", text)
        self.assertIn("T-01", text)
        self.assertIn("203.0.113.55", text)
        self.assertIn("승인 대기", text)
        ledger = engine.LEDGER_PATH.read_text(encoding="utf-8")
        self.assertIn(tid, ledger)

    def test_scope_enforcement_can_be_disabled(self) -> None:
        clear_pending()
        os.environ["REDTEAM_SCOPE_ENFORCE"] = "0"
        try:
            self.assertIsNone(decision_of(pre("Bash", {"command": "curl 198.51.100.7"})))
        finally:
            os.environ["REDTEAM_SCOPE_ENFORCE"] = "1"

    def test_pending_classification_gate_still_works(self) -> None:
        clear_pending()
        pre("Bash", {"command": "curl http://192.0.2.10/"})
        with engine.locked_state() as state:
            for item in state["pending"].values():
                item["status"] = "AWAITING_CLASSIFICATION"
        result = pre("Bash", {"command": "curl http://192.0.2.10/robots.txt"})
        self.assertEqual(decision_of(result), "deny")
        self.assertIn("동기화 대기", result["hookSpecificOutput"]["permissionDecisionReason"])


class InternalCallDetection(unittest.TestCase):
    """하네스 자기 호출 판정은 범위 검사와 이벤트 기록을 통째로 건너뛴다.

    느슨하면 그대로 우회 통로가 되므로, 마커 문자열이 명령 어딘가에 있다는 이유로
    참이 되어서는 안 된다. 아래는 예전 substring 판정에서 전부 통과하던 형태들이다.
    """

    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            engine.render_unlocked(state)
        clear_pending()

    def _internal(self, command: str) -> bool:
        return engine.is_internal_harness_call(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )

    def test_real_mapctl_call_is_internal(self) -> None:
        # Claude always uses $REDTEAM_COMMON which _expand_common resolves
        self.assertTrue(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status'))
        self.assertTrue(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" clue --event E-0001'))

    def test_marker_in_comment_is_not_internal(self) -> None:
        self.assertFalse(self._internal("nmap 203.0.113.55  # mapctl.py"))

    def test_marker_as_filename_is_not_internal(self) -> None:
        self.assertFalse(self._internal("curl http://203.0.113.55/ -o mapctl.py"))

    def test_chained_command_is_not_internal(self) -> None:
        self.assertFalse(self._internal("echo mapctl.py; nmap 203.0.113.55"))
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status && nmap 203.0.113.55'))
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status | tee /tmp/out'))

    def test_newline_chained_command_is_not_internal(self) -> None:
        # 줄바꿈은 shlex가 공백처럼 삼켜 제어 토큰을 남기지 않는다. 첫 줄이 mapctl
        # 호출인 멀티라인 블록을 internal로 오판하면 나머지 줄이 범위 검사를 건너뛴다.
        self.assertFalse(
            self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status\ncurl http://198.51.100.7/')
        )
        self.assertFalse(
            self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status\r\nnmap 203.0.113.55')
        )

    def test_command_substitution_is_not_internal(self) -> None:
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status --x "$(nmap 203.0.113.55)"'))
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status --x `nmap 203.0.113.55`'))

    def test_script_outside_harness_is_not_internal(self) -> None:
        self.assertFalse(self._internal("python3 /tmp/mapctl.py --anything"))

    def test_smuggled_commands_are_denied_by_scope(self) -> None:
        """판정이 막히면 범위 검사가 이어받아 실제로 거부해야 한다."""
        for command in (
            "nmap 203.0.113.55  # mapctl.py",
            "curl http://203.0.113.55/ -o mapctl.py",
            "echo mapctl.py; nmap 203.0.113.55",
            'python3 "$REDTEAM_COMMON/mapctl.py" status\nnmap 203.0.113.55',
        ):
            with self.subTest(command=command):
                clear_pending()
                self.assertEqual(decision_of(pre("Bash", {"command": command})), "deny")


class ScopeMatching(unittest.TestCase):
    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        clear_pending()

    def _approve(self, value: str) -> None:
        with engine.locked_state() as state:
            engine.register_initial_target(state, value, "stage1")
            engine.render_unlocked(state)

    def test_cidr_scope_allows_hosts_in_range(self) -> None:
        self._approve("192.0.2.0/24")
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "curl http://192.0.2.10/"})))
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "nmap 192.0.2.254"})))

    def test_cidr_scope_still_blocks_outside(self) -> None:
        self._approve("192.0.2.0/24")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 198.51.100.7"})), "deny")

    def test_single_host_scope_does_not_widen(self) -> None:
        self._approve("192.0.2.10")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 192.0.2.11"})), "deny")

    def test_decimal_and_hex_ip_forms_are_detected(self) -> None:
        # 203.0.113.55 를 정수·16진수로 적어도 같은 주소로 해석해야 한다.
        self.assertIn(
            ipaddress.IPv4Address("203.0.113.55"),
            engine.extract_targets("curl http://3405803831/"),
        )
        self.assertIn(
            ipaddress.IPv4Address("203.0.113.55"),
            engine.extract_targets("curl http://0xCB007137/"),
        )
        self._approve("192.0.2.10")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "curl http://3405803831/"})), "deny")

    def test_ipv6_literal_is_detected(self) -> None:
        self.assertIn(
            ipaddress.IPv6Address("2001:db8::dead:beef"),
            engine.extract_targets("nmap 2001:db8::dead:beef"),
        )
        self._approve("192.0.2.10")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 2001:db8::dead:beef"})), "deny")

    def test_approved_ipv6_is_allowed(self) -> None:
        self._approve("2001:db8::/32")
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "nmap 2001:db8::dead:beef"})))

    def test_ports_and_versions_are_not_read_as_ips(self) -> None:
        self.assertEqual(engine.extract_targets("listening on 8080 nginx/1.27.5"), set())
        self.assertEqual(engine.extract_targets("elapsed 1699999999 ms"), set())

    def test_leading_zero_ip_is_detected_not_crashed(self) -> None:
        # leading-zero 옥텟은 ipaddress가 예외를 던진다. 정규화해서 크래시 없이
        # 같은 주소로 탐지해야 한다. 크래시하면 훅이 죽어 범위 검사가 통째로 열린다.
        self.assertIn(
            ipaddress.IPv4Address("192.168.1.5"),
            engine.extract_targets("scan 192.168.001.5"),
        )
        self._approve("192.0.2.10")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 192.168.001.5"})), "deny")


class SurrogateResilience(unittest.TestCase):
    """Windows에서 surrogateescape로 읽힌 명령 출력에는 lone surrogate(\\uD800-\\uDFFF)가
    섞인다. json.dumps(ensure_ascii=False)는 이걸 예외 없이 통과시키므로, 실제 크래시는
    나중의 utf-8 write/encode에서 터진다. 그 지점들을 훅이 살아서 넘겨야 한다.
    크래시하면 pre는 fail-closed로 정상 행동까지 막고 post는 기록이 끊긴다.
    """

    SURROGATE = "out abc\udced\udcba def 한글"

    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            engine.render_unlocked(state)

    def test_payload_bytes_survives_surrogates(self) -> None:
        # value.encode("utf-8")가 서로게이트에서 죽으면 io_bytes 집계 전체가 멈춘다.
        self.assertIsInstance(engine.payload_bytes(self.SURROGATE), int)
        self.assertIsInstance(engine.payload_bytes({"r": self.SURROGATE}), int)

    def test_dumps_safe_output_is_utf8_encodable(self) -> None:
        text = engine.dumps_safe({"r": self.SURROGATE})
        # 반환값은 반드시 utf-8로 인코딩 가능해야 한다(파일/소켓 write가 그걸 한다).
        text.encode("utf-8")

    def test_atomic_text_survives_surrogate_content(self) -> None:
        target = engine.WORK_DIR / "surrogate_probe.txt"
        engine._atomic_text(target, self.SURROGATE)
        # 파일은 유효한 utf-8이어야 하고(서로게이트 미치환 시 read가 깨진다) 크래시가 없어야 한다.
        target.read_text(encoding="utf-8")

    def test_pre_hook_survives_surrogates_in_tool_input(self) -> None:
        """PreToolUse가 가장 위험하다: 크래시하면 fail-closed로 정상 행동까지 막는다.

        json.dumps(ensure_ascii=False)는 서로게이트를 예외 없이 통과시키므로,
        try/except UnicodeEncodeError로 json.dumps만 감싸는 건 효과가 없다.
        실제 크래시는 그 뒤의 stdout.write/file.write에서 터진다.
        """
        clear_pending()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hook.run_pre_fail_closed(
                {
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": "curl http://192.0.2.10/\udcec\udcb4\udcac",
                        "description": "\udced\udcb4\udcac surrogate path",
                    },
                    "tool_use_id": "tu-surro-pre",
                    "agent_id": "main",
                }
            )
        raw = buffer.getvalue().strip()
        # 크래시 없이 끝나야 하고, deny가 나오면 안 된다(정상 IP이므로).
        if raw:
            result = json.loads(raw)
            self.assertNotEqual(
                decision_of(result),
                "deny",
                "서로게이트 때문에 fail-closed deny가 나오면 안 된다",
            )

    def test_pre_hook_deny_path_survives_surrogates(self) -> None:
        """범위 밖 IP + 서로게이트 조합에서도 크래시 없이 deny를 내야 한다."""
        clear_pending()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hook.run_pre_fail_closed(
                {
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": "curl http://203.0.113.55/\udcec\udcb4\udcac",
                    },
                    "tool_use_id": "tu-surro-deny",
                    "agent_id": "main",
                }
            )
        result = json.loads(buffer.getvalue().strip())
        self.assertEqual(decision_of(result), "deny")

    def test_finish_hook_records_surrogate_output_without_crashing(self) -> None:
        clear_pending()
        before = engine.EVENTS_PATH.read_text(encoding="utf-8").count("\n") if engine.EVENTS_PATH.exists() else 0
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hook._finish(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo 한글", "description": "한글 설명"},
                    "tool_use_id": "tu-surrogate",
                    "agent_id": "main",
                    "tool_response": self.SURROGATE,
                },
                failed=False,
            )
        after = engine.EVENTS_PATH.read_text(encoding="utf-8").count("\n")
        self.assertGreater(after, before)  # 기록이 실제로 한 줄 늘었다


class DashboardCsrf(unittest.TestCase):
    """/api/target은 상태를 바꾸는 엔드포인트다.

    client_address만 보면 사용자가 열어둔 아무 웹페이지나 127.0.0.1로 요청을 보내
    대상을 대신 승인시킬 수 있다. text/plain은 프리플라이트도 없다.
    """

    server: ThreadingHTTPServer
    token = "test-token-" + "a" * 32

    @classmethod
    def setUpClass(cls) -> None:
        import map_viewer

        root = Path(os.environ["REDTEAM_RUN_DIR"])
        page = (
            map_viewer.PAGE.replace("__DASHBOARD_LABEL__", "TEST")
            .replace("__CSRF_TOKEN__", cls.token)
            .encode("utf-8")
        )

        def quiet(self, *_args: object) -> None:  # 테스트 출력에 접근 로그를 섞지 않는다
            return

        handler = type(
            "TestDashboardHandler",
            (map_viewer.DashboardHandler,),
            {
                "map_path": root / "MAP.md",
                "events_path": root / "EVENTS.jsonl",
                "ledger_path": root / "LEDGER.md",
                "state_path": root / "runtime" / "STATE.json",
                "page_bytes": page,
                "stage_filter": None,
                "csrf_token": cls.token,
                "allowed_origins": frozenset(),
                "log_message": quiet,
            },
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        handler.allowed_origins = frozenset({"http://127.0.0.1:{0}".format(cls.port)})
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            engine.render_unlocked(state)
        self.tid, _, _ = engine.propose_target("203.0.113.55", "E-0001", "테스트")

    def _post(self, headers: dict) -> int:
        body = json.dumps({"id": self.tid, "action": "approve"}).encode("utf-8")
        request = "POST /api/target HTTP/1.1\r\nHost: 127.0.0.1:{0}\r\n".format(self.port)
        for key, value in headers.items():
            request += "{0}: {1}\r\n".format(key, value)
        request += "Content-Length: {0}\r\nConnection: close\r\n\r\n".format(len(body))
        sock = socket.create_connection(("127.0.0.1", self.port), 5)
        try:
            sock.sendall(request.encode("utf-8") + body)
            head = sock.recv(4096).decode("utf-8", "replace").split("\r\n")[0]
        finally:
            sock.close()
        return int(head.split()[1])

    def _status_of(self, tid: str) -> str:
        with engine.locked_state() as state:
            return str(state["targets"][tid]["status"])

    def test_cross_origin_simple_request_is_rejected(self) -> None:
        status = self._post({"Origin": "https://evil.example", "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_missing_token_is_rejected(self) -> None:
        self.assertEqual(self._post({"Content-Type": "text/plain"}), 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_cross_site_fetch_metadata_is_rejected(self) -> None:
        status = self._post(
            {
                "Content-Type": "application/json",
                "Sec-Fetch-Site": "cross-site",
                "X-Redteam-Token": self.token,
            }
        )
        self.assertEqual(status, 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_wrong_token_is_rejected(self) -> None:
        status = self._post({"Content-Type": "application/json", "X-Redteam-Token": "nope"})
        self.assertEqual(status, 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_dashboard_request_still_works(self) -> None:
        status = self._post(
            {
                "Origin": "http://127.0.0.1:{0}".format(self.port),
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
                "X-Redteam-Token": self.token,
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(self._status_of(self.tid), "approved")

    def test_page_carries_the_token(self) -> None:
        with urllib.request.urlopen("http://127.0.0.1:{0}/".format(self.port), timeout=5) as page:
            text = page.read().decode("utf-8")
        self.assertIn(self.token, text)
        self.assertNotIn("__CSRF_TOKEN__", text)


class HookWiring(unittest.TestCase):
    """settings.json의 훅 배선이 실제 파일을 가리키는지 확인한다.

    다른 테스트는 hook.handle_pre를 직접 호출하므로 이 배선을 전혀 검증하지 못한다.
    실제로 launcher가 한 단계 얕아졌을 때 경로만 ../../common으로 남아 모든 훅이
    로드 실패했고, 테스트 36개는 전부 통과한 채로 그 회귀를 놓쳤다.
    경로가 틀리면 E-ID 발급·범위 차단·지도 동기화가 통째로 죽는다.
    """

    def _hook_specs(self, settings: dict | None = None) -> list[tuple[str, list[str]]]:
        if settings is None:
            settings = json.loads((COMMON / "settings.json").read_text(encoding="utf-8"))
        specs = []
        for event, entries in settings["hooks"].items():
            for entry in entries:
                for spec in entry["hooks"]:
                    args = spec.get("args", [])
                    if any("hook.py" in str(arg) for arg in args):
                        specs.append((event, [str(arg) for arg in args]))
        return specs

    def test_settings_template_uses_the_absolute_path_token(self) -> None:
        # 상대 경로 배선으로 되돌아가면 실행 폴더 깊이가 바뀌는 순간 다시 끊긴다.
        raw = (COMMON / "settings.json").read_text(encoding="utf-8")
        self.assertIn(engine.HARNESS_COMMON_TOKEN, raw)
        self.assertNotIn("CLAUDE_PROJECT_DIR", raw)

    def test_rendered_settings_hook_paths_resolve_to_real_file(self) -> None:
        # 실제로 claude에 넘어가는 것은 prepare_run이 만든 실행별 settings.json이다.
        run_dir = Path(_TEMP.name) / "render-check"
        run_dir.mkdir(parents=True, exist_ok=True)
        engine.prepare_run(run_dir)
        settings = json.loads((run_dir / "settings.json").read_text(encoding="utf-8"))
        specs = self._hook_specs(settings)
        self.assertTrue(specs, "렌더된 settings.json에서 hook.py 배선을 찾지 못했다")
        for event, args in specs:
            resolved = Path(args[0]).resolve()
            self.assertTrue(
                resolved.is_absolute(),
                "{0} 훅 경로가 절대 경로가 아니다: {1}".format(event, args[0]),
            )
            self.assertEqual(
                resolved,
                (COMMON / "hook.py").resolve(),
                "{0} 훅 경로가 실제 hook.py가 아니다: {1}".format(event, args[0]),
            )

    def test_settings_hook_modes_are_understood(self) -> None:
        # 모드 문자열이 틀리면 hook.main이 SystemExit으로 죽는다.
        for event, args in self._hook_specs():
            self.assertGreater(len(args), 1, "{0}: 모드 인자가 없다".format(event))
            self.assertIn(args[1], {"session", "bootstrap", "prepare-run", "pre", "post", "failure"})

    def test_launcher_uses_rendered_settings(self) -> None:
        """launcher.py가 실행별 settings.json을 넘기는지 확인한다."""
        text = (ROOT / "launcher.py").read_text(encoding="utf-8")
        # launcher가 settings_file을 실행 폴더 기준으로 설정하는지 확인
        self.assertIn("settings_file", text)
        self.assertIn("run_dir", text)
        self.assertIn("--settings", text)
        # common/settings.json을 직접 넘기지 않는지 확인
        self.assertNotIn("COMMON_DIR / \"settings.json\"", text)

    def test_launcher_exports_run_identity(self) -> None:
        """launcher.py가 실행 식별 환경변수를 설정하는지 확인한다."""
        text = (ROOT / "launcher.py").read_text(encoding="utf-8")
        for name in ("REDTEAM_RUN_ID", "REDTEAM_CONFIG_LABEL", "REDTEAM_HARNESS_REV"):
            self.assertIn(name, text)


class HookFailsClosed(unittest.TestCase):
    """pre 훅이 판단을 마치지 못하면 통과가 아니라 차단이어야 한다.

    훅이 예외로 죽으면 Claude Code는 그 도구를 그대로 실행한다. 범위 차단이
    존재 이유인 하네스에서 이건 가장 위험한 실패 양식이다.
    """

    def test_unexpected_failure_denies_instead_of_passing(self) -> None:
        original = hook.handle_pre
        hook.handle_pre = lambda _hook: (_ for _ in ()).throw(RuntimeError("파서가 죽었다"))
        hook._EMITTED = False
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                hook.run_pre_fail_closed({"tool_name": "Bash", "tool_input": {"command": "nmap x"}})
            result = json.loads(buffer.getvalue().strip())
        finally:
            hook.handle_pre = original
            hook._EMITTED = False
        self.assertEqual(decision_of(result), "deny")
        self.assertIn("RuntimeError", result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_existing_decision_is_not_double_emitted(self) -> None:
        # 이미 deny를 낸 뒤 죽는 경우 JSON을 두 번 쓰면 출력이 깨진다.
        def emit_then_die(_hook: dict) -> None:
            hook.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "먼저 낸 결정"}})
            raise RuntimeError("그 뒤에 죽음")

        original = hook.handle_pre
        hook.handle_pre = emit_then_die
        hook._EMITTED = False
        try:
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                hook.run_pre_fail_closed({"tool_name": "Bash", "tool_input": {}})
            lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        finally:
            hook.handle_pre = original
            hook._EMITTED = False
        self.assertEqual(len(lines), 1, "결정 JSON은 한 번만 출력되어야 한다")
        self.assertEqual(json.loads(lines[0])["hookSpecificOutput"]["permissionDecisionReason"], "먼저 낸 결정")


class RunTelemetry(unittest.TestCase):
    """EVENTS.jsonl 한 줄만 보고 어느 실행·구성·코드였는지 알 수 있어야 한다.

    이 값들이 일부 줄에서만 빠져도 구성별 집계는 조용히 그 줄들을 잃는다.
    빠졌다는 신호가 없으므로 결과는 그냥 조금 다르게 나올 뿐이다.
    """

    def setUp(self) -> None:
        os.environ["REDTEAM_RUN_ID"] = "20260826T000000Z-test"
        os.environ["REDTEAM_CONFIG_LABEL"] = "테스트 구성"
        os.environ["REDTEAM_HARNESS_REV"] = "abc1234-dirty"
        engine._RUN_META = None  # run_meta는 프로세스 단위로 캐시된다
        engine.EVENTS_PATH.unlink(missing_ok=True)
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            engine.render_unlocked(state)
        clear_pending()

    def tearDown(self) -> None:
        for name in ("REDTEAM_RUN_ID", "REDTEAM_CONFIG_LABEL", "REDTEAM_HARNESS_REV"):
            os.environ.pop(name, None)
        engine._RUN_META = None

    def _events(self) -> list[dict]:
        text = engine.EVENTS_PATH.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def _assert_identity(self, event: dict) -> None:
        self.assertEqual(event["run_id"], "20260826T000000Z-test")
        self.assertEqual(event["config_label"], "테스트 구성")
        self.assertEqual(event["harness_rev"], "abc1234-dirty")
        self.assertIn("io_bytes", event)

    def test_start_event_carries_identity_and_input_bytes(self) -> None:
        tool_input = {"command": "curl http://192.0.2.10/"}
        pre("Bash", tool_input)
        event = self._events()[-1]
        self.assertEqual(event["phase"], "start")
        self._assert_identity(event)
        self.assertEqual(
            event["io_bytes"],
            len(json.dumps(tool_input, ensure_ascii=False).encode("utf-8")),
        )

    def test_finish_event_records_response_bytes(self) -> None:
        pre("Bash", {"command": "curl http://192.0.2.10/"})
        with engine.locked_state() as state:
            tool_use_id = next(iter(state["tool_map"]))
        response = "A" * 500
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            hook._finish(
                {
                    "tool_name": "Bash",
                    "tool_use_id": tool_use_id,
                    "agent_id": "main",
                    "tool_response": response,
                },
                failed=False,
            )
        event = self._events()[-1]
        self.assertEqual(event["phase"], "finish")
        self._assert_identity(event)
        self.assertEqual(event["io_bytes"], 500)

    def test_classification_event_carries_identity_with_zero_io(self) -> None:
        # 분류 줄이 실행 식별자를 빼먹으면 단서 수 집계만 통째로 어긋난다.
        pre("Bash", {"command": "curl http://192.0.2.10/"})
        with engine.locked_state() as state:
            eid = next(iter(state["pending"]))
            item = dict(state["pending"][eid])
        import mapctl

        mapctl.append_classification(eid, item, "변화 없음", "closed", [])
        event = self._events()[-1]
        self.assertEqual(event["phase"], "classification")
        self._assert_identity(event)
        self.assertEqual(event["io_bytes"], 0)

    def test_missing_env_falls_back_to_run_folder_name(self) -> None:
        # runs/<run_id>/engagement 구조면 폴더 이름이 곧 실행 ID다.
        for name in ("REDTEAM_RUN_ID", "REDTEAM_CONFIG_LABEL", "REDTEAM_HARNESS_REV"):
            os.environ.pop(name, None)
        engine._RUN_META = None
        original = engine.ROOT
        engine.ROOT = Path("/somewhere/runs/20260826T010203Z-9f2a/engagement")
        try:
            meta = engine.run_meta()
        finally:
            engine.ROOT = original
            engine._RUN_META = None
        self.assertEqual(meta["run_id"], "20260826T010203Z-9f2a")
        self.assertEqual(meta["config_label"], "default")

    def test_payload_bytes_counts_utf8_not_characters(self) -> None:
        self.assertEqual(engine.payload_bytes("가나다"), 9)
        self.assertEqual(engine.payload_bytes(None), 0)
        self.assertEqual(engine.payload_bytes(b"1234"), 4)


class RunPreparation(unittest.TestCase):
    def test_prepare_run_writes_manifest_and_settings(self) -> None:
        os.environ["REDTEAM_RUN_ID"] = "20260826T000000Z-prep"
        os.environ["REDTEAM_CONFIG_LABEL"] = "prep"
        os.environ["REDTEAM_HARNESS_REV"] = "deadbee"
        engine._RUN_META = None
        run_dir = Path(_TEMP.name) / "prep-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            manifest = engine.prepare_run(run_dir)
        finally:
            for name in ("REDTEAM_RUN_ID", "REDTEAM_CONFIG_LABEL", "REDTEAM_HARNESS_REV"):
                os.environ.pop(name, None)
            engine._RUN_META = None

        self.assertEqual(manifest["run_id"], "20260826T000000Z-prep")
        stored = json.loads((run_dir / "RUN.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["config_label"], "prep")
        self.assertEqual(stored["harness_rev"], "deadbee")
        self.assertIn("started_at", stored)

        rendered = (run_dir / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn(engine.HARNESS_COMMON_TOKEN, rendered)
        json.loads(rendered)

    def test_python_token_is_rendered(self) -> None:
        """settings.json의 __PYTHON__ 토큰이 실제 Python 경로로 치환되는지 확인."""
        run_dir = Path(_TEMP.name) / "python-token-check"
        run_dir.mkdir(parents=True, exist_ok=True)
        engine.prepare_run(run_dir)
        rendered = (run_dir / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn(engine.PYTHON_TOKEN, rendered)
        # 치환된 값이 실제 Python 실행 파일 이름을 포함하는지
        self.assertIn("python", rendered.lower())


class RunStatAggregation(unittest.TestCase):
    """구성별 비교 집계. 실패하거나 빈 실행이 조용히 사라지면 비교가 편향된다."""

    @classmethod
    def setUpClass(cls) -> None:
        import runstat

        cls.runstat = runstat
        cls.runs_dir = Path(_TEMP.name) / "runstat" / "runs"

        def make_run(run_id: str, config: str, rev: str, events: list[dict]) -> None:
            engagement = cls.runs_dir / run_id / "engagement"
            engagement.mkdir(parents=True, exist_ok=True)
            (cls.runs_dir / run_id / "RUN.json").write_text(
                json.dumps({"run_id": run_id, "config_label": config, "harness_rev": rev}),
                encoding="utf-8",
            )
            (engagement / "EVENTS.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
            )

        def action(eid: str, ok: bool, io_in: int, io_out: int, second: int) -> list[dict]:
            stamp = "2026-08-26T00:00:{0:02d}.000Z".format(second)
            return [
                {"event_id": eid, "phase": "start", "ts_utc": stamp, "io_bytes": io_in, "stage_id": "stage1"},
                {
                    "event_id": eid,
                    "phase": "finish",
                    "ts_utc": stamp,
                    "io_bytes": io_out,
                    "stage_id": "stage1",
                    "status": "success" if ok else "failed",
                },
            ]

        make_run("run-a1", "baseline", "aaa1111", action("E-0001", True, 100, 900, 0) + action("E-0002", False, 100, 100, 10))
        make_run("run-a2", "baseline", "aaa1111", action("E-0003", True, 200, 800, 0))
        make_run("run-b1", "tuned", "aaa1111", action("E-0004", True, 50, 50, 0))
        # 아무것도 하지 못한 실행. 집계에서 빠지면 비교가 성공한 실행 쪽으로 치우친다.
        make_run("run-b2", "tuned", "bbb2222", [])

    def _configs(self) -> dict:
        runs = self.runstat.collect_runs(self.runs_dir)
        return {item["config_label"]: item for item in self.runstat.group_by_config(runs)}

    def test_runs_group_by_config_label(self) -> None:
        configs = self._configs()
        self.assertEqual(configs["baseline"]["runs"], 2)
        self.assertEqual(configs["tuned"]["runs"], 2)

    def test_empty_run_is_counted_not_dropped(self) -> None:
        configs = self._configs()
        self.assertEqual(configs["tuned"]["empty_runs"], 1)
        self.assertEqual(configs["tuned"]["actions_mean"], 0.5)

    def test_success_rate_is_pooled_over_attempts(self) -> None:
        # baseline은 3번 시도해 2번 성공했다. 실행별 성공률(50%, 100%)의 평균인
        # 75%가 아니라 66.7%여야 짧은 실행이 과대 대표되지 않는다.
        configs = self._configs()
        self.assertAlmostEqual(configs["baseline"]["success_rate"], 0.667, places=3)

    def test_io_bytes_are_summed_per_run(self) -> None:
        runs = {run["run_id"]: run for run in self.runstat.collect_runs(self.runs_dir)}
        self.assertEqual(runs["run-a1"]["io_in"], 200)
        self.assertEqual(runs["run-a1"]["io_out"], 1000)
        self.assertEqual(runs["run-a1"]["io_total"], 1200)

    def test_mixed_harness_revs_are_surfaced(self) -> None:
        configs = self._configs()
        self.assertEqual(configs["tuned"]["harness_revs"], ["aaa1111", "bbb2222"])
        report = self.runstat.format_report(
            self.runstat.collect_runs(self.runs_dir), list(configs.values()), per_run=True
        )
        self.assertIn("하네스 코드가 섞였다", report)

    def test_truncated_last_line_does_not_drop_the_run(self) -> None:
        events = self.runs_dir / "run-a2" / "engagement" / "EVENTS.jsonl"
        original = events.read_text(encoding="utf-8")
        events.write_text(original + '{"event_id": "E-9999", "pha', encoding="utf-8")
        try:
            runs = {run["run_id"]: run for run in self.runstat.collect_runs(self.runs_dir)}
            self.assertEqual(runs["run-a2"]["actions"], 1)
        finally:
            events.write_text(original, encoding="utf-8")


class StageLabelling(unittest.TestCase):
    def test_next_stage_skips_used_labels(self) -> None:
        state = engine.default_state()
        state["targets"] = {
            "T-01": {"value": "a", "status": "approved", "stage": "stage1"},
            "T-02": {"value": "b", "status": "approved", "stage": "stage2"},
        }
        self.assertEqual(engine.next_stage_label(state), "stage3")


class CrossPlatform(unittest.TestCase):
    """크로스플랫폼 관련 테스트."""

    def test_file_lock_works(self) -> None:
        """파일 잠금이 현재 플랫폼에서 동작하는지 확인."""
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "test.lock"
            with engine._file_lock(lock_path):
                pass
            with engine._file_lock(lock_path):
                pass

    def test_atomic_text_works_on_current_platform(self) -> None:
        """_atomic_text가 현재 플랫폼에서 파일을 올바르게 쓰는지 확인."""
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            engine._atomic_text(path, "테스트 내용\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "테스트 내용\n")

    def test_python_name_includes_platform_default(self) -> None:
        """Windows의 'python'과 Unix의 'python3' 모두 인식하는지 확인."""
        self.assertIn("python", engine._PYTHON_NAMES)
        self.assertIn("python3", engine._PYTHON_NAMES)

    def test_launcher_script_exists(self) -> None:
        """launcher.py가 존재하는지 확인."""
        self.assertTrue((ROOT / "launcher.py").exists())

    def test_launcher_is_listed_in_harness_scripts(self) -> None:
        """engine._HARNESS_SCRIPTS에 launcher.py가 포함되어 있는지 확인."""
        self.assertIn("launcher.py", engine._HARNESS_SCRIPTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
