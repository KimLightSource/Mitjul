"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch("/api" + path, init);
  if (!res.ok) {
    let msg = res.status + "";
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 2500);
}

function store(key, val) {
  try {
    if (val === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, val);
  } catch { return null; }
}

// ---------- 발음 (브라우저 내장 TTS) ----------

// 발음 억양. 폰에 깔린 음성(Android는 보통 Google 음성 엔진)을 쓴다 — 서버·비용 없음.
// 토익 리스닝처럼 억양을 섞어 들을 수 있게 "mix"는 카드마다 무작위로 고른다.
const ACCENTS = { us: { lang: "en-US", label: "미국" }, gb: { lang: "en-GB", label: "영국" }, au: { lang: "en-AU", label: "호주" } };
let accentMode = store("accent") || "us";
if (!(accentMode in ACCENTS) && accentMode !== "mix") accentMode = "us";
let cardAccent = "us"; // mix일 때 지금 카드의 억양
const voices = {}; // accent → SpeechSynthesisVoice (기기에 없으면 없음)
function pickVoices() {
  const all = speechSynthesis.getVoices();
  for (const [k, a] of Object.entries(ACCENTS)) {
    const vs = all.filter((v) => v.lang.replace("_", "-").toLowerCase() === a.lang.toLowerCase());
    voices[k] = vs.find((v) => /google/i.test(v.name)) || vs[0] || null;
  }
}
if ("speechSynthesis" in window) {
  pickVoices();
  speechSynthesis.addEventListener("voiceschanged", pickVoices);
}
function rollAccent() {
  // 음성 목록을 못 받았으면(일부 브라우저) 억양만 지정해서 기기에 맡긴다
  const have = Object.keys(ACCENTS).filter((k) => voices[k]);
  const pool = have.length ? have : Object.keys(ACCENTS);
  cardAccent = pool[Math.floor(Math.random() * pool.length)];
}
const currentAccent = () => (accentMode === "mix" ? cardAccent : accentMode);
function speak(text, rate = 0.95, accent = currentAccent()) {
  if (!("speechSynthesis" in window) || !text) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = ACCENTS[accent].lang;
  if (voices[accent]) u.voice = voices[accent];
  u.rate = rate;
  const btn = $(".card .speak");
  u.onstart = () => btn?.classList.add("playing");
  u.onend = u.onerror = () => btn?.classList.remove("playing");
  speechSynthesis.speak(u);
}

// Android 햅틱 (지원 안 하면 조용히 무시)
const buzz = (ms) => { try { navigator.vibrate?.(ms); } catch {} };

// ---------- 뒤로가기 (Android) ----------
// 한 페이지짜리 앱이라 history가 비어 있으면 뒤로가기 = 앱 종료다. 화면 위에 "층"(탭 이동,
// 학습 세트, 설정 시트, 사진 뷰어)을 열 때마다 history에 한 칸 쌓고, 뒤로가기(popstate)가 오면
// 맨 위 층을 닫는다. 화면 버튼으로 닫을 때도 history.back()을 거쳐 기록과 화면이 어긋나지 않게 한다.
const layers = [];
const layerClose = {};
function openLayer(name) {
  if (layers.includes(name)) return;
  layers.push(name);
  history.pushState({ depth: layers.length }, "");
}
function closeLayer(name) {
  if (layers[layers.length - 1] === name) return history.back();
  const i = layers.indexOf(name);
  if (i >= 0) { layers.splice(i, 1); layerClose[name](); }
}
addEventListener("popstate", () => {
  const name = layers.pop();
  if (name) layerClose[name]();
});

// ---------- 탭 ----------

let currentView = "study";
function showView(name) {
  currentView = name;
  for (const b of document.querySelectorAll(".tabs button")) b.classList.toggle("active", b.dataset.view === name);
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== "view-" + name;
  if (name === "study" && !$("#study-home").hidden) loadStats();
  if (name === "words") loadWords();
  if (name === "add") loadJobs();
}
for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => {
  const name = b.dataset.view;
  if (name === currentView) return;
  if (name === "study") return closeLayer("tab");
  showView(name);
  openLayer("tab"); // 단어장·추가 탭에서 뒤로가기 → 학습 탭
});
layerClose.tab = () => showView("study");

// ---------- 학습 ----------

const SET_SIZE = 20;
let queue = [];
let pos = 0;
let firstTry = new Map(); // word id → 처음 넘길 때 알았는지
let undoStack = []; // 이번 세트에서 넘긴 순서 — 되돌리기는 뒤에서부터 하나씩 무른다
let revealed = false;
let busy = false;

// 학습 범위: "" 섞어서 / vocab 어휘만 / grammar 문법만. 기기별로 기억한다.
const KIND_NAME = { "": "단어가", vocab: "어휘가", grammar: "문법 공식이", starred: "북마크가" };
let studyKind = store("studyKind") || "";
if (!(studyKind in KIND_NAME)) studyKind = "";
const kindQuery = () => (studyKind ? "&kind=" + studyKind : "");

function renderKindPick() {
  for (const b of $("#kind-pick").children) {
    const on = b.dataset.kind === studyKind;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", on);
  }
}
renderKindPick();
$("#kind-pick").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.dataset.kind === studyKind) return;
  studyKind = b.dataset.kind;
  store("studyKind", studyKind);
  buzz(8);
  renderKindPick();
  loadStats();
});

// ---------- 암기 테스트 ----------
// 암기 완료(3단계 이상) 어휘에서 무작위로 내고, 뜻을 적게 한다. 서버가 즉석 채점(거의 같은 뜻 → 정답,
// 빈 답 → 오답)하고, 판단이 필요한 답은 시험이 끝나면 AI가 '대체로 맞는지' 채점한다(워커, 1~2분).
// 오답은 서버가 0단계로 내려 다시 학습 카드로 나오게 한다.
const TEST_SIZE = 20;
let test = null, ti = 0, testPoll = null;
const VERDICT = { correct: "정답", wrong: "오답", pending: "채점 중", error: "채점 실패" };

async function startTest() {
  try { test = await api("/tests?limit=" + TEST_SIZE, { method: "POST" }); }
  catch (e) { return toast(e.message); }
  ti = 0;
  $("#test-result").hidden = true;
  $("#test-q").hidden = false;
  $("#test-screen").hidden = false;
  document.body.classList.add("studying");
  openLayer("test");
  showQuestion();
}

function showQuestion() {
  const it = test.items[ti];
  $("#test-word").textContent = it.word;
  $("#test-count").textContent = `${ti + 1}/${test.items.length}`;
  $("#test-bar").style.width = (ti / test.items.length) * 100 + "%";
  $("#test-input").value = "";
  $("#test-form").hidden = false;
  $("#test-feedback").hidden = true;
  $("#test-input").focus();
  if ($("#opt-autospeak").checked) speak(it.word);
}

