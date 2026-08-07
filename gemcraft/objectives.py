"""가공 목표(objective) 정의.

목표는 "가공이 끝난 시점의 젬"에 점수를 매기는 함수다. DP 는 이 점수의
기댓값을 최대화하는 선택을 찾는다. 지표(indicator) 형태의 목표를 쓰면
결과값이 곧 "목표 달성 확률"이 되므로 해석이 쉽다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import rules
from .state import ATTR_GOOD, ATTR_LEVELS

ScoreFn = Callable[[np.ndarray, np.ndarray], np.ndarray]


@dataclass(frozen=True)
class Objective:
    name: str
    description: str
    score_fn: ScoreFn
    #: 점수가 0~1 확률이면 True (출력 포맷용)
    is_probability: bool

    def terminal_values(self) -> np.ndarray:
        """shape (N_ATTR_STATES,) — 모든 종료 상태의 점수."""
        values = np.asarray(self.score_fn(ATTR_LEVELS, ATTR_GOOD), dtype=np.float64)
        if values.shape != (ATTR_LEVELS.shape[1],):
            raise ValueError(f"목표 함수가 잘못된 shape 을 반환했습니다: {values.shape}")
        return values


def _grade_at_least(label: str) -> ScoreFn:
    threshold = dict(rules.GEM_GRADE_THRESHOLDS)[label]

    def fn(levels: np.ndarray, good: np.ndarray) -> np.ndarray:
        return (levels.sum(axis=0) >= threshold).astype(np.float64)

    return fn


def _target(mins: tuple[int, int, int, int], require_good: bool,
            min_total: int | None) -> ScoreFn:
    def fn(levels: np.ndarray, good: np.ndarray) -> np.ndarray:
        ok = np.ones(levels.shape[1], dtype=bool)
        for attr, need in enumerate(mins):
            if need > rules.MIN_LEVEL:
                ok &= levels[attr] >= need
        if min_total is not None:
            ok &= levels.sum(axis=0) >= min_total
        if require_good:
            # 목표 레벨을 요구한 효과 슬롯만 '원하는 효과'여야 한다.
            if mins[rules.EFF1] > rules.MIN_LEVEL:
                ok &= good[0]
            if mins[rules.EFF2] > rules.MIN_LEVEL:
                ok &= good[1]
        return ok.astype(np.float64)

    return fn


def _weighted(weights: tuple[float, float, float, float],
              bad_effect_scale: float) -> ScoreFn:
    w = np.asarray(weights, dtype=np.float64)

    def fn(levels: np.ndarray, good: np.ndarray) -> np.ndarray:
        contrib = w[:, None] * levels
        # 원하지 않는 효과가 붙은 슬롯은 가치를 깎는다.
        contrib[rules.EFF1] *= np.where(good[0], 1.0, bad_effect_scale)
        contrib[rules.EFF2] *= np.where(good[1], 1.0, bad_effect_scale)
        return contrib.sum(axis=0)

    return fn


_TARGET_RE = re.compile(
    r"^target=(\d),(\d),(\d),(\d)(?P<flags>(?:\+good|\+total\d+)*)$"
)
_WEIGHTED_RE = re.compile(
    r"^weighted=([\d.]+),([\d.]+),([\d.]+),([\d.]+)$"
)

BUILTIN_HELP = """\
사용 가능한 목표(--goal):

  ancient            고대 젬(레벨 총합 19 이상) 확률을 최대화
  relic              유물 이상(총합 16 이상) 확률을 최대화
  total              레벨 총합의 기댓값을 최대화
  target=W,P,E1,E2   각 옵션이 지정 레벨 이상일 확률을 최대화
                     (0 은 '상관없음'. 예: target=4,4,0,0 → 의지력·포인트 4 이상)
                     뒤에 +good 를 붙이면 레벨을 요구한 효과 슬롯이
                     '원하는 효과'일 것까지 요구한다. (예: target=4,4,3,0+good)
                     뒤에 +total19 처럼 붙이면 총합 조건을 추가한다.
  weighted=a,b,c,d   의지력/포인트/효과1/효과2 레벨의 가중합 기댓값을 최대화
                     (예: weighted=1,1,0.6,0.6)
"""


def parse_goal(spec: str, bad_effect_scale: float = 0.0) -> Objective:
    """목표 문자열을 :class:`Objective` 로 변환한다."""
    spec = spec.strip()

    if spec == "ancient":
        return Objective("ancient", "고대 젬(총합 19+) 확률",
                         _grade_at_least("고대"), True)
    if spec == "relic":
        return Objective("relic", "유물 이상(총합 16+) 확률",
                         _grade_at_least("유물"), True)
    if spec == "total":
        return Objective("total", "레벨 총합 기댓값",
                         lambda levels, good: levels.sum(axis=0).astype(np.float64),
                         False)

    match = _TARGET_RE.match(spec)
    if match:
        mins = tuple(int(match.group(i)) for i in range(1, 5))
        flags = match.group("flags") or ""
        require_good = "+good" in flags
        total_match = re.search(r"\+total(\d+)", flags)
        min_total = int(total_match.group(1)) if total_match else None
        for attr, need in enumerate(mins):
            if need and not rules.MIN_LEVEL <= need <= rules.MAX_LEVEL:
                raise ValueError(
                    f"{rules.ATTR_NAMES[attr]} 목표 레벨이 범위를 벗어났습니다: {need}"
                )
        parts = [
            f"{rules.ATTR_NAMES[a]}≥{n}" for a, n in enumerate(mins) if n > rules.MIN_LEVEL
        ]
        if min_total is not None:
            parts.append(f"총합≥{min_total}")
        if require_good:
            parts.append("효과 종류 일치")
        desc = " · ".join(parts) if parts else "조건 없음"
        return Objective(spec, f"{desc} 달성 확률",
                         _target(mins, require_good, min_total), True)

    match = _WEIGHTED_RE.match(spec)
    if match:
        weights = tuple(float(match.group(i)) for i in range(1, 5))
        desc = "가중 점수 기댓값 (" + ", ".join(
            f"{rules.ATTR_NAMES[a]}×{w:g}" for a, w in enumerate(weights)
        ) + ")"
        return Objective(spec, desc, _weighted(weights, bad_effect_scale), False)

    raise ValueError(f"알 수 없는 목표: {spec!r}\n\n{BUILTIN_HELP}")
