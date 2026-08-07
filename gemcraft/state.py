"""가공 상태의 인코딩과 선택지 전이 테이블.

가공 상태는 두 부분으로 나뉜다.

* **속성 상태(attribute state)** — 젬 자체의 상태. 4가지 옵션 레벨,
  두 효과가 원하는 효과인지 여부, 그리고 누적 비용 배율.
  DP 에서 하나의 정수 인덱스로 인코딩된다 (총 7,500가지).
* **진행 상태(progress)** — 남은 가공 시도 횟수와 남은 '다른 항목 보기' 횟수.
  DP 의 레이어로 다뤄진다.

효과 '종류'는 8종 이상이지만, 최적 선택에 실제로 영향을 주는 것은
"지금 붙어 있는 효과가 내가 원하는 효과인가" 뿐이다. 따라서 효과 종류를
슬롯당 1비트(`good`)로 축약하고, '효과 변경'은 확률 `p_good` 으로 원하는
효과가 되는 전이로 모델링한다. `p_good` 은 사용자가 지정한다
(예: 딜러가 8종 효과 중 3종을 원하면 3/8 = 0.375).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import rules
from .rules import EFF1, EFF2, MAX_LEVEL, MIN_LEVEL, OPTION_TABLE

LEVELS = MAX_LEVEL - MIN_LEVEL + 1        # 5
N_LEVEL_STATES = LEVELS ** 4              # 625
N_COST = rules.COST_MOD_MAX - rules.COST_MOD_MIN + 1  # 3
N_ATTR_STATES = N_LEVEL_STATES * 2 * 2 * N_COST       # 7500
N_OPTIONS = len(OPTION_TABLE)

OPTION_IDS = [row[0] for row in OPTION_TABLE]
OPTION_LABELS = {row[0]: row[1] for row in OPTION_TABLE}
OPTION_INDEX = {oid: i for i, oid in enumerate(OPTION_IDS)}
BASE_WEIGHTS = np.array([row[2] for row in OPTION_TABLE], dtype=np.float64)


@dataclass(frozen=True)
class GemState:
    """사람이 읽는 형태의 가공 상태."""

    will: int = MIN_LEVEL
    point: int = MIN_LEVEL
    eff1: int = MIN_LEVEL
    eff2: int = MIN_LEVEL
    eff1_good: bool = True
    eff2_good: bool = True
    cost_mod: int = 0          # -1 / 0 / +1  (-100% / 0% / +100%)
    attempts_left: int = 0
    rerolls_left: int = 0

    @property
    def levels(self) -> tuple[int, int, int, int]:
        return (self.will, self.point, self.eff1, self.eff2)

    @property
    def total_level(self) -> int:
        return sum(self.levels)

    @property
    def gem_grade(self) -> str:
        return rules.gem_grade(self.total_level)

    def validate(self) -> None:
        for name, value in zip(rules.ATTR_NAMES.values(), self.levels):
            if not MIN_LEVEL <= value <= MAX_LEVEL:
                raise ValueError(f"{name} 레벨은 {MIN_LEVEL}~{MAX_LEVEL} 이어야 합니다: {value}")
        if not rules.COST_MOD_MIN <= self.cost_mod <= rules.COST_MOD_MAX:
            raise ValueError(f"비용 배율 단계가 범위를 벗어났습니다: {self.cost_mod}")
        if self.attempts_left < 0 or self.rerolls_left < 0:
            raise ValueError("남은 횟수는 음수일 수 없습니다.")

    def attr_index(self) -> int:
        return encode(
            self.will, self.point, self.eff1, self.eff2,
            self.eff1_good, self.eff2_good, self.cost_mod,
        )


def encode(will: int, point: int, eff1: int, eff2: int,
           good1: bool, good2: bool, cost_mod: int) -> int:
    lvl = (((will - 1) * LEVELS + (point - 1)) * LEVELS + (eff1 - 1)) * LEVELS + (eff2 - 1)
    return ((lvl * 2 + int(good1)) * 2 + int(good2)) * N_COST + (cost_mod - rules.COST_MOD_MIN)


def decode(index: int) -> tuple[int, int, int, int, bool, bool, int]:
    index, cost = divmod(index, N_COST)
    index, good2 = divmod(index, 2)
    lvl, good1 = divmod(index, 2)
    lvl, e2 = divmod(lvl, LEVELS)
    lvl, e1 = divmod(lvl, LEVELS)
    will, point = divmod(lvl, LEVELS)
    return (will + 1, point + 1, e1 + 1, e2 + 1,
            bool(good1), bool(good2), cost + rules.COST_MOD_MIN)


# --- 전체 속성 상태를 배열로 펼쳐 둔다 (벡터화용) --------------------------------
_ALL = np.arange(N_ATTR_STATES)
_rest, _COST_ARR = np.divmod(_ALL, N_COST)
_rest, _GOOD2_ARR = np.divmod(_rest, 2)
_lvl, _GOOD1_ARR = np.divmod(_rest, 2)
_lvl, _E2_ARR = np.divmod(_lvl, LEVELS)
_lvl, _E1_ARR = np.divmod(_lvl, LEVELS)
_WILL_ARR, _POINT_ARR = np.divmod(_lvl, LEVELS)

#: shape (4, N_ATTR_STATES) — 각 속성의 레벨(1~5)
ATTR_LEVELS = np.stack([_WILL_ARR, _POINT_ARR, _E1_ARR, _E2_ARR]) + 1
#: shape (2, N_ATTR_STATES) — 각 효과 슬롯이 원하는 효과인지
ATTR_GOOD = np.stack([_GOOD1_ARR, _GOOD2_ARR]).astype(bool)
#: shape (N_ATTR_STATES,) — 비용 배율 단계 (-1/0/+1)
ATTR_COST = _COST_ARR + rules.COST_MOD_MIN


def _validity_matrix() -> np.ndarray:
    """shape (N_ATTR_STATES, N_OPTIONS) — 각 상태에서 그 선택지가 등장할 수 있는가.

    공식 확률표의 '미등장 조건'을 그대로 옮긴 것이다.
      * +k 증가: 해당 옵션 레벨이 (MAX - k + 1) 이상이면 미등장
                 → 증가 결과가 5를 넘는 일이 없다.
      * -1 감소: 해당 옵션 레벨이 1이면 미등장.
      * 비용 ±100%: 이미 해당 한계에 도달했으면 미등장.
    """
    valid = np.ones((N_ATTR_STATES, N_OPTIONS), dtype=bool)
    for oid, _label, _weight, kind, param in OPTION_TABLE:
        col = OPTION_INDEX[oid]
        if kind == "delta":
            attr, delta = param
            level = ATTR_LEVELS[attr]
            if delta > 0:
                valid[:, col] = level <= MAX_LEVEL - delta
            else:
                valid[:, col] = level > MIN_LEVEL
        elif kind == "cost":
            limit = rules.COST_MOD_MAX if param > 0 else rules.COST_MOD_MIN
            valid[:, col] = ATTR_COST != limit
    return valid


VALID = _validity_matrix()


def validity(attempts_left: int) -> np.ndarray:
    """남은 가공 횟수까지 반영한 선택지 유효성.

    공식 확률표상 마지막 가공 차수에는 다음 차수에만 영향을 주는 비용 변경과
    다른 항목 보기 증가가 등장하지 않는다.
    """
    if attempts_left < 1:
        raise ValueError("남은 가공 횟수는 1 이상이어야 합니다.")
    if attempts_left != 1:
        return VALID
    out = VALID.copy()
    for oid in ("cost+", "cost-", "reroll+1", "reroll+2"):
        out[:, OPTION_INDEX[oid]] = False
    return out


def hand_probabilities(attempts_left: int = 2) -> np.ndarray:
    """shape (N_ATTR_STATES, N_OPTIONS) — 상태별로 재정규화된 선택지 등장 확률."""
    valid = validity(attempts_left)
    weights = np.where(valid, BASE_WEIGHTS[None, :], 0.0)
    total = weights.sum(axis=1, keepdims=True)
    if not np.all(total > 0):
        raise AssertionError("모든 선택지가 미등장 조건에 걸리는 상태가 존재합니다.")
    return weights / total


def transitions(p_good: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """선택지를 골랐을 때의 다음 속성 상태.

    반환값 ``(succ_a, succ_b, mix_a, gained_rerolls)`` 는 모두
    shape ``(N_ATTR_STATES, N_OPTIONS)``. 다음 상태는 확률 ``mix_a`` 로
    ``succ_a``, 나머지 확률로 ``succ_b`` 가 된다 (효과 변경만 확률적이고
    나머지는 ``succ_a == succ_b``).
    """
    if not 0.0 <= p_good <= 1.0:
        raise ValueError(f"p_good 은 0~1 이어야 합니다: {p_good}")

    levels = ATTR_LEVELS.copy()
    good = ATTR_GOOD.copy()
    cost = ATTR_COST.copy()

    def enc(lv: np.ndarray, gd: np.ndarray, cs: np.ndarray) -> np.ndarray:
        flat = (((lv[0] - 1) * LEVELS + (lv[1] - 1)) * LEVELS + (lv[2] - 1)) * LEVELS + (lv[3] - 1)
        return ((flat * 2 + gd[0].astype(int)) * 2 + gd[1].astype(int)) * N_COST \
            + (cs - rules.COST_MOD_MIN)

    identity = enc(levels, good, cost)
    succ_a = np.repeat(identity[:, None], N_OPTIONS, axis=1)
    succ_b = succ_a.copy()
    mix_a = np.ones((N_ATTR_STATES, N_OPTIONS))
    gained = np.zeros((N_ATTR_STATES, N_OPTIONS), dtype=np.int64)

    for oid, _label, _weight, kind, param in OPTION_TABLE:
        col = OPTION_INDEX[oid]
        if kind == "delta":
            attr, delta = param
            lv = levels.copy()
            lv[attr] = np.clip(lv[attr] + delta, MIN_LEVEL, MAX_LEVEL)
            succ_a[:, col] = succ_b[:, col] = enc(lv, good, cost)
        elif kind == "change":
            slot = 0 if param == EFF1 else 1
            gd_good = good.copy()
            gd_good[slot] = True
            gd_bad = good.copy()
            gd_bad[slot] = False
            succ_a[:, col] = enc(levels, gd_good, cost)
            succ_b[:, col] = enc(levels, gd_bad, cost)
            # 효과 변경 시 기존 효과와 반대편 슬롯의 효과는 후보에서 빠진다.
            # 각 젬 세부 타입에는 서로 다른 효과 4종이 있으므로, 원하는 효과의
            # 전체 비율(p_good)과 두 슬롯의 현재 적합 여부만으로 조건부 확률을
            # 정확히 구할 수 있다: (원하는 효과 수 - 제외된 원하는 효과 수) / 2.
            desired = 4.0 * p_good
            mix_a[:, col] = np.clip(
                (desired - good[0].astype(float) - good[1].astype(float)) / 2.0,
                0.0, 1.0,
            )
        elif kind == "cost":
            cs = np.clip(cost + param, rules.COST_MOD_MIN, rules.COST_MOD_MAX)
            succ_a[:, col] = succ_b[:, col] = enc(levels, good, cs)
        elif kind == "reroll":
            gained[:, col] = param
        elif kind == "keep":
            pass
        else:  # pragma: no cover - rules.py 와 어긋난 경우
            raise ValueError(f"알 수 없는 선택지 종류: {kind}")

    return succ_a, succ_b, mix_a, gained
