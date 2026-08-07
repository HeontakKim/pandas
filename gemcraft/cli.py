"""젬 가공 최적 선택 계산기 CLI."""

from __future__ import annotations

import argparse
import sys

from . import rules, state as st
from .objectives import BUILTIN_HELP, parse_goal
from .simulate import simulate
from .solver import Policy, initial_state

#: 입력 편의를 위한 짧은 별칭
ALIASES = {
    "w": "will", "ㅇ": "will",
    "p": "point", "pt": "point",
    "a": "eff1", "e1": "eff1", "1": "eff1",
    "b": "eff2", "e2": "eff2", "2": "eff2",
    "ca": "chg1", "cb": "chg2",
    "c+": "cost+", "c-": "cost-",
    "k": "keep",
    "rr1": "reroll+1", "rr2": "reroll+2", "rr": "reroll+1",
}


def resolve_option_id(text: str) -> str:
    """``w+2``, ``p-1``, ``ca``, ``point+2`` 같은 입력을 정식 선택지 id 로 바꾼다."""
    token = text.strip().lower().replace(" ", "")
    if token in st.OPTION_INDEX:
        return token
    if token in ALIASES:
        return ALIASES[token]
    for sign in ("+", "-"):
        if sign in token:
            head, _, tail = token.partition(sign)
            base = ALIASES.get(head, head)
            candidate = f"{base}{sign}{tail}"
            if candidate in st.OPTION_INDEX:
                return candidate
    raise ValueError(f"알 수 없는 선택지: {text!r}")


def _fmt(policy: Policy, value: float) -> str:
    return f"{value * 100:6.2f}%" if policy.objective.is_probability else f"{value:8.4f}"


def _state_line(gem: st.GemState) -> str:
    return (
        f"의지력 {gem.will} · 포인트 {gem.point} · "
        f"효과1 {gem.eff1}{'' if gem.eff1_good else '(불필요)'} · "
        f"효과2 {gem.eff2}{'' if gem.eff2_good else '(불필요)'} "
        f"| 총합 {gem.total_level} ({gem.gem_grade}) "
        f"| 남은 시도 {gem.attempts_left} · 리롤 {gem.rerolls_left}"
        + (f" · 비용 {gem.cost_mod * 100:+d}%" if gem.cost_mod else "")
    )


def print_ranking(policy: Policy, gem: st.GemState, hand: list[str] | None = None) -> None:
    print(f"\n  {_state_line(gem)}")
    print(f"  현재 기대치: {_fmt(policy, policy.state_value(gem))}"
          f"   ({policy.objective.description})")

    rows = [row for row in policy.option_values(gem) if row.available]

    print(f"\n  {'ID':<12}{'가능성':<26}{'기준률':>8}{'적용 후':>12}")
    print("  " + "-" * 50)
    for row in rows:
        print(f"  {row.option_id:<12}{row.label:<26}{row.appear_prob * 100:7.2f}%"
              f"{_fmt(policy, row.value):>12}")
    if hand:
        decision, _cards = policy.recommend(gem, hand)
        names = {"process": "가공하기", "reroll": "다른 항목 보기", "complete": "가공 완료"}
        print(f"\n  추천: {names[decision.action]} · 가공 기대치 {_fmt(policy, decision.process_value)}")
        if decision.reroll_value is not None:
            print(f"  다른 항목 보기 가치: {_fmt(policy, decision.reroll_value)}")
        if decision.complete_value is not None:
            print(f"  지금 완료 가치: {_fmt(policy, decision.complete_value)}")
    else:
        print("\n  --hand에 인게임의 서로 다른 가능성 4개 ID를 쉼표로 입력하세요.")


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        raise SystemExit(0)


def run_interactive(policy: Policy, gem: st.GemState) -> None:
    print(f"\n{'=' * 56}")
    print(f"  {policy.grade} 젬 가공  |  목표: {policy.objective.description}")
    print(f"  가공 시도 {policy.attempts}회 · 다른 항목 보기 기본 {policy.base_rerolls}회")
    print(f"{'=' * 56}")
    print("\n입력 예시:  p+2 (포인트 +2)   w-1 (의지력 -1)   ca (첫번째 효과 변경)")
    print("            k (상태 유지)   c+ / c- (비용)   rr1 / rr2 (리롤 획득)")
    print("            r = 다른 항목 보기 사용,  q = 종료\n")

    while gem.attempts_left > 0:
        print_ranking(policy, gem)
        answer = _ask("\n> 표시된 가능성 4개 (쉼표 구분): ").strip().lower()
        if answer in ("q", "quit", "exit"):
            return
        try:
            hand = [resolve_option_id(token) for token in answer.split(",")]
            decision, _cards = policy.recommend(gem, hand)
        except ValueError as exc:
            print(f"  ! {exc}")
            continue
        print(f"  → 추천: {decision.action}")
        if decision.action == "reroll":
            gem = st.GemState(**{**gem.__dict__, "rerolls_left": gem.rerolls_left - 1})
            continue
        if decision.action == "complete":
            gem = st.GemState(**{**gem.__dict__, "attempts_left": 0})
            break
        applied = _ask("  가공 후 실제 적용된 가능성 ID: ")
        try:
            option_id = resolve_option_id(applied)
            if option_id not in hand:
                raise ValueError("입력한 네 가능성 중 하나를 입력해야 합니다.")
        except ValueError as exc:
            print(f"  ! {exc}")
            continue
        gem = apply_option(gem, option_id, policy)

    print(f"\n{'=' * 56}")
    print(f"  가공 완료: {_state_line(gem)}")
    print(f"  최종 등급: {gem.gem_grade} (레벨 총합 {gem.total_level})")
    print(f"{'=' * 56}\n")