async function submitAnswer(answer) {
  const it = test.items[ti];
  let verdict;
  try {
    ({ verdict } = await api(`/tests/${test.id}/answers/${it.aid}`, { method: "POST", json: { answer } }));
  } catch (e) { return toast("저장 실패: " + e.message); }
  it.verdict = verdict;
  it.answer = answer;
  buzz(verdict === "correct" ? 12 : verdict === "wrong" ? [10, 40, 10] : 8);
  $("#test-input").blur();
  $("#test-form").hidden = true;
  const v = $("#test-verdict");
  v.className = "verdict " + verdict;
  v.innerHTML = {
    correct: "✓ 정답",
    wrong: "✗ 몰랐어요 <small>다시 학습 카드로 보낼게요</small>",
    pending: "⏳ AI가 채점할게요 <small>시험이 끝나면 확인해요</small>",
  }[verdict];
  $("#test-answer").innerHTML = sensesHtml(it);
  $("#test-mine").textContent = answer || "—";
  $("#test-mine-row").hidden = !answer;
  $("#test-feedback").hidden = false;
  $("#test-next").textContent = ti + 1 < test.items.length ? "다음" : "결과 보기";
  $("#test-next").focus();
}

async function nextQuestion() {
  if (ti + 1 < test.items.length) { ti++; return showQuestion(); }
  try { test = await api(`/tests/${test.id}/finish`, { method: "POST" }); }
  catch (e) { return toast("저장 실패: " + e.message); }
  $("#test-bar").style.width = "100%";
  $("#test-q").hidden = true;
  $("#test-result").hidden = false;
  renderTestResult();
}

function renderTestResult() {
  const items = test.items;
  const correct = items.filter((i) => i.verdict === "correct").length;
  const wrong = items.filter((i) => i.verdict === "wrong").length;
  $("#test-score").textContent = correct;
  $("#test-score-of").textContent = "/" + items.length;
  requestAnimationFrame(() => $("#test-ring").style.setProperty("--p", items.length ? (correct / items.length) * 100 : 0));
  $("#test-score-sub").textContent = test.grading
    ? "채점이 끝나면 점수가 바뀔 수 있어요"
    : wrong ? `틀린 ${wrong}개는 다시 학습 카드로 보냈어요` : "모두 맞혔어요 🎉";
  $("#test-grading").hidden = !test.grading;
  $("#test-list").innerHTML = items.map((i) => `
    <li>
      <span class="tl-word">${esc(i.word)}</span>
      <span class="tl-chip ${i.verdict}">${VERDICT[i.verdict] || ""}</span>
      <span class="tl-detail">내 답 <b>${esc(i.answer || "—")}</b> · 정답 ${sensesInline(i)}</span>
      ${i.graded_by === "ai" && i.reason ? `<span class="tl-reason">AI: ${esc(i.reason)}</span>` : ""}
    </li>`).join("");
  clearTimeout(testPoll);
  if (test.grading) testPoll = setTimeout(async () => {
    try { test = await api(`/tests/${test.id}`); renderTestResult(); } catch {}
  }, 4000);
}

layerClose.test = async () => {
  clearTimeout(testPoll);
  speechSynthesis?.cancel();
  const t = test, midway = $("#test-result").hidden;
  test = null;
  $("#test-screen").hidden = true;
  if (!layers.includes("history")) document.body.classList.remove("studying");
  // 중간에 나가도 답한 문항은 채점되게 끝내기를 보낸다(안 푼 문항은 서버가 버린다)
  let grading = t?.grading;
  if (t && midway) {
    try { grading = (await api(`/tests/${t.id}/finish`, { method: "POST" })).grading; } catch {}
  }
  if (grading && !layers.includes("history")) toast("채점이 끝나면 ‘채점 기록’에서 볼 수 있어요");
  if (layers.includes("history")) loadHistory();
  loadStats();
};

// 지난 결과 다시 보기(채점 기록에서). 채점 중이면 결과 화면이 그대로 4초마다 갱신된다.
async function openTestResult(id) {
  try { test = await api(`/tests/${id}`); } catch (e) { return toast("불러오기 실패: " + e.message); }
  $("#test-q").hidden = true;
  $("#test-result").hidden = false;
  $("#test-bar").style.width = "100%";
  $("#test-count").textContent = `${test.items.length}문제`;
  $("#test-screen").hidden = false;
  document.body.classList.add("studying");
  openLayer("test");
  renderTestResult();
}

// ---------- 채점 기록 ----------
let historyPoll = null;
async function loadHistory() {
  clearTimeout(historyPoll);
  let list;
  try { list = await api("/tests"); } catch (e) { return toast("불러오기 실패: " + e.message); }
  $("#history-list").innerHTML = list.length ? list.map((t) => `
    <li><button data-id="${t.id}">
      <span class="hl-score">${t.correct}<span>/${t.total}</span></span>
      <span class="hl-body">
        <b>${fmtTime(t.created_at)}</b>
        ${t.grading ? `<span class="grading-dot"><span class="pulse"></span>AI 채점 중 · ${t.pending}문항</span>`
          : `<small>정답 ${t.correct} · 오답 ${t.wrong}${t.error ? ` · 채점 실패 ${t.error}` : ""}</small>`}
      </span>
      <span class="test-entry-go">›</span>
    </button></li>`).join("")
    : `<li class="empty">아직 채점 기록이 없어요.</li>`;
  if (list.some((t) => t.grading)) historyPoll = setTimeout(loadHistory, 4000);
}
function openHistory() {
  $("#history-screen").hidden = false;
  document.body.classList.add("studying");
  openLayer("history");
  loadHistory();
}
layerClose.history = () => {
  clearTimeout(historyPoll);
  $("#history-screen").hidden = true;
  document.body.classList.remove("studying");
  loadStats();
};
$("#btn-test-history").addEventListener("click", openHistory);
$("#history-close").addEventListener("click", () => closeLayer("history"));
$("#history-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-id]");
  if (b) openTestResult(+b.dataset.id);
});
$("#btn-test").addEventListener("click", startTest);
$("#test-quit").addEventListener("click", () => closeLayer("test"));
$("#test-close").addEventListener("click", () => closeLayer("test"));
$("#test-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const a = $("#test-input").value.trim();
  if (!a) return $("#test-input").focus();
  submitAnswer(a);
});
$("#test-skip").addEventListener("click", () => submitAnswer(""));
$("#test-next").addEventListener("click", nextQuestion);
$("#test-speak").addEventListener("click", () => test && speak(test.items[ti].word));

// ---------- 이어 하기 ----------
// 진행 중인 세트(카드 순서·위치·처음 결과)는 서버에 저장된다. 카드를 넘길 때마다 저장하고, 세트를
// 끝까지 마치면 지운다. 앱을 끄거나 ✕로 나가도 홈에서 "이어서 학습"으로 돌아온다(다른 기기에서도).
let pending = null; // 서버에 남아 있는 세트 (없으면 null)

function sessionBody(at) {
  return {
    kind: studyKind,
    pos: at,
    queue: queue.map((w) => ({ id: w.id, again: !!w.again })),
    first: Object.fromEntries(firstTry),
  };
}
function saveSession(at = pos) {
  api("/session", { method: "PUT", json: sessionBody(at) }).catch((e) => toast("진행 저장 실패: " + e.message));
}
function clearSession() {
  pending = null;
  api("/session", { method: "DELETE" }).catch(() => {});
}

function renderStartButton(due) {
  const label = $("#btn-start-label");
  if (pending) {
    label.innerHTML = `이어서 학습 <span class="resume-count">${pending.pos + 1}/${pending.queue.length}</span>`;
    $("#btn-start").disabled = false;
    $("#btn-new-set").hidden = due === 0;
  } else {
    label.textContent = "학습 시작";
    $("#btn-start").disabled = due === 0;
    $("#btn-new-set").hidden = true;
  }
}

