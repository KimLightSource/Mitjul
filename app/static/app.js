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

let voice = null;
function pickVoice() {
  const vs = speechSynthesis.getVoices().filter((v) => v.lang.replace("_", "-").startsWith("en-US"));
  voice = vs.find((v) => /google/i.test(v.name)) || vs[0] || null;
}
if ("speechSynthesis" in window) {
  pickVoice();
  speechSynthesis.addEventListener("voiceschanged", pickVoice);
}
function speak(text, rate = 0.95) {
  if (!("speechSynthesis" in window) || !text) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-US";
  if (voice) u.voice = voice;
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

async function loadStats() {
  try {
    const s = await api("/stats");
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
    $("#btn-start").disabled = s.due === 0;
  } catch (e) { toast("서버 연결 실패: " + e.message); }
}

function setStudyScreen(which) {
  // 카드를 넘기는 동안은 하단 탭바를 숨겨 화면을 카드에 다 쓴다
  document.body.classList.toggle("studying", which === "run");
  $("#study-home").hidden = which !== "home";
  $("#study-run").hidden = which !== "run";
  $("#study-done").hidden = which !== "done";
}

async function startSet() {
  try {
    queue = await api("/study?limit=" + SET_SIZE);
  } catch (e) { return toast("불러오기 실패: " + e.message); }
  if (!queue.length) {
    if (layers.includes("study")) return closeLayer("study");
    setStudyScreen("home");
    return loadStats();
  }
  pos = 0;
  firstTry = new Map();
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
  if (w.seen === 0) return ["새 단어", "new"];
  if (w.box === 0) return ["다시 보기", "again"];
  return ["복습 " + w.box + "단계", ""];
}

function highlight(example, word) {
  const html = esc(example);
  const words = word.trim().split(/\s+/);
  let re;
  if (words.length === 1) {
    // 활용형(allocated, allocating 등)까지 잡으려고 어간 앞부분으로 찾는다.
    const w = words[0];
    const stem = w.length > 4 ? w.slice(0, Math.max(4, w.length - 2)) : w;
    re = new RegExp("\\b(" + stem.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "[a-z]*)", "i");
  } else {
    re = new RegExp("(" + word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "i");
  }
  return html.replace(re, "<mark>$1</mark>");
}

function showCard() {
  const w = queue[pos];
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
  if (!known) queue.push({ ...w, seen: w.seen + 1, box: 0 });

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

$("#btn-start").addEventListener("click", startSet);
$("#btn-again").addEventListener("click", startSet);
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

async function resize(file, max = 2000) {
  // 폰 원본(수 MB)을 그대로 보내지 않는다. 긴 변 2000px JPEG이면 밑줄 판별에 충분하다.
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