def apply_option(gem: st.GemState, option_id: str, policy: Policy | None = None) -> st.GemState:
    """선택지를 적용한 다음 상태. '효과 변경'은 결과를 사용자에게 물어본다."""
    fields = dict(gem.__dict__)
    _id, _label, _weight, kind, param = next(
        row for row in rules.OPTION_TABLE if row[0] == option_id
    )
    attr_field = {rules.WILL: "will", rules.POINT: "point",
                  rules.EFF1: "eff1", rules.EFF2: "eff2"}

    if kind == "delta":
        attr, delta = param
        key = attr_field[attr]
        fields[key] = min(rules.MAX_LEVEL, max(rules.MIN_LEVEL, fields[key] + delta))
    elif kind == "change":
        slot = "eff1_good" if param == rules.EFF1 else "eff2_good"
        reply = _ask("  바뀐 효과가 원하는 효과인가요? [y/N]: ").strip().lower()
        fields[slot] = reply in ("y", "yes", "ㅛ")
    elif kind == "cost":
        fields["cost_mod"] = min(rules.COST_MOD_MAX,
                                 max(rules.COST_MOD_MIN, fields["cost_mod"] + param))
    elif kind == "reroll":
        limit = policy.max_rerolls if policy else fields["rerolls_left"] + param
        fields["rerolls_left"] = min(limit, fields["rerolls_left"] + param)

    fields["attempts_left"] -= 1
    return st.GemState(**fields)


def build_state(args: argparse.Namespace, policy: Policy) -> st.GemState:
    levels = [int(x) for x in args.levels.split(",")]
    if len(levels) != 4:
        raise ValueError("--levels 는 '의지력,포인트,효과1,효과2' 형식이어야 합니다.")
    attempts = args.attempts if args.attempts is not None else policy.attempts
    rerolls = args.rerolls if args.rerolls is not None else policy.base_rerolls
    gem = st.GemState(
        will=levels[0], point=levels[1], eff1=levels[2], eff2=levels[3],
        eff1_good=not args.eff1_bad, eff2_good=not args.eff2_bad,
        cost_mod=args.cost, attempts_left=attempts, rerolls_left=rerolls,
    )
    gem.validate()
    return gem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gemcraft",
        description="로스트아크 아크 그리드 젬 가공 최적 선택 계산기",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=BUILTIN_HELP,
    )
    parser.add_argument("command", choices=["rank", "play", "sim", "goals"],
                        help="rank=현재 상태의 선택지 순위표, play=대화형 진행, "
                             "sim=정책 시뮬레이션, goals=목표 목록")
    parser.add_argument("--grade", default="영웅", help="젬 등급 (고급/희귀/영웅)")
    parser.add_argument("--goal", default="ancient", help="가공 목표 (--help 참고)")
    parser.add_argument("--levels", default="1,1,1,1",
                        help="현재 옵션 레벨 '의지력,포인트,효과1,효과2'")
    parser.add_argument("--attempts", type=int, default=None, help="남은 가공 시도 횟수")
    parser.add_argument("--rerolls", type=int, default=None, help="남은 다른 항목 보기 횟수")
    parser.add_argument("--cost", type=int, default=0, choices=[-1, 0, 1],
                        help="누적 가공 비용 배율 단계 (-1=-100%%, 0=기본, 1=+100%%)")
    parser.add_argument("--eff1-bad", action="store_true", help="첫번째 효과가 원하지 않는 효과")
    parser.add_argument("--eff2-bad", action="store_true", help="두번째 효과가 원하지 않는 효과")
    parser.add_argument("--p-good", type=float, default=1.0,
                        help="해당 젬의 효과 4종 중 원하는 효과의 비율 (예: 2종이면 0.5)")
    parser.add_argument("--hand", default=None,
                        help="현재 표시된 서로 다른 가능성 4개 ID (쉼표 구분)")
    parser.add_argument("--bad-effect-scale", type=float, default=0.0,
                        help="weighted 목표에서 원하지 않는 효과 슬롯에 곱할 배율")
    parser.add_argument("--runs", type=int, default=20_000, help="sim 반복 횟수")
    parser.add_argument("--seed", type=int, default=0, help="sim 난수 시드")
    args = parser.parse_args(argv)

    if args.command == "goals":
        print(BUILTIN_HELP)
        return 0

    try:
        objective = parse_goal(args.goal, args.bad_effect_scale)
        policy = Policy(args.grade, objective, args.p_good)
        gem = build_state(args, policy)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2

    if args.command == "rank":
        if gem.attempts_left == 0:
            print(f"  {_state_line(gem)}\n  가공이 끝난 상태입니다.")
            return 0
        hand = [resolve_option_id(x) for x in args.hand.split(",")] if args.hand else None
        print_ranking(policy, gem, hand)
        print()
    elif args.command == "play":
        run_interactive(policy, gem)
    elif args.command == "sim":
        print(f"\n{policy.grade} 젬 · 목표: {policy.objective.description}")
        print(simulate(policy, runs=args.runs, seed=args.seed, start=gem).report())
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
