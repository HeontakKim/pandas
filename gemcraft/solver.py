"""가공 최적 선택 DP.

문제의 구조
-----------
가공은 유한 지평 마르코프 결정 과정이다.

* 상태: (젬 속성 상태, 남은 가공 시도 횟수 ``n``, 남은 다른 항목 보기 횟수 ``r``)
* 매 턴 4개의 선택지가 확률적으로 제시된다.
* 행동: 4개 중 하나를 고르거나(시도 1회 소모), '다른 항목 보기'를 쓴다
  (시도는 소모하지 않고 ``r`` 만 1 줄어든 뒤 4개를 다시 뽑는다).

따라서 값 함수는 아래 두 식으로 정확히 계산된다.

    Q(s, n, r, 선택지) = V(s', n-1, r')                     # 선택지 적용 후
    V(s, n, r)         = E_hand[ max( max_{o∈hand} Q(s,n,r,o),
                                      V(s, n, r-1) ) ]      # 리롤 가능할 때
    V(s, n, 0)         = E_hand[ max_{o∈hand} Q(s,n,0,o) ]
    V(s, 0, r)         = objective(s)

``V(s,n,r-1)`` 은 같은 레이어의 더 낮은 ``r`` 이므로 ``r`` 오름차순으로 풀면
고정점 반복 없이 한 번에 계산된다. 리롤을 여러 번 연속으로 쓰는 것도
이 재귀에 자연히 포함된다.

선택지 추출 모델
----------------
공식 확률표는 선택지 하나하나에 확률을 부여하고 합이 정확히 100% 다.
이를 **4개 슬롯이 각각 독립적으로 이 분포에서 뽑힌다**(중복 등장 가능)고
해석한다. 만약 실제로는 중복 없이 4개를 뽑는다면 값 함수가 미세하게
달라지지만, 눈앞의 4장 중 무엇을 고를지는 ``Q`` 의 대소 관계로 결정되고
그 순서는 이 가정에 거의 영향을 받지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import rules, state as st
from .objectives import Objective
from .rules import HAND_SIZE


@dataclass(frozen=True)
class OptionValue:
    option_id: str
    label: str
    value: float
    available: bool
    appear_prob: float


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

        self._probs = st.hand_probabilities()
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

    def _hand_value(self, q: np.ndarray, reroll_value: np.ndarray | None) -> np.ndarray:
        """E[ max(손패 최고가치, 리롤 가치) ] — 4장을 독립 추출한다고 가정."""
        order = np.argsort(-q, axis=1, kind="stable")
        q_sorted = np.take_along_axis(q, order, axis=1)
        p_sorted = np.take_along_axis(self._probs, order, axis=1)

        # 손패 최고가치가 정확히 i번째 선택지일 확률
        cum = np.cumsum(p_sorted, axis=1)
        tail_before = np.clip(1.0 - (cum - p_sorted), 0.0, 1.0)
        tail_after = np.clip(1.0 - cum, 0.0, 1.0)
        # 부동소수점 오차로 아주 작은 음수 가중치가 생길 수 있어 0으로 눌러 준다.
        weight = np.maximum(tail_before ** HAND_SIZE - tail_after ** HAND_SIZE, 0.0)

        best = q_sorted if reroll_value is None else np.maximum(q_sorted, reroll_value[:, None])
        return (best * weight).sum(axis=1)

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
            for r in range(n_rerolls):
                q = self._q_matrix(n, r)
                reroll_value = values[n, r - 1] if (can_reroll and r >= 1) else None
                values[n, r] = self._hand_value(q, reroll_value)
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
        probs = self._probs[idx]
        valid = st.VALID[idx]
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

    def recommend(self, gem: st.GemState, hand: list[str]) -> tuple[str, list[OptionValue]]:
        """제시된 4장 중 최선의 선택을 고른다.

        반환값은 ``("리롤" 또는 선택지 id, 손패 각 장의 가치)``.
        """
        ranked = {row.option_id: row for row in self.option_values(gem)}
        unknown = [oid for oid in hand if oid not in ranked]
        if unknown:
            raise ValueError(f"알 수 없는 선택지 id: {', '.join(unknown)}")
        cards = [ranked[oid] for oid in hand]
        best = max(cards, key=lambda row: row.value)
        rv = self.reroll_value(gem)
        if rv is not None and rv > best.value:
            return "reroll", cards
        return best.option_id, cards


def initial_state(grade: str, eff1_good: bool = True, eff2_good: bool = True) -> st.GemState:
    """가공을 시작하기 직전의 상태 (모든 옵션 Lv.1)."""
    grade = rules.normalize_grade(grade)
    attempts, rerolls = rules.GRADES[grade]
    return st.GemState(
        eff1_good=eff1_good, eff2_good=eff2_good,
        attempts_left=attempts, rerolls_left=rerolls,
    )
