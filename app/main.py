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

import json
import os
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

# 간격 반복 단계(box)별 다음 복습까지의 일수. '알아요'면 한 단계 올라가고 '모름'이면 0으로.
INTERVALS = [0, 1, 3, 7, 14, 30, 60, 120]
MASTERED_BOX = 3  # '알아요' 3번 연속(다음 복습 7일 뒤) 이상이면 암기 완료. 암기 테스트도 여기서 낸다.
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
    # 2026-10-02 복습 시각을 24시간 단위에서 학습일 단위로 — 예전 due_at을 그날 06:00으로 내린다.
    # 이미 맞춰진 값은 그대로라 매번 실행해도 안전하다.
    c.execute(
        "update words set due_at = (due_at + ?) / ? * ? - ? where (due_at + ?) % ? != 0",
        (KST - DAY_START, DAY, DAY, KST - DAY_START, KST - DAY_START, DAY),
    )

app = FastAPI(title="vocab", docs_url=None, redoc_url=None)


# ---------- 모델 ----------

class WordIn(BaseModel):
    word: str
    pos: str = ""
    meaning: str = ""
    example: str = ""
    example_ko: str = ""
    kind: str = "vocab"
    # AI가 어휘 카드와 함께 만든다. 사용자 편집(PUT)에서는 쓰지 않는다.
    paraphrases: list[dict] = []
    confusables: list[dict] = []


class RelatedIn(BaseModel):
    """기존 어휘에 바꿔 말하기·헷갈리는 단어를 채우는 작업(job kind 'enrich')의 결과 한 줄."""
    id: int
    paraphrases: list[dict] = []
    confusables: list[dict] = []


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


def clean(w: WordIn) -> dict:
    d = {k: (getattr(w, k) or "").strip() for k in WORD_FIELDS}
    d["word"] = " ".join(d["word"].split())
    if d["kind"] not in KINDS:
        d["kind"] = "vocab"
    return d


# ---------- 바꿔 말하기 · 헷갈리는 단어 ----------
# words.related = {"paraphrases": [{word, meaning}], "confusables": [{word, meaning, tip}]} (JSON 문자열, 어휘만)
# 카드 뒷면에 참고용으로만 보여 준다. 헷갈리는 단어를 카드로 만들어 짝으로 묶던 방식은 쉬운 파생어 카드가
# 수백 개 생겨서 하루 만에 뺐다(2026-10-05, 사용자 결정).
MAX_PARAPHRASES, MAX_CONFUSABLES = 3, 2


def related_json(paraphrases, confusables) -> str:
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
    return json.dumps({"paraphrases": p, "confusables": cf}, ensure_ascii=False) if p or cf else ""


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


def kind_filter(kind):
    """학습 범위: None(섞어서) / vocab(어휘만) / grammar(문법만) / starred(북마크만).
    SQL 조각과 인자, 그리고 복습일을 따질지를 돌려준다. 북마크는 복습일과 상관없이 전부 낸다 —
    따로 모아 다시 보려고 찍어 둔 것이라, 복습일이 안 됐다고 빠지면 북마크 학습이 비어 버린다."""
    if kind is None:
        return "", (), True
    if kind == "starred":
        return " and starred = 1", (), False
    if kind not in KINDS:
        raise HTTPException(400, "kind는 vocab, grammar, starred 중 하나")
    return " and kind = ?", (kind,), True


@app.get("/api/stats")
def stats(kind: str | None = None):
    t = now()
    kf, ka, by_due = kind_filter(kind)
    with db() as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        return {
            "total": q("select count(*) from words"),
            "due": q("select count(*) from words where meaning != '' and due_at <= ?" + kf, t if by_due else 2**62, *ka),
            "starred": q("select count(*) from words where starred = 1"),
            "new": q("select count(*) from words where seen = 0"),
            "mastered": q("select count(*) from words where box >= ?", MASTERED_BOX),
            "testable": q(f"select count(*) from words where {TESTABLE}", MASTERED_BOX, study_day_start(t)),
            "tested_today": q(f"select count(*) from words where kind = 'vocab' and box >= ? and id in ({TESTED_TODAY})",
                              MASTERED_BOX, study_day_start(t)),
            "tests": q("select count(distinct test_id) from test_answers where verdict is not null"),
            "tests_grading": q("select count(distinct a.test_id) from test_answers a join tests t on t.id = a.test_id "
                               "where a.verdict = 'pending' and t.finished_at is not null"),
            "jobs_active": q("select count(*) from jobs where status in ('pending','processing')"),
            "today": q("select coalesce(max(reviews),0) from activity where day = ?", kst_day(t)),
            "streak": streak(c),
        }


