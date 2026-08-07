"""정책 시뮬레이터.

DP 가 계산한 값이 실제로 그 확률을 내는지 검증하고, 기대 골드 비용과
최종 젬 등급 분포처럼 DP 값에는 안 담기는 통계를 뽑는 데 쓴다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from . import rules, state as st
from .solver import Policy, initial_state


@dataclass
class SimulationResult:
    runs: int
    goal_rate: float           # 지표 목표의 실제 달성 비율 (또는 평균 점수)
    predicted: float           # DP 가 예측한 값
    mean_gold: float
    mean_levels: tuple[float, float, float, float]
    grade_counts: Counter = field(default_factory=Counter)
    reroll_uses: float = 0.0

    def report(self) -> str:
        lines = [
            f"시뮬레이션 {self.runs:,}회",
            f"  DP 예측값        : {self.predicted:.4f}",
            f"  실측값           : {self.goal_rate:.4f}",
            f"  평균 소모 골드    : {self.mean_gold:,.0f} G",
            f"  평균 리롤 사용    : {self.reroll_uses:.2f}회",
            "  평균 레벨        : " + ", ".join(
                f"{rules.ATTR_NAMES[a]} {v:.2f}" for a, v in enumerate(self.mean_levels)
            ),
            "  최종 등급 분포    : " + ", ".join(
                f"{label} {self.grade_counts[label] / self.runs:.1%}"
                for label, _ in rules.GEM_GRADE_THRESHOLDS
            ),
        ]
        return "\n".join(lines)


def simulate(policy: Policy, runs: int = 20_000, seed: int = 0,
             start: st.GemState | None = None) -> SimulationResult:
    rng = np.random.default_rng(seed)
    start = start or initial_state(policy.grade)
    policy._check(start)

    cumulative = np.cumsum(policy._probs, axis=1)
    terminal = policy.objective.terminal_values()
    gained = policy._gained_col
    mix_a = policy._mix_a
    succ_a = policy._succ_a
    succ_b = policy._succ_b

    score_total = 0.0
    gold_total = 0
    reroll_total = 0
    level_total = np.zeros(4)
    grades: Counter = Counter()

    for _ in range(runs):
        idx = start.attr_index()
        n, r = start.attempts_left, start.rerolls_left
        while n > 0:
            turn = policy.attempts - n
            q = policy._q_row(idx, n, r)
            hand = np.searchsorted(cumulative[idx], rng.random(rules.HAND_SIZE))
            best = hand[int(np.argmax(q[hand]))]

            if r >= 1 and turn >= rules.REROLL_AVAILABLE_FROM_TURN:
                reroll_value = policy._values[n, r - 1, idx]
                if reroll_value > q[best]:
                    r -= 1
                    reroll_total += 1
                    continue

            cost_mod = int(st.ATTR_COST[idx])
            gold_total += rules.BASE_COST_GOLD * (1 + cost_mod)

            if mix_a[idx, best] < 1.0 and rng.random() >= mix_a[idx, best]:
                idx = int(succ_b[idx, best])
            else:
                idx = int(succ_a[idx, best])
            r = min(r + int(gained[best]), policy.max_rerolls)
            n -= 1

        score_total += float(terminal[idx])
        level_total += st.ATTR_LEVELS[:, idx]
        grades[rules.gem_grade(int(st.ATTR_LEVELS[:, idx].sum()))] += 1

    return SimulationResult(
        runs=runs,
        goal_rate=score_total / runs,
        predicted=policy.state_value(start),
        mean_gold=gold_total / runs,
        mean_levels=tuple(level_total / runs),
        grade_counts=grades,
        reroll_uses=reroll_total / runs,
    )
