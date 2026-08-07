"""web/gemcraft.html 을 jsdom 으로 실제로 띄워 UI 동작을 확인한다.

엔진이 맞는 값을 내도 UI 가 그 값을 잘못 쓰면 계산기로서는 틀린 것이라,
브라우저와 같은 DOM 위에서 버튼을 눌러 가며 검사한다. 검사 항목은
tests/ui_driver.js 에 있다.

jsdom 이 없으면 skip 한다:  npm install
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "ui_driver.js"
HTML = ROOT / "web" / "gemcraft.html"


def _jsdom_available() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(
        ["node", "-e", "require.resolve('jsdom')"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(
    not _jsdom_available(), reason="node 와 jsdom 이 필요합니다 (npm install)"
)


@pytest.fixture(scope="module")
def ui_result() -> dict:
    out = subprocess.run(
        ["node", str(DRIVER), str(HTML)],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    payload = out.stdout[out.stdout.index("{"):] if "{" in out.stdout else ""
    if not payload:
        raise AssertionError(f"UI 드라이버가 결과를 내지 않았습니다:\n{out.stderr[-3000:]}")
    result = json.loads(payload)
    result["returncode"] = out.returncode
    result["stderr"] = out.stderr
    return result


def test_ui_checks_all_pass(ui_result):
    failed = [c for c in ui_result["checks"] if not c["ok"]]
    detail = "\n".join(f"  - {c['name']}: {c['detail']}" for c in failed)
    assert not failed, f"UI 검사 {len(failed)}건 실패:\n{detail}\n{ui_result['stderr'][-2000:]}"
    assert ui_result["returncode"] == 0


def test_ui_covers_the_whole_flow(ui_result):
    """드라이버가 실수로 조기 종료해도 알아채도록 검사 개수를 고정한다."""
    assert len(ui_result["checks"]) >= 30


def test_browser_solve_is_fast_enough(ui_result):
    """브라우저에서 목표를 바꿀 때마다 다시 푸는데, 체감상 즉시여야 한다."""
    assert ui_result["solveMs"] < 10_000, f"{ui_result['solveMs']}ms"
