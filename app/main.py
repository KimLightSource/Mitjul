"""토익 단어장 "밑줄" — API + PWA 정적 파일 서버.

- 사용자용 API(/api/...)는 인증이 없다. 내부망 전용, 사용자 1명이 전제다. 인터넷에 그대로 노출하지 않는다.
- 워커용 API(/api/worker/...)는 X-Worker-Token 헤더가 필요하다. 워커(worker/vocab_worker.py)는
  claude CLI가 로그인된 다른 머신의 cron에서 돌며, 사진 → 단어 추출을 처리한다.

설정(환경변수):
  VOCAB_WORKER_TOKEN  필수. 워커와 공유하는 비밀 토큰
  VOCAB_DATA          DB·업로드 사진 위치 (기본 /data)
  VOCAB_CA_CERT       선택. 기기에 설치할 사설 CA의 공개 인증서 파일 → /ca.crt 로 제공
  VOCAB_CA_URL        선택. 그 인증서를 받을 수 있는 (HTTPS가 아닌) 주소 — 앱 설치 안내에 표시
"""

import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DATA = os.environ.get("VOCAB_DATA", "/data")
DB_PATH = os.path.join(DATA, "vocab.db")
UPLOADS = os.path.join(DATA, "uploads")
WORKER_TOKEN = os.environ["VOCAB_WORKER_TOKEN"]
STATIC = os.path.join(os.path.dirname(__file__), "static")
LC_FILE = os.path.join(os.path.dirname(__file__), "lc-words.tsv")

# 간격 반복 단계(box)별 다음 복습까지의 일수. '알아요'면 한 단계 올라가고 '모름'이면 0으로.
INTERVALS = [0, 1, 3, 7, 14, 30, 60, 120]
MASTERED_BOX = 3  # '알아요' 3번 연속(다음 복습 7일 뒤) 이상이면 암기 완료. 암기 테스트도 여기서 낸다.
# 암기 테스트 전용 간격: 테스트에서 n번 연속 맞히면 TEST_INTERVALS[n-1]일 뒤에 다시 시험에 낸다.
TEST_INTERVALS = [3, 7, 14, 30, 60, 120]
DAY = 86400
MAX_UPLOAD = 15 * 1024 * 1024
STALE_SECONDS = 15 * 60  # 처리중인 채로 이보다 오래된 작업은 워커가 죽은 것으로 본다
MAX_ATTEMPTS = 3

SCHEMA = """
create table if not exists words (
  id integer primary key,
  word text not null unique collate nocase,
  pos text not null default '',
  meaning text not null default '',
  example text not null default '',
  example_ko text not null default '',
  source text not null default 'manual',
  kind text not null default 'vocab',
  job_id integer,
  box integer not null default 0,
  due_at integer not null default 0,
  seen integer not null default 0,
  known integer not null default 0,
  starred integer not null default 0,
  related text not null default '',  -- JSON: 바꿔 말하기·헷갈리는 단어 (어휘만)
  created_at integer not null
);
create table if not exists jobs (
  id integer primary key,
  kind text not null,
  filename text,
  payload text,
  status text not null default 'pending',
  attempts integer not null default 0,
  error text,
  result text,
  created_at integer not null,
  updated_at integer not null
);
create table if not exists activity (
  day text primary key,
  reviews integer not null default 0
);
-- 암기 테스트: 암기 완료 어휘에서 낸 시험과 문항별 답·판정
create table if not exists tests (
  id integer primary key,
  created_at integer not null,
  finished_at integer
);
create table if not exists test_answers (
  id integer primary key,
  test_id integer not null,
  word_id integer not null,
  answer text,
  verdict text,          -- null(미응답) / correct / wrong / pending(AI 채점 대기) / error(AI 채점 실패)
  reason text,
  graded_by text         -- local / ai / skip
);
-- 진행 중인 학습 세트(한 개뿐). 앱을 끄거나 나가도 이어 하도록 카드를 넘길 때마다 저장된다.
create table if not exists study_session (
  id integer primary key check (id = 1),
  data text not null,
  updated_at integer not null
);
-- LC 단어 체크: 빈출 목록(lc-words.tsv)에서 낸 단어에 답한 기록. 알면 다시 안 내고, 모르면 단어장에 넣는다.
create table if not exists lc_check (
  word text primary key collate nocase,
  known integer not null,
  queued integer not null default 0,  -- 모르는 단어를 AI 채우기 작업으로 넘겼는지
  day text not null,                  -- 학습일(KST 06:00 기준) — 하루 몇 개 했는지 센다
  at integer not null
);
-- 파트 5 문제: 카드에서 나온 빈칸 4지선다. 문법 카드의 출제 유형(related.exam)을 그대로 옮긴 것(card)과
-- AI가 단어장 카드로 새로 만든 것(ai). 맞힌 문제는 다시 안 내고, 틀린 문제는 다음 학습일부터 다시 낸다.
create table if not exists quiz_items (
  id integer primary key,
  word_id integer,                     -- 근거 카드 (지워져도 문제는 남는다)
  kind text not null,                  -- grammar / vocab
  type text not null default '',       -- 출제 유형 (품사 자리, 전치사 vs 접속사, 어휘 …)
  q text not null unique,              -- 빈칸(_____) 문장
  choices text not null,               -- JSON 배열, 보기 4개
  answer text not null,
  explain text not null default '',    -- 한국어 해설
  q_ko text not null default '',       -- 문장 해석
  detail text not null default '',     -- JSON: 문장 구조·보기별 이유·문장 속 단어 (자세한 해설)
  source text not null default 'ai',   -- ai / card (card는 2026-10-21에 출제를 멈췄다)
  created_at integer not null
);
create table if not exists quiz_answers (
  id integer primary key,
  item_id integer not null,
  choice text not null,
  correct integer not null,
  ms integer,                          -- 푸는 데 걸린 시간
  at integer not null
);
create index if not exists words_due on words(due_at);
create index if not exists jobs_status on jobs(status);
"""

WORD_FIELDS = ("word", "pos", "meaning", "example", "example_ko", "kind")
KINDS = ("vocab", "grammar")  # 어휘(파란 펜) / 문법 공식(빨간 펜)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> int:
    return int(time.time())


KST = 9 * 3600


def kst_day(t: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t + KST))


# 복습 날짜는 24시간이 아니라 '학습일' 단위다. 하루는 KST 새벽 6시에 바뀐다 —
# 어제 오후에 본 1일 뒤 카드도 오늘 아침에 바로 나오고, 새벽 2시에 본 건 전날 공부로 친다.
DAY_START = 6 * 3600


def study_day_start(t: int) -> int:
    """t가 속한 학습일이 시작된 시각(KST 06:00)."""
    return (t + KST - DAY_START) // DAY * DAY - KST + DAY_START


def due_after(days: int) -> int:
    return study_day_start(now()) + days * DAY


def streak(c) -> int:
    """오늘(없으면 어제)부터 거꾸로 하루도 안 빠지고 학습한 날 수."""
    days = {r[0] for r in c.execute("select day from activity where reviews > 0")}
    t = now()
    if kst_day(t) not in days:
        t -= 86400
    n = 0
    while kst_day(t) in days:
        n += 1
        t -= 86400
    return n


# ---------- LC 빈출 목록 ----------
# app/lc-words.tsv(단어<TAB>뜻<TAB>분류). 'LC 단어 체크'가 하루 LC_DAILY개씩 낸다 — 이미 단어장에 있거나
# 전에 답한 단어는 뺀다. 분류가 섞이게 목록 순서가 아니라 단어 해시 순으로 낸다(늘 같은 순서).
LC_DAILY = 20


