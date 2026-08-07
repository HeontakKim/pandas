/**
 * jsdom 으로 web/gemcraft.html 을 실제로 띄워 UI 를 클릭해 보는 end-to-end 검사.
 *
 * tests/test_web_ui.py 가 이 스크립트를 실행한다. 결과는 stdout 에 JSON 으로
 * 나가고, 실패하면 0 이 아닌 종료 코드를 반환한다.
 *
 *   node tests/ui_driver.js <html 경로>
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const htmlPath = process.argv[2] || path.join(__dirname, "..", "web", "gemcraft.html");
const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok: !!ok, detail: detail === undefined ? null : String(detail) });
  if (!ok) throw new Error(`검사 실패: ${name} — ${detail}`);
}

const dom = new JSDOM(fs.readFileSync(htmlPath, "utf8"), {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "file:///gemcraft.html",
});
const { window } = dom;
const doc = window.document;
const $ = (id) => doc.getElementById(id);
const visible = (id) => !$(id).classList.contains("hide");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, label, timeoutMs = 60000) {
  const t0 = Date.now();
  while (!fn()) {
    if (Date.now() - t0 > timeoutMs) throw new Error(`대기 시간 초과: ${label}`);
    await sleep(10);
  }
}

function stateCells() {
  const out = {};
  for (const cell of $("stateGrid").querySelectorAll(".cell")) {
    out[cell.querySelector(".k").textContent] = cell.querySelector(".v").textContent;
  }
  return out;
}

function optionRows() {
  return [...$("optList").querySelectorAll("button[data-opt]")].map((b) => ({
    id: b.dataset.opt,
    label: b.querySelector("span:nth-child(2)").textContent,
    value: b.querySelector(".val").textContent,
  }));
}

(async () => {
  // 스크립트가 다 돌 때까지
  await waitFor(() => $("grade").children.length > 0, "초기 렌더");
  check("등급 버튼 3개", $("grade").children.length === 3);
  check("세션 패널은 처음엔 숨김", !visible("session"));

  // --- 희귀 젬 / 의지력·포인트 4 이상 목표로 계산 시작 ---
  [...$("grade").children].find((b) => b.dataset.grade === "희귀").click();
  check("등급 선택 반영", $("grade").children[1].getAttribute("aria-pressed") === "true");
  $("goal").value = "target=4,4,0,0";
  $("goal").dispatchEvent(new window.Event("change"));

  const t0 = Date.now();
  $("start").click();
  await waitFor(() => visible("session"), "DP 계산 완료");
  const solveMs = Date.now() - t0;
  check("계산 시간이 10초 미만", solveMs < 10000, `${solveMs}ms`);
  check("진행 바는 다시 숨김", !visible("progress"));
  check("시작 버튼 다시 활성", !$("start").disabled);

  // --- 초기 상태 표시 ---
  let cells = stateCells();
  check("초기 레벨 총합 4", cells["레벨 총합"].startsWith("4 "), cells["레벨 총합"]);
  check("희귀 젬 시도 7회", cells["남은 시도"] === "7", cells["남은 시도"]);
  check("희귀 젬 리롤 1회", cells["남은 리롤"] === "1", cells["남은 리롤"]);
  check("기대치가 퍼센트로 표시", /^\d+\.\d\d%$/.test($("value").textContent), $("value").textContent);
  check("첫 턴엔 리롤 불가 안내", $("rerollBox").textContent.includes("1회 진행한 뒤"),
        $("rerollBox").textContent);

  // --- 순위표가 가치 내림차순인가 ---
  const rows = optionRows();
  check("선택지가 표시됨", rows.length > 10, rows.length);
  const values = rows.map((r) => parseFloat(r.value));
  check("가치 내림차순 정렬", values.every((v, i) => i === 0 || values[i - 1] >= v - 1e-9));
  check("1순위에 강조 표시",
        $("optList").querySelector("button[data-opt]").classList.contains("top"));

  // --- 1순위를 골랐을 때, 표시된 값이 실제 다음 상태의 값과 같은가 ---
  // (UI 에 뜬 '선택 후' 수치가 진짜 그 상태의 값인지 확인하는 핵심 검사)
  const topRow = rows[0];
  const shown = parseFloat(topRow.value);
  $("optList").querySelector(`button[data-opt="${topRow.id}"]`).click();
  await sleep(0);
  const afterValue = parseFloat($("value").textContent);
  check("선택 후 표시값 = 실제 다음 상태 값",
        Math.abs(shown - afterValue) < 1e-6, `${shown} vs ${afterValue}`);
  cells = stateCells();
  check("시도 횟수 1 감소", cells["남은 시도"] === "6", cells["남은 시도"]);

  // --- 이제 리롤이 열려야 한다 ---
  check("2턴부터 리롤 버튼 등장", $("rerollBox").querySelector("#doReroll") !== null);
  const beforeReroll = stateCells()["남은 리롤"];
  $("rerollBox").querySelector("#doReroll").click();
  await sleep(0);
  check("리롤 사용 시 리롤만 1 감소",
        stateCells()["남은 리롤"] === String(Number(beforeReroll) - 1)
        && stateCells()["남은 시도"] === "6", JSON.stringify(stateCells()));

  // --- 되돌리기 ---
  $("undo").click();
  await sleep(0);
  check("되돌리기로 리롤 복구", stateCells()["남은 리롤"] === beforeReroll);

  // --- 효과 변경은 결과를 되묻는다 ---
  // 렌더할 때마다 목록이 새로 그려지므로 버튼은 매번 다시 찾아야 한다.
  const changeBtn = () => $("optList").querySelector('button[data-opt="chg1"]');
  check("효과 변경 선택지 존재", changeBtn() !== null);
  changeBtn().click();
  await sleep(0);
  check("효과 변경 시 확인 상자 표시", visible("askBox"));
  check("확인 중에는 선택지 감춤", $("optList").children.length === 0);
  $("askBox").querySelector('button[data-good="cancel"]').click();
  await sleep(0);
  check("취소하면 원복", !visible("askBox") && $("optList").children.length > 0);
  check("숨긴 확인 상자는 비워 둔다", $("askBox").children.length === 0);

  changeBtn().click();
  await sleep(0);
  $("askBox").querySelector('button[data-good="0"]').click();
  await sleep(0);
  check("'아니오' 선택 시 효과1에 ✗ 표시",
        stateCells()["첫번째 효과"].includes("✗"), stateCells()["첫번째 효과"]);

  // --- 끝까지 진행 ---
  let guard = 0;
  while (visible("choices") && guard++ < 30) {
    const btn = $("optList").querySelector("button[data-opt]");
    if (!btn) break;
    btn.click();
    await sleep(0);
    if (visible("askBox")) {
      $("askBox").querySelector('button[data-good="1"]').click();
      await sleep(0);
    }
  }
  check("가공 완료 패널 표시", visible("finished"));
  check("선택지 패널은 숨김", !visible("choices"));
  const finishedText = $("finished").textContent;
  check("최종 등급 표기", /(전설|유물|고대) 젬/.test(finishedText), finishedText);
  check("남은 시도 0", stateCells()["남은 시도"] === "0");

  // --- 처음부터 ---
  $("restart").click();
  await sleep(0);
  check("재시작 시 시도 횟수 복구", stateCells()["남은 시도"] === "7");
  check("재시작 시 레벨 초기화", stateCells()["레벨 총합"].startsWith("4 "));

  // --- 잘못된 입력은 막는다 ---
  $("manual").click();
  $("curAttempts").value = "99";
  $("start").click();
  await sleep(50);
  check("범위를 벗어난 입력은 경고", $("setupHint").textContent.startsWith("⚠"),
        $("setupHint").textContent);

  console.log(JSON.stringify({ solveMs, checks }, null, 1));
  window.close();
})().catch((err) => {
  console.error(err.stack || String(err));
  console.log(JSON.stringify({ checks }, null, 1));
  process.exit(1);
});