async function loadStats() {
  try {
    pending = await api("/session").catch(() => null);
    const s = await api("/stats?" + kindQuery().slice(1));
    $("#st-due").textContent = s.due;
    $("#st-new").textContent = s.new;
    $("#st-total").textContent = s.total;
    $("#st-mastered").textContent = s.mastered;
    $("#st-streak").textContent = s.streak;
    $("#st-today").textContent = s.today;
    // 링: 오늘 넘긴 수 / (넘긴 수 + 남은 복습). 다 끝내면 꽉 찬다.
    const pct = s.today + s.due ? (s.today / (s.today + s.due)) * 100 : 0;
    requestAnimationFrame(() => $("#ring").style.setProperty("--p", pct.toFixed(1)));
    $("#study-empty").hidden = s.due > 0;
    // 북마크 범위는 복습일과 상관없이 북마크 전부를 낸다
    $(".ring-in span").textContent = studyKind === "starred" ? "북마크" : "복습할 단어";
    $("#study-empty").innerHTML = studyKind === "starred" && s.total
      ? "북마크한 단어가 없어요.<br>카드나 단어장에서 ☆를 눌러 모아 보세요."
      : studyKind && s.total
      ? `지금 복습할 ${KIND_NAME[studyKind]} 없어요.<br>다른 범위를 골라 보세요.`
      : "지금 복습할 단어가 없어요.<br>추가 탭에서 책 사진을 올려 보세요.";
    $("#study-empty").hidden = s.due > 0 || !!pending;
    renderStartButton(s.due);
    $("#btn-test").disabled = !s.testable;
    // 오늘 테스트에서 맞힌 단어는 서버가 그날 테스트에서 뺀다(testable에 이미 빠져 있다)
    const today = s.tested_today ? ` · 오늘 맞힌 ${s.tested_today}개 제외` : "";
    $("#test-entry-sub").textContent = s.testable
      ? `암기 완료 ${s.testable}개 중 최대 ${Math.min(s.testable, TEST_SIZE)}문제${today || " · 뜻 쓰기"}`
      : s.tested_today ? `오늘은 암기 완료 어휘를 다 맞혔어요 (${s.tested_today}개)`
      : "암기 완료한 단어가 생기면 열려요";
    $("#btn-test-history").hidden = !s.tests;
    $("#test-history-grading").hidden = !s.tests_grading;
  } catch (e) { toast("서버 연결 실패: " + e.message); }
}

function setStudyScreen(which) {
  // 카드를 넘기는 동안은 하단 탭바를 숨겨 화면을 카드에 다 쓴다
  document.body.classList.toggle("studying", which === "run");
  $("#study-home").hidden = which !== "home";
  $("#study-run").hidden = which !== "run";
  $("#study-done").hidden = which !== "done";
}

async function startSet({ fresh = false } = {}) {
  if (!fresh) {
    // 진행 중인 세트가 있으면 그 자리에서 이어 한다 (홈을 거치지 않고 들어온 경우를 위해 다시 확인)
    try { pending = await api("/session"); } catch { pending = null; }
  }
  if (pending && !fresh) {
    queue = pending.queue;
    pos = pending.pos;
    firstTry = new Map(Object.entries(pending.first).map(([k, v]) => [Number(k), v]));
    pending = null;
    undoStack = []; // 이어 하기 전에 넘긴 카드는 되돌리지 않는다
  } else {
    try {
      queue = await api("/study?limit=" + SET_SIZE + kindQuery());
    } catch (e) { return toast("불러오기 실패: " + e.message); }
    if (!queue.length) {
      clearSession();
      if (layers.includes("study")) return closeLayer("study");
      setStudyScreen("home");
      return loadStats();
    }
    pos = 0;
    firstTry = new Map();
    undoStack = [];
    saveSession();
  }
  setStudyScreen("run");
  openLayer("study"); // 학습 중 뒤로가기 → 홈
  showCard();
}

const isGrammar = (w) => w.kind === "grammar";

// ---------- 뜻(품사별 여러 줄) ----------
// 품사가 여럿인 단어(leave: 동사·명사)는 뜻을 줄로 나눠 저장한다. 첫 줄은 대표 품사(words.pos)의 뜻,
// 둘째 줄부터는 "[명사] 휴가"처럼 품사를 앞에 붙인다. 예전 형식 "뜻; (명사) 뜻"도 같은 모양으로 읽는다.
const POS_NAMES = "명사|동사|형용사|부사|전치사|접속사|구";
function senses(w) {
  const text = (w.meaning || "").replace(new RegExp(`\\s*[;；]\\s*\\((${POS_NAMES})\\)\\s*`, "g"), "\n[$1] ");
  return text.split("\n").map((s) => s.trim()).filter(Boolean).map((line, i) => {
    const m = line.match(/^\[(.+?)\]\s*/);
    return m ? { pos: m[1], text: line.slice(m[0].length) } : { pos: i === 0 ? w.pos : "", text: line };
  });
}
const sensesHtml = (w) => senses(w).map((s) =>
  `<div class="meaning-row">${s.pos ? `<span class="pos">${esc(s.pos)}</span>` : ""}<span class="meaning">${esc(s.text)}</span></div>`
).join("");
const sensesInline = (w) => senses(w).map((s) =>
  (s.pos ? `<span class="pos-s">${esc(s.pos)}</span>` : "") + esc(s.text)
).join(" · ");

// "사역동사(make/have/let) + 목적어 + 동사원형" → 세로로 쌓인 칸 + 칸 + 칸
function parseFormula(text) {
  return text.split(/\s+\+\s+/).map((t) => {
    const m = t.match(/^(.*?)\s*\((.+)\)\s*$/);
    const main = (m ? m[1] : t).trim();
    const subs = m ? m[2].split(/\s*\/\s*/).filter(Boolean) : [];
    return { main, subs, en: !/[가-힣]/.test(main) };
  });
}

function formulaHtml(text) {
  const parts = parseFormula(text);
  return '<div class="formula">' + parts.map((p, i) =>
    (i ? '<div class="op" aria-hidden="true">+</div>' : "") +
    `<div class="slot ${p.en ? "en" : ""}" style="--i:${i}">` +
      `<span class="slot-main">${esc(p.main)}</span>` +
      (p.subs.length ? `<span class="slot-subs">${p.subs.map((x) => `<i>${esc(x)}</i>`).join("")}</span>` : "") +
    "</div>"
  ).join("") + "</div>";
}

// 공식에서 예문 속에 나타날 영어 표현을 뽑는다.
//  - 괄호 속 대표 단어:  사역동사(make/have/let) → make, have, let
//  - 영어로 된 칸:       the number of, It is, be asked/required → be asked, be required
//  - 괄호 없는 / 나열:   begin/start/continue → 각각, have trouble/difficulty → have trouble, have difficulty
//  - p.p. · -ing · ~ 같은 문법 기호는 예문에 그대로 나오지 않으므로 뺀다
function formulaTerms(formula) {
  const out = [];
  const add = (text) => {
    const alts = text.split("/").map((x) => x.trim()).filter(Boolean);
    const firstWords = (alts[0] || "").split(/\s+/);
    for (let a of alts) {
      const n = a.split(/\s+/).length;
      // 'have trouble/difficulty'처럼 뒤 대안이 짧으면 앞 대안의 앞부분을 빌려 온다
      if (n < firstWords.length) a = firstWords.slice(0, firstWords.length - n).join(" ") + " " + a;
      a = a.split(/\s+/).filter((t) => !/^(p\.p\.|-ing|~|\(.*\))$/i.test(t)).join(" ");
      if (a && !/[가-힣]/.test(a)) out.push(a);
    }
  };
  for (const p of parseFormula(formula)) {
    if (p.en) add(p.main);
    for (const x of p.subs) add(x);
  }
  return out;
}

