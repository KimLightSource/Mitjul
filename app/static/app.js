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
let revealed = false;
let busy = false;

// 학습 범위: "" 섞어서 / vocab 어휘만 / grammar 문법만. 기기별로 기억한다.
const KIND_NAME = { "": "단어가", vocab: "어휘가", grammar: "문법 공식이" };
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
  $("#test-answer-pos").textContent = it.pos;
  $("#test-answer").textContent = it.meaning;
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
      <span class="tl-detail">내 답 <b>${esc(i.answer || "—")}</b> · 정답 ${esc(i.meaning)}</span>
      ${i.graded_by === "ai" && i.reason ? `<span class="tl-reason">AI: ${esc(i.reason)}</span>` : ""}
    </li>`).join("");
  clearTimeout(testPoll);
  if (test.grading) testPoll = setTimeout(async () => {
    try { test = await api(`/tests/${test.id}`); renderTestResult(); } catch {}
  }, 4000);
}

layerClose.test = () => {
  clearTimeout(testPoll);
  speechSynthesis?.cancel();
  // 중간에 나가도 답한 문항은 채점되게 끝내기를 보낸다(안 푼 문항은 서버가 버린다)
  if (test && $("#test-result").hidden) api(`/tests/${test.id}/finish`, { method: "POST" }).catch(() => {});
  test = null;
  $("#test-screen").hidden = true;
  document.body.classList.remove("studying");
  loadStats();
};
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
    $("#study-empty").innerHTML = studyKind && s.total
      ? `지금 복습할 ${KIND_NAME[studyKind]} 없어요.<br>다른 범위를 골라 보세요.`
      : "지금 복습할 단어가 없어요.<br>추가 탭에서 책 사진을 올려 보세요.";
    $("#study-empty").hidden = s.due > 0 || !!pending;
    renderStartButton(s.due);
    $("#btn-test").disabled = !s.testable;
    $("#test-entry-sub").textContent = s.testable
      ? `암기 완료 ${s.testable}개 중 최대 ${Math.min(s.testable, TEST_SIZE)}문제 · 뜻 쓰기`
      : "암기 완료한 단어가 생기면 열려요";
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
    saveSession();
  }
  setStudyScreen("run");
  openLayer("study"); // 학습 중 뒤로가기 → 홈
  showCard();
}

const isGrammar = (w) => w.kind === "grammar";

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

// 공식에 자주 나오는 동사의 불규칙 변화 (had the employee submit → have 강조)
const IRREGULAR = {
  have: ["had", "has"], make: ["made"], get: ["got", "gotten"], see: ["saw", "seen"],
  hear: ["heard"], feel: ["felt"], keep: ["kept"], leave: ["left"], find: ["found"],
  be: ["is", "are", "was", "were", "been"], do: ["did", "does", "done"], go: ["went", "gone"],
};

// 문법 예문에서 공식의 영어 부분(the number of, make/have/let …)을 찾아 강조한다
function highlightGrammar(example, formula) {
  const terms = [];
  for (const p of parseFormula(formula)) {
    if (p.en) terms.push(p.main);
    for (const x of p.subs) if (!/[가-힣]/.test(x) && !x.startsWith("-")) terms.push(x, ...(IRREGULAR[x.toLowerCase()] || []));
  }
  const html = esc(example);
  if (!terms.length) return html;
  const alt = terms
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+"))
    .sort((a, b) => b.length - a.length)
    .map((t) => (/\s/.test(t) ? t : t + "[a-z]*"))
    .join("|");
  return html.replace(new RegExp("\\b(" + alt + ")", "gi"), "<mark>$1</mark>");
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
  $("#card-pos").textContent = w.pos;
  $("#card-meaning").textContent = w.meaning;
  $("#card-example").innerHTML = g ? highlightGrammar(w.example, w.word) : highlight(w.example, w.word);
  $("#card-example-ko").textContent = w.example_ko;
  $(".example").hidden = !w.example;
  $("#card-back").hidden = true;
  $("#card-hint").hidden = false;
  $("#progress-text").textContent = `${Math.min(pos + 1, queue.length)}/${queue.length}`;
  $("#progress-bar").style.width = (pos / queue.length) * 100 + "%";
  busy = false;
  // 문법 공식은 한국어 섞인 공식이라 읽지 않는다 (예문만 읽음)
  if ($("#opt-autospeak").checked && !g) speak(w.word);
}

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
  if (!firstTry.has(w.id)) firstTry.set(w.id, known);
  api(`/words/${w.id}/review`, { method: "POST", json: { known } }).catch((e) => toast("저장 실패: " + e.message));
  // 모르는 단어는 이번 세트 끝에 한 번 더 나온다 (산타 토익과 같은 방식).
  if (!known) queue.push({ ...w, seen: w.seen + 1, box: 0, again: true });
  // 넘긴 직후 상태를 저장 — 여기서 앱을 꺼도 다음 카드부터 이어진다
  if (pos + 1 < queue.length) saveSession(pos + 1);
  else clearSession();

  const card = $("#card");
  card.classList.add("fly");
  card.style.transform = `translateX(${known ? 130 : -130}vw) rotate(${known ? 25 : -25}deg)`;
  card.style.opacity = 0;
  speechSynthesis?.cancel();
  setTimeout(() => {
    pos++;
    if (pos >= queue.length) finishSet();
    else showCard();
  }, 260);
}

function finishSet() {
  const vals = [...firstTry.values()];
  const knew = vals.filter(Boolean).length;
  $("#done-text").textContent = `${vals.length}개 중 ${knew}개를 바로 알았어요. 모른 ${vals.length - knew}개는 곧 다시 나와요.`;
  $("#progress-bar").style.width = "100%";
  setStudyScreen("done");
}

// 스와이프 / 탭 처리
(() => {
  const card = $("#card");
  let sx = 0, sy = 0, dx = 0, dragging = false, down = false, armed = false;
  const THRESH = 90;

  card.addEventListener("pointerdown", (e) => {
    if (busy) return;
    down = true; dragging = false; armed = false;
    sx = e.clientX; sy = e.clientY; dx = 0;
    card.classList.remove("back");
  });
  card.addEventListener("pointermove", (e) => {
    if (!down) return;
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
    const target = e.target.closest("[data-speak]");
    const w = queue[pos];
    if (target?.dataset.speak === "example" && revealed) speak(w.example, 0.9);
    else if (!isGrammar(w) && (target?.dataset.speak === "word" || !revealed)) speak(w.word);
    reveal();
  });
  card.addEventListener("pointercancel", () => { down = false; if (dragging) snapBack(); });
})();

$("#btn-start").addEventListener("click", () => startSet());
$("#btn-again").addEventListener("click", () => startSet({ fresh: true }));
$("#btn-new-set").addEventListener("click", () => { clearSession(); startSet({ fresh: true }); });
layerClose.study = () => { speechSynthesis?.cancel(); setStudyScreen("home"); loadStats(); };
$("#btn-home").addEventListener("click", () => closeLayer("study"));
$("#btn-quit").addEventListener("click", () => closeLayer("study"));
$("#btn-know").addEventListener("click", () => decide(true));
$("#btn-dunno").addEventListener("click", () => decide(false));
document.addEventListener("keydown", (e) => {
  if ($("#study-run").hidden || currentView !== "study") return;
  if (e.key === "ArrowRight") decide(true);
  else if (e.key === "ArrowLeft") decide(false);
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
          <div class="wl-word ${isGrammar(w) ? "g" : ""}">${esc(w.word)}</div>
          <div class="wl-meaning"><span class="pos-s">${esc(w.pos)}</span>${esc(w.meaning)}</div>
        </div>
        ${level(w.box)}
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
        <label>뜻<input name="meaning" value="${esc(w.meaning)}"></label>
        <label>예문<textarea name="example" rows="2">${esc(w.example)}</textarea></label>
        <label>예문 해석<textarea name="example_ko" rows="2">${esc(w.example_ko)}</textarea></label>
        <div class="row">
          <button type="button" class="speak-btn icon-btn" aria-label="발음 듣기"><svg><use href="#i-speaker"/></svg></button>
          <button type="button" class="danger">삭제</button>
          <button type="submit" class="primary">저장</button>
        </div>
      </form>`);
    return;
  }
  if (e.target.closest(".speak-btn")) return speak(w.word);
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
async function showInstallHelp() {
  try {
    const { ca_url } = await api("/config");
    if (ca_url) { $("#ca-url").textContent = ca_url; $("#ca-help").hidden = false; }
  } catch {}
  $("#install-help").hidden = false;
}
if (!("serviceWorker" in navigator)) showInstallHelp();
else navigator.serviceWorker.register("sw.js").catch(showInstallHelp);
$("#install-help-close").addEventListener("click", () => ($("#install-help").hidden = true));
Promise.race([Promise.all([splashMin, loadStats()]), splashMax]).then(hideSplash);
