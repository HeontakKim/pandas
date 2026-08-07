"""web/gemcraft.html 의 JS 엔진이 Python 엔진과 같은 값을 내는지 검증한다.

브라우저용 HTML 은 Python 패키지를 손으로 옮긴 것이라 방치하면 반드시 갈라진다.
이 테스트는 HTML 에서 엔진 블록만 떼어내 node 로 실행하고, 같은 상태·같은 목표에
대해 Python 이 낸 값과 일치하는지 본다. 규칙 테이블도 통째로 비교한다.

node 가 없으면 skip 한다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gemcraft import rules  # noqa: E402
from gemcraft.objectives import parse_goal  # noqa: E402
from gemcraft.solver import Policy  # noqa: E402
from gemcraft.state import GemState  # noqa: E402

HTML = ROOT / "web" / "gemcraft.html"
START = "// ==GEMCRAFT-ENGINE-START=="
END = "// ==GEMCRAFT-ENGINE-END=="

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 가 필요합니다")


def extract_engine() -> str:
    text = HTML.read_text(encoding="utf-8")
    assert START in text and END in text, "엔진 블록 마커를 찾을 수 없습니다."
    return text.split(START, 1)[1].split(END, 1)[0]


@pytest.fixture(scope="module")
def engine_path(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("web") / "engine.js"
    path.write_text(extract_engine(), encoding="utf-8")
    return path


def run_node(engine: Path, script: str, payload: dict) -> dict:
    driver = engine.parent / "driver.js"
    driver.write_text(
        f'const E = require({str(engine)!r});\n'
        f'const job = JSON.parse(process.argv[2]);\n'
        + textwrap.dedent(script),
        encoding="utf-8",
    )
    out = subprocess.run(
        ["node", str(driver), json.dumps(payload)],
        capture_output=True, text=True, timeout=300,
    )
    if out.returncode != 0:
        raise AssertionError(f"node 실행 실패:\n{out.stderr}")
    return json.loads(out.stdout)


# --- 규칙 테이블 자체가 같은가 ------------------------------------------------------
def test_rules_table_matches_python(engine_path):
    js = run_node(engine_path, "console.log(JSON.stringify(E.RULES));", {})

    assert js["handSize"] == rules.HAND_SIZE
    assert js["minLevel"] == rules.MIN_LEVEL and js["maxLevel"] == rules.MAX_LEVEL
    assert js["costMin"] == rules.COST_MOD_MIN and js["costMax"] == rules.COST_MOD_MAX
    assert js["baseCostGold"] == rules.BASE_COST_GOLD
    assert js["rerollAvailableFromTurn"] == rules.REROLL_AVAILABLE_FROM_TURN
    assert js["attrNames"] == [rules.ATTR_NAMES[a] for a in range(4)]
    assert js["grades"] == {g: list(v) for g, v in rules.GRADES.items()}
    assert js["gradeThresholds"] == [list(t) for t in rules.GEM_GRADE_THRESHOLDS]

    expected = [
        [oid, label, weight, kind,
         (list(param) if isinstance(param, tuple) else param)]
        for oid, label, weight, kind, param in rules.OPTION_TABLE
    ]
    assert js["options"] == expected


def test_js_option_weights_sum_to_100(engine_path):
    js = run_node(engine_path, "console.log(JSON.stringify(E.RULES));", {})
    assert sum(row[2] for row in js["options"]) == pytest.approx(100.0, abs=1e-9)


# --- 값 함수가 같은가 -------------------------------------------------------------
CASES = [
    # (등급, 목표, p_good, 시도횟수 override)
    ("영웅", "ancient", 1.0, 2),
    ("영웅", "target=4,4,3,0+good+total17", 0.5, 3),
    ("희귀", "weighted=1,1,0.6,0.6", 0.5, 2),
]

GEMS = [
    (1, 1, 1, 1, True, True, 0),
    (4, 3, 2, 1, True, True, 0),
    (5, 3, 2, 1, True, False, 1),
    (4, 4, 2, 1, False, True, -1),
    (5, 5, 4, 4, True, True, 0),
    (2, 3, 5, 1, False, False, 0),
]

JS_VALUES = """
const objective = E.parseGoal(job.goal);
const policy = new E.Policy(job.grade, objective, job.pGood, job.attempts);
policy.solve();
const out = { values: [], options: null, reroll: null };
for (const g of job.gems) {
  const gem = { will: g[0], point: g[1], eff1: g[2], eff2: g[3],
                eff1Good: g[4], eff2Good: g[5], cost: g[6],
                attemptsLeft: job.attemptsLeft, rerollsLeft: job.rerollsLeft };
  out.values.push(policy.stateValue(gem));
}
const probe = job.gems[1];
const gem = { will: probe[0], point: probe[1], eff1: probe[2], eff2: probe[3],
              eff1Good: probe[4], eff2Good: probe[5], cost: probe[6],
              attemptsLeft: job.attemptsLeft, rerollsLeft: job.rerollsLeft };