// 문법 예문에서 공식의 영어 부분을 찾아 강조한다. 찾는 방식은 어휘 강조(highlightRegex)와 같다 —
// be동사 변화, 활용형, 불규칙 변화(had·saw·took)까지.
function highlightGrammar(example, formula) {
  const text = example || "";
  const spans = [];
  for (const term of formulaTerms(formula)) {
    const re = new RegExp(highlightRegex(term).source, "gi");
    for (const m of text.matchAll(re)) if (m[0]) spans.push([m.index, m.index + m[0].length]);
  }
  spans.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  let html = "", at = 0;
  for (const [i, j] of spans) {
    if (i < at) continue; // 겹치는 구간은 앞의 것만
    html += esc(text.slice(at, i)) + "<mark>" + esc(text.slice(i, j)) + "</mark>";
    at = j;
  }
  return html + esc(text.slice(at));
}

// ---------- 바꿔 말하기 · 헷갈리는 단어 ----------
// words.related(JSON): AI가 어휘 카드와 함께 만든다. 카드 뒷면에 참고용으로만 보여 준다.
function relatedOf(w) {
  try { return w.related ? JSON.parse(w.related) : {}; } catch { return {}; }
}
function relatedHtml(w) {
  const { paraphrases = [], confusables = [] } = relatedOf(w);
  let html = "";
  if (confusables.length) {
    html += `<div class="rel-block conf"><div class="rel-title">헷갈리는 단어</div>` + confusables.map((x) => `
      <div class="rel-conf" data-say="${esc(x.word)}">
        <div class="rel-line"><b>${esc(x.word)}</b><span>${esc(x.meaning)}</span></div>
        ${x.tip ? `<small>${esc(x.tip)}</small>` : ""}
      </div>`).join("") + "</div>";
  }
  if (paraphrases.length) {
    html += `<div class="rel-block"><div class="rel-title">바꿔 말하기 <span>토익 패러프레이징</span></div><div class="rel-chips">` +
      paraphrases.map((x) => `<span class="rel-chip" data-say="${esc(x.word)}"><b>${esc(x.word)}</b>${x.meaning ? ` ${esc(x.meaning)}` : ""}</span>`).join("") +
      "</div></div>";
  }
  return html;
}

function boxLabel(w) {
  if (w.again) return ["다시 보기", "again"]; // 이번 세트에서 '모름'으로 뒤에 다시 붙은 카드
  if (w.seen === 0) return ["새 단어", "new"];
  if (w.box === 0) return ["다시 보기", "again"];
  return ["복습 " + w.box + "단계", ""];
}

// 예문에서 표제어를 찾아 강조한다. 숙어는 예문에서 모양이 바뀌므로 단어 단위로 느슨하게 찾는다:
//  - be → is/are/was/…   (be situated in → "is situated in")
//  - 활용형·불규칙형     (remain → remains, look → looking, take → took)
//  - 사이에 낀 목적어     (run A by → "run these figures by"; 토큰 사이 최대 3단어)
//  - 자리 표시 A/B/someone/one's/oneself 는 아무 단어로
const BE_FORMS = "be|is|are|was|were|been|being|am";
const IRREGULAR_VERBS = {
  take: "took|taken", make: "made", give: "gave|given", get: "got|gotten", go: "went|gone",
  come: "came", keep: "kept", hold: "held", bring: "brought", run: "ran", see: "saw|seen",
  find: "found", leave: "left", meet: "met", pay: "paid", lay: "laid", set: "set", put: "put",
  buy: "bought", sell: "sold", send: "sent", spend: "spent", tell: "told", think: "thought",
  carry: "carried", seek: "sought", draw: "drew|drawn", fall: "fell|fallen", write: "wrote|written",
  break: "broke|broken", choose: "chose|chosen", rise: "rose|risen", lead: "led", deal: "dealt",
  have: "had|has", do: "did|does|done", say: "said", stand: "stood", catch: "caught",
  arise: "arose|arisen", become: "became", begin: "began|begun", hear: "heard", feel: "felt",
  let: "let", know: "knew|known", grow: "grew|grown", show: "shown", understand: "understood",
  win: "won", build: "built", teach: "taught", forget: "forgot|forgotten", speak: "spoke|spoken",
};
const FUNCTION_WORDS = new Set("a an the to in on at by for with of up out off as into onto from over under and or than about".split(" "));
const PLACEHOLDER = /^(a|b|sb|sth|someone|somebody|something|~|\.\.\.|…)$/i;

function tokenPattern(t, single) {
  const low = t.toLowerCase();
  const reEsc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (low === "be") return `(?:${BE_FORMS})\\b`;
  if (/^(one's|someone's|one’s)$/.test(low)) return "(?:my|your|his|her|its|our|their|[\\w-]+['’]s)\\b";
  if (low === "oneself") return "[\\w]+(?:self|selves)\\b";
  if (!single && FUNCTION_WORDS.has(low)) return reEsc(low) + "\\b";
  // 활용형까지: 끝의 e/y는 떼고(situate → situat-ed, satisfy → satisf-ied) 뒤에 영문자 허용
  const stem = low.length > 3 ? low.replace(/(e|y)$/, "") : low;
  const alts = [reEsc(stem) + "[a-z]*"];
  if (IRREGULAR_VERBS[low]) alts.push(IRREGULAR_VERBS[low]);
  return `(?:${alts.join("|")})`;
}

function highlightRegex(word) {
  let toks = word.trim().split(/\s+/);
  // 앞뒤 자리 표시(furnish A with B의 B)는 강조 범위에서 뺀다
  while (toks.length > 1 && PLACEHOLDER.test(toks[0])) toks.shift();
  while (toks.length > 1 && PLACEHOLDER.test(toks[toks.length - 1])) toks.pop();
  const single = toks.length === 1;
  let pat = "", gapNeeded = false;
  for (const t of toks) {
    if (PLACEHOLDER.test(t)) { gapNeeded = true; continue; }
    if (pat) pat += gapNeeded ? "\\s+(?:[\\w'’,-]+\\s+){1,4}?" : "\\s+(?:[\\w'’,-]+\\s+){0,3}?";
    pat += tokenPattern(t, single);
    gapNeeded = false;
  }
  return new RegExp("(?<![\\w'’])" + pat, "i");
}

function highlight(example, word) {
  // 원문에서 찾고 나서 조각마다 이스케이프 (tomorrow's 같은 따옴표가 &#39;로 바뀌기 전에 찾기 위해)
  const m = (example || "").match(highlightRegex(word));
  if (!m) return esc(example);
  const i = m.index, j = i + m[0].length;
  return esc(example.slice(0, i)) + "<mark>" + esc(example.slice(i, j)) + "</mark>" + esc(example.slice(j));
}

