"""가공 최적 선택 DP.

문제의 구조
-----------
가공은 유한 지평 마르코프 결정 과정이다.

* 상태: (젬 속성 상태, 남은 가공 시도 횟수 ``n``, 남은 다른 항목 보기 횟수 ``r``)
* 매 턴 중복 없는 4개의 가능성이 확률적으로 제시된다.
* 행동: 가공(4개 중 하나가 각 25%로 무작위 적용), '다른 항목 보기', 가공 완료
  (시도는 소모하지 않고 ``r`` 만 1 줄어든 뒤 4개를 다시 뽑는다).

따라서 값 함수는 아래 두 식으로 정확히 계산된다.

    process(hand)      = sum(Q(s,n,r,o) for o in hand) / 4
    V(s, n, r)         = E_hand[max(process(hand), reroll, complete)]
    V(s, 0, r)         = objective(s)

``V(s,n,r-1)`` 은 같은 레이어의 더 낮은 ``r`` 이므로 ``r`` 오름차순으로 풀면
고정점 반복 없이 한 번에 계산된다. 리롤을 여러 번 연속으로 쓰는 것도
이 재귀에 자연히 포함된다.

공식 규칙대로 한 손패 안에는 같은 가능성이 중복되지 않는다. 확률표의 가중치로
하나씩 뽑고 이미 뽑은 항목을 제외한 뒤 재정규화하는 비복원 추출로 모델링한다.
손패 공간이 크므로 사전 상태의 기대값은 상태마다 고정된 결정적 표본으로 계산한다.
눈앞에 표시된 실제 4개에 대한 가공/리롤/완료 비교는 근사가 아닌 정확한 계산이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from . import rules, state as st
from .objectives import Objective
from .rules import HAND_SIZE

HAND_SAMPLES = 24


@dataclass(frozen=True)
class OptionValue:
    option_id: str
    label: str
    value: float
    available: bool
    appear_prob: float


@dataclass(frozen=True)
class HandDecision:
    action: str
    process_value: float
    reroll_value: float | None
    complete_value: float | None


class Policy:
    """특정 (등급, 목표) 조합에 대해 미리 풀어 둔 최적 정책."""

    def __init__(self, grade: str, objective: Objective, p_good: float = 1.0,
                 attempts: int | None = None):
        self.grade = rules.normalize_grade(grade)
        self.objective = objective
        self.p_good = float(p_good)
        self.attempts, self.base_rerolls = rules.GRADES[self.grade]
        if attempts is not None:
            # 짧은 지평만 풀고 싶을 때(테스트/부분 가공) 시도 횟수를 줄인다.
            self.attempts = int(attempts)
        self.max_rerolls = self.base_rerolls + 2 * self.attempts

        self._succ_a, self._succ_b, self._mix_a, self._gained = st.transitions(self.p_good)
        self._gained_col = self._gained[0]  # 리롤 획득량은 상태와 무관
        self._values = self._solve(objective.terminal_values())

    # --- 내부 ---------------------------------------------------------------
    def _q_matrix(self, n: int, r: int) -> np.ndarray:
        """shape (N_ATTR_STATES, N_OPTIONS) — 남은 시도 n, 리롤 r 에서 각 선택지의 가치."""
        prev = self._values[n - 1]
        q = np.empty((st.N_ATTR_STATES, st.N_OPTIONS), dtype=np.float64)
        for col in range(st.N_OPTIONS):
            nxt = prev[min(r + int(self._gained_col[col]), self.max_rerolls)]
            a = nxt[self._succ_a[:, col]]
            b = nxt[self._succ_b[:, col]]
            mix = self._mix_a[:, col]
            q[:, col] = np.where(mix == 1.0, a, mix * a + (1.0 - mix) * b)
        return q

    def _q_row(self, idx: int, n: int, r: int) -> np.ndarray:
        """shape (N_OPTIONS,) — 상태 하나에 대한 선택지 가치 (조회/시뮬레이션용)."""
        prev = self._values[n - 1]
        a = self._succ_a[idx]
        b = self._succ_b[idx]
        mix = self._mix_a[idx]
        rows = np.minimum(r + self._gained_col, self.max_rerolls)
        return mix * prev[rows, a] + (1.0 - mix) * prev[rows, b]

    @staticmethod
    @lru_cache(maxsize=2)
    def _sample_hands(attempts_left: int) -> np.ndarray:
        """상태별 결정적 비복원 손패 표본. shape=(state, sample, 4)."""
        attempts_left = 1 if attempts_left == 1 else 2
        valid = st.validity(attempts_left)
        out = np.empty((st.N_ATTR_STATES, HAND_SAMPLES, HAND_SIZE), dtype=np.int16)
        mask32 = (1 << 32) - 1
        for s in range(st.N_ATTR_STATES):
            choices = np.flatnonzero(valid[s])
            weights = st.BASE_WEIGHTS[choices]
            for k in range(HAND_SAMPLES):
                remaining = choices.tolist()
                remaining_w = weights.tolist()
                seed = (0x9E3779B9 ^ (s * 0x85EBCA6B) ^ (k * 0xC2B2AE35)
                        ^ (attempts_left * 0x27D4EB2F)) & mask32
                for d in range(HAND_SIZE):
                    seed = (1664525 * seed + 1013904223) & mask32
                    target = (seed / 2**32) * sum(remaining_w)
                    acc = 0.0
                    pick = len(remaining) - 1
                    for j, weight in enumerate(remaining_w):
                        acc += weight
                        if target < acc:
                            pick = j
                            break
                    out[s, k, d] = remaining.pop(pick)
                    remaining_w.pop(pick)
        return out

    def _hand_value(self, q: np.ndarray, reroll_value: np.ndarray | None,
                    complete_value: np.ndarray | None, attempts_left: int) -> np.ndarray:
        hands = self._sample_hands(attempts_left)
        rows = np.arange(st.N_ATTR_STATES)[:, None, None]
        process = q[rows, hands].mean(axis=2)
        if reroll_value is not None:
            process = np.maximum(process, reroll_value[:, None])
        if complete_value is not None:
            process = np.maximum(process, complete_value[:, None])
        return process.mean(axis=1)

    def _solve(self, terminal: np.ndarray) -> np.ndarray:
        n_layers = self.attempts + 1
        n_rerolls = self.max_rerolls + 1
        values = np.empty((n_layers, n_rerolls, st.N_ATTR_STATES), dtype=np.float64)
        values[0, :, :] = terminal[None, :]
        # _q_matrix / _q_row 가 이미 채워진 레이어를 참조하므로 먼저 붙여 둔다.
        self._values = values

        for n in range(1, n_layers):
            # '다른 항목 보기'는 가공을 1회 진행한 뒤부터 쓸 수 있다.
            turn = self.attempts - n
            can_reroll = turn >= rules.REROLL_AVAILABLE_FROM_TURN
            can_complete = turn >= 1
            for r in range(n_rerolls):
                q = self._q_matrix(n, r)
                reroll_value = values[n, r - 1] if (can_reroll and r >= 1) else None
                complete_value = terminal if can_complete else None
                values[n, r] = self._hand_value(
                    q, reroll_value, complete_value, n)
        return values

    def _check(self, gem: st.GemState) -> None:
        gem.validate()
        if gem.attempts_left > self.attempts:
            raise ValueError(
                f"{self.grade} 젬의 가공 시도 횟수는 최대 {self.attempts}회입니다: "
                f"{gem.attempts_left}"
            )
        if gem.rerolls_left > self.max_rerolls:
            raise ValueError(f"다른 항목 보기 횟수가 너무 큽니다: {gem.rerolls_left}")

    # --- 조회 ---------------------------------------------------------------
    def state_value(self, gem: st.GemState) -> float:
        """이 상태에서 최적으로 진행했을 때의 목표 기댓값(지표 목표라면 달성 확률)."""
        self._check(gem)
        return float(self._values[gem.attempts_left, gem.rerolls_left, gem.attr_index()])

    def option_values(self, gem: st.GemState) -> list[OptionValue]:
        """모든 선택지를 가치 내림차순으로 반환한다.

        눈앞에 뜬 4장 중 이 목록에서 가장 위에 있는 것을 고르면 그것이 최적이다.
        """
        self._check(gem)
        if gem.attempts_left == 0:
            raise ValueError("남은 가공 시도 횟수가 0 입니다.")
        idx = gem.attr_index()
        q = self._q_row(idx, gem.attempts_left, gem.rerolls_left)
        probs = st.hand_probabilities(gem.attempts_left)[idx]
        valid = st.validity(gem.attempts_left)[idx]
        rows = [
            OptionValue(oid, st.OPTION_LABELS[oid], float(q[i]), bool(valid[i]), float(probs[i]))
            for i, oid in enumerate(st.OPTION_IDS)
        ]
        rows.sort(key=lambda row: (row.available, row.value), reverse=True)
        return rows

    def reroll_value(self, gem: st.GemState) -> float | None:
        """'다른 항목 보기'를 눌렀을 때의 가치. 쓸 수 없으면 None.

        손패 4장의 최고 가치가 이 값보다 낮으면 리롤하는 것이 이득이다.
        """
        self._check(gem)
        turn = self.attempts - gem.attempts_left
        if gem.rerolls_left < 1 or turn < rules.REROLL_AVAILABLE_FROM_TURN:
            return None
        return float(self._values[gem.attempts_left, gem.rerolls_left - 1, gem.attr_index()])

    def recommend(self, gem: st.GemState, hand: list[str]) -> tuple[HandDecision, list[OptionValue]]:
        """실제 손패에서 가공(25%씩), 리롤, 완료 중 최선의 행동을 고른다."""
        if len(hand) != HAND_SIZE or len(set(hand)) != HAND_SIZE:
            raise ValueError("손패에는 서로 다른 가능성 4개가 필요합니다.")
        ranked = {row.option_id: row for row in self.option_values(gem)}
        unknown = [oid for oid in hand if oid not in ranked]
        if unknown:
            raise ValueError(f"알 수 없는 선택지 id: {', '.join(unknown)}")
        cards = [ranked[oid] for oid in hand]
        if not all(card.available for card in cards):
            raise ValueError("현재 상태에서 등장할 수 없는 가능성이 포함되어 있습니다.")
        process = sum(card.value for card in cards) / HAND_SIZE
        rv = self.reroll_value(gem)
        turn = self.attempts - gem.attempts_left
        complete = float(self.objective.terminal_values()[gem.attr_index()]) if turn >= 1 else None
        candidates = [("process", process)]
        if rv is not None:
            candidates.append(("reroll", rv))
        if complete is not None:
            candidates.append(("complete", complete))
        action = max(candidates, key=lambda item: item[1])[0]
        return HandDecision(action, process, rv, complete), cards


def initial_state(grade: str, eff1_good: bool = True, eff2_good: bool = True) -> st.GemState:
    """가공을 시작하기 직전의 상태 (모든 옵션 Lv.1)."""
    grade = rules.normalize_grade(grade)
    attempts, rerolls = rules.GRADES[grade]
    return st.GemState(
        eff1_good=eff1_good, eff2_good=eff2_good,
        attempts_left=attempts, rerolls_left=rerolls,
    )
