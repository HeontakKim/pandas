"""gemcraft 검증 테스트.

핵심은 ``test_bruteforce_matches_dp`` 다. numpy DP 와 전이 테이블을 전혀
쓰지 않고, 공식 확률표(rules.OPTION_TABLE)만 보고 순수 파이썬으로 값 함수를
다시 계산해 비교한다. 인코딩·유효성·전이·손패 기댓값 어디에 버그가 있어도
이 테스트가 잡아낸다.
"""

from __future__ import annotations

import itertools
import math
import sys
from functools import lru_cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gemcraft import rules, state as st  # noqa: E402
from gemcraft.objectives import parse_goal  # noqa: E402
from gemcraft.simulate import simulate  # noqa: E402
from gemcraft.solver import Policy, initial_state  # noqa: E402


# --- 규칙 테이블 자체 -----------------------------------------------------------
def test_probability_table_sums_to_100():
    total = sum(row[2] for row in rules.OPTION_TABLE)
    assert total == pytest.approx(100.0, abs=1e-9)


def test_every_option_id_is_unique():
    ids = [row[0] for row in rules.OPTION_TABLE]
    assert len(ids) == len(set(ids))


def test_increase_options_never_exceed_max_level():
    """'미등장 조건'대로라면 증가 옵션의 결과는 항상 Lv.5 이하여야 한다."""
    for _oid, _label, _w, kind, param in rules.OPTION_TABLE:
        if kind != "delta":
            continue
        attr, delta = param
        for level in range(rules.MIN_LEVEL, rules.MAX_LEVEL + 1):
            idx = st.encode(*[level if a == attr else 1 for a in range(4)], True, True, 0)
            if st.VALID[idx, st.OPTION_INDEX[_oid]]:
                assert rules.MIN_LEVEL <= level + delta <= rules.MAX_LEVEL


def test_encode_decode_roundtrip():
    for args in itertools.product(range(1, 6), range(1, 6), range(1, 6), range(1, 6),
                                  [False, True], [False, True], [-1, 0, 1]):
        assert st.decode(st.encode(*args)) == args


def test_hand_probabilities_normalized():
    probs = st.hand_probabilities()
    assert probs.sum(axis=1) == pytest.approx(1.0)
    assert (probs[~st.VALID] == 0).all()


# --- 독립 구현으로 DP 검증 --------------------------------------------------------
Gem = tuple  # (will, point, eff1, eff2, good1, good2, cost)


def _brute_policy(objective_name: str, attempts: int, base_rerolls: int, p_good: float):
    """순수 파이썬 재귀로 값 함수를 다시 계산한다 (전이 테이블 미사용)."""
    objective = parse_goal(objective_name)
    field = {rules.WILL: 0, rules.POINT: 1, rules.EFF1: 2, rules.EFF2: 3}

    def terminal(gem: Gem) -> float:
        import numpy as np
        levels = np.array(gem[:4]).reshape(4, 1)
        good = np.array(gem[4:6]).reshape(2, 1)
        return float(objective.score_fn(levels, good)[0])

    def available(gem: Gem) -> list[tuple[str, float]]:
        out = []
        for oid, _label, weight, kind, param in rules.OPTION_TABLE:
            if kind == "delta":
                attr, delta = param
                level = gem[field[attr]]
                if delta > 0 and level + delta > rules.MAX_LEVEL:
                    continue
                if delta < 0 and level + delta < rules.MIN_LEVEL:
                    continue
            elif kind == "cost":
                if gem[6] + param not in (-1, 0, 1):
                    continue
            out.append((oid, weight))
        total = sum(w for _, w in out)
        return [(oid, w / total) for oid, w in out]

    @lru_cache(maxsize=None)
    def value(gem: Gem, n: int, r: int) -> float:
        if n == 0:
            return terminal(gem)
        options = available(gem)

        def after(oid: str) -> float:
            _i, _l, _w, kind, param = next(x for x in rules.OPTION_TABLE if x[0] == oid)
            if kind == "delta":
                attr, delta = param
                nxt = list(gem)
                nxt[field[attr]] += delta
                return value(tuple(nxt), n - 1, r)
            if kind == "change":
                slot = 4 if param == rules.EFF1 else 5
                good, bad = list(gem), list(gem)
                good[slot], bad[slot] = True, False
                return (p_good * value(tuple(good), n - 1, r)
                        + (1 - p_good) * value(tuple(bad), n - 1, r))
            if kind == "cost":
                nxt = list(gem)
                nxt[6] += param
                return value(tuple(nxt), n - 1, r)
            if kind == "reroll":
                return value(gem, n - 1, min(r + param, base_rerolls + 2 * attempts))
            return value(gem, n - 1, r)  # keep

        q = {oid: after(oid) for oid, _ in options}
        turn = attempts - n
        floor = (value(gem, n, r - 1)
                 if r >= 1 and turn >= rules.REROLL_AVAILABLE_FROM_TURN else -math.inf)

        # 손패 4장을 독립 추출: 최고가치의 분포를 순위별로 누적확률로 계산
        ranked = sorted(options, key=lambda item: q[item[0]], reverse=True)
        acc = 0.0
        expected = 0.0
        for oid, prob in ranked:
            before, acc = acc, acc + prob
            weight = (1 - before) ** rules.HAND_SIZE - (1 - acc) ** rules.HAND_SIZE
            expected += max(q[oid], floor) * weight
        return expected

    return value