def load_lc() -> list[dict]:
    out, seen = [], set()
    with open(LC_FILE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            word, meaning, part = (line.rstrip("\n").split("\t") + ["", ""])[:3]
            word = " ".join(word.split())
            if word and word.lower() not in seen:
                seen.add(word.lower())
                out.append({"word": word, "meaning": meaning.strip(), "part": part.strip()})
    return sorted(out, key=lambda w: hashlib.sha1(w["word"].lower().encode()).hexdigest())


LC_WORDS = load_lc()


def mark_lc(c, words=None):
    """LC 빈출 표시: 목록에 있는 단어와, LC 체크에서 모른다고 해 넣은 단어(words)."""
    names = [w["word"].lower() for w in LC_WORDS] + [w.lower() for w in words or []]
    c.execute("update words set lc = 1 where lc = 0 and lower(word) in (select value from json_each(?))",
              (json.dumps(names),))


def study_day(t: int) -> str:
    return kst_day(study_day_start(t))


os.makedirs(UPLOADS, exist_ok=True)
with db() as c:
    c.execute("pragma journal_mode=wal")
    c.executescript(SCHEMA)
    # 2026-09-30 문법 공식 카드 추가 전에 만들어진 DB 마이그레이션
    if "kind" not in {r[1] for r in c.execute("pragma table_info(words)")}:
        c.execute("alter table words add column kind text not null default 'vocab'")
    # 2026-10-05 북마크
    if "starred" not in {r[1] for r in c.execute("pragma table_info(words)")}:
        c.execute("alter table words add column starred integer not null default 0")
    # 2026-10-05 바꿔 말하기(패러프레이징)·헷갈리는 단어
    if "related" not in {r[1] for r in c.execute("pragma table_info(words)")}:
        c.execute("alter table words add column related text not null default ''")
    # 2026-10-21 파트 5 자세한 해설. 카드 예문으로 만든 문제(card)와 해설이 짧은 옛 AI 문제는 안 푼 것만 지운다
    # (푼 기록이 있는 문제는 남기되 출제하지 않는다 — QUIZ_OPEN).
    if "detail" not in {r[1] for r in c.execute("pragma table_info(quiz_items)")}:
        c.execute("alter table quiz_items add column detail text not null default ''")
    c.execute("delete from quiz_items where (source = 'card' or detail = '') "
              "and id not in (select item_id from quiz_answers)")
    # 2026-10-15 LC(듣기) 빈출 표시
    if "lc" not in {r[1] for r in c.execute("pragma table_info(words)")}:
        c.execute("alter table words add column lc integer not null default 0")
    # 2026-10-14 암기 테스트 간격 반복 — 테스트 단계(test_box)·다음 시험일(test_due_at).
    # 처음 추가할 때 지난 테스트 기록에서 채운다 — 최근에 맞혔으면(틀린 뒤로 연속 정답) 몇 번이든
    # "한 번 맞힘"(test_box 1, 마지막 정답일 + 3일)으로 친다(2026-10-14 사용자 결정).
    # 2026-10-23 토익 출제 중요도(0 = 아직 안 매김, 1 가끔 · 2 빈출 · 3 필수). 기존 카드는 rank 작업으로 채운다.
    if "freq" not in {r[1] for r in c.execute("pragma table_info(words)")}:
        c.execute("alter table words add column freq integer not null default 0")
    if "test_box" not in {r[1] for r in c.execute("pragma table_info(words)")}:
        c.execute("alter table words add column test_box integer not null default 0")
        c.execute("alter table words add column test_due_at integer not null default 0")
        hist = {}
        for r in c.execute(
            "select a.word_id, a.verdict, t.created_at from test_answers a join tests t on t.id = a.test_id "
            "where a.verdict in ('correct', 'wrong') order by a.id"
        ):
            run, last = hist.get(r[0], (0, 0))
            if r[1] != "correct":
                hist[r[0]] = (0, 0)
            elif not run or study_day_start(r[2]) != study_day_start(last):  # 같은 날 두 번 맞힌 건 한 번으로
                hist[r[0]] = (run + 1, r[2])
        for wid, (run, last) in hist.items():
            if run:
                due = study_day_start(last) + TEST_INTERVALS[0] * DAY
                c.execute("update words set test_box = 1, test_due_at = ? where id = ?", (due, wid))
    # 2026-10-02 복습 시각을 24시간 단위에서 학습일 단위로 — 예전 due_at을 그날 06:00으로 내린다.
    # 이미 맞춰진 값은 그대로라 매번 실행해도 안전하다.
    c.execute(
        "update words set due_at = (due_at + ?) / ? * ? - ? where (due_at + ?) % ? != 0",
        (KST - DAY_START, DAY, DAY, KST - DAY_START, KST - DAY_START, DAY),
    )
    mark_lc(c)  # 목록에 새로 넣은 단어가 이미 단어장에 있으면 표시

app = FastAPI(title="vocab", docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_stale_static(request, call_next):
    """화면 파일(app.js·style.css·index.html)은 매번 서버에 바뀌었는지 묻게 한다(바뀌지 않았으면 304).
    Cache-Control이 없으면 Chrome이 Last-Modified로 캐시 기간을 추측해, 배포 후에도 옛 app.js로
    새 데이터를 그리는 일이 생겼다(2026-10-17 — 새 공식이 옛 '+' 칸 화면으로 보였다)."""
    res = await call_next(request)
    if not request.url.path.startswith("/api/"):
        res.headers.setdefault("Cache-Control", "no-cache")
    return res


# ---------- 모델 ----------

class WordIn(BaseModel):
    word: str
    pos: str = ""
    meaning: str = ""
    example: str = ""
    example_ko: str = ""
    kind: str = "vocab"
    # AI가 어휘 카드와 함께 만든다. 사용자 편집(PUT)에서는 쓰지 않는다.
    freq: int = 0  # 토익 출제 중요도 1~3 (AI 카드만)
    paraphrases: list[dict] = []
    confusables: list[dict] = []
    terms: list[dict] = []  # 문법 공식 속 영어 단어의 뜻 (문법만)
    exam: dict | None = None  # 문법 공식의 토익 출제 유형 (문법만)


class RelatedIn(BaseModel):
    """기존 카드 채우기(job kind 'enrich')의 결과 한 줄. 어휘는 바꿔 말하기·헷갈리는 단어, 문법은 terms."""
    id: int
    paraphrases: list[dict] = []
    confusables: list[dict] = []
    terms: list[dict] = []


class FreqIn(BaseModel):
    """중요도 매기기(job kind 'rank') 결과 한 줄."""
    id: int
    freq: int


class ReviewIn(BaseModel):
    known: bool


class StarIn(BaseModel):
    starred: bool


class UndoIn(BaseModel):
    """review가 돌려준 `undo` 그대로 — 넘기기 전 상태와 그때 센 날짜."""
    box: int
    due_at: int
    known: bool
    day: str


class AnswerIn(BaseModel):
    answer: str = ""


class GradeIn(BaseModel):
    aid: int
    correct: bool
    reason: str = ""


class SessionItem(BaseModel):
    id: int
    again: bool = False  # '모름'으로 세트 끝에 다시 붙은 카드


class SessionIn(BaseModel):
    kind: str = ""
    pos: int = 0
    queue: list[SessionItem]
    first: dict[str, bool] = {}  # 카드 id → 처음 넘길 때 알았는지


class ResultIn(BaseModel):
    words: list[WordIn] = []
    error: str | None = None
    note: str | None = None
    # 작업 탓이 아닌 실패(워커의 claude 인증 만료 등): 시도 횟수를 되돌려 대기열에 남긴다.
    release: bool = False
    grades: list[GradeIn] = []  # 암기 테스트 AI 채점(job kind 'grade') 결과
    related: list[RelatedIn] = []  # 기존 어휘 채우기(job kind 'enrich') 결과
    quiz: list[dict] = []  # 파트 5 문제 만들기(job kind 'quiz') 결과
    freq: list[FreqIn] = []  # 중요도 매기기(job kind 'rank') 결과


def clean(w: WordIn) -> dict:
    d = {k: (getattr(w, k) or "").strip() for k in WORD_FIELDS}
    d["word"] = " ".join(d["word"].split())
    if d["kind"] not in KINDS:
        d["kind"] = "vocab"
    return d


# ---------- 바꿔 말하기 · 헷갈리는 단어 ----------
# words.related = {"paraphrases": [{word, meaning}], "confusables": [{word, meaning, tip}]} (JSON 문자열, 어휘)
#               = {"terms": [{word, meaning}], "exam": {type, q, choices, answer}} (문법 공식 — 공식 속 영어 단어의
#                 뜻과 토익 출제 유형, 2026-10-17)
# 카드 뒷면에 참고용으로만 보여 준다. 헷갈리는 단어를 카드로 만들어 짝으로 묶던 방식은 쉬운 파생어 카드가
# 수백 개 생겨서 하루 만에 뺐다(2026-10-05, 사용자 결정).
MAX_PARAPHRASES, MAX_CONFUSABLES, MAX_TERMS = 3, 2, 24


def clean_exam(e):
    """출제 유형 {type, q, choices, answer} — 빈칸(___)이 있고 정답이 보기 중 하나여야 한다. 아니면 None."""
    if not isinstance(e, dict):
        return None
    choices = [" ".join(str(c).split())[:60] for c in e.get("choices") or [] if str(c).strip()]
    ex = {"type": " ".join(str(e.get("type") or "").split())[:40], "q": " ".join(str(e.get("q") or "").split())[:300],
          "choices": choices, "answer": " ".join(str(e.get("answer") or "").split())}
    if "___" not in ex["q"] or not 2 <= len(choices) <= 5 or ex["answer"] not in choices:
        return None
    return ex


def related_json(paraphrases=(), confusables=(), terms=(), exam=None) -> str:
    def items(xs, keys, limit):
        out = []
        for x in xs if isinstance(xs, list) else []:
            if not isinstance(x, dict):
                continue
            d = {k: " ".join(str(x.get(k) or "").split())[:80] for k in keys}
            if d["word"] and d["word"].lower() not in {o["word"].lower() for o in out}:
                out.append(d)
        return out[:limit]
    p = items(paraphrases, ("word", "meaning"), MAX_PARAPHRASES)
    cf = items(confusables, ("word", "meaning", "tip"), MAX_CONFUSABLES)
    tm = [x for x in items(terms, ("word", "meaning"), MAX_TERMS) if x["meaning"]]
    ex = clean_exam(exam)
    if tm or ex:
        return json.dumps({**({"terms": tm} if tm else {}), **({"exam": ex} if ex else {})}, ensure_ascii=False)
    return json.dumps({"paraphrases": p, "confusables": cf}, ensure_ascii=False) if p or cf else ""


def has_english_word(formula: str) -> bool:
    """공식에 뜻을 달 영어 단어가 있는지 (p.p. · -ing · be · the 같은 기호·기초 단어만 있으면 없음)."""
    rest = re.sub(r"p\.p\.|-ing|\b(?:be|the)\b", "", formula, flags=re.I)
    return bool(re.search(r"(?<![\w.-])[A-Za-z]{2,}(?![가-힣])", rest))


def job_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["result"] = json.loads(d["result"]) if d["result"] else None
    d["payload"] = json.loads(d["payload"]) if d["payload"] else None
    return d


# ---------- 사용자 API ----------

@app.get("/api/health")
def health():
    with db() as c:
        c.execute("select 1")
    return {"ok": True}


# '자주 모름': 학습 카드에서 '모름'을 MISSED_MIN번 이상 누른 카드(2026-10-23, 사용자 요청 — 여러 번 봤는데도
# 계속 모르는 단어만 따로 모아 보려고). '모름' 횟수는 따로 세지 않고 seen(넘긴 횟수) - known('알아요' 횟수)이다.
MISSED_MIN = 3
MISSES = "(seen - known)"


def kind_filter(kind):
    """학습 범위: None(섞어서) / vocab(어휘만) / grammar(문법만) / starred(북마크만) / lc(LC 빈출만) /
    missed(자주 모름). SQL 조각과 인자, 그리고 복습일을 따질지를 돌려준다. 북마크·자주 모름은 복습일과 상관없이
    전부 낸다 — 따로 모아 다시 보려는 것이라, 복습일이 안 됐다고 빠지면 학습이 비어 버린다."""
    if kind is None:
        return "", (), True
    if kind == "starred":
        return " and starred = 1", (), False
    if kind == "missed":
        return f" and {MISSES} >= ?", (MISSED_MIN,), False
    if kind == "lc":
        return " and lc = 1", (), True
    if kind not in KINDS:
        raise HTTPException(400, "kind는 vocab, grammar, starred, lc, missed 중 하나")
    return " and kind = ?", (kind,), True


@app.get("/api/stats")
def stats(kind: str | None = None):
    t = now()
    kf, ka, by_due = kind_filter(kind)
    with db() as c:
        rank_refill(c)  # 중요도가 없는 카드(직접 넣은 카드 등)가 있으면 AI 작업으로 넘긴다
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        return {
            "total": q("select count(*) from words"),
            "due": q("select count(*) from words where meaning != '' and due_at <= ?" + kf, t if by_due else 2**62, *ka),
            # 다음 세트에 들어갈 새 카드·복습 수 (세트 구성은 study와 같은 mix_counts)
            "mix": dict(zip(("new", "review"), mix_counts(20, *due_counts(c, kf, ka)))) if by_due else None,
            "starred": q("select count(*) from words where starred = 1"),
            "missed": q(f"select count(*) from words where meaning != '' and {MISSES} >= ?", MISSED_MIN),
            "new": q("select count(*) from words where seen = 0"),
            "mastered": q("select count(*) from words where box >= ?", MASTERED_BOX),
            "testable": q(f"select count(*) from words where {TESTABLE}", MASTERED_BOX, t),
            # 맞혀서 다음 시험일을 기다리는 단어 수와 그중 가장 이른 시험일
            "test_resting": q(f"select count(*) from words where {MASTERED_VOCAB} and test_due_at > ?", MASTERED_BOX, t),
            "test_next": q(f"select min(test_due_at) from words where {MASTERED_VOCAB} and test_due_at > ?", MASTERED_BOX, t),
            "tests": q("select count(distinct test_id) from test_answers where verdict is not null"),
            "tests_grading": q("select count(distinct a.test_id) from test_answers a join tests t on t.id = a.test_id "
                               "where a.verdict = 'pending' and t.finished_at is not null"),
            "jobs_active": q("select count(*) from jobs where status in ('pending','processing')"),
            "today": q("select coalesce(max(reviews),0) from activity where day = ?", kst_day(t)),
            "lc": q("select count(*) from words where lc = 1"),
            "lc_today": q("select count(*) from lc_check where day = ?", study_day(t)),
            "lc_left": len(lc_candidates(c)),
            "lc_daily": LC_DAILY,
            **quiz_stats(c),
            "streak": streak(c),
        }


# ---------- 세트 구성 ----------
# 한 세트는 복습과 새 카드를 지금 쌓인 비율대로 섞는다(2026-10-21, 사용자 요청 — 하루 몇 장이라는 한도 대신,
# 짬짬이 몇 세트를 하든 세트마다 알아서 섞이게). 새 카드 몫은 쌓인 비율을 따르되 NEW_SHARE_MIN~MAX로 묶는다 —
# 밀린 복습이 많아도 새 단어가 멈추지 않고, 사진을 많이 올려 새 카드가 쌓여도 복습이 밀려나지 않게.
NEW_SHARE_MIN, NEW_SHARE_MAX = 0.15, 0.4
# 복습은 잊었을 위험이 큰 것부터: 복습일이 지난 시간 ÷ 그 단계의 간격. 1일 간격 카드가 3일 밀린 것(3)이 14일 간격
# 카드가 3일 밀린 것(0.2)보다 먼저 나온다. '모름'으로 0단계가 된 카드는 간격을 0.5일로 쳐서 앞쪽에 온다.
RISK = ("(? - due_at) * 1.0 / (case box " + " ".join(f"when {b} then {max(d, 0.5) * DAY}" for b, d in enumerate(INTERVALS))
        + f" else {INTERVALS[-1] * DAY} end)")


def mix_counts(limit: int, n_new: int, n_rev: int) -> tuple[int, int]:
    """세트에 넣을 (새 카드 수, 복습 수). 한쪽이 없으면 다른 쪽으로 다 채운다."""
    if not n_new or not n_rev:
        return min(n_new, limit), min(n_rev, limit)
    share = min(max(n_new / (n_new + n_rev), NEW_SHARE_MIN), NEW_SHARE_MAX)
    k = min(n_new, max(1, round(limit * share)))
    r = min(n_rev, limit - k)
    return min(n_new, limit - r), r


def spread(reviews: list, news: list) -> list:
    """새 카드를 복습 사이에 고르게 끼운다(앞에 몰리지 않게)."""
    out, bi, n = [], 0, len(reviews) + len(news)
    it = iter(reviews)
    for i in range(n):
        if bi < len(news) and (bi < round((i + 1) * len(news) / n) or len(out) - bi >= len(reviews)):
            out.append(news[bi])
            bi += 1
        else:
            out.append(next(it))
    return out


def due_counts(c, kf="", ka=()) -> tuple[int, int]:
    """지금 낼 수 있는 (새 카드, 복습) 수."""
    r = c.execute("select coalesce(sum(seen = 0), 0), coalesce(sum(seen > 0), 0) from words "
                  "where meaning != '' and due_at <= ?" + kf, (now(), *ka)).fetchone()
    return r[0], r[1]


@app.get("/api/study")
def study(limit: int = 20, kind: str | None = None):
    limit = max(1, min(limit, 100))
    kf, ka, by_due = kind_filter(kind)
    t = now()
    with db() as c:
        if not by_due:  # 북마크: 복습일과 상관없이 전부, 복습일 순
            # 자주 모름: 복습일이 된 것부터 '모름'이 많은 순, 그다음 복습일 순 — '알아요'를 누르면 복습일이 밀려
            # 뒤로 가고, 다음 세트에 아직 안 본 카드가 나온다.
            order = f"due_at > {t}, {MISSES} desc, {FREQ_ORDER} desc, due_at, id" if kind == "missed" else "due_at, id"
            rows = c.execute("select * from words where meaning != ''" + kf + f" order by {order} limit ?",
                             (*ka, limit)).fetchall()
            return [dict(r) for r in rows]
        k, n = mix_counts(limit, *due_counts(c, kf, ka))
        base = "select * from words where meaning != '' and due_at <= ?" + kf
        reviews = c.execute(base + f" and seen > 0 order by {RISK} desc, id limit ?", (t, *ka, t, n)).fetchall()
        # 새 카드는 북마크 → 토익 중요도 높은 것 → 들어온 순
        news = c.execute(base + f" and seen = 0 order by starred desc, {FREQ_ORDER} desc, id limit ?", (t, *ka, k)).fetchall()
    return [dict(r) for r in spread(reviews, news)]


# ---------- 암기 테스트 ----------

_PAREN = re.compile(r"\(.*?\)|\[.*?\]")
_JUNK = re.compile(r"[\s~·.,!?'\"…\-]")
_SUFFIXES = ("시키다", "하다", "되다", "하는", "되는", "스럽다", "스러운", "적인", "적으로", "하게", "롭다", "한", "된", "히", "의")


def _norm_ko(s: str) -> str:
    s = _JUNK.sub("", _PAREN.sub("", s or ""))
    for suf in _SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf) + 1:
            return s[: -len(suf)]
    return s


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} or {s}


