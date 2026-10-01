#!/usr/bin/env python3
"""단어장 AI 워커 — claude CLI가 로그인된 머신의 cron에서 1분마다 실행한다.

앱 서버에서 대기 중인 작업을 가져가(pull) claude CLI로 처리하고 결과를 돌려보낸다.
워커 쪽은 포트를 열 필요가 없다.
- photo: 책 사진에서 파란 볼펜 표시 → 어휘, 빨간 볼펜 표시 → 문법 공식으로 정리
- text : 사용자가 직접 입력한 단어의 뜻·품사·예문 생성
- grade: 암기 테스트에서 즉석 채점이 못 정한 답을 AI가 '대체로 맞는 뜻인지' 채점
문법 카드는 grammar-formulas.tsv(검증된 토익 문법 공식 목록)에 있는 공식만, 목록의 표기·포인트로 만든다.

설정은 ~/.config/vocab/worker.env (권한 0600, 저장소 밖)에서 읽는다:
  VOCAB_URL=http://<앱 서버>:8000
  VOCAB_WORKER_TOKEN=...
  VOCAB_MODEL=opus              (선택, 기본 opus — sonnet은 사진 속 작은 볼펜 표시를 더 많이 놓친다)
claude 인증은 ~/.config/claude-headless.env 의 CLAUDE_CODE_OAUTH_TOKEN(`claude setup-token`으로
발급한 1년짜리 토큰)을 쓴다. 파일이 없으면 ~/.claude의 로그인 세션을 그대로 쓴다.
표준 라이브러리만 쓴다 — pip/venv 없이 python3만 있으면 된다. Pillow(Debian `python3-pil`)가
있으면 사진을 겹치는 조각으로 잘라 함께 보여 준다(작은 볼펜 표시 인식이 크게 좋아진다). 없으면 전체 사진만 쓴다.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # 선택 의존성 — 없으면 조각 없이 전체 사진만 보낸다
    Image = None

HERE = Path(__file__).resolve().parent
# 검증된 토익 문법 공식 목록. 문법 카드는 이 목록에 있는 공식만, 목록의 표기·포인트 그대로 만든다.
FORMULAS_FILE = HERE / "grammar-formulas.tsv"
ENV_FILE = Path.home() / ".config/vocab/worker.env"
CLAUDE_ENV_FILE = Path.home() / ".config/claude-headless.env"
# claude는 이 디렉터리에서 실행한다. 저장소 안에서 돌리면 그 저장소의 CLAUDE.md가 프롬프트에 딸려 들어온다.
WORK = Path.home() / ".cache/vocab-worker"
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
MAX_JOBS_PER_RUN = 10
CLAUDE_TIMEOUT = 840  # 서버의 STALE_SECONDS(15분)보다 짧게
# 사진을 2×2로 겹치게 자른다. claude는 긴 변 약 1568px로 줄여서 보므로, 전체 사진 한 장으로는
# 두 페이지짜리 사진의 얇은 밑줄이 뭉개진다. 조각은 긴 변 TILE_LONG으로 맞춘다(작으면 확대).
TILE_GRID = 2
TILE_OVERLAP = 0.12
TILE_LONG = 1500
# claude 자체가 실패하면(인증 만료 등) 이 파일을 남기고, 그동안은 매분 재시도하지 않는다.
BACKOFF_FILE = WORK / ".claude-failed"
BACKOFF_SECONDS = 15 * 60


class ClaudeUnavailable(Exception):
    """작업 내용과 무관하게 claude를 쓸 수 없는 상태."""


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def load_env(path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class Api:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("X-Worker-Token", self.token)
        if data:
            r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=30) as res:
            raw = res.read()
            return res.status, raw


def load_formulas():
    """grammar-formulas.tsv → [(공식, 포인트), ...]"""
    out = []
    for line in FORMULAS_FILE.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            formula, point = line.split("\t", 1)
            out.append((formula.strip(), point.strip()))
    return out


def norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def enforce_formulas(data, formulas):
    """문법 항목을 목록의 공식으로 고정한다. 모델이 목록 밖 공식을 만들었으면 버리고 note에 남긴다.
    프롬프트만 믿지 않고 코드에서 한 번 더 막는 것 — 사용자는 검증된 공식만 원한다."""
    by_norm = {norm(f): (f, p) for f, p in formulas}
    words, skipped = [], []
    for w in data["words"]:
        if w.get("kind") != "grammar":
            words.append(w)
            continue
        # 표기가 목록과 같으면 그것으로, 아니면 번호로 (번호를 잘못 적는 실수가 더 흔해서 표기 우선)
        hit = by_norm.get(norm(w.get("word")))
        no = w.get("formula_no")
        if not hit and isinstance(no, int) and 1 <= no <= len(formulas):
            hit = formulas[no - 1]
        if not hit:
            skipped.append(w.get("word") or "?")
            continue
        words.append({**w, "word": hit[0], "pos": "문법", "meaning": hit[1]})
    data["words"] = words
    if skipped:
        msg = "검증된 공식 목록에 없어 건너뜀: " + ", ".join(skipped)
        data["note"] = f"{data.get('note') or ''} {msg}".strip()
    return data


def make_tiles(image_path):
    """사진을 TILE_GRID×TILE_GRID로 겹치게 잘라 WORK에 저장하고 파일 이름 목록을 돌려준다."""
    if Image is None:
        return []
    im = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    w, h = im.size
    tw, th = w / TILE_GRID, h / TILE_GRID
    names = []
    for r in range(TILE_GRID):
        for c in range(TILE_GRID):
            box = (max(0, int(c * tw - tw * TILE_OVERLAP)), max(0, int(r * th - th * TILE_OVERLAP)),
                   min(w, int((c + 1) * tw + tw * TILE_OVERLAP)), min(h, int((r + 1) * th + th * TILE_OVERLAP)))
            tile = im.crop(box)
            scale = TILE_LONG / max(tile.size)
            tile = tile.resize((round(tile.width * scale), round(tile.height * scale)), Image.LANCZOS)
            name = f"{image_path.stem}-r{r + 1}c{c + 1}.jpg"
            tile.save(WORK / name, quality=92)
            names.append(name)
    return names


def build_prompt(job, image_name, formulas, tiles=()):
    template = (HERE / "prompt.md").read_text()
    if job["kind"] == "photo":
        if tiles:
            listing = "\n".join(f"- `{t}`" for t in tiles)
            how = (
                "볼펜 표시가 작아서 전체 사진만으로는 놓치기 쉽기 때문에, 같은 사진을 겹치게 잘라 확대한 조각도 준비했다:\n"
                f"{listing}\n\n"
                "Read 도구로 전체 사진을 먼저 열어 배치를 파악한 뒤, **조각을 하나도 빠짐없이 전부** 열어 표시를 찾아라. "
                "조각끼리 가장자리가 겹치므로 같은 표시는 한 번만 센다.\n\n"
            )
        else:
            how = "Read 도구로 이 이미지를 열어라.\n\n"
        task = (
            f"현재 디렉터리의 `{image_name}` 파일은 사용자가 공부 중인 토익 교재 페이지를 찍은 사진이다. "
            f"{how}"
            "사용자는 볼펜 색으로 표시를 구분한다(밑줄, 동그라미, 덧칠 모두 해당):\n"
            "- **파란색** = 모르는 어휘 → 어휘 항목. 밑줄이 숙어의 일부에 걸쳤으면 의미가 통하는 덩어리로 잡는다. "
            "선택지 (A)~(D)의 단어에 표시가 있어도 똑같이 다룬다.\n"
            "- **빨간색** = 문법 → 표시된 부분과 그 문장이 보여 주는 문법 규칙이 아래 **검증된 토익 문법 공식 목록**의 "
            "어느 공식에 해당하는지 고른다. 같거나 비슷한 규칙이면 그 공식으로 문법 공식 항목을 만들고, "
            "목록에 해당하는 공식이 없으면 **만들지 말고** note에 적는다. 한 문장에 규칙이 여럿 표시돼 있으면 각각 고른다.\n"
            "- 단어 옆·위·아래에 **한글 뜻을 적어 둔** 표시는 색과 상관없이 모르는 어휘다 → 어휘 항목으로 만든다"
            "(밑줄이 흐리거나 빨간 펜이어도).\n"
            "문제 번호에 친 동그라미, 정답 체크, 선택지를 지운 사선은 표시로 보지 않는다. "
            "표시가 없는 단어는 어려워 보여도 넣지 않는다.\n"
            "인쇄된 강조(굵은 글씨, 인쇄된 색 글자, 인쇄된 밑줄), 검정 펜, 연필 표시는 무시한다. "
            "파란·빨간 표시가 하나도 없으면 words를 빈 배열로 하고 note에 그 사실을 적는다."
        )
    else:
        words = "\n".join(f"- {w}" for w in job["payload"]["words"])
        task = (
            "사용자가 직접 입력한 아래 항목 각각에 대해 카드를 만들어라. 단어·숙어면 어휘 항목으로 만든다. "
            "'사역동사 + 목적어 + 동사원형'처럼 문법 규칙이면 아래 **검증된 토익 문법 공식 목록**에서 같거나 "
            "비슷한 공식을 골라 문법 공식 항목으로 만들고, 해당하는 공식이 없으면 만들지 말고 note에 적는다. "
            f"철자가 틀렸으면 바로잡은 형태로 쓴다.\n\n{words}"
        )
    listing = "\n".join(f"{i}. {f}  — {p}" for i, (f, p) in enumerate(formulas, 1))
    return template.replace("{TASK}", task).replace("{GRAMMAR_FORMULAS}", listing)


def parse_result(text):
    # 모델이 규칙을 어기고 코드 펜스나 설명을 붙여도 JSON 객체 부분만 뽑는다.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("응답에 JSON이 없음: " + text[:300])
    data = json.loads(text[start : end + 1])
    if not isinstance(data.get("words"), list):
        raise ValueError("words 배열이 없음")
    return data


def run_claude(prompt, model):
    """claude -p 를 돌려 응답 텍스트를 돌려준다. 인증 만료 등 claude 자체 문제는 ClaudeUnavailable."""
    WORK.mkdir(parents=True, exist_ok=True)
    cmd = [
        CLAUDE, "-p", prompt,
        "--model", model,
        "--output-format", "json",
        "--allowedTools", "Read",
        "--no-session-persistence",
    ]
    env = {**os.environ, **load_env(CLAUDE_ENV_FILE)}
    p = subprocess.run(cmd, cwd=WORK, env=env, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise ClaudeUnavailable(f"claude 실행 실패(종료코드 {p.returncode}): {(p.stderr or p.stdout)[-300:]}")
    if p.returncode != 0 or out.get("is_error"):
        msg = str(out.get("result"))[:300]
        # 모델이 한 글자도 못 냈으면(인증 만료·사용량 한도 등) 작업 탓이 아니다
        if not (out.get("usage") or {}).get("output_tokens"):
            raise ClaudeUnavailable("AI 워커 사용 불가: " + msg)
        raise RuntimeError("claude 오류: " + msg)
    return out.get("result", "")


GRADE_PROMPT = """너는 한국인 수험생의 토익 단어 시험 채점자다. 각 문항은 영어 표제어, 단어장에 적힌 한국어 뜻,
학습자가 적은 답이다. 학습자의 답이 그 단어의 뜻과 **대체로 맞는지** 판정한다.