@pytest.mark.parametrize("goal,attempts,rerolls", [
    ("total", 1, 0),
    ("total", 2, 1),
    ("ancient", 3, 1),
    ("target=4,4,0,0", 3, 2),
])
def test_bruteforce_matches_dp(goal, attempts, rerolls):
    # 브루트포스는 자신의 attempts 를 기준으로 리롤 가능 턴을 판단하므로
    # 정책도 같은 시도 횟수에서 시작하도록 맞춘다.
    policy = Policy("영웅", parse_goal(goal), p_good=1.0, attempts=attempts)
    brute = _brute_policy(goal, attempts, rerolls, p_good=1.0)

    for levels in [(1, 1, 1, 1), (4, 3, 2, 1), (5, 3, 2, 1), (4, 4, 2, 1), (5, 5, 4, 4)]:
        gem = st.GemState(*levels, True, True, 0, attempts, rerolls)
        assert policy.state_value(gem) == pytest.approx(
            brute((*levels, True, True, 0), attempts, rerolls), rel=1e-9
        )


def test_bruteforce_matches_dp_with_effect_change():
    goal, attempts, rerolls, p_good = "weighted=1,1,1,1", 2, 1, 0.375
    objective = parse_goal(goal, bad_effect_scale=0.0)
    policy = Policy("영웅", objective, p_good=p_good, attempts=attempts)

    brute_objective = parse_goal(goal, bad_effect_scale=0.0)

    def terminal(gem):
        import numpy as np
        return float(brute_objective.score_fn(
            np.array(gem[:4]).reshape(4, 1), np.array(gem[4:6]).reshape(2, 1))[0])

    for levels, goods in [((3, 3, 2, 2), (True, False)), ((2, 2, 1, 1), (False, False))]:
        gem = st.GemState(*levels, *goods, 0, attempts, rerolls)
        brute = _brute_policy_weighted(terminal, attempts, rerolls, p_good)
        assert policy.state_value(gem) == pytest.approx(
            brute((*levels, *goods, 0), attempts, rerolls), rel=1e-9)


def _brute_policy_weighted(terminal_fn, attempts, base_rerolls, p_good):
    field = {rules.WILL: 0, rules.POINT: 1, rules.EFF1: 2, rules.EFF2: 3}

    @lru_cache(maxsize=None)
    def value(gem: Gem, n: int, r: int) -> float:
        if n == 0:
            return terminal_fn(gem)
        options = []
        for oid, _label, weight, kind, param in rules.OPTION_TABLE:
            if kind == "delta":
                attr, delta = param
                level = gem[field[attr]]
                if not rules.MIN_LEVEL <= level + delta <= rules.MAX_LEVEL:
                    continue
            elif kind == "cost" and gem[6] + param not in (-1, 0, 1):
                continue
            options.append((oid, weight, kind, param))
        total = sum(o[1] for o in options)

        q = {}
        for oid, weight, kind, param in options:
            if kind == "delta":
                attr, delta = param
                nxt = list(gem)
                nxt[field[attr]] += delta
                q[oid] = value(tuple(nxt), n - 1, r)
            elif kind == "change":
                slot = 4 if param == rules.EFF1 else 5
                good, bad = list(gem), list(gem)
                good[slot], bad[slot] = True, False
                q[oid] = (p_good * value(tuple(good), n - 1, r)
                          + (1 - p_good) * value(tuple(bad), n - 1, r))
            elif kind == "cost":
                nxt = list(gem)
                nxt[6] += param
                q[oid] = value(tuple(nxt), n - 1, r)
            elif kind == "reroll":
                q[oid] = value(gem, n - 1, min(r + param, base_rerolls + 2 * attempts))
            else:
                q[oid] = value(gem, n - 1, r)

        turn = attempts - n
        floor = (value(gem, n, r - 1)
                 if r >= 1 and turn >= rules.REROLL_AVAILABLE_FROM_TURN else -math.inf)
        acc, expected = 0.0, 0.0
        for oid, weight, _k, _p in sorted(options, key=lambda o: q[o[0]], reverse=True):
            before, acc = acc, acc + weight / total
            expected += max(q[oid], floor) * (
                (1 - before) ** rules.HAND_SIZE - (1 - acc) ** rules.HAND_SIZE)
        return expected

    return value