function showCard() {
  const w = queue[pos];
  if (accentMode === "mix") rollAccent();
  renderCardAccent();
  const card = $("#card");
  card.className = "card";
  void card.offsetWidth; // 애니메이션 재시작
  card.classList.add("enter");
  card.style.transform = "";
  card.style.opacity = "";
  for (const s of card.querySelectorAll(".stamp, .tint")) s.style.opacity = 0;
  card.scrollTop = 0;
  revealed = false;
  const g = isGrammar(w);
  card.classList.toggle("grammar", g);
  if (g) $("#card-word").innerHTML = formulaHtml(w.word);
  else $("#card-word").textContent = w.word;
  const [label, cls] = boxLabel(w);
  $("#card-box").textContent = g ? label.replace("단어", "공식") : label;
  $("#card-box").className = "chip " + (g ? "grammar" : cls);
  renderCardStar();
  hideAddSel();
  const rel = g ? "" : relatedHtml(w);
  $("#card-related").innerHTML = rel;
  $("#card-related").hidden = !rel;
  $("#card-senses").innerHTML = sensesHtml(w);
  $("#card-senses").classList.toggle("multi", senses(w).length > 1);
  $("#card-example").innerHTML = g ? highlightGrammar(w.example, w.word) : highlight(w.example, w.word);
  $("#card-example-ko").textContent = w.example_ko;
  $(".example").hidden = !w.example;
  $("#card-back").hidden = true;
  $("#card-hint").hidden = false;
  $("#progress-text").textContent = `${Math.min(pos + 1, queue.length)}/${queue.length}`;
  $("#progress-bar").style.width = (pos / queue.length) * 100 + "%";
  $("#btn-undo").disabled = !undoStack.length;
  busy = false;
  // 문법 공식은 한국어 섞인 공식이라 읽지 않는다 (예문만 읽음)
  if ($("#opt-autospeak").checked && !g) speak(w.word);
}

// ---------- 북마크 ----------
// 카드·단어장의 별. 누르면 바로 바뀌고(낙관적 갱신) 서버에 저장한다. 실패하면 되돌린다.
function setStarIcon(btn, on) {
  btn.classList.toggle("on", on);
  btn.setAttribute("aria-pressed", on);
  btn.setAttribute("aria-label", on ? "북마크 해제" : "북마크");
}
function renderCardStar() {
  const w = queue[pos];
  if (w) setStarIcon($("#card-star"), !!w.starred);
}
async function toggleStar(w, after) {
  const on = !w.starred;
  // 같은 단어가 여러 곳(세트 끝에 다시 붙은 카드, 단어장 목록)에 있으면 같이 바꾼다
  const sync = (v) => {
    for (const x of [...queue, ...allWords]) if (x.id === w.id) x.starred = v;
    w.starred = v;
    after?.();
  };
  sync(on ? 1 : 0);
  buzz(on ? 12 : 8);
  try {
    await api(`/words/${w.id}/star`, { method: "POST", json: { starred: on } });
    toast(on ? "북마크에 추가했어요" : "북마크에서 뺐어요");
  } catch (e) {
    sync(on ? 0 : 1);
    toast("북마크 저장 실패: " + e.message);
  }
}
$("#card-star").addEventListener("click", () => {
  const w = queue[pos];
  if (w && !busy) toggleStar(w, renderCardStar);
});

function reveal() {
  if (revealed) return;
  revealed = true;
  $("#card").classList.add("revealed");
  $("#card-back").hidden = false;
  $("#card-hint").hidden = true;
}

function decide(known) {
  if (busy) return;
  busy = true;
  buzz(known ? 12 : [10, 40, 10]);
  const w = queue[pos];
  const setFirst = !firstTry.has(w.id);
  if (setFirst) firstTry.set(w.id, known);
  // 서버가 넘기기 전 상태(undo)를 돌려준다 — 되돌리기가 그걸 그대로 /undo에 보낸다
  const saved = api(`/words/${w.id}/review`, { method: "POST", json: { known } })
    .then((r) => r.undo)
    .catch((e) => { toast("저장 실패: " + e.message); return null; });
  undoStack.push({ id: w.id, known, setFirst, saved });
  // 모르는 단어는 이번 세트 끝에 한 번 더 나온다 (산타 토익과 같은 방식).
  if (!known) queue.push({ ...w, seen: w.seen + 1, box: 0, again: true });
  // 넘긴 직후 상태를 저장 — 여기서 앱을 꺼도 다음 카드부터 이어진다
  if (pos + 1 < queue.length) saveSession(pos + 1);
  else clearSession();

  // 버튼·단축키로 넘길 때도 스와이프처럼 도장을 찍고 옆으로 날아가는 게 보이게 한다.
  // CSS transition은 카드의 enter 애니메이션(fill: both)과 reduced-motion 규칙에 덮여
  // 바로 다음 카드로 바뀌어 보였다 — Web Animations로 직접 돌리고 끝날 때까지 기다린다.
  const card = $("#card");
  card.classList.remove("enter", "back");
  const side = known ? "know" : "dunno";
  card.querySelector(`.stamp-${side}`).style.opacity = 1;
  card.querySelector(`.tint-${side}`).style.opacity = 1;
  const from = getComputedStyle(card).transform; // 드래그 중이면 손을 뗀 위치에서 출발
  const dir = known ? 1 : -1;
  speechSynthesis?.cancel();
  const fly = card.animate([
    { transform: from === "none" ? "translateX(0) rotate(0deg)" : from },
    { transform: `translateX(${dir * 130}vw) rotate(${dir * 25}deg)` },
  ], { duration: 420, easing: "cubic-bezier(.3, 0, .8, .15)", fill: "forwards" });
  fly.finished.then(() => {
    fly.cancel(); // showCard가 같은 틱에 새 카드로 바꾸므로 원위치가 화면에 보이지 않는다
    pos++;
    if (pos >= queue.length) finishSet();
    else showCard();
  });
}

// 방금 넘긴 카드로 돌아간다 — 카드가 나갔던 쪽에서 다시 날아 들어온다. 세트 완료 화면에서도 된다.
async function undo() {
  if (busy || !undoStack.length) return;
  busy = true;
  buzz(8);
  const h = undoStack.pop();
  if (!h.known) queue.pop(); // '모름'으로 세트 끝에 붙였던 카드
  if (h.setFirst) firstTry.delete(h.id);
  pos--;
  const prev = await h.saved;
  if (prev) api(`/words/${h.id}/undo`, { method: "POST", json: prev }).catch((e) => toast("되돌리기 저장 실패: " + e.message));
  saveSession(); // 마지막 카드였다면 finishSet이 지운 세트를 다시 만든다
  setStudyScreen("run");
  speechSynthesis?.cancel();
  showCard();
  const card = $("#card");
  card.classList.remove("enter");
  const dir = h.known ? 1 : -1;
  card.animate([
    { transform: `translateX(${dir * 130}vw) rotate(${dir * 25}deg)` },
    { transform: "translateX(0) rotate(0deg)" },
  ], { duration: 420, easing: "cubic-bezier(.2, .8, .3, 1)" });
}

function finishSet() {
  const vals = [...firstTry.values()];
  const knew = vals.filter(Boolean).length;
  $("#done-text").textContent = `${vals.length}개 중 ${knew}개를 바로 알았어요. 모른 ${vals.length - knew}개는 곧 다시 나와요.`;
  $("#progress-bar").style.width = "100%";
  $("#btn-done-undo").hidden = !undoStack.length;
  setStudyScreen("done");
}