관대하게 채점한다:
- 동의어·비슷한 표현, 여러 뜻 중 하나만 적은 것, 단어장에 없어도 사전에 있는 올바른 뜻은 정답.
- 품사나 어미가 조금 다른 것(배분 / 배분하다 / 배분하는), 맞춤법·띄어쓰기 실수는 정답.
엄격하게 보는 것:
- 뜻이 다르거나 반대인 것, 너무 막연한 것(예: '하다', '좋은'), 비슷하게 생긴 다른 단어의 뜻은 오답.

문항(JSON):
{items}

JSON 객체 하나만 출력한다. 설명이나 코드 펜스를 붙이지 않는다. reason은 한국어 한 문장(오답이면 왜 틀렸는지)이고,
reason 안에서 단어를 인용할 때는 큰따옴표(") 대신 작은따옴표(')만 쓴다:
{{"results": [{{"aid": 1, "correct": true, "reason": "..."}}]}}
"""


def grade(job, model):
    """암기 테스트 AI 채점(job kind 'grade') → [{aid, correct, reason}]"""
    items = [{"aid": i["aid"], "word": i["word"], "pos": i["pos"], "meaning": i["meaning"], "answer": i["answer"]}
             for i in job["payload"]["items"]]
    text = run_claude(GRADE_PROMPT.format(items=json.dumps(items, ensure_ascii=False, indent=1)), model)
    return parse_grades(text)


def parse_grades(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("채점 응답에 JSON이 없음: " + text[:300])
    try:
        results = json.loads(text[start : end + 1]).get("results")
    except json.JSONDecodeError:
        # reason 속 따옴표 때문에 JSON이 깨진 경우: 문항 번호·정답 여부만이라도 건진다
        results = [
            {"aid": int(m[1]), "correct": m[2] == "true", "reason": m[3] or ""}
            for m in re.finditer(r'"aid"\s*:\s*(\d+)\s*,\s*"correct"\s*:\s*(true|false)(?:\s*,\s*"reason"\s*:\s*"(.*?)"\s*\})?', text, re.S)
        ]
        if not results:
            raise
    if not isinstance(results, list):
        raise ValueError("results 배열이 없음")
    return [{"aid": int(r["aid"]), "correct": bool(r["correct"]), "reason": str(r.get("reason", ""))}
            for r in results if "aid" in r and "correct" in r]


def process(api, job, model):
    WORK.mkdir(parents=True, exist_ok=True)
    image_name, tiles = None, []
    if job["kind"] == "photo":
        _, raw = api.req("GET", f"/api/worker/jobs/{job['id']}/image")
        ext = Path(job["filename"]).suffix or ".jpg"
        image_name = f"job-{job['id']}{ext}"
        (WORK / image_name).write_bytes(raw)
        try:
            tiles = make_tiles(WORK / image_name)
        except Exception as e:  # 조각을 못 만들어도 전체 사진으로는 처리한다
            log(f"job {job['id']} 조각 만들기 실패, 전체 사진만 사용: {e}")
            tiles = []
    formulas = load_formulas()
    try:
        text = run_claude(build_prompt(job, image_name, formulas, tiles), model)
        return enforce_formulas(parse_result(text), formulas)
    finally:
        for name in ([image_name] if image_name else []) + tiles:
            (WORK / name).unlink(missing_ok=True)


def main():
    env = load_env(ENV_FILE)
    api = Api(env["VOCAB_URL"], env["VOCAB_WORKER_TOKEN"])
    model = env.get("VOCAB_MODEL", "opus")
    if BACKOFF_FILE.exists() and time.time() - BACKOFF_FILE.stat().st_mtime < BACKOFF_SECONDS:
        return 0

    for _ in range(MAX_JOBS_PER_RUN):
        try:
            status, raw = api.req("POST", "/api/worker/claim")
        except (urllib.error.URLError, OSError) as e:
            log(f"vocab 서버 연결 실패: {e}")
            return 1
        if status == 204:
            return 0
        job = json.loads(raw)
        log(f"job {job['id']} ({job['kind']}) 시작, 시도 {job['attempts'] + 1}")
        try:
            if job["kind"] == "grade":
                grades = grade(job, model)
                _, raw = api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"grades": grades})
                log(f"job {job['id']} 채점 완료: {json.loads(raw)['graded']}문항")
                BACKOFF_FILE.unlink(missing_ok=True)
                continue
            data = process(api, job, model)
            _, raw = api.req("POST", f"/api/worker/jobs/{job['id']}/result",
                             {"words": data["words"], "note": data.get("note") or None})
            res = json.loads(raw)
            log(f"job {job['id']} 완료: 추가 {res['added']}, 중복 {res['duplicates']}")
            BACKOFF_FILE.unlink(missing_ok=True)
        except ClaudeUnavailable as e:
            log(f"job {job['id']} 보류 — {e} ({BACKOFF_SECONDS // 60}분 뒤 재시도)")
            api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"error": str(e), "release": True})
            BACKOFF_FILE.touch()
            return 1
        except Exception as e:  # 작업 하나가 실패해도 다음 작업은 계속한다
            log(f"job {job['id']} 실패: {e}")
            try:
                api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"error": str(e)[:2000]})
            except Exception as e2:
                log(f"job {job['id']} 실패 보고도 실패: {e2}")
    return 0


if __name__ == "__main__":
    # cron이 1분마다 부르므로, 앞 실행이 아직 사진을 처리 중이면 조용히 빠진다.
    lock = open(Path.home() / ".cache/vocab-worker.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit(main())
