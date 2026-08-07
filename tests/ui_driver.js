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
  runScripts: "dangerously", pretendToBeVisual: true, url: "file:///gemcraft.html",
});
const { window } = dom, doc = window.document;
const $ = (id) => doc.getElementById(id);
const visible = (id) => !$(id).classList.contains("hide");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, label, timeout = 60000) {
  const start = Date.now();
  while (!fn()) { if (Date.now() - start > timeout) throw new Error(`대기 초과: ${label}`); await sleep(10); }
}
function stateCells() {
  const out = {};
  for (const cell of $("stateGrid").querySelectorAll(".cell")) {
    out[cell.querySelector(".k").textContent] = cell.querySelector(".v").textContent;
  }
  return out;
}
function optionButtons() { return [...$("optList").querySelectorAll("button[data-opt]")]; }
function chooseFour() {
  for (let i = 0; i < 4; i++) optionButtons().find((b) => !b.classList.contains("top")).click();
}

(async () => {
  await waitFor(() => $("grade").children.length === 3, "초기 렌더");
  check("등급 버튼 3개", $("grade").children.length === 3);
  check("세션은 처음에 숨김", !visible("session"));
  [...$("grade").children].find((b) => b.dataset.grade === "희귀").click();
  check("희귀 선택 반영", $("grade").children[1].getAttribute("aria-pressed") === "true");
  $("goal").value = "target=4,4,0,0"; $("goal").dispatchEvent(new window.Event("change"));
  const t0 = window.performance.now(); $("start").click();
  await waitFor(() => visible("session"), "계산 완료");
  const solveMs = window.performance.now() - t0;
  check("10초 안에 계산", solveMs < 10000, solveMs);
  check("진행 바 숨김", !visible("progress"));
  check("시작 버튼 활성", !$("start").disabled);
  let cells = stateCells();
  check("초기 총합 4", cells["레벨 총합"].startsWith("4 "), cells["레벨 총합"]);
  check("희귀 시도 7", cells["남은 시도"] === "7", cells["남은 시도"]);
  check("희귀 리롤 1", cells["남은 리롤"] === "1", cells["남은 리롤"]);
  check("기대치 퍼센트", /^\d+\.\d\d%$/.test($("value").textContent), $("value").textContent);
  check("가능성 목록 표시", optionButtons().length > 10, optionButtons().length);
  check("초기 선택 0/4", $("rerollBox").textContent.includes("0/4"), $("rerollBox").textContent);

  chooseFour();
  check("서로 다른 4개 선택", optionButtons().filter((b) => b.classList.contains("top")).length === 4);
  check("추천 표시", $("rerollBox").textContent.includes("추천"), $("rerollBox").textContent);
  check("첫 턴 리롤 버튼 없음", $("doReroll") === null);
  check("첫 턴 완료 버튼 없음", $("doComplete") === null);
  check("가공 버튼 있음", $("doProcess") !== null);
  $("doProcess").click();
  check("결과 입력 안내", $("pickerTitle").textContent.includes("실제로 적용"));
  const resultCards = [...$("handSlots").querySelectorAll("button[data-hand-opt]")];
  check("입력한 네 가능성만 결과 카드로 표시", resultCards.length === 4, resultCards.length);
  check("결과 입력 중 선택기 숨김", optionButtons().length === 0, optionButtons().length);
  resultCards[0].click();
  await sleep(0);
  if (visible("askBox")) { $("askBox").querySelector('[data-good="1"]').click(); await sleep(0); }
  cells = stateCells();
  check("가공 후 시도 1 감소", cells["남은 시도"] === "6", cells["남은 시도"]);
  check("다음 손패 선택 초기화", $("rerollBox").textContent.includes("0/4"));

  chooseFour();
  check("2턴 추천 표시", $("rerollBox").textContent.includes("추천"));
  check("2턴 리롤 버튼 있음", $("doReroll") !== null);
  check("2턴 완료 버튼 있음", $("doComplete") !== null);
  const before = stateCells()["남은 리롤"];
  $("doReroll").click(); await sleep(0);
  check("리롤만 감소", stateCells()["남은 리롤"] === String(Number(before) - 1));
  check("리롤은 시도 유지", stateCells()["남은 시도"] === "6");
  $("undo").click(); await sleep(0);
  check("되돌리기 리롤 복구", stateCells()["남은 리롤"] === before);

  chooseFour();
  $("doComplete").click(); await sleep(0);
  check("중도 완료 패널", visible("finished"));
  check("중도 완료 시도 0", stateCells()["남은 시도"] === "0");
  check("선택 패널 숨김", !visible("choices"));
  check("최종 등급 표시", /(전설|유물|고대) 젬/.test($("finished").textContent));
  $("restart").click(); await sleep(0);
  check("재시작 시도 복구", stateCells()["남은 시도"] === "7");
  check("재시작 레벨 복구", stateCells()["레벨 총합"].startsWith("4 "));

  $("manual").click(); $("curAttempts").value = "99"; $("start").click(); await sleep(20);
  check("잘못된 입력 경고", $("setupHint").textContent.startsWith("⚠"), $("setupHint").textContent);
  console.log(JSON.stringify({ solveMs, checks }, null, 1)); window.close();
})().catch((err) => {
  console.error(err.stack || String(err));
  console.log(JSON.stringify({ checks }, null, 1)); process.exit(1);
});