@app.get("/api/study")
def study(limit: int = 20, kind: str | None = None):
    limit = max(1, min(limit, 100))
    kf, ka, by_due = kind_filter(kind)
    with db() as c:
        rows = c.execute(
            "select * from words where meaning != '' and due_at <= ?" + kf + " "
            "order by due_at, id limit ?",
            (now() if by_due else 2**62, *ka, limit),
        ).fetchall()
    return [dict(r) for r in rows]


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
    """테스트에서 틀린 단어는 '모름'과 같이 0단계로 — 다시 학습 카드로 나온다."""
    c.execute("update words set box = 0, due_at = ? where id = ?", (now(), word_id))


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


# 오늘(학습일, KST 06:00부터) 테스트에서 맞힌 단어 — 그날 테스트에서는 다시 내지 않는다.
# AI 채점을 기다리는 답도 뺀다(오답이 되면 0단계로 내려가 어차피 대상에서 빠진다).
TESTED_TODAY = (
    "select a.word_id from test_answers a join tests t on t.id = a.test_id "
    "where t.created_at >= ? and a.verdict in ('correct', 'pending')"
)
TESTABLE = f"kind = 'vocab' and meaning != '' and box >= ? and id not in ({TESTED_TODAY})"


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
            (MASTERED_BOX, study_day_start(t), limit),
        ).fetchall()
        if not words:
            if c.execute("select 1 from words where kind = 'vocab' and box >= ? limit 1", (MASTERED_BOX,)).fetchone():
                raise HTTPException(409, "오늘은 암기 완료 어휘를 모두 맞혔어요")
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
def list_words(q: str = "", kind: str = ""):
    sql, args = "select * from words where 1=1", []
    if q:
        sql += " and (word like ? or meaning like ?)"
        args += [f"%{q}%"] * 2
    if kind in KINDS:
        sql += " and kind = ?"
        args.append(kind)
    elif kind == "starred":
        sql += " and starred = 1"
    with db() as c:
        rows = c.execute(sql + " order by created_at desc, id desc", args).fetchall()
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
        rows = c.execute("select * from jobs where kind not in ('grade','enrich') order by id desc limit 50").fetchall()
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
            rel = related_json(w.paraphrases, w.confusables) if d["kind"] == "vocab" else ""
            cur = c.execute(
                "insert into words (word,pos,meaning,example,example_ko,kind,source,job_id,due_at,created_at,related) "
                "values (?,?,?,?,?,?,?,?,?,?,?) on conflict(word) do nothing",
                (*[d[k] for k in WORD_FIELDS], "ai", job_id, t, t, rel),
            )
            if cur.rowcount:
                added.append(d["word"])
            else:
                dupes.append(d["word"])
                # 이미 있는 카드가 아직 비어 있으면 채워 준다
                if rel:
                    c.execute("update words set related = ? where word = ? and related = ''", (rel, d["word"]))
        res = {"added": added, "duplicates": dupes, "note": body.note}
        c.execute(
            "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
            (json.dumps(res, ensure_ascii=False), t, job_id),
        )
    return res


def apply_related(c, job_id, body, t):
    """기존 어휘 채우기 결과를 저장한다."""
    done = 0
    for it in body.related:
        rel = related_json(it.paraphrases, it.confusables)
        if rel:
            done += c.execute("update words set related = ? where id = ? and kind = 'vocab'", (rel, it.id)).rowcount
    res = {"added": [], "duplicates": [], "filled": done}
    c.execute(
        "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
        (json.dumps(res, ensure_ascii=False), t, job_id),
    )
    return res


ENRICH_BATCH = 20


@app.post("/api/enrich")
def enrich():
    """바꿔 말하기·헷갈리는 단어가 비어 있는 어휘를 ENRICH_BATCH개씩 AI 작업으로 넘긴다.
    이미 대기 중인 enrich 작업에 들어간 단어는 다시 넣지 않는다."""
    t = now()
    with db() as c:
        queued = set()
        for (p,) in c.execute("select payload from jobs where kind = 'enrich' and status in ('pending','processing')"):
            queued.update(i["id"] for i in json.loads(p)["items"])
        rows = [dict(r) for r in c.execute(
            "select id, word, pos, meaning from words where kind = 'vocab' and meaning != '' and related = '' order by id"
        ) if r["id"] not in queued]
        jobs = []
        for i in range(0, len(rows), ENRICH_BATCH):
            payload = json.dumps({"items": rows[i : i + ENRICH_BATCH]}, ensure_ascii=False)
            jobs.append(c.execute(
                "insert into jobs (kind,payload,created_at,updated_at) values ('enrich',?,?,?)", (payload, t, t)
            ).lastrowid)
    return {"words": len(rows), "jobs": jobs}


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
        if not g.correct:
            row = c.execute("select word_id from test_answers where id = ?", (g.aid,)).fetchone()
            demote(c, row["word_id"])
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