// 스와이프 / 탭 처리
(() => {
  const card = $("#card");
  let sx = 0, sy = 0, dx = 0, dragging = false, down = false, armed = false, textHold = false;
  const THRESH = 90;

  card.addEventListener("pointerdown", (e) => {
    if (busy || e.target.closest(".star-btn")) return;
    down = true; dragging = false; armed = false;
    // 마우스로 예문을 드래그하면 카드를 넘기지 않고 글자를 고른다 (터치는 꾹 눌러 고른다)
    textHold = revealed && e.pointerType === "mouse" && !!e.target.closest(".example-en");
    sx = e.clientX; sy = e.clientY; dx = 0;
    card.classList.remove("back");
  });
  card.addEventListener("pointermove", (e) => {
    if (!down || textHold || selectedText()) return;
    dx = e.clientX - sx;
    const dy = e.clientY - sy;
    if (!dragging && Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy)) {
      dragging = true;
      card.setPointerCapture(e.pointerId);
    }
    if (!dragging) return;
    card.style.transform = `translateX(${dx}px) rotate(${dx / 18}deg)`;
    const k = Math.min(Math.abs(dx) / THRESH, 1);
    card.querySelector(".stamp-know").style.opacity = dx > 0 ? k : 0;
    card.querySelector(".stamp-dunno").style.opacity = dx < 0 ? k : 0;
    card.querySelector(".tint-know").style.opacity = dx > 0 ? k : 0;
    card.querySelector(".tint-dunno").style.opacity = dx < 0 ? k : 0;
    // 임계점을 넘는 순간 짧게 진동 — 손을 떼면 넘어간다는 신호
    if ((Math.abs(dx) > THRESH) !== armed) { armed = !armed; if (armed) buzz(8); }
  });
  const snapBack = () => {
    card.classList.add("back");
    card.style.transform = "";
    for (const s of card.querySelectorAll(".stamp, .tint")) s.style.opacity = 0;
  };
  card.addEventListener("pointerup", (e) => {
    if (!down) return;
    down = false;
    if (dragging) {
      if (Math.abs(dx) > THRESH) decide(dx > 0);
      else snapBack();
      return;
    }
    // 탭: 뜻을 펼치고, 단어/예문을 눌렀으면 읽어준다
    if (selectedText()) return; // 예문에서 단어를 고른 것 — 추가 버튼이 대신 뜬다
    const say = e.target.closest("[data-say]");
    if (say && revealed) return speak(say.dataset.say); // 헷갈리는 단어·바꿔 말하기
    const target = e.target.closest("[data-speak]");
    const w = queue[pos];
    if (target?.dataset.speak === "example" && revealed) speak(w.example, 0.9);
    else if (!isGrammar(w) && (target?.dataset.speak === "word" || !revealed)) speak(w.word);
    reveal();
  });
  card.addEventListener("pointercancel", () => { down = false; if (dragging) snapBack(); });
})();

// ---------- 예문에서 모르는 단어 추가 ----------
// 뒤집은 카드의 영어 예문은 글자를 고를 수 있다(꾹 누르기 / 드래그). 고르면 "단어장에 추가" 버튼이 뜨고,
// 누르면 직접 추가와 같은 AI 채우기 작업으로 넘긴다(사전형·뜻·예문은 AI가, 중복은 서버가 거른다).
function selectedText() {
  const sel = getSelection();
  if (!sel || sel.isCollapsed || !$("#card-example").contains(sel.anchorNode)) return "";
  let t = sel.toString().replace(/\s+/g, " ").trim().replace(/^[^A-Za-z]+|[^A-Za-z]+$/g, "");
  if (!t || /[가-힣]/.test(t) || t.split(" ").length > 6 || t.length > 60) return "";
  // 문장 첫 글자라 대문자인 일반 단어는 소문자로 (Please → please). 약어(HR)는 그대로.
  if (/^[A-Z][a-z]/.test(t)) t = t[0].toLowerCase() + t.slice(1);
  return t;
}
function hideAddSel() { $("#add-sel").hidden = true; }
document.addEventListener("selectionchange", () => {
  const t = $("#study-run").hidden ? "" : selectedText();
  $("#add-sel").hidden = !t;
  if (t) $("#add-sel span").textContent = `‘${t}’ 단어장에 추가`;
});
$("#add-sel").addEventListener("click", async () => {
  const t = selectedText();
  if (!t) return hideAddSel();
  getSelection().removeAllRanges();
  hideAddSel();
  buzz(12);
  try {
    await api("/words", { method: "POST", json: { word: t } });
    toast(`‘${t}’ — AI가 뜻·예문을 채워서 단어장에 넣을게요`);
  } catch (e) { toast("추가 실패: " + e.message); }
});

$("#btn-start").addEventListener("click", () => startSet());
$("#btn-again").addEventListener("click", () => startSet({ fresh: true }));
$("#btn-new-set").addEventListener("click", () => { clearSession(); startSet({ fresh: true }); });
layerClose.study = () => { speechSynthesis?.cancel(); setStudyScreen("home"); loadStats(); };
$("#btn-home").addEventListener("click", () => closeLayer("study"));
$("#btn-quit").addEventListener("click", () => closeLayer("study"));
$("#btn-know").addEventListener("click", () => decide(true));
$("#btn-dunno").addEventListener("click", () => decide(false));
$("#btn-undo").addEventListener("click", undo);
$("#btn-done-undo").addEventListener("click", undo);
document.addEventListener("keydown", (e) => {
  if (currentView !== "study" || e.repeat || e.target.closest("input, textarea")) return;
  // 되돌리기: Backspace · Z · Ctrl/Cmd+Z — 세트 완료 화면에서도
  const z = e.key === "z" || e.key === "Z";
  if ((e.key === "Backspace" && !e.ctrlKey && !e.metaKey && !e.altKey) || (z && !e.altKey)) {
    if (!$("#study-run").hidden || !$("#study-done").hidden) { e.preventDefault(); undo(); }
    return;
  }
  if ($("#study-run").hidden) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "ArrowRight" || e.key === "2") decide(true);
  else if (e.key === "ArrowLeft" || e.key === "1") decide(false);
  else if (e.key === " ") { e.preventDefault(); reveal(); if (!isGrammar(queue[pos])) speak(queue[pos].word); }
});

// 학습 화면의 억양 버튼. 누르면 바로 바꾸고 지금 카드를 그 억양으로 다시 읽는다.
function renderAccentPick() {
  for (const b of $("#accent-pick").children) {
    const on = b.dataset.accent === accentMode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", on);
  }
}
function renderCardAccent() {
  const w = queue[pos];
  $("#card-accent").hidden = accentMode !== "mix" || !w;
  $("#card-accent").textContent = ACCENTS[cardAccent].label + " 발음";
}
const warned = new Set();
function warnMissingVoice(accent) {
  // 음성 목록을 아직 못 받았으면 없는지 확정할 수 없다
  if (!("speechSynthesis" in window) || !speechSynthesis.getVoices().length) return;
  if (voices[accent] || warned.has(accent)) return;
  warned.add(accent);
  toast(`이 폰에 ${ACCENTS[accent].label} 음성이 없어요 — 폰 설정 → 텍스트 음성 변환 → Google 엔진 → 음성 데이터 설치`);
}
renderAccentPick();
$("#accent-pick").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  accentMode = b.dataset.accent;
  store("accent", accentMode);
  buzz(8);
  renderAccentPick();
  if (accentMode === "mix") rollAccent();
  renderCardAccent();
  if (accentMode !== "mix") warnMissingVoice(accentMode);
  const w = queue[pos];
  if (!w) return;
  // 뒤집은 카드는 예문, 아니면 단어. 문법 공식은 예문만 읽는다.
  if (revealed && w.example) speak(w.example, 0.9);
  else if (!isGrammar(w)) speak(w.word);
});