out.options = policy.optionValues(gem).map(r => [r.id, r.value, r.available, r.appearProb]);
out.reroll = policy.rerollValue(gem);
console.log(JSON.stringify(out));
"""


@pytest.mark.parametrize("grade,goal,p_good,attempts", CASES)
def test_state_values_match(engine_path, grade, goal, p_good, attempts):
    policy = Policy(grade, parse_goal(goal), p_good=p_good, attempts=attempts)
    attempts_left = min(3, policy.attempts)
    rerolls_left = min(1, policy.max_rerolls)

    js = run_node(engine_path, JS_VALUES, {
        "grade": grade, "goal": goal, "pGood": p_good, "attempts": attempts,
        "gems": [list(g) for g in GEMS],
        "attemptsLeft": attempts_left, "rerollsLeft": rerolls_left,
    })

    for gem_args, js_value in zip(GEMS, js["values"]):
        gem = GemState(*gem_args, attempts_left=attempts_left, rerolls_left=rerolls_left)
        assert policy.state_value(gem) == pytest.approx(js_value, rel=1e-9, abs=1e-12)

    probe = GemState(*GEMS[1], attempts_left=attempts_left, rerolls_left=rerolls_left)
    py_options = {row.option_id: row for row in policy.option_values(probe)}
    assert len(js["options"]) == len(py_options)
    for oid, value, available, appear in js["options"]:
        row = py_options[oid]
        assert row.available == available
        assert row.appear_prob == pytest.approx(appear, rel=1e-12)
        if available:
            assert row.value == pytest.approx(value, rel=1e-9, abs=1e-12)

    py_reroll = policy.reroll_value(probe)
    if py_reroll is None:
        assert js["reroll"] is None
    else:
        assert py_reroll == pytest.approx(js["reroll"], rel=1e-9, abs=1e-12)


def test_js_hand_decision_matches_python(engine_path):
    """실제 네 가능성에 대한 행동 추천이 같아야 한다."""
    grade, goal = "영웅", "target=4,4,0,0+total16"
    policy = Policy(grade, parse_goal(goal), attempts=3)
    gem_args = (4, 3, 2, 1, True, True, 0)
    gem = GemState(*gem_args, attempts_left=3, rerolls_left=1)
    hand = ["will+1", "point+2", "keep", "eff1+1"]
    py_decision, _ = policy.recommend(gem, hand)
    script = JS_VALUES.replace(
        "console.log(JSON.stringify(out));",
        f"out.decision = policy.recommendHand(gem, {json.dumps(hand)}); console.log(JSON.stringify(out));",
    )
    js = run_node(engine_path, script, {
        "grade": grade, "goal": goal, "pGood": 1.0, "attempts": 3,
        "gems": [list(gem_args), list(gem_args)], "attemptsLeft": 3, "rerollsLeft": 1,
    })
    assert js["decision"]["action"] == py_decision.action
    assert js["decision"]["processValue"] == pytest.approx(py_decision.process_value)
