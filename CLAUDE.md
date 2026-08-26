# Unified Pentest Harness

`common/unified-prompt.md`를 정본으로 따른다.
Scope는 `SCOPE.yaml`을 참조한다.
실행은 반드시 `launcher.py`로 시작한다.

## 핵심 루프

모든 행동은 이 4단계를 따른다. 건너뛰지 않는다.

```
ANALYZE (전체 수집) → PLAN (공격 설계) → EXECUTE (실행) → REVIEW (판정)
                ↑                                              │
                └──── 실패 4분류 후 적절한 단계로 복귀 ────────┘
```

## 실패 4분류

| 분류 | mapctl 처리 | 다음 |
|------|-------------|------|
| 가설오류 | resolve --outcome closed | PLAN (다음 가설) |
| 방법오류 | resolve --outcome candidate | PLAN (같은 가설, 다른 방법) |
| 정보부족 | clue --existence hypothesis | ANALYZE 복귀 |
| 방어기제 | clue --existence confirmed | PLAN (우회) |

## E/C/B 추적

- **E-이벤트**: 훅이 자동 기록. 외부 행동 완료 후 mapctl로 분류 필수.
- **C-단서**: 관찰 결과의 의미. ANALYZE 필수 체크리스트의 근거.
- **B-가지**: 탐색 경로. 사용자가 방향 조정에 사용.

## 산출물

| 파일 | 용도 | 소유 |
|------|------|------|
| MAP.md | 실시간 침투 지도 | 하네스 (편집 금지) |
| LEDGER.md | 구조화 단서 대장 | 하네스 (편집 금지) |
| EVENTS.jsonl | 시간순 행동 로그 | 하네스 (편집 금지) |
| work/stageN/ | 작업 파일 | Claude가 자유 생성 |
| SCOPE.yaml | Scope 정의 | 사용자 |

## 금지

- ANALYZE 필수 C-단서 없이 PLAN 진입
- REVIEW 없이 다음 EXECUTE로 넘어가기
- 실패 원인 4분류 없이 같은 방법 반복
- MAP.md, LEDGER.md 직접 편집