const auto = $("#opt-autospeak");
auto.checked = store("autospeak") !== "0";
auto.addEventListener("change", () => store("autospeak", auto.checked ? "1" : "0"));

// ---------- 단어장 ----------

let allWords = [];
let searchTimer;
let kindFilter = "";

$("#kind-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  kindFilter = b.dataset.kind;
  for (const x of $("#kind-filter").children) x.classList.toggle("on", x === b);
  loadWords();
});

async function loadWords() {
  try {
    allWords = await api("/words?q=" + encodeURIComponent($("#search").value.trim()) + "&kind=" + kindFilter);
  } catch (e) { return toast("불러오기 실패: " + e.message); }
  renderWords();
}

function level(box) {
  // 7단계(120일)가 최고. 링이 차오르는 만큼 외운 것.
  return `<span class="level" style="--l:${Math.round((box / 7) * 100)}" title="학습 단계 ${box}/7"><i>${box}</i></span>`;
}

function renderWords() {
  $("#words-count").textContent = `${allWords.length}개`;
  $("#word-list").innerHTML = allWords.map((w) => `
    <li data-id="${w.id}">
      <div class="wl-head">
        <div class="wl-text">
          <div class="wl-word ${isGrammar(w) ? "g" : ""}">${isGrammar(w) ? esc(w.word).replace(/\//g, "/<wbr>") : esc(w.word)}</div>
          <div class="wl-meaning">${sensesInline(w)}</div>
        </div>
        <div class="wl-side">
          <button type="button" class="star-btn wl-star ${w.starred ? "on" : ""}" aria-pressed="${!!w.starred}" aria-label="${w.starred ? "북마크 해제" : "북마크"}"><svg><use href="#i-star"/></svg></button>
          ${level(w.box)}
        </div>
      </div>
    </li>`).join("");
}

$("#search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadWords, 250); });

function armedButton(btn, label, action) {
  // 파괴적 동작은 두 번 눌러야 실행된다 (confirm() 대신).
  if (btn.classList.contains("armed")) return action();
  const orig = btn.textContent;
  btn.classList.add("armed");
  btn.textContent = label;
  setTimeout(() => { btn.classList.remove("armed"); btn.textContent = orig; }, 3000);
}

$("#word-list").addEventListener("click", async (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  const w = allWords.find((x) => x.id == li.dataset.id);
  const star = e.target.closest(".wl-star");
  if (star) {
    return toggleStar(w, () => {
      setStarIcon(star, !!w.starred);
      // 북마크 목록을 보는 중에 해제하면 목록에서 빠진다
      if (kindFilter === "starred" && !w.starred) { allWords = allWords.filter((x) => x.starred); renderWords(); }
    });
  }
  if (e.target.closest(".wl-head")) {
    const open = li.querySelector(".edit");
    if (open) return open.remove();
    li.insertAdjacentHTML("beforeend", `
      <form class="edit">
        <label>종류<select name="kind">
          <option value="vocab" ${isGrammar(w) ? "" : "selected"}>어휘</option>
          <option value="grammar" ${isGrammar(w) ? "selected" : ""}>문법 공식</option>
        </select></label>
        <label>${isGrammar(w) ? "공식" : "단어"}<input name="word" value="${esc(w.word)}"></label>
        <label>품사<input name="pos" value="${esc(w.pos)}"></label>
        <label>뜻 <small>품사가 여럿이면 줄을 나누고 둘째 줄부터 [명사] 처럼 품사를 앞에</small><textarea name="meaning" rows="${Math.max(1, senses(w).length)}">${esc(w.meaning)}</textarea></label>
        <label>예문<textarea name="example" rows="2">${esc(w.example)}</textarea></label>
        <label>예문 해석<textarea name="example_ko" rows="2">${esc(w.example_ko)}</textarea></label>
        ${!isGrammar(w) && w.related ? `<div class="related in-edit">${relatedHtml(w)}</div>` : ""}
        <div class="row">
          <button type="button" class="speak-btn icon-btn" aria-label="발음 듣기"><svg><use href="#i-speaker"/></svg></button>
          <button type="button" class="danger">삭제</button>
          <button type="submit" class="primary">저장</button>
        </div>
      </form>`);
    return;
  }
  if (e.target.closest(".speak-btn")) return speak(w.word);
  const say = e.target.closest("[data-say]");
  if (say) return speak(say.dataset.say);
  const del = e.target.closest(".danger");
  if (del) {
    armedButton(del, "한 번 더 누르면 삭제", async () => {
      await api(`/words/${w.id}`, { method: "DELETE" });
      toast(`'${w.word}' 삭제됨`);
      loadWords();
    });
  }
});

$("#word-list").addEventListener("submit", async (e) => {
  e.preventDefault();
  const li = e.target.closest("li");
  const body = Object.fromEntries(new FormData(e.target));
  try {
    await api(`/words/${li.dataset.id}`, { method: "PUT", json: body });
    toast("저장됨");
    loadWords();
  } catch (err) { toast("저장 실패: " + err.message); }
});

// ---------- 추가 ----------

async function resize(file, max = 3000) {
  // 폰 원본(수 MB)을 그대로 보내지 않는다. 두 페이지를 한 장에 찍으면 볼펜 밑줄이 얇아서
  // 2000px로는 놓쳤다(2026-09-30). 워커가 이 사진을 2×2 조각으로 잘라 보므로 3000px이면 충분하다.
  const bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, max / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * scale);
  c.height = Math.round(bmp.height * scale);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return new Promise((r) => c.toBlob(r, "image/jpeg", 0.88));
}

$("#file-input").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (!files.length) return;
  const status = $("#upload-status");
  try {
    const fd = new FormData();
    for (let i = 0; i < files.length; i++) {
      status.textContent = `사진 준비 중… (${i + 1}/${files.length})`;
      fd.append("files", await resize(files[i]), `page-${i + 1}.jpg`);
    }
    status.textContent = "올리는 중…";
    await api("/jobs", { method: "POST", body: fd });
    status.textContent = "";
    toast(`${files.length}장 올림 — 처리되면 단어장에 들어가요`);
    loadJobs();
  } catch (err) {
    status.textContent = "";
    toast("업로드 실패: " + err.message);
  }
});

$("#btn-manual").addEventListener("click", async () => {
  const ta = $("#manual-words");
  if (!ta.value.trim()) return;
  try {
    await api("/words", { method: "POST", json: { word: ta.value } });
    ta.value = "";
    toast("AI 채우기 대기열에 추가했어요");
    loadJobs();
  } catch (e) { toast("추가 실패: " + e.message); }
});

const STATUS = { pending: "대기 중", processing: "처리 중", done: "완료", error: "오류" };
let jobsTimer;