def local_grade(answer: str, meaning: str) -> str:
    """즉석 채점. 뜻 목록 중 하나와 거의 같으면 correct, 빈 답은 wrong, 나머지는 pending(AI가 판단)."""
    a = _norm_ko(answer)
    if not a:
        return "wrong"
    # 뜻은 품사별로 줄이 나뉠 수 있다("떠나다\n[명사] 휴가"). [명사] 같은 품사 표시는 _norm_ko가 지운다.
    for sense in re.split(r"[,;/\n]|또는", meaning or ""):
        m = _norm_ko(sense)
        if not m:
            continue
        if a == m or (len(a) >= 2 and len(m) >= 2 and (a in m or m in a)):
            return "correct"
        A, B = _bigrams(a), _bigrams(m)
        if 2 * len(A & B) / (len(A) + len(B)) >= 0.6:
            return "correct"
    return "pending"


def demote(c, word_id: int):
    """테스트에서 틀린 단어는 '모름'과 같이 0단계로 — 다시 학습 카드로 나오고, 테스트 단계도 처음부터."""
    c.execute("update words set box = 0, due_at = ?, test_box = 0, test_due_at = 0 where id = ?", (now(), word_id))


def test_pass(c, word_id: int):
    """테스트에서 맞힌 단어: 테스트 간격(3·7·14·30…일)만큼 시험에서 쉬고, 카드 복습에서도 '알아요'로 친다
    (단계 +1, 복습일은 새 단계 간격 — 이미 더 먼 복습일이면 그대로)."""
    w = c.execute("select box, due_at, test_box from words where id = ?", (word_id,)).fetchone()
    if not w:
        return
    tb = w["test_box"] + 1
    box = min(w["box"] + 1, len(INTERVALS) - 1)
    c.execute(
        "update words set test_box = ?, test_due_at = ?, box = ?, due_at = ? where id = ?",
        (tb, due_after(TEST_INTERVALS[min(tb, len(TEST_INTERVALS)) - 1]), box,
         max(w["due_at"], due_after(INTERVALS[box])), word_id),
    )


