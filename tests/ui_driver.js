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
  for (let i = 0; i < 4; i++) optionButtons().find((b) => !b.classList.contains("active")).click();
}

(async () => {
  await waitFor(() => $("grade").children.length === 3, "초기 렌더");
  check("등급 버튼 3개", $("grade").children.length === 3);
  const t0 = window.performance.now();
  await waitFor(() => visible("session") && !$("start").disabled, "초기 자동 계산");
  const solveMs = window.performance.now() - t0;
  check("처음부터 젬 UI 표시", visible("session") && visible("choices"));
  check("10초 안에 자동 계산", solveMs < 10000, solveMs);
  check("초기 등급은 영웅", stateCells()["남은 시도"] === "9");

  [...$("grade").children].find((b) => b.dataset.grade === "희귀").click();
  check("희귀 선택 반영", $("grade").children[1].getAttribute("aria-pressed") === "true");
  await waitFor(() => stateCells()["남은 시도"] === "7" && !$("start").disabled, "등급 자동 반영");
  check("등급 변경 즉시 시도 반영", stateCells()["남은 시도"] === "7");
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }));
  await sleep(0);
  check("Ctrl+Z로 이전 등급 복원", stateCells()["남은 시도"] === "9");
  [...$("grade").children].find((b) => b.dataset.grade === "희귀").click();
  await waitFor(() => stateCells()["남은 시도"] === "7" && !$("start").disabled, "희귀 재선택");

  $("goal").value = "target=4,4,0,0"; $("goal").dispatchEvent(new window.Event("change"));
  await waitFor(() => !$("start").disabled, "목표 자동 계산");
  check("진행 바 숨김", !visible("progress"));
  check("시작 버튼 활성", !$("start").disabled);
  let cells = stateCells();
  check("초기 총합 4", cells["레벨 총합"].startsWith("4 "), cells["레벨 총합"]);
  check("희귀 시도 7", cells["남은 시도"] === "7", cells["남은 시도"]);
  check("희귀 리롤 1", cells["남은 리롤"] === "1", cells["남은 리롤"]);
  check("기대치 퍼센트", /^\d+\.\d\d%$/.test($("value").textContent), $("value").textContent);
  check("옵션 종류 4개로 통합", $("optList").querySelectorAll("button[data-attr]").length === 4);
  check("증감량 별도 선택", optionButtons().some((b) => b.textContent.includes("+4 증가")));
  check("초기 선택 0/4", $("rerollBox").textContent.includes("0/4"), $("rerollBox").textContent);

  const willDiamond = $("stateGrid").querySelector('[data-edit-stat="will"]');
  willDiamond.click();
  check("마름모 클릭 시 상태 편집기", visible("stateEditor"));
  $("stateEditor").querySelector('[data-level="2"]').click(); await sleep(0);
  check("마름모에서 레벨 변경", stateCells()["의지력 효율"] === "2");
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }));
  check("상태 편집도 Ctrl+Z 복원", stateCells()["의지력 효율"] === "1");
  $("stateGrid").querySelector('[data-edit-stat="eff1"]').click();
  $("effectName").value = "공격력";
  $("effectName").dispatchEvent(new window.Event("change")); await sleep(0);
  check("효과 마름모에서 효과명 변경", $("stateGrid").querySelector('[data-edit-stat="eff1"]').textContent.includes("공격력"));
  doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "z", ctrlKey: true, bubbles: true }));

  chooseFour();
  check("서로 다른 4개 선택", $("handSlots").querySelectorAll("button[data-hand-opt]").length === 4);
  check("추천 표시", $("rerollBox").textContent.includes("추천"), $("rerollBox").textContent);
  check("첫 턴 리롤 버튼 없음", $("doReroll") === null);
  check("첫 턴 완료 버튼 없음", $("doComplete") === null);
  check("중앙 가공 버튼은 적용 카드 전 비활성", $("applyProcess").disabled);
  const resultCards = [...$("handSlots").querySelectorAll("button[data-hand-opt]")];
  resultCards[0].click(); await sleep(0);
  check("실제 적용 카드 선택", resultCards[0].dataset.handOpt && !$("applyProcess").disabled);
  check("가공 버튼에 적용 효과 표시", $("applyProcess").textContent.includes("증가"));
  $("applyProcess").click();
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