# --- 정책의 성질 ---------------------------------------------------------------
@pytest.fixture(scope="module")
def epic_ancient() -> Policy:
    return Policy("영웅", parse_goal("ancient"))


def test_value_increases_with_attempts_and_rerolls(epic_ancient):
    base = dict(will=2, point=2, eff1=2, eff2=2)
    values = [epic_ancient.state_value(st.GemState(**base, attempts_left=n, rerolls_left=1))
              for n in range(0, 6)]
    assert values == sorted(values)

    with_rerolls = [
        epic_ancient.state_value(st.GemState(**base, attempts_left=5, rerolls_left=r))
        for r in range(0, 4)
    ]
    assert with_rerolls == sorted(with_rerolls)


def test_will_and_point_are_symmetric(epic_ancient):
    """'ancient' 는 네 옵션에 대칭이므로 레벨을 맞바꿔도 값이 같아야 한다."""
    a = epic_ancient.state_value(st.GemState(5, 3, 2, 1, attempts_left=4, rerolls_left=1))
    b = epic_ancient.state_value(st.GemState(3, 5, 2, 1, attempts_left=4, rerolls_left=1))
    c = epic_ancient.state_value(st.GemState(2, 1, 5, 3, attempts_left=4, rerolls_left=1))
    assert a == pytest.approx(b)
    assert a == pytest.approx(c)


def test_probability_objective_stays_in_unit_interval(epic_ancient):
    # 확률 목표이므로 값은 [0, 1] 안에 있어야 한다 (부동소수점 오차만 허용).
    assert epic_ancient._values.min() >= -1e-9
    assert epic_ancient._values.max() <= 1.0 + 1e-9


def test_maxed_gem_is_certain(epic_ancient):
    gem = st.GemState(5, 5, 5, 5, attempts_left=0, rerolls_left=0)
    assert epic_ancient.state_value(gem) == pytest.approx(1.0)


def test_simulation_matches_dp():
    policy = Policy("희귀", parse_goal("ancient"))
    result = simulate(policy, runs=30_000, seed=7)
    assert result.goal_rate == pytest.approx(result.predicted, abs=0.01)


def test_recommend_picks_best_of_hand(epic_ancient):
    gem = initial_state("영웅")
    hand = ["will+1", "point+3", "keep", "eff1-1"]
    choice, cards = epic_ancient.recommend(gem, hand)
    assert choice == "point+3"
    assert len(cards) == 4


def test_unavailable_options_are_flagged(epic_ancient):
    gem = st.GemState(5, 1, 1, 1, attempts_left=3, rerolls_left=0)
    rows = {row.option_id: row for row in epic_ancient.option_values(gem)}
    assert not rows["will+1"].available   # 의지력 5 → 증가 옵션 미등장
    assert rows["will-1"].available       # 감소는 등장 가능
    assert not rows["point-1"].available  # 포인트 1 → 감소 미등장


def test_grade_thresholds():
    assert rules.gem_grade(4) == "전설"
    assert rules.gem_grade(15) == "전설"
    assert rules.gem_grade(16) == "유물"
    assert rules.gem_grade(18) == "유물"
    assert rules.gem_grade(19) == "고대"
    assert rules.gem_grade(20) == "고대"