def test_view(c, test_id: int) -> dict:
    t = c.execute("select * from tests where id = ?", (test_id,)).fetchone()
    if not t:
        raise HTTPException(404)
    rows = c.execute(
        "select a.id as aid, a.answer, a.verdict, a.reason, a.graded_by, w.id as word_id, w.word, w.pos, "
        "w.meaning, w.example, w.example_ko from test_answers a join words w on w.id = a.word_id "
        "where a.test_id = ? order by a.id",
        (test_id,),
    ).fetchall()
    items = [dict(r) for r in rows]
    return {"id": test_id, "finished": bool(t["finished_at"]), "items": items,
            "grading": any(i["verdict"] == "pending" for i in items) and bool(t["finished_at"])}


# 시험에 낼 수 있는 단어: 암기 완료 어휘 중 다음 시험일(test_due_at)이 된 것.
# AI 채점을 기다리는 답은 뺀다(정답이면 다음 시험일이 밀리고, 오답이면 0단계로 내려가 빠진다).
PENDING_WORDS = "select word_id from test_answers where verdict = 'pending'"
MASTERED_VOCAB = "kind = 'vocab' and meaning != '' and box >= ?"
TESTABLE = f"{MASTERED_VOCAB} and test_due_at <= ? and id not in ({PENDING_WORDS})"


@app.get("/api/tests")
def list_tests(limit: int = 30):
    """채점 기록: 답한 문항이 있는 시험을 최근 순으로. 채점 중인 시험도 여기서 다시 연다."""
    limit = max(1, min(limit, 100))
    with db() as c:
        rows = c.execute(
            "select t.id, t.created_at, t.finished_at, count(*) as total, "
            "sum(a.verdict = 'correct') as correct, sum(a.verdict = 'wrong') as wrong, "
            "sum(a.verdict = 'pending') as pending, sum(a.verdict = 'error') as error "
            "from tests t join test_answers a on a.test_id = t.id and a.verdict is not null "
            "group by t.id order by t.id desc limit ?",
            (limit,),
        ).fetchall()
    return [{**dict(r), "grading": bool(r["finished_at"]) and r["pending"] > 0} for r in rows]


@app.post("/api/tests")
def create_test(limit: int = 20):
    limit = max(1, min(limit, 50))
    t = now()
    with db() as c:
        words = c.execute(
            f"select id from words where {TESTABLE} order by random() limit ?",
            (MASTERED_BOX, t, limit),
        ).fetchall()
        if not words:
            if c.execute(f"select 1 from words where {MASTERED_VOCAB} limit 1", (MASTERED_BOX,)).fetchone():
                raise HTTPException(409, "맞힌 단어는 쉬는 중이에요 — 다음 시험일에 다시 나와요")
            raise HTTPException(409, "암기 완료된 어휘가 없어요")
        tid = c.execute("insert into tests (created_at) values (?)", (t,)).lastrowid
        c.executemany("insert into test_answers (test_id, word_id) values (?, ?)", [(tid, w["id"]) for w in words])
        return test_view(c, tid)


@app.get("/api/tests/{test_id}")
def get_test(test_id: int):
    with db() as c:
        return test_view(c, test_id)


@app.post("/api/tests/{test_id}/answers/{aid}")
def answer_test(test_id: int, aid: int, body: AnswerIn):
    with db() as c:
        r = c.execute(
            "select a.word_id, w.meaning from test_answers a join words w on w.id = a.word_id "
            "where a.id = ? and a.test_id = ?",
            (aid, test_id),
        ).fetchone()
        if not r:
            raise HTTPException(404)
        verdict = local_grade(body.answer, r["meaning"])
        by = "skip" if not body.answer.strip() else ("local" if verdict == "correct" else None)
        c.execute(
            "update test_answers set answer = ?, verdict = ?, graded_by = ?, reason = null where id = ?",
            (body.answer.strip(), verdict, by, aid),
        )
        if verdict == "wrong":
            demote(c, r["word_id"])
        elif verdict == "correct":
            test_pass(c, r["word_id"])
    return {"verdict": verdict}