function fmtTime(t) {
  const d = new Date(t * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

async function loadJobs() {
  clearTimeout(jobsTimer);
  let jobs;
  try { jobs = await api("/jobs"); } catch (e) { return toast("불러오기 실패: " + e.message); }
  $("#job-list").innerHTML = jobs.map((j) => {
    const thumb = j.kind === "photo"
      ? `<button class="thumb-btn" data-img="/api/jobs/${j.id}/image" aria-label="사진 크게 보기"><img src="/api/jobs/${j.id}/image" loading="lazy" alt=""></button>`
      : `<div class="thumb"><svg><use href="#i-pen"/></svg></div>`;
    let detail = "";
    if (j.kind === "text" && j.status !== "done") detail = esc(j.payload.words.join(", "));
    if (j.status === "done") {
      const r = j.result;
      detail = `<b>+${r.added.length}개</b> ${esc(r.added.join(", "))}`;
      if (r.duplicates.length) detail += ` · 이미 있음 ${r.duplicates.length}개(${esc(r.duplicates.join(", "))})`;
      if (!r.added.length && !r.duplicates.length) detail = "찾은 단어 없음";
      if (r.note) detail += `<br><i>${esc(r.note)}</i>`;
    }
    if (j.status === "error" || (j.status === "pending" && j.error)) detail = esc(j.error || "");
    return `<li data-id="${j.id}">
      ${thumb}
      <div class="job-body">
        <span class="badge ${j.status}">${STATUS[j.status]}</span> <span class="muted">${fmtTime(j.created_at)}</span>
        <div class="words">${detail}</div>
      </div>
      <div class="job-actions">
        ${j.status === "error" ? '<button class="retry">재시도</button>' : ""}
        ${j.status !== "processing" ? '<button class="del">삭제</button>' : ""}
      </div>
    </li>`;
  }).join("") || '<p class="muted small center">아직 없어요</p>';
  // 진행 중인 작업이 있으면 추가 탭을 보는 동안 자동 새로고침
  if (currentView === "add" && jobs.some((j) => j.status === "pending" || j.status === "processing")) {
    jobsTimer = setTimeout(loadJobs, 5000);
  }
}

$("#job-list").addEventListener("click", async (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  const thumb = e.target.closest(".thumb-btn");
  if (thumb) return openViewer(thumb.dataset.img);
  try {
    if (e.target.closest(".retry")) {
      await api(`/jobs/${li.dataset.id}/retry`, { method: "POST" });
      loadJobs();
    }
    const del = e.target.closest(".del");
    if (del) {
      armedButton(del, "확인", async () => {
        await api(`/jobs/${li.dataset.id}`, { method: "DELETE" });
        loadJobs();
      });
    }
  } catch (err) { toast(err.message); }
});

// ---------- 설정: 테마 ----------

const THEME_BAR = { light: "#f5f3ff", dark: "#0c0b12" };
const darkMq = matchMedia("(prefers-color-scheme: dark)");

function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  for (const b of $("#theme-pick").children) b.classList.toggle("on", b.dataset.theme === t);
  // 상태바 색도 테마를 따라가게 (스플래시가 떠 있는 동안은 보라 유지)
  if ($("#splash")) return;
  const eff = t === "system" ? (darkMq.matches ? "dark" : "light") : t;
  $('meta[name="theme-color"]').content = THEME_BAR[eff];
}
let theme = store("theme") || "system";
applyTheme(theme);
darkMq.addEventListener("change", () => theme === "system" && applyTheme(theme));
$("#theme-pick").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  theme = b.dataset.theme;
  store("theme", theme);
  buzz(8);
  applyTheme(theme);
});

function openSheet(open) {
  $("#sheet").hidden = $("#sheet-scrim").hidden = !open;
}
layerClose.sheet = () => openSheet(false);
$("#btn-settings").addEventListener("click", () => { openSheet(true); openLayer("sheet"); });
$("#sheet-close").addEventListener("click", () => closeLayer("sheet"));
$("#sheet-scrim").addEventListener("click", () => closeLayer("sheet"));

// ---------- 사진 뷰어 ----------
// 처리 내역의 책 사진을 앱 안에서 연다(링크로 열면 앱 밖으로 나가 뒤로가기에 앱이 꺼졌다).
function openViewer(src) {
  const v = $("#viewer");
  $("#viewer-img").src = src;
  v.classList.remove("zoomed");
  v.hidden = false;
  openLayer("viewer");
}
layerClose.viewer = () => { $("#viewer").hidden = true; $("#viewer-img").removeAttribute("src"); };
$("#viewer-close").addEventListener("click", () => closeLayer("viewer"));
// 탭하면 2배 확대(스크롤로 이동), 다시 탭하면 원래대로
$("#viewer-img").addEventListener("click", (e) => {
  const v = $("#viewer"), box = $(".viewer-scroll");
  const zoom = !v.classList.contains("zoomed");
  const rx = e.offsetX / e.target.clientWidth, ry = e.offsetY / e.target.clientHeight;
  v.classList.toggle("zoomed", zoom);
  if (zoom) requestAnimationFrame(() => {
    box.scrollLeft = rx * box.scrollWidth - box.clientWidth / 2;
    box.scrollTop = ry * box.scrollHeight - box.clientHeight / 2;
  });
});

// ---------- 스플래시 ----------
// 최소 1.1초는 보여 주고, 첫 데이터를 받으면(최대 2.5초) 걷어낸다.
const splashMin = new Promise((r) => setTimeout(r, 1100));
const splashMax = new Promise((r) => setTimeout(r, 2500));
function hideSplash() {
  const s = $("#splash");
  if (!s) return;
  s.classList.add("out");
  setTimeout(() => { s.remove(); applyTheme(theme); }, 500);
}

// ---------- 시작 ----------

(() => {
  const d = new Date();
  $("#hello-date").textContent = `${d.getMonth() + 1}월 ${d.getDate()}일 ${"일월화수목금토"[d.getDay()]}요일`;
})();

// 서비스 워커 등록이 실패하면 대개 폰이 Caddy 루트 인증서를 신뢰하지 않는 경우다 → 앱 설치 불가
// 앱 설치 안내(인증서) 배너는 '폰에 앱으로 설치하려는' 경우에만 의미가 있다:
// Android 브라우저 + 아직 설치 안 된 상태 + 이 기기에서 닫은 적 없음. PC 웹에서는 띄우지 않는다.
function wantsInstallHelp() {
  const installed = matchMedia("(display-mode: standalone)").matches;
  return /Android/i.test(navigator.userAgent) && !installed && store("installHelpDismissed") !== "1";
}
async function showInstallHelp() {
  if (!wantsInstallHelp()) return;
  try {
    const { ca_url } = await api("/config");
    if (ca_url) { $("#ca-url").textContent = ca_url; $("#ca-help").hidden = false; }
  } catch {}
  $("#install-help").hidden = false;
}
if (!("serviceWorker" in navigator)) showInstallHelp();
else navigator.serviceWorker.register("sw.js").catch(showInstallHelp);
$("#install-help-close").addEventListener("click", () => {
  $("#install-help").hidden = true;
  store("installHelpDismissed", "1"); // 이 기기에서는 다시 띄우지 않는다
});
Promise.race([Promise.all([splashMin, loadStats()]), splashMax]).then(hideSplash);
