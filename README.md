# Unified Pentest Harness

v3-harness의 ANALYZE→PLAN→EXECUTE→REVIEW 공격 루프와 redteam-harness의 E/C/B 증적 추적 시스템을 통합한 모의침투 하네스.

## 특징

- **구조적 공격 루프**: 4단계(ANALYZE→PLAN→EXECUTE→REVIEW)를 강제하여 삽질 방지
- **실시간 증적 추적**: E(이벤트)/C(단서)/B(가지) 시스템으로 모든 행동 자동 기록
- **분류 게이트**: 이전 행동을 분류하지 않으면 다음 외부 행동 차단
- **실패 4분류**: 가설오류/방법오류/정보부족/방어기제로 실패 원인 구조화
- **실시간 대시보드**: 브라우저에서 MAP 확인, 대상 승인/거부
- **크로스플랫폼**: Windows + macOS/Linux

## 빠른 시작

1. `SCOPE.yaml`을 편집하여 대상과 범위 설정
2. 실행:

```bash
python launcher.py
```

3. Claude 세션이 시작되면 SCOPE.yaml을 읽고 mapctl init 호출
4. 대시보드에서 실시간 MAP 확인 (기본 http://localhost:8765)

## 구조

```
unified-harness/
  common/
    engine.py          # 상태 엔진 (크로스플랫폼)
    hook.py            # Claude Code 훅 (PreToolUse/PostToolUse)
    mapctl.py          # CLI: 단서 분류, 가지 관리, 대상 승인
    map_viewer.py      # 실시간 대시보드 서버
    runstat.py         # 실행 간 비교 집계
    settings.json      # 훅 배선 템플릿
    unified-prompt.md  # 통합 시스템 프롬프트
  launcher.py          # 크로스플랫폼 런처
  runs/                # 실행별 격리 폴더
  SCOPE.yaml           # Scope 설정 템플릿
  CLAUDE.md            # Claude Code 프로젝트 설정
```

## 환경 변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `REDTEAM_RUN_ID` | 자동 생성 | 실행 ID |
| `REDTEAM_CONFIG_LABEL` | `default` | 구성 라벨 (비교용) |
| `REDTEAM_PORT` | `8765` | 대시보드 포트 |
| `REDTEAM_SCOPE_ENFORCE` | `1` | 범위 강제 (0=끔) |
| `CLAUDE_BIN` | 자동 탐색 | claude CLI 경로 |

## 실행 비교

여러 번 실행한 뒤 구성별 비교:

```bash
python common/runstat.py --per-run
```

## 라이선스

MIT (redteam-harness 기반)