@app.post("/api/tests/{test_id}/finish")
def finish_test(test_id: int):
    """끝내기: 즉석 채점이 못 정한 답을 모아 AI 채점 작업 하나로 넘긴다."""
    t = now()
    with db() as c:
        c.execute("update tests set finished_at = coalesce(finished_at, ?) where id = ?", (t, test_id))
        rows = c.execute(
            "select a.id as aid, a.answer, w.word, w.pos, w.meaning from test_answers a "
            "join words w on w.id = a.word_id where a.test_id = ? and a.verdict = 'pending'",
            (test_id,),
        ).fetchall()
        # 끝까지 안 푼 문항은 채점에서 뺀다
        c.execute("delete from test_answers where test_id = ? and verdict is null", (test_id,))
        if not c.execute("select 1 from test_answers where test_id = ? limit 1", (test_id,)).fetchone():
            # 한 문제도 안 풀고 나간 시험은 기록에 남기지 않는다
            c.execute("delete from tests where id = ?", (test_id,))
            return {"id": test_id, "finished": True, "items": [], "grading": False}
        if rows:
            payload = {"test_id": test_id, "items": [dict(r) for r in rows]}
            c.execute(
                "insert into jobs (kind, payload, created_at, updated_at) values ('grade', ?, ?, ?)",
                (json.dumps(payload, ensure_ascii=False), t, t),
            )
        return test_view(c, test_id)


# ---------- LC 단어 체크 ----------
# 빈출 목록에서 하루 LC_DAILY개를 카드로 내고 '알아요/모름'만 고른다. 아는 단어는 기록만 남기고(다시 안 냄),
# 모르는 단어는 끝낼 때 한꺼번에 AI 채우기 작업(text, lc 표시)으로 넘겨 LC 빈출 카드로 단어장에 넣는다.

class LcCheckIn(BaseModel):
    word: str
    known: bool


def lc_candidates(c) -> list[dict]:
    """아직 안 낸 LC 단어: 체크한 적 없고 단어장에도 없는 것(목록 순서 그대로)."""
    done = {r[0].lower() for r in c.execute("select word from lc_check")}
    done |= {r[0].lower() for r in c.execute("select word from words")}
    return [w for w in LC_WORDS if w["word"].lower() not in done]


@app.get("/api/lc")
def lc_next(limit: int = LC_DAILY):
    limit = max(1, min(limit, 50))
    with db() as c:
        left = lc_candidates(c)
        today = c.execute("select count(*) from lc_check where day = ?", (study_day(now()),)).fetchone()[0]
    return {"items": left[:limit], "left": len(left), "today": today, "daily": LC_DAILY}


@app.post("/api/lc/check")
def lc_check(body: LcCheckIn):
    word = " ".join(body.word.split())
    if word.lower() not in {w["word"].lower() for w in LC_WORDS}:
        raise HTTPException(404, "LC 목록에 없는 단어")
    with db() as c:
        c.execute(
            "insert into lc_check (word, known, day, at) values (?, ?, ?, ?) "
            "on conflict(word) do update set known = excluded.known, day = excluded.day, at = excluded.at "
            "where queued = 0",
            (word, int(body.known), study_day(now()), now()),
        )
    return {"ok": True}


@app.delete("/api/lc/check")
def lc_uncheck(word: str):
    """되돌리기 — 아직 단어장으로 안 넘긴 답만."""
    with db() as c:
        n = c.execute("delete from lc_check where word = ? and queued = 0", (" ".join(word.split()),)).rowcount
    if not n:
        raise HTTPException(409, "이미 단어장으로 넘긴 단어")
    return {"ok": True}


@app.post("/api/lc/finish")
def lc_finish():
    """모른다고 한 단어(아직 안 넘긴 것)를 AI 채우기 작업 하나로 넘긴다. 중간에 나가도 부른다."""
    t = now()
    with db() as c:
        words = [r[0] for r in c.execute("select word from lc_check where known = 0 and queued = 0 order by at")]
        if not words:
            return {"words": [], "job_id": None}
        c.execute("update lc_check set queued = 1 where known = 0 and queued = 0")
        job = c.execute(
            "insert into jobs (kind,payload,created_at,updated_at) values ('text',?,?,?)",
            (json.dumps({"words": words, "lc": True}, ensure_ascii=False), t, t),
        ).lastrowid
    return {"words": words, "job_id": job}


# ---------- 파트 5 문제 ----------
# 단어장 카드로 AI가 새로 만든 토익 파트 5(빈칸 4지선다) 문제를 QUIZ_SIZE개씩 낸다(job kind 'quiz', 카드마다 한 문제).
# 카드의 예문·출제 유형 예시는 이미 카드 뒷면에서 본 문장이라 문제로 쓰지 않는다(2026-10-21, 사용자 요청 — 처음엔
# 문법 카드의 출제 유형을 그대로 옮긴 card 문제도 냈다). 안 푼 문제가 QUIZ_REFILL개보다 적으면 세트를 열 때
# 작업을 최대 QUIZ_JOBS개까지 넣는다. 해설은 자세하게 — 해석, 문장 구조, 정답 근거, 보기마다 이유, 문장 속 단어.
# 맞힌 문제는 다시 안 낸다. 틀린 문제는 다음 학습일부터 다시 나오고, 근거 카드는 바로 복습할 카드가 된다.
QUIZ_SIZE = 10
QUIZ_REFILL = 30
QUIZ_BATCH = 10
QUIZ_JOBS = 3
# 낼 수 있는 문제: AI 문제 중 맞힌 적 없고 오늘(학습일) 푼 적도 없는 것. 인자: 오늘 학습일 시작 시각.
QUIZ_OPEN = ("q.source = 'ai' and not exists (select 1 from quiz_answers a where a.item_id = q.id "
             "and (a.correct = 1 or a.at >= ?))")
QUIZ_ACTIVE = "select payload from jobs where kind = 'quiz' and status in ('pending','processing')"


class QuizAnswerIn(BaseModel):
    choice: str
    ms: int | None = None


def clean_quiz(it):
    """AI가 만든 문제 하나 — 빈칸 하나, 서로 다른 보기 4개, 정답이 보기 중 하나여야 한다. 아니면 None.
    자세한 해설(structure·why·vocab)은 형식에 맞는 항목만 남겨 detail(JSON)로 묶는다."""
    ex = clean_exam(it)
    if (not ex or len(ex["choices"]) != 4 or len({x.lower() for x in ex["choices"]}) != 4
            or len(re.findall(r"_{3,}", ex["q"])) != 1):
        return None
    s = lambda v, n: " ".join(str(v or "").split())[:n]
    rows = lambda key, fields, n: [
        {f: s(x.get(f), m) for f, m in fields} for x in (it.get(key) if isinstance(it.get(key), list) else [])
        if isinstance(x, dict) and all(s(x.get(f), m) for f, m in fields)][:n]
    why = [w for w in rows("why", (("choice", 60), ("reason", 300)), 4) if w["choice"] in ex["choices"]]
    detail = {"structure": rows("structure", (("role", 20), ("text", 120)), 8), "why": why,
              "vocab": rows("vocab", (("word", 40), ("meaning", 60)), 6)}
    return {**ex, "explain": s(it.get("explain"), 1200), "q_ko": s(it.get("q_ko"), 300),
            "detail": json.dumps(detail, ensure_ascii=False)}


