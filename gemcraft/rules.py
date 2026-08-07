"""젬 가공 규칙 상수.

출처
----
* 확률표: 로스트아크 공식 확률 정보 공개 - "젬 가공, 젬 융합"
  https://m-lostark.game.onstove.com/Probability/젬%20가공,%20젬%20융합
* 등급별 가공 시도 / 다른 항목 보기 횟수, 등급 판정: 공식 게임 가이드 - "아크 그리드"
  https://m-lostark.game.onstove.com/GameGuide/Pages/아크%20그리드

패치로 수치가 바뀌면 이 파일만 고치면 된다. 엔진은 여기 값을 읽어서 동작한다.
"""

from __future__ import annotations

# --- 젬의 4가지 옵션 ---------------------------------------------------------
# 모두 Lv.1에서 시작해 Lv.5까지 오른다. 높을수록 좋다.
WILL = 0   # 의지력 효율   (높을수록 젬이 소모하는 의지력이 적다)
POINT = 1  # 질서/혼돈 포인트 (코어 활성화에 기여하는 포인트)
EFF1 = 2   # 첫번째 효과
EFF2 = 3   # 두번째 효과

ATTR_NAMES = {
    WILL: "의지력 효율",
    POINT: "질서/혼돈 포인트",
    EFF1: "첫번째 효과",
    EFF2: "두번째 효과",
}

MIN_LEVEL = 1
MAX_LEVEL = 5

# --- 등급별 가공 조건 ---------------------------------------------------------
# (가공 시도 횟수, 다른 항목 보기 기본 횟수)
GRADES: dict[str, tuple[int, int]] = {
    "고급": (5, 0),
    "희귀": (7, 1),
    "영웅": (9, 2),
}
GRADE_ALIASES = {
    "uncommon": "고급", "advanced": "고급",
    "rare": "희귀",
    "epic": "영웅", "heroic": "영웅", "legendary": "영웅",
}

# '다른 항목 보기'는 가공을 1회 진행한 후부터 사용 가능하다.
REROLL_AVAILABLE_FROM_TURN = 1  # 0-indexed 턴 번호

# 가공 1회 기본 비용(골드). 가공 비용 ±100% 옵션이 이 값에 곱해진다.
BASE_COST_GOLD = 900
COST_MOD_MIN = -1  # -100%
COST_MOD_MAX = +1  # +100%

# --- 가공 완료 후 젬 등급 -------------------------------------------------------
# 4가지 옵션 레벨의 총합으로 판정된다 (최소 4, 최대 20).
GEM_GRADE_THRESHOLDS = [
    ("전설", 4),
    ("유물", 16),
    ("고대", 19),
]


def gem_grade(total_level: int) -> str:
    name = GEM_GRADE_THRESHOLDS[0][0]
    for label, threshold in GEM_GRADE_THRESHOLDS:
        if total_level >= threshold:
            name = label
    return name


# --- 매 가공 시 등장하는 선택지 풀 ------------------------------------------------
# (id, 표시명, 등장 확률(%), 종류, 파라미터)
#
# `미등장 조건`은 options.py 의 validity 로직에 반영되어 있다. 어떤 선택지가
# 미등장 조건에 걸리면 풀에서 제외되고 나머지 확률이 재정규화된다.
#
# 확률 합계는 정확히 100.0000% 이다 (tests 에서 검증).
OPTION_TABLE: list[tuple[str, str, float, str, object]] = [
    # 의지력 효율
    ("will+1",  "의지력 효율 +1 증가",       11.6500, "delta", (WILL, +1)),
    ("will+2",  "의지력 효율 +2 증가",        4.4000, "delta", (WILL, +2)),
    ("will+3",  "의지력 효율 +3 증가",        1.7500, "delta", (WILL, +3)),
    ("will+4",  "의지력 효율 +4 증가",        0.4500, "delta", (WILL, +4)),
    ("will-1",  "의지력 효율 -1 감소",        3.0000, "delta", (WILL, -1)),
    # 질서/혼돈 포인트
    ("point+1", "질서/혼돈 포인트 +1 증가",  11.6500, "delta", (POINT, +1)),
    ("point+2", "질서/혼돈 포인트 +2 증가",   4.4000, "delta", (POINT, +2)),
    ("point+3", "질서/혼돈 포인트 +3 증가",   1.7500, "delta", (POINT, +3)),
    ("point+4", "질서/혼돈 포인트 +4 증가",   0.4500, "delta", (POINT, +4)),
    ("point-1", "질서/혼돈 포인트 -1 감소",   3.0000, "delta", (POINT, -1)),
    # 첫번째 효과
    ("eff1+1",  "첫번째 효과 Lv. 1 증가",    11.6500, "delta", (EFF1, +1)),
    ("eff1+2",  "첫번째 효과 Lv. 2 증가",     4.4000, "delta", (EFF1, +2)),
    ("eff1+3",  "첫번째 효과 Lv. 3 증가",     1.7500, "delta", (EFF1, +3)),
    ("eff1+4",  "첫번째 효과 Lv. 4 증가",     0.4500, "delta", (EFF1, +4)),
    ("eff1-1",  "첫번째 효과 Lv. 1 감소",     3.0000, "delta", (EFF1, -1)),
    # 두번째 효과
    ("eff2+1",  "두번째 효과 Lv. 1 증가",    11.6500, "delta", (EFF2, +1)),
    ("eff2+2",  "두번째 효과 Lv. 2 증가",     4.4000, "delta", (EFF2, +2)),
    ("eff2+3",  "두번째 효과 Lv. 3 증가",     1.7500, "delta", (EFF2, +3)),
    ("eff2+4",  "두번째 효과 Lv. 4 증가",     0.4500, "delta", (EFF2, +4)),
    ("eff2-1",  "두번째 효과 Lv. 1 감소",     3.0000, "delta", (EFF2, -1)),
    # 효과 종류 변경 (레벨은 유지되고 효과 종류만 다시 뽑는다)
    ("chg1",    "첫번째 효과 변경",           3.2500, "change", EFF1),
    ("chg2",    "두번째 효과 변경",           3.2500, "change", EFF2),
    # 비용
    ("cost+",   "가공 비용 +100% 증가",       1.7500, "cost", +1),
    ("cost-",   "가공 비용 -100% 감소",       1.7500, "cost", -1),
    # 기타
    ("keep",    "가공 상태 유지",             1.7500, "keep", None),
    ("reroll+1", "다른 항목 보기 +1회 증가",  2.5000, "reroll", 1),
    ("reroll+2", "다른 항목 보기 +2회 증가",  0.7500, "reroll", 2),
]

# 매 가공마다 제시되는 선택지 개수
HAND_SIZE = 4


def normalize_grade(name: str) -> str:
    key = name.strip()
    if key in GRADES:
        return key
    lowered = key.lower()
    if lowered in GRADE_ALIASES:
        return GRADE_ALIASES[lowered]
    raise ValueError(f"알 수 없는 젬 등급: {name!r} (가능: {', '.join(GRADES)})")
