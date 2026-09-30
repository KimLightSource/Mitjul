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

app = FastAPI(title="vocab", docs_url=None, redoc_url=None)


# ---------- 모델 ----------

class WordIn(BaseModel):
    word: str
    pos: str = ""
    meaning: str = ""
    example: str = ""
    example_ko: str = ""
    kind: str = "vocab"


class ReviewIn(BaseModel):
    known: bool


class ResultIn(BaseModel):
    words: list[WordIn] = []
    error: str | None = None
    note: str | None = None
    # 작업 탓이 아닌 실패(워커의 claude 인증 만료 등): 시도 횟수를 되돌려 대기열에 남긴다.
    release: bool = False


def clean(w: WordIn) -> dict:
    d = {k: (getattr(w, k) or "").strip() for k in WORD_FIELDS}
    d["word"] = " ".join(d["word"].split())
    if d["kind"] not in KINDS:
        d["kind"] = "vocab"
    return d


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


@app.get("/api/stats")
def stats():
    t = now()
    with db() as c:
        q = lambda sql, *a: c.execute(sql, a).fetchone()[0]
        return {
            "total": q("select count(*) from words"),
            "due": q("select count(*) from words where meaning != '' and due_at <= ?", t),
            "new": q("select count(*) from words where seen = 0"),
            "mastered": q("select count(*) from words where box >= 5"),
            "jobs_active": q("select count(*) from jobs where status in ('pending','processing')"),
            "today": q("select coalesce(max(reviews),0) from activity where day = ?", kst_day(t)),
            "streak": streak(c),
        }


@app.get("/api/study")
def study(limit: int = 20):
    limit = max(1, min(limit, 100))
    with db() as c:
        rows = c.execute(
            "select * from words where meaning != '' and due_at <= ? "
            "order by due_at, id limit ?",
            (now(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/words/{word_id}/review")
def review(word_id: int, body: ReviewIn):
    with db() as c:
        r = c.execute("select box from words where id = ?", (word_id,)).fetchone()
        if not r:
            raise HTTPException(404)
        box = min(r["box"] + 1, len(INTERVALS) - 1) if body.known else 0
        due = now() + INTERVALS[box] * DAY
        c.execute(
            "update words set box = ?, due_at = ?, seen = seen + 1, known = known + ? where id = ?",
            (box, due, int(body.known), word_id),
        )
        c.execute(
            "insert into activity (day, reviews) values (?, 1) "
            "on conflict(day) do update set reviews = reviews + 1",
            (kst_day(now()),),
        )
    return {"box": box, "due_at": due}


@app.get("/api/words")
def list_words(q: str = "", kind: str = ""):
    sql, args = "select * from words where 1=1", []
    if q:
        sql += " and (word like ? or meaning like ?)"
        args += [f"%{q}%"] * 2
    if kind in KINDS:
        sql += " and kind = ?"
        args.append(kind)
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
        rows = c.execute("select * from jobs order by id desc limit 50").fetchall()
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
        r = c.execute("select * from jobs where status='pending' order by id limit 1").fetchone()
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
        r = c.execute("select attempts from jobs where id=?", (job_id,)).fetchone()
        if not r:
            raise HTTPException(404)
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
            return {"status": status}
        added, dupes = [], []
        for w in body.words:
            d = clean(w)
            if not d["word"] or not d["meaning"]:
                continue
            cur = c.execute(
                "insert into words (word,pos,meaning,example,example_ko,kind,source,job_id,due_at,created_at) "
                "values (?,?,?,?,?,?,?,?,?,?) on conflict(word) do nothing",
                (*[d[k] for k in WORD_FIELDS], "ai", job_id, t, t),
            )
            (added if cur.rowcount else dupes).append(d["word"])
        res = {"added": added, "duplicates": dupes, "note": body.note}
        c.execute(
            "update jobs set status='done', error=null, result=?, updated_at=? where id=?",
            (json.dumps(res, ensure_ascii=False), t, job_id),
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