def quiz_refill(c) -> bool:
    """안 푼 문제가 적으면 문제 만들기 작업을 넣는다(대기 중인 작업 몫까지 쳐서). 작업이 대기·처리 중이면 True."""
    active = [json.loads(p) for (p,) in c.execute(QUIZ_ACTIVE)]
    left = c.execute(f"select count(*) from quiz_items q where {QUIZ_OPEN}", (study_day_start(now()),)).fetchone()[0]
    busy = {i["id"] for p in active for i in p["items"]}  # 이미 작업에 들어간 카드는 또 넣지 않는다
    made = "(select count(*) from quiz_items q where q.word_id = w.id)"
    n_jobs = len(active)
    while n_jobs < QUIZ_JOBS and left + n_jobs * QUIZ_BATCH < QUIZ_REFILL:
        # 한 번이라도 학습한 카드 중 문제가 적게 나온 것부터, 문법 반·어휘 반
        rows = []
        for kind, n in (("grammar", QUIZ_BATCH // 2), ("vocab", QUIZ_BATCH - QUIZ_BATCH // 2)):
            rows += [dict(r) for r in c.execute(
                f"select id, kind, word, pos, meaning, example, related from words w "
                f"where kind = ? and meaning != '' and seen > 0 and id not in (select value from json_each(?)) "
                f"order by {made}, random() limit ?", (kind, json.dumps(list(busy)), n))]
        if not rows:
            break
        for r in rows:
            # 카드에서 이미 본 문장(예문·출제 유형 예시) — AI가 이것과 다른 새 문장을 만들게 넘긴다
            exam = (json.loads(r.pop("related")) if r["related"] else {}).get("exam") or {}
            r["avoid"] = [x for x in (r.pop("example"), exam.get("q")) if x]
            busy.add(r["id"])
        t = now()
        c.execute("insert into jobs (kind,payload,created_at,updated_at) values ('quiz',?,?,?)",
                  (json.dumps({"items": rows}, ensure_ascii=False), t, t))
        n_jobs += 1
    return n_jobs > 0


def quiz_stats(c) -> dict:
    q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    return {
        "quiz_left": q(f"select count(*) from quiz_items q where {QUIZ_OPEN}", study_day_start(now())),
        "quiz_solved": q("select count(distinct item_id) from quiz_answers where correct = 1"),
        "quiz_answered": q("select count(*) from quiz_answers"),
        "quiz_correct": q("select coalesce(sum(correct), 0) from quiz_answers"),
        "quiz_generating": bool(c.execute(QUIZ_ACTIVE).fetchone()),
    }


@app.post("/api/quiz")
def start_quiz(limit: int = QUIZ_SIZE):
    """파트 5 세트: 낼 수 있는 문제에서 어휘·문법 반씩(모자라면 다른 쪽으로 채움). 덜 푼 문제 먼저."""
    limit = max(1, min(limit, 30))
    with db() as c:
        pick = lambda kind: c.execute(
            "select q.*, w.word as card, (select count(*) from quiz_answers a where a.item_id = q.id) as tries "
            f"from quiz_items q left join words w on w.id = q.word_id where q.kind = ? and {QUIZ_OPEN} "
            "order by tries, random() limit ?",
            (kind, study_day_start(now()), limit),
        ).fetchall()
        g, v = pick("grammar"), pick("vocab")
        nv = min(len(v), max(limit // 2, limit - len(g)))
        items = [dict(r) for r in v[:nv] + g[: limit - nv]]
        random.shuffle(items)
        generating = quiz_refill(c)
    for it in items:
        it["choices"] = json.loads(it["choices"])
        it["detail"] = json.loads(it["detail"]) if it["detail"] else {}
    if not items:
        raise HTTPException(409, "AI가 문제를 만드는 중이에요 — 2~3분 뒤에 다시 열어 주세요" if generating
                            else "풀 문제가 없어요 — 카드를 학습하면 문제가 만들어져요")
    return {"items": items, "generating": generating}


@app.post("/api/quiz/{item_id}/answer")
def answer_quiz(item_id: int, body: QuizAnswerIn):
    """답 기록. 틀리면 근거 카드를 바로 복습할 카드로 돌린다(단계는 그대로)."""
    with db() as c:
        r = c.execute("select word_id, answer from quiz_items where id = ?", (item_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        ok = " ".join(body.choice.split()) == r["answer"]
        ms = max(0, min(body.ms, 3_600_000)) if body.ms is not None else None
        c.execute("insert into quiz_answers (item_id, choice, correct, ms, at) values (?,?,?,?,?)",
                  (item_id, body.choice.strip()[:80], int(ok), ms, now()))
        if not ok and r["word_id"]:
            c.execute("update words set due_at = min(due_at, ?) where id = ?", (now(), r["word_id"]))
    return {"correct": ok, "answer": r["answer"]}


@app.get("/api/session")
def get_session():
    """진행 중인 세트를 카드 내용과 함께 돌려준다. 그사이 지워진 카드는 빼고 위치를 맞춘다."""
    with db() as c:
        r = c.execute("select data, updated_at from study_session where id = 1").fetchone()
        if not r:
            return Response(status_code=204)
        s = json.loads(r["data"])
        ids = {q["id"] for q in s["queue"]}
        rows = {
            w["id"]: dict(w)
            for w in c.execute(
                f"select * from words where id in ({','.join('?' * len(ids))})", tuple(ids)
            )
        } if ids else {}
    queue, pos = [], s["pos"]
    for i, q in enumerate(s["queue"]):
        if q["id"] in rows:
            queue.append({**rows[q["id"]], "again": q.get("again", False)})
        elif i < s["pos"]:
            pos -= 1
    if pos >= len(queue):
        return Response(status_code=204)  # 남은 카드가 없으면 끝난 세트
    return {"kind": s.get("kind", ""), "pos": pos, "queue": queue, "first": s.get("first", {}),
            "updated_at": r["updated_at"]}


@app.put("/api/session")
def put_session(body: SessionIn):
    with db() as c:
        c.execute(
            "insert into study_session (id, data, updated_at) values (1, ?, ?) "
            "on conflict(id) do update set data = excluded.data, updated_at = excluded.updated_at",
            (body.model_dump_json(), now()),
        )
    return {"ok": True}


@app.delete("/api/session")
def delete_session():
    with db() as c:
        c.execute("delete from study_session where id = 1")
    return {"ok": True}


@app.post("/api/words/{word_id}/review")
def review(word_id: int, body: ReviewIn):
    with db() as c:
        r = c.execute("select box, due_at from words where id = ?", (word_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        box = min(r["box"] + 1, len(INTERVALS) - 1) if body.known else 0
        due = due_after(INTERVALS[box])
        c.execute(
            "update words set box = ?, due_at = ?, seen = seen + 1, known = known + ? where id = ?",
            (box, due, int(body.known), word_id),
        )
        day = kst_day(now())
        c.execute(
            "insert into activity (day, reviews) values (?, 1) "
            "on conflict(day) do update set reviews = reviews + 1",
            (day,),
        )
    # 앱의 되돌리기가 이 값을 그대로 /undo에 보낸다
    undo = {"box": r["box"], "due_at": r["due_at"], "known": body.known, "day": day}
    return {"box": box, "due_at": due, "undo": undo}


@app.post("/api/words/{word_id}/undo")
def undo_review(word_id: int, body: UndoIn):
    """방금 한 review를 무른다 — 단계·복습일을 넘기기 전으로, 횟수와 그날 학습량을 하나 뺀다."""
    if not 0 <= body.box < len(INTERVALS):
        raise HTTPException(400, "box 범위 밖")
    with db() as c:
        n = c.execute(
            "update words set box = ?, due_at = ?, seen = max(seen - 1, 0), known = max(known - ?, 0) where id = ?",
            (body.box, body.due_at, int(body.known), word_id),
        ).rowcount
        if not n:
            raise HTTPException(404)
        c.execute("update activity set reviews = max(reviews - 1, 0) where day = ?", (body.day,))
    return {"ok": True}


@app.get("/api/words")
def list_words(q: str = "", kind: str = "", sort: str = ""):
    sql, args = "select * from words where 1=1", []
    if q:
        sql += " and (word like ? or meaning like ?)"
        args += [f"%{q}%"] * 2
    if kind in KINDS:
        sql += " and kind = ?"
        args.append(kind)
    elif kind == "starred":
        sql += " and starred = 1"
    elif kind == "lc":
        sql += " and lc = 1"
    elif kind == "missed":
        sql += f" and {MISSES} >= ?"
        args.append(MISSED_MIN)
    order = f"{MISSES} desc, " if kind == "missed" else ""
    if sort == "freq":  # 중요도순(같으면 아래 순서)
        order = f"freq desc, {order}"
    with db() as c:
        rows = c.execute(sql + f" order by {order}created_at desc, id desc", args).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/words")
def add_words(body: WordIn):
    """뜻까지 채워서 보내면 바로 저장. 단어만 보내면(여러 줄 가능) AI 채우기 작업으로 넘긴다."""
    d = clean(body)
    t = now()
    with db() as c:
        if d["meaning"]:
            try:
                cur = c.execute(
                    "insert into words (word,pos,meaning,example,example_ko,kind,source,due_at,created_at) "
                    "values (?,?,?,?,?,?,'manual',?,?)",
                    (*[d[k] for k in WORD_FIELDS], t, t),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, "이미 있는 단어")
            mark_lc(c)
            return {"id": cur.lastrowid}
        words = [" ".join(x.split()) for x in body.word.replace(",", "\n").splitlines()]
        words = [w for w in words if w]
        if not words:
            raise HTTPException(400, "단어가 비어 있음")
        cur = c.execute(
            "insert into jobs (kind,payload,created_at,updated_at) values ('text',?,?,?)",
            (json.dumps({"words": words}, ensure_ascii=False), t, t),
        )
    return {"job_id": cur.lastrowid}


@app.put("/api/words/{word_id}")
def edit_word(word_id: int, body: WordIn):
    d = clean(body)
    with db() as c:
        try:
            n = c.execute(
                "update words set word=?,pos=?,meaning=?,example=?,example_ko=?,kind=? where id=?",
                (*[d[k] for k in WORD_FIELDS], word_id),
            ).rowcount
        except sqlite3.IntegrityError:
            raise HTTPException(409, "이미 있는 단어")
    if not n:
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/words/{word_id}/star")
def star_word(word_id: int, body: StarIn):
    with db() as c:
        n = c.execute("update words set starred = ? where id = ?", (int(body.starred), word_id)).rowcount
    if not n:
        raise HTTPException(404)
    return {"starred": body.starred}


@app.delete("/api/words/{word_id}")
def delete_word(word_id: int):
    with db() as c:
        c.execute("delete from words where id = ?", (word_id,))
    return {"ok": True}


@app.post("/api/jobs")
async def upload(files: list[UploadFile] = File(...)):
    ids = []
    t = now()
    for f in files:
        if not (f.content_type or "").startswith("image/"):
            raise HTTPException(400, f"이미지가 아님: {f.filename}")
        data = await f.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"파일이 너무 큼: {f.filename}")
        ext = {"image/png": ".png", "image/webp": ".webp"}.get(f.content_type, ".jpg")
        name = uuid.uuid4().hex + ext
        with open(os.path.join(UPLOADS, name), "wb") as out:
            out.write(data)
        with db() as c:
            cur = c.execute(
                "insert into jobs (kind,filename,created_at,updated_at) values ('photo',?,?,?)",
                (name, t, t),
            )
            ids.append(cur.lastrowid)
    return {"job_ids": ids}


@app.get("/api/jobs")
def list_jobs():
    with db() as c:
        rows = c.execute("select * from jobs where kind not in ('grade','enrich','quiz','rank') order by id desc limit 50").fetchall()
    return [job_row(r) for r in rows]


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int):
    with db() as c:
        n = c.execute(
            "update jobs set status='pending', attempts=0, error=null, updated_at=? "
            "where id=? and status='error'",
            (now(), job_id),
        ).rowcount
    if not n:
        raise HTTPException(409, "오류 상태의 작업만 재시도할 수 있음")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    """작업 기록과 사진만 지운다. 이미 추가된 단어는 남는다."""
    with db() as c:
        r = c.execute("select filename, status from jobs where id=?", (job_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        if r["status"] == "processing":
            raise HTTPException(409, "처리 중인 작업은 지울 수 없음")
        c.execute("delete from jobs where id=?", (job_id,))
    if r["filename"]:
        try:
            os.remove(os.path.join(UPLOADS, r["filename"]))
        except FileNotFoundError:
            pass
    return {"ok": True}


@app.get("/api/jobs/{job_id}/image")
def job_image(job_id: int):
    with db() as c:
        r = c.execute("select filename from jobs where id=?", (job_id,)).fetchone()
    if not r or not r["filename"]:
        raise HTTPException(404)
    return FileResponse(os.path.join(UPLOADS, r["filename"]))


# ---------- 워커 API ----------

def check_token(token: str | None):
    if not token or not secrets.compare_digest(token, WORKER_TOKEN):
        raise HTTPException(401)


@app.post("/api/worker/claim")
def claim(x_worker_token: str | None = Header(None)):
    check_token(x_worker_token)
    t = now()
    with db() as c:
        c.execute("begin immediate")
        # 워커가 처리 도중 죽은 작업을 되살리거나, 재시도 한도를 넘었으면 오류로 닫는다.
        c.execute(
            "update jobs set status = case when attempts >= ? then 'error' else 'pending' end, "
            "error = case when attempts >= ? then '처리 시간 초과' else error end, updated_at = ? "
            "where status = 'processing' and updated_at < ?",
            (MAX_ATTEMPTS, MAX_ATTEMPTS, t, t - STALE_SECONDS),
        )
        # 암기 테스트 채점은 사용자가 결과 화면에서 기다리므로 사진보다 먼저
        r = c.execute(
            "select * from jobs where status='pending' order by (kind = 'grade') desc, id limit 1"
        ).fetchone()
        if not r:
            return Response(status_code=204)
        c.execute(
            "update jobs set status='processing', attempts=attempts+1, updated_at=? where id=?",
            (t, r["id"]),
        )
    return job_row(r)


@app.get("/api/worker/jobs/{job_id}/image")
def worker_image(job_id: int, x_worker_token: str | None = Header(None)):
    check_token(x_worker_token)
    return job_image(job_id)


@app.post("/api/worker/jobs/{job_id}/result")
def result(job_id: int, body: ResultIn, x_worker_token: str | None = Header(None)):
    check_token(x_worker_token)
    t = now()
    with db() as c:
        r = c.execute("select attempts, kind, payload from jobs where id=?", (job_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        if r["kind"] == "grade" and not body.error:
            return apply_grades(c, job_id, r, body, t)
        if r["kind"] == "enrich" and not body.error:
            return apply_related(c, job_id, body, t)
        if r["kind"] == "quiz" and not body.error:
            return apply_quiz(c, job_id, r, body, t)
        if r["kind"] == "rank" and not body.error:
            return apply_freq(c, job_id, body, t)
        if body.error and body.release:
            c.execute(
                "update jobs set status='pending', attempts=max(attempts-1,0), error=?, updated_at=? where id=?",
                (body.error[:2000], t, job_id),
            )
            return {"status": "pending"}
        if body.error:
            status = "error" if r["attempts"] >= MAX_ATTEMPTS else "pending"
            c.execute(
                "update jobs set status=?, error=?, updated_at=? where id=?",
                (status, body.error[:2000], t, job_id),
            )
            if status == "error" and r["kind"] == "grade":
                aids = [i["aid"] for i in json.loads(r["payload"])["items"]]
                c.execute(
                    f"update test_answers set verdict = 'error', reason = 'AI 채점 실패' "
                    f"where verdict = 'pending' and id in ({','.join('?' * len(aids))})",
                    aids,
                )
            return {"status": status}
        added, dupes = [], []
        for w in body.words:
            d = clean(w)
            if not d["word"] or not d["meaning"]:
                continue
            rel = (related_json(w.paraphrases, w.confusables) if d["kind"] == "vocab"
                   else related_json(terms=w.terms, exam=w.exam))
            cur = c.execute(
                "insert into words (word,pos,meaning,example,example_ko,kind,source,job_id,due_at,created_at,related,freq) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?) on conflict(word) do nothing",
                (*[d[k] for k in WORD_FIELDS], "ai", job_id, t, t, rel, w.freq if w.freq in FREQS else 0),
            )
            if cur.rowcount:
                added.append(d["word"])
            else:
                dupes.append(d["word"])
                # 이미 있는 카드가 아직 비어 있으면 채워 준다
                if rel:
                    c.execute("update words set related = ? where word = ? and related = ''", (rel, d["word"]))
        # LC 체크에서 넘긴 작업이면 결과 단어(이미 있던 것 포함)를 LC 빈출로 표시. 목록 단어는 늘 표시.
        payload = json.loads(r["payload"]) if r["payload"] else {}
        mark_lc(c, added + dupes if payload.get("lc") else [])
        res = {"added": added, "duplicates": dupes, "note": body.note}
        c.execute(
            "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
            (json.dumps(res, ensure_ascii=False), t, job_id),
        )
    return res


def apply_related(c, job_id, body, t):
    """기존 카드 채우기 결과를 저장한다(어휘: 바꿔 말하기·헷갈리는 단어, 문법: 공식 속 단어 뜻)."""
    done = 0
    for it in body.related:
        row = c.execute("select kind, related from words where id = ?", (it.id,)).fetchone()
        if not row:
            continue
        if row["kind"] == "vocab":
            rel = related_json(it.paraphrases, it.confusables)
        else:  # 문법: 이미 있는 출제 유형은 그대로 두고 단어 뜻만 채운다
            old = json.loads(row["related"]) if row["related"] else {}
            rel = related_json(terms=it.terms, exam=old.get("exam"))
        if rel:
            done += c.execute("update words set related = ? where id = ?", (rel, it.id)).rowcount
    res = {"added": [], "duplicates": [], "filled": done}
    c.execute(
        "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
        (json.dumps(res, ensure_ascii=False), t, job_id),
    )
    return res


def apply_freq(c, job_id, body, t):
    """AI가 매긴 토익 출제 중요도를 저장한다. 1~3 밖의 값은 버린다."""
    done = sum(c.execute("update words set freq = ? where id = ?", (f.freq, f.id)).rowcount
               for f in body.freq if f.freq in FREQS)
    res = {"added": [], "duplicates": [], "ranked": done}
    c.execute(
        "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
        (json.dumps(res), t, job_id),
    )
    return res


def apply_quiz(c, job_id, job, body, t):
    """AI가 만든 파트 5 문제를 은행에 넣는다. 종류는 근거 카드를 따르고, 형식이 안 맞는 문제는 버린다."""
    kinds = {i["id"]: i["kind"] for i in json.loads(job["payload"])["items"]}
    added = 0
    for it in body.quiz:
        q, wid = clean_quiz(it), it.get("word_id")
        if not q or wid not in kinds:
            continue
        added += c.execute(
            "insert into quiz_items (word_id,kind,type,q,choices,answer,explain,q_ko,detail,source,created_at) "
            "values (?,?,?,?,?,?,?,?,?,'ai',?) on conflict(q) do nothing",
            (wid, kinds[wid], q["type"], q["q"], json.dumps(q["choices"], ensure_ascii=False), q["answer"],
             q["explain"], q["q_ko"], q["detail"], t),
        ).rowcount
    res = {"added": [], "duplicates": [], "quiz": added}
    c.execute(
        "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
        (json.dumps(res), t, job_id),
    )
    return res


ENRICH_BATCH = 20

# ---------- 토익 출제 중요도 ----------
# words.freq: 3 필수(거의 매회) · 2 빈출 · 1 가끔(고난도·드묾) · 0 아직 안 매김(2026-10-23, 사용자 요청 — 중요한 것부터
# 보려고). ETS가 단어별 빈도를 공개하지 않으므로 AI의 판단(토익 출제 경향 기준)이다. 새 AI 카드는 만들 때 같이 매기고,
# 직접 넣은 카드·기존 카드는 rank_refill이 RANK_BATCH개씩 rank 작업으로 넘긴다(홈 통계를 불러올 때, 대기 작업이 없을 때만).
FREQS = (1, 2, 3)
RANK_BATCH = 40
# 학습 순서에 쓰는 중요도 — 아직 안 매긴 카드는 중간(2)으로 친다
FREQ_ORDER = "(case freq when 0 then 2 else freq end)"


def rank_refill(c, limit: int | None = None) -> int:
    """중요도가 없는 카드를 rank 작업으로 넘긴다. 이미 대기·처리 중인 rank 작업이 있으면 넣지 않는다."""
    if c.execute("select 1 from jobs where kind = 'rank' and status in ('pending','processing')").fetchone():
        return 0
    rows = [dict(r) for r in c.execute(
        "select id, kind, word, pos, meaning from words where freq = 0 and meaning != '' order by id"
        + (" limit ?" if limit else ""), (limit,) if limit else ())]
    t = now()
    for i in range(0, len(rows), RANK_BATCH):
        c.execute("insert into jobs (kind,payload,created_at,updated_at) values ('rank',?,?,?)",
                  (json.dumps({"items": rows[i : i + RANK_BATCH]}, ensure_ascii=False), t, t))
    return len(rows)


@app.post("/api/rank")
def rank(limit: int | None = None):
    """중요도가 없는 카드를 지금 바로 rank 작업으로 넘긴다(limit만큼만 — 기준을 먼저 볼 때)."""
    with db() as c:
        return {"words": rank_refill(c, limit)}


@app.post("/api/enrich")
def enrich():
    """related가 비어 있는 카드를 ENRICH_BATCH개씩 AI 작업으로 넘긴다 — 어휘는 바꿔 말하기·헷갈리는 단어,
    문법은 공식 속 영어 단어 뜻(영어 단어가 있는 공식만). 종류별로 작업을 나눈다(payload.kind).
    이미 대기 중인 enrich 작업에 들어간 카드는 다시 넣지 않는다."""
    t = now()
    with db() as c:
        queued = set()
        for (p,) in c.execute("select payload from jobs where kind = 'enrich' and status in ('pending','processing')"):
            queued.update(i["id"] for i in json.loads(p)["items"])
        jobs, total = [], 0
        for kind in KINDS:
            rows = [dict(r) for r in c.execute(
                "select id, word, pos, meaning from words where kind = ? and meaning != '' and "
                + ("related = ''" if kind == "vocab" else "related not like '%\"terms\"%'") + " order by id",
                (kind,),
            ) if r["id"] not in queued and (kind == "vocab" or has_english_word(r["word"]))]
            total += len(rows)
            for i in range(0, len(rows), ENRICH_BATCH):
                payload = json.dumps({"kind": kind, "items": rows[i : i + ENRICH_BATCH]}, ensure_ascii=False)
                jobs.append(c.execute(
                    "insert into jobs (kind,payload,created_at,updated_at) values ('enrich',?,?,?)", (payload, t, t)
                ).lastrowid)
    return {"words": total, "jobs": jobs}


def apply_grades(c, job_id, job, body, t):
    """AI 채점 결과를 답안에 반영하고, 오답 단어는 0단계로 내린다."""
    aids = {i["aid"] for i in json.loads(job["payload"])["items"]}
    done = 0
    for g in body.grades:
        if g.aid not in aids:
            continue
        verdict = "correct" if g.correct else "wrong"
        c.execute(
            "update test_answers set verdict = ?, reason = ?, graded_by = 'ai' where id = ? and verdict = 'pending'",
            (verdict, g.reason[:300], g.aid),
        )
        row = c.execute("select word_id from test_answers where id = ?", (g.aid,)).fetchone()
        (test_pass if g.correct else demote)(c, row["word_id"])
        done += 1
    # 모델이 빠뜨린 문항은 실패로 표시(무한 대기 방지)
    c.execute(
        f"update test_answers set verdict = 'error', reason = 'AI가 채점하지 않음' "
        f"where verdict = 'pending' and id in ({','.join('?' * len(aids))})",
        tuple(aids),
    )
    res = {"graded": done}
    c.execute(
        "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
        (json.dumps(res), t, job_id),
    )
    return res


@app.get("/api/config")
def client_config():
    return {"ca_url": os.environ.get("VOCAB_CA_URL") or None}


@app.get("/ca.crt")
def ca_cert():
    """사설 CA의 **공개** 루트 인증서(VOCAB_CA_CERT). 내부 도메인을 사설 CA로 HTTPS 처리할 때,
    폰이 그 CA를 신뢰해야 PWA 설치(서비스 워커)가 된다. 인증서가 없는 기기는 HTTPS로 못 받으니
    HTTP 주소(VOCAB_CA_URL)로 받게 한다. 개인키는 절대 여기에 두지 않는다."""
    path = os.environ.get("VOCAB_CA_CERT")
    if not path or not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="application/x-x509-ca-cert", filename="ca.crt")


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
