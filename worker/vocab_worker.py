#!/usr/bin/env python3
"""단어장 AI 워커 — claude CLI가 로그인된 머신의 cron에서 1분마다 실행한다.

앱 서버에서 대기 중인 작업을 가져가(pull) claude CLI로 처리하고 결과를 돌려보낸다.
워커 쪽은 포트를 열 필요가 없다.
- photo: 책 사진에서 파란 볼펜 표시 → 어휘, 빨간 볼펜 표시 → 문법 공식으로 정리
- text : 사용자가 직접 입력한 단어의 뜻·품사·예문 생성
- grade: 암기 테스트에서 즉석 채점이 못 정한 답을 AI가 '대체로 맞는 뜻인지' 채점
- quiz : 단어장 카드로 토익 파트 5(빈칸 4지선다) 문제 만들기
문법 카드는 먼저 grammar-formulas.tsv(검증된 토익 문법 공식 목록)에서 찾고, 없으면 실제로 맞는
문법 규칙인 경우에만 새 공식을 만들어 목록에 추가한다(목록은 이렇게 계속 늘어난다).

설정은 ~/.config/vocab/worker.env (권한 0600, 저장소 밖)에서 읽는다:
  VOCAB_URL=http://<앱 서버>:8000
  VOCAB_WORKER_TOKEN=...
  VOCAB_MODEL=opus              (선택, 사진 작업 모델. 기본 opus — sonnet은 사진 속 작은 볼펜 표시를 더 많이 놓친다)
  VOCAB_MODEL_TEXT=sonnet       (선택, 직접 입력·바꿔 말하기 채우기 모델. 기본 sonnet)
  VOCAB_MODEL_GRADE=sonnet      (선택, 암기 테스트 채점 모델. 기본 sonnet)
  VOCAB_MODEL_QUIZ=opus         (선택, 파트 5 문제 만들기 모델. 기본 opus — sonnet은 정답이 둘인 보기를 더 자주 냈다)
  VOCAB_MODEL_RANK=opus         (선택, 토익 출제 중요도 매기기 모델. 기본 opus — sonnet은 단골 어휘를 낮게 매겼다)
claude 인증은 ~/.config/claude-headless.env 의 CLAUDE_CODE_OAUTH_TOKEN(`claude setup-token`으로
발급한 1년짜리 토큰)을 쓴다. 파일이 없으면 ~/.claude의 로그인 세션을 그대로 쓴다.
표준 라이브러리만 쓴다 — pip/venv 없이 python3만 있으면 된다. Pillow(Debian `python3-pil`)가
있으면 사진을 겹치는 조각으로 잘라 함께 보여 준다(작은 볼펜 표시 인식이 크게 좋아진다). 없으면 전체 사진만 쓴다.

claude는 Claude Code 기본 시스템 프롬프트·도구 없이 부른다(--system-prompt, --tools ""). 기본값으로 부르면
호출마다 약 37k 토큰이 붙고, 사진을 Read 도구로 한 장씩 열 때마다 턴이 늘어 그만큼 다시 보낸다.
사진은 stream-json 입력으로 한 메시지에 모두 넣어 한 턴에 끝낸다. 작업마다 토큰 사용량을 로그에 남긴다.
"""

import base64
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
# 검증된 토익 문법 공식 목록. 문법 카드는 이 목록에서 먼저 찾고, 없으면(진짜 문법 규칙일 때만)
# 워커가 새 공식을 이 파일 끝에 추가한다(enforce_formulas).
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
# 전체 사진은 claude가 어차피 긴 변 약 1568px로 줄여서 보므로 미리 줄여 보낸다(요청 크기 제한도 피한다).
FULL_LONG = 1568
SYSTEM_PROMPT = "너는 사용자가 지시한 형식의 JSON 객체 하나만 출력하는 도우미다."
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
    """grammar-formulas.tsv → [(공식, 포인트, 출제 유형 dict 또는 None), ...]"""
    out = []
    for line in FORMULAS_FILE.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            cols = [c.strip() for c in line.split("\t")]
            exam = None
            if len(cols) >= 6:
                exam = clean_exam({"type": cols[2], "q": cols[3], "choices": cols[4].split("|"), "answer": cols[5]})
            out.append((cols[0], cols[1], exam))
    return out


def clean_exam(e):
    """출제 유형 {type, q, choices, answer}를 검사해 정리한다. 형식이 안 맞으면 None."""
    if not isinstance(e, dict):
        return None
    choices = [str(c).strip() for c in e.get("choices") or [] if str(c).strip()]
    ex = {"type": str(e.get("type") or "").strip(), "q": str(e.get("q") or "").strip(),
          "choices": choices, "answer": str(e.get("answer") or "").strip()}
    if not ex["q"] or "___" not in ex["q"] or not 2 <= len(choices) <= 5 or ex["answer"] not in choices:
        return None
    return ex


def norm(s):
    return re.sub(r"\s+", "", s or "").lower()


def append_formula(formula, point, exam=None):
    """새 공식을 grammar-formulas.tsv 끝에 한 줄 추가한다(목록을 영구히 넓힌다)."""
    cols = [formula, point]
    if exam:
        cols += [exam["type"], exam["q"], " | ".join(exam["choices"]), exam["answer"]]
    with FORMULAS_FILE.open("a") as f:
        f.write("\t".join(c.replace("\t", " ").replace("\n", " ") for c in cols) + "\n")


def enforce_formulas(data, formulas):
    """문법 항목을 목록의 공식으로 고정한다. 목록에 있으면 표기·포인트를 목록 것으로 맞추고,
    없으면 모델이 만든 새 공식을 그대로 받아 목록에 추가한다(2026-10-14, 사용자 요청 — 목록에
    없다고 전부 버려지면 새로 배우는 문법이 하나도 안 쌓였다). 지어낸 규칙 거르기는 프롬프트가
    맡고, 여기서는 형식(단어·뜻이 비어 있지 않은지)만 한 번 더 본다."""
    by_norm = {norm(f): (f, p, e) for f, p, e in formulas}
    words, new_formulas = [], []
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
            formula, point = (w.get("word") or "").strip(), (w.get("meaning") or "").strip()
            if not formula or not point:
                continue  # 형식이 안 맞으면 버린다
            hit = (formula, point, clean_exam(w.get("exam")))
            by_norm[norm(formula)] = hit  # 같은 작업에서 또 나와도 새 줄을 중복으로 만들지 않는다
            new_formulas.append(hit)
        # 출제 유형은 목록 값이 우선(목록에 없으면 모델이 만든 것)
        words.append({**w, "word": hit[0], "pos": "문법", "meaning": hit[1], "exam": hit[2] or clean_exam(w.get("exam"))})
    data["words"] = words
    if new_formulas:
        for formula, point, exam in new_formulas:
            append_formula(formula, point, exam)
        msg = "목록에 없던 새 문법 공식을 추가했습니다: " + ", ".join(f for f, _, _ in new_formulas)
        data["note"] = f"{data.get('note') or ''} {msg}".strip()
    return data


def make_tiles(image_path):
    """사진을 TILE_GRID×TILE_GRID로 겹치게 잘라 WORK에 저장하고 파일 이름 목록을 돌려준다."""
    if Image is None:
        return []
    im = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
    w, h = im.size
    tw, th = w / TILE_GRID, h / TILE_GRID
    full = im.copy()
    full.thumbnail((FULL_LONG, FULL_LONG), Image.LANCZOS)
    full.save(image_path.with_suffix(".full.jpg"), quality=92)
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


# 어휘 카드의 바꿔 말하기·헷갈리는 단어 규칙. 새 카드(prompt.md)와 기존 카드 채우기(ENRICH_PROMPT)가 같이 쓴다.
RELATED_RULES = """- `paraphrases`: **토익 패러프레이징** 0~3개. 지문의 이 단어가 문제·선택지에서 바꿔 쓰여 나오는 표현
  (예: purchase → buy, acquire / alternative → option, substitute). 각 항목 `word`(영어)와 `meaning`(짧은 한국어 뜻).
  자연스러운 것이 없으면 빈 배열.
- `confusables`: **헷갈리는 단어** 0~2개. **철자·모양이 비슷한데 뜻이 다른** 단어여서 뜻을 착각하기 쉬운 것만
  (예: alternative ↔ alternation, adapt ↔ adopt, considerable ↔ considerate, economic ↔ economical,
  successful ↔ successive, inhibit ↔ inhabit, affect ↔ effect). 각 항목 `word`(사전형), `meaning`(짧은 한국어 뜻),
  `tip`(둘을 구분하는 포인트, 한국어 30자 이내, 예: "-tive는 '대안', -tion은 '교대'").
  넣지 않는 것: 표제어 자신, 활용형(allocated), **뜻은 같고 품사만 다른 파생어**(allocate → allocation,
  tax → taxable, satisfy → satisfying, finalize → final), 철자가 닮지 않은 단어, 쉬운 기초 단어(tax, design,
  copy, live). 대부분의 단어는 해당이 없다 — **억지로 채우지 말고 빈 배열**로 둔다.
- 문법 공식 항목에는 이 두 필드를 넣지 않는다.
"""

# 문법 카드의 공식 속 영어 단어 뜻(2026-10-17). 새 카드(prompt.md)와 기존 카드 채우기(ENRICH_GRAMMAR_PROMPT)가 같이 쓴다.
TERMS_RULES = """- `terms`: 공식에 들어 있는 **영어 단어·표현 각각의 한국어 뜻**. 괄호 속 대표 단어(roughly/approximately/nearly →
  각각), 영어로 된 칸(the number of, by far), `/` 나열(too/as well → 각각)을 공식에 나온 순서대로 빠짐없이 넣는다.
  각 항목 `word`(공식에 쓰인 표기 그대로)와 `meaning`(그 공식에서 쓰이는 뜻, 한국어로 짧게 — 예: hardly "거의 ~않다",
  thereafter "그 이후에", quite "꽤, 상당히"). 관사·be동사 같은 기초 단어(a/an, the, it, be)와 p.p.·-ing·to부정사 같은
  문법 기호는 넣지 않는다. 공식에 영어 단어가 없으면 빈 배열.
"""

# 토익 출제 중요도(2026-10-23). 새 카드(prompt.md)와 기존 카드 매기기(RANK_PROMPT)가 같이 쓴다.
FREQ_RULES = """- `freq`: **토익 출제 중요도** 1~3 (정수). 일반 영어에서 얼마나 흔한지·어려운지가 아니라 **토익 시험(LC·RC)에 얼마나
  자주 나오는지**로 매긴다. 비율은 대략 3이 30%, 2가 45%, 1이 25% 안팎이 되게 — 모두 3으로 몰지 않는다.
  - 3 필수: 거의 매회 어딘가에 나온다. 공지·이메일·광고·회의 지문의 단골 어휘와 파트 5 정답·보기 단골
    (예: submit, deadline, reimbursement, comply with, in advance, renovation, eligible, mandatory, attendance, prior to).
    문법은 파트 5·6에 매회 한두 문제씩 나오는 규칙(품사 자리, 수 일치, 전치사 vs 접속사, 능동태 vs 수동태, 관계대명사).
  - 2 빈출: 여러 회에 한 번꼴로 지문이나 보기에 나온다(예: tentative, commensurate, adjacent to, inadvertently).
    문법은 자주는 아니어도 꾸준히 나오는 규칙(도치, 가정법, 비교급 강조, 특정 to부정사·동명사 목적어 동사).
  - 1 가끔: 토익에 드물게 나오거나 고난도 파트 5에서 가끔만 나오는 단어, 토익보다 일반·학술 영어에 가까운 단어.
    문법은 거의 안 나오는 세부 규칙.
"""

RANK_PROMPT = """너는 토익(TOEIC) 출제 경향을 잘 아는 강사다. 한국인 수험생의 단어장 카드 각각에 토익 출제 중요도를 매긴다.
카드의 내용은 바꾸지 않는다. kind가 vocab이면 어휘, grammar이면 문법 공식(word가 공식, meaning이 핵심 포인트)이다.

{rules}
카드(JSON):
{items}

JSON 객체 하나만 출력한다. 설명이나 코드 펜스를 붙이지 않는다. 모든 카드에 대해 id를 그대로 써서 한 줄씩:
{{"freq": [{{"id": 1, "freq": 3}}]}}
"""

ENRICH_PROMPT = """너는 한국인 수험생의 토익(TOEIC) 단어장을 만드는 도우미다. 아래 어휘 카드 각각에 바꿔 말하기와 헷갈리는
단어를 채운다. 카드의 뜻·품사는 바꾸지 않는다.

{rules}
카드(JSON):
{items}

JSON 객체 하나만 출력한다. 설명이나 코드 펜스를 붙이지 않는다. 모든 카드에 대해 id를 그대로 써서 한 줄씩:
{{"related": [{{"id": 1, "paraphrases": [{{"word": "...", "meaning": "..."}}], "confusables": [{{"word": "...", "meaning": "...", "tip": "..."}}]}}]}}
"""


ENRICH_GRAMMAR_PROMPT = """너는 한국인 수험생의 토익(TOEIC) 단어장을 만드는 도우미다. 아래 문법 공식 카드 각각에, 공식 속 영어 단어의
뜻을 채운다. 공식(word)과 포인트(meaning)는 바꾸지 않는다.

{rules}
카드(JSON — word가 공식, meaning이 핵심 포인트):
{items}

JSON 객체 하나만 출력한다. 설명이나 코드 펜스를 붙이지 않는다. 모든 카드에 대해 id를 그대로 써서 한 줄씩:
{{"related": [{{"id": 1, "terms": [{{"word": "...", "meaning": "..."}}]}}]}}
"""


def enrich(job, model):
    """기존 카드 채우기(job kind 'enrich') → [{id, paraphrases, confusables}] 또는 문법이면 [{id, terms}]"""
    items = [{"id": i["id"], "word": i["word"], "pos": i["pos"], "meaning": i["meaning"]} for i in job["payload"]["items"]]
    items_json = json.dumps(items, ensure_ascii=False, indent=1)
    if job["payload"].get("kind") == "grammar":
        prompt = ENRICH_GRAMMAR_PROMPT.format(rules=TERMS_RULES, items=items_json)
    else:
        prompt = ENRICH_PROMPT.format(rules=RELATED_RULES, items=items_json)
    text = run_claude(prompt, model)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("응답에 JSON이 없음: " + text[:300])
    related = json.loads(text[start : end + 1]).get("related")
    if not isinstance(related, list):
        raise ValueError("related 배열이 없음")
    return related


def rank(job, model):
    """중요도 매기기(job kind 'rank') → [{id, freq}]"""
    items = json.dumps(job["payload"]["items"], ensure_ascii=False, indent=1)
    text = run_claude(RANK_PROMPT.format(rules=FREQ_RULES, items=items), model)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("응답에 JSON이 없음: " + text[:300])
    freq = json.loads(text[start : end + 1]).get("freq")
    if not isinstance(freq, list):
        raise ValueError("freq 배열이 없음")
    return freq


def build_prompt(job, image_name, formulas, tiles=()):
    template = (HERE / "prompt.md").read_text()
    if job["kind"] == "photo":
        if tiles:
            listing = "\n".join(f"- `{t}`" for t in tiles)
            how = (
                "볼펜 표시가 작아서 전체 사진만으로는 놓치기 쉽기 때문에, 같은 사진을 2×2로 겹치게 잘라 확대한 조각도 "
                f"이 메시지 위에 첨부했다(위·왼쪽부터):\n{listing}\n\n"
                "전체 사진으로 배치를 파악한 뒤, **조각을 하나도 빠짐없이 전부** 살펴 표시를 찾아라. "
                "조각끼리 가장자리가 겹치므로 같은 표시는 한 번만 센다.\n\n"
            )
        else:
            how = "\n\n"
        task = (
            f"이 메시지 위에 첨부한 `{image_name}` 사진은 사용자가 공부 중인 토익 교재 페이지를 찍은 것이다. "
            f"{how}"
            "사용자는 볼펜 색으로 표시를 구분한다(밑줄, 동그라미, 덧칠 모두 해당):\n"
            "- **파란색** = 모르는 어휘 → 어휘 항목. 밑줄이 숙어의 일부에 걸쳤으면 의미가 통하는 덩어리로 잡는다. "
            "선택지 (A)~(D)의 단어에 표시가 있어도 똑같이 다룬다.\n"
            "- **빨간색** = 문법 → 표시된 부분과 그 문장이 보여 주는 문법 규칙이 아래 **검증된 토익 문법 공식 목록**의 "
            "어느 공식에 해당하는지 고른다. 같거나 비슷한 규칙이면 그 공식으로 문법 공식 항목을 만든다. "
            "목록에 해당하는 공식이 없으면, 실제로 맞는 토익 문법 규칙인 경우에만 목록과 같은 표기 방식으로 "
            "새 공식을 만들어 추가하고(이 공식은 목록에 편입된다), 지어낸 규칙이거나 틀린 설명이면 만들지 말고 "
            "note에 적는다. 한 문장에 규칙이 여럿 표시돼 있으면 각각 고른다.\n"
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
            "비슷한 공식을 골라 문법 공식 항목으로 만든다. 해당하는 공식이 없으면, 실제로 맞는 토익 문법 "
            "규칙인 경우에만 목록과 같은 표기 방식으로 새 공식을 만들어 추가하고(이 공식은 목록에 편입된다), "
            "지어낸 규칙이거나 틀린 설명이면 만들지 말고 note에 적는다. "
            f"철자가 틀렸으면 바로잡은 형태로 쓴다.\n\n{words}"
        )
        if job["payload"].get("lc"):
            # 'LC 단어 체크'에서 모른다고 고른 토익 듣기 빈출 표현들
            task += (
                "\n\n이 항목들은 토익 **LC(듣기)** 빈출 표현이다. `word`는 입력한 형태 그대로 쓴다"
                "(be stacked를 stack으로 바꾸지 않는다). 예문은 파트 5·6·7 문장이 아니라 듣기에 나오는 문장으로 — "
                "사진 묘사 표현이면 파트 1처럼 사진을 묘사하는 짧은 문장(\"Some boxes are stacked in a corner.\"), "
                "그 밖에는 파트 2·3 대화나 파트 4 안내 방송에 나올 법한 자연스러운 구어 문장으로 만든다."
            )
    listing = "\n".join(f"{i}. {f}  — {p}" for i, (f, p, _) in enumerate(formulas, 1))
    return (template.replace("{TASK}", task).replace("{GRAMMAR_FORMULAS}", listing)
            .replace("{RELATED_RULES}", RELATED_RULES).replace("{TERMS_RULES}", TERMS_RULES)
            .replace("{FREQ_RULES}", FREQ_RULES))


def parse_result(text):
    # 모델이 규칙을 어기고 코드 펜스나 설명을 붙여도 JSON 객체 부분만 뽑는다.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("응답에 JSON이 없음: " + text[:300])
    data = json.loads(text[start : end + 1])
    if not isinstance(data.get("words"), list):
        raise ValueError("words 배열이 없음")
    return data


def image_block(path):
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(path.read_bytes()).decode()}}


def run_claude(prompt, model, images=()):
    """claude -p 를 돌려 응답 텍스트를 돌려준다. 인증 만료 등 claude 자체 문제는 ClaudeUnavailable.
    images: [(이름, 경로)] — 프롬프트 앞에 이름표와 함께 붙인다. 사용량은 LAST_USAGE에 남긴다."""
    WORK.mkdir(parents=True, exist_ok=True)
    content = []
    for name, path in images:
        content += [{"type": "text", "text": f"`{name}`:"}, image_block(path)]
    content.append({"type": "text", "text": prompt})
    message = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
    cmd = [
        CLAUDE, "-p",
        "--model", model,
        "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    env = {**os.environ, **load_env(CLAUDE_ENV_FILE)}
    p = subprocess.run(cmd, cwd=WORK, env=env, input=message, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
    out = None
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            out = ev
    if out is None:
        raise ClaudeUnavailable(f"claude 실행 실패(종료코드 {p.returncode}): {(p.stderr or p.stdout)[-300:]}")
    usage = out.get("usage") or {}
    LAST_USAGE.update(model=model, turns=out.get("num_turns"), cost=out.get("total_cost_usd"),
                      input=usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                      cache_read=usage.get("cache_read_input_tokens", 0), output=usage.get("output_tokens", 0))
    if p.returncode != 0 or out.get("is_error"):
        msg = str(out.get("result"))[:300]
        # 모델이 한 글자도 못 냈으면(인증 만료·사용량 한도 등) 작업 탓이 아니다
        if not usage.get("output_tokens"):
            raise ClaudeUnavailable("AI 워커 사용 불가: " + msg)
        raise RuntimeError("claude 오류: " + msg)
    return out.get("result", "")


LAST_USAGE = {}


def usage_note():
    """마지막 claude 호출의 사용량 한 줄 (로그용). cost는 API 요금 환산값 — 구독 사용량 비교용 지표."""
    u = LAST_USAGE
    if not u:
        return ""
    cost = f", ${u['cost']:.3f}" if isinstance(u.get("cost"), (int, float)) else ""
    return (f" [{u['model']} 입력 {u['input']:,} + 캐시 {u['cache_read']:,}, 출력 {u['output']:,}, "
            f"{u['turns']}턴{cost}]")


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


QUIZ_PROMPT = """너는 토익(TOEIC) 파트 5 출제자이자 해설 강사다. 한국인 수험생의 단어장 카드 각각으로 실제 시험과 같은
파트 5 문제를 하나씩 **새로** 만들고, 문법을 처음 배우는 사람도 이해할 만큼 자세히 해설한다. 카드는 어휘(kind "vocab":
word가 단어·숙어, meaning이 뜻)와 문법 공식(kind "grammar": word가 공식, meaning이 핵심 포인트)이다.

파트 5 형식:
- 회사·업무·고객 안내·공지·일정·계약 같은 비즈니스 맥락의 한 문장(12~28단어), 빈칸 `_____` 하나.
- 보기 4개, 같은 형태끼리(모두 한 단어 또는 모두 구). 대문자는 문장 첫머리 빈칸일 때만.
- **정답은 오직 하나**여야 한다. 다른 보기를 넣으면 문법적으로 틀리거나 뜻이 분명히 어색해야 한다.
  애매하면 문장에 단서(시제 표지, 수 일치, 목적어 유무, 연결어)를 더 넣는다.
- 오답은 **수험생이 실제로 고를 만한 함정**이어야 한다 — 실제로 쓰이는 단어·형태만. 사전에 없는 말
  (usualness), 영어에 없는 형태(to usually), 누가 봐도 틀린 보기는 쓰지 않는다.
- **새 문장**으로 만든다. 카드의 `avoid`는 수험생이 카드에서 이미 본 문장이다 — 그 문장을 고쳐 쓰거나 같은
  상황·같은 핵심 단어 조합을 재사용하지 않는다. 문법 공식에 대표 단어가 여럿이면 avoid에 나온 것과 다른 단어를 고른다.
- 다 만들면 **오답을 하나씩 빈칸에 넣어 읽어 본다.** 원어민이 보기에 문법적으로 맞고 뜻도 자연스러우면
  (예: qualify for a tax break 문제에 tax refund) 그 오답을 바꾸거나 문장을 고친다.

카드별 출제 방식:
- 문법 카드: 그 공식이 정답의 핵심 근거인 문제. **실제 시험처럼 복합적으로 만들어도 좋다** — 문장에 다른 문법 요소
  (시제, 수 일치, 관계사, 분사구, 접속사 등)가 함께 들어가거나, 보기가 두 기준을 동시에 따져야 고를 수 있게
  (예: 품사와 태 — complete / completion / completed / completing). 오답은 실제 시험의 함정으로 — 같은 어근의
  다른 품사, 시제·태, 전치사 vs 접속사(despite / although), 대명사 격 등.
- 어휘 카드: 그 단어(문장에 맞게 활용한 형태 가능)가 정답. 둘 중 하나를 고른다 —
  (1) 어휘 문제: 오답 3개는 같은 품사·같은 형태의 토익 빈출 단어로, 뜻이 비슷해 보이지만 이 문맥에는 안 맞는 것
  (2) 품사 자리 문제: 오답 3개는 그 단어의 파생어(allocate / allocation / allocated / allocating) — 빈칸 앞뒤로
  자리가 분명히 정해지게 쓴다. 숙어는 숙어 속 한 단어(전치사 등)를 빈칸으로 해도 된다(be eligible ___ → for).

각 문제 필드 (해설은 모두 한국어, 문법 용어는 쉬운 말로 풀어서):
- `word_id`: 카드 id 그대로
- `type`: 출제 유형, 15자 이내 (예: "품사 자리 — 부사", "어휘", "전치사 vs 접속사", "수 일치", "능동태 vs 수동태")
- `q`: 문제 문장 (빈칸은 밑줄 5개 `_____`)
- `choices`: 보기 4개 (정답 위치는 섞는다)
- `answer`: 정답, 보기 중 하나와 글자까지 똑같이
- `q_ko`: 빈칸을 정답으로 채운 문장의 자연스러운 해석
- `structure`: 정답을 채운 문장을 의미 덩어리로 끊어 역할을 단 것, 3~7개, 문장 순서대로. 각 항목 `role`(주어, 동사,
  목적어, 보어, 목적격 보어, 수식어, 부사구, 전치사구, 접속사, 관계사절, 분사구 등 짧게)과 `text`(문장 그대로의 덩어리).
  빈칸이 들어간 덩어리는 정답을 채운 형태로 쓴다.
- `explain`: 정답 해설 3~5문장. ① 빈칸 앞뒤를 보고 빈칸이 문장에서 어떤 자리인지(무슨 품사·형태가 와야 하는지)
  ② 정답이 맞는 문법 규칙이나 뜻(카드의 공식·단어와 연결해서) ③ 복합 문제면 함께 따져야 했던 다른 단서
  ④ 실전에서 빨리 푸는 요령 한 마디.
- `why`: 보기 4개 **모두**에 대해 한 줄씩, 보기 순서대로. 각 항목 `choice`(보기 그대로)와 `reason` — 그 보기의 품사·형태나
  뜻을 먼저 밝히고(예: "명사 '완료'", "과거분사/과거형", "'~에도 불구하고' 전치사") 이 자리에 맞는지 / 왜 안 되는지.
- `vocab`: 문장 속에서 수험생이 모를 만한 단어·표현 2~5개(정답 제외, 너무 쉬운 단어 제외). 각 항목 `word`(문장에 나온
  형태)와 `meaning`(이 문장에서의 뜻, 짧게).
문자열 안에서 인용할 때는 큰따옴표(") 대신 작은따옴표(')만 쓴다.

카드(JSON):
{items}

JSON 객체 하나만 출력한다. 설명이나 코드 펜스를 붙이지 않는다. 카드마다 한 문제씩:
{{"items": [{{"word_id": 1, "type": "...", "q": "...", "choices": ["...", "...", "...", "..."], "answer": "...",
"q_ko": "...", "structure": [{{"role": "주어", "text": "..."}}], "explain": "...",
"why": [{{"choice": "...", "reason": "..."}}], "vocab": [{{"word": "...", "meaning": "..."}}]}}]}}
"""


def quiz(job, model):
    """파트 5 문제 만들기(job kind 'quiz') → [{word_id, type, q, choices, answer, q_ko, structure, explain, why, vocab}].
    형식 검사는 서버가 한다."""
    items = [{k: i[k] for k in ("id", "kind", "word", "pos", "meaning", "avoid") if k in i}
             for i in job["payload"]["items"]]
    text = run_claude(QUIZ_PROMPT.format(items=json.dumps(items, ensure_ascii=False, indent=1)), model)
    start = text.find("{")
    if start < 0:
        raise ValueError("응답에 JSON이 없음: " + text[:300])
    # 첫 JSON 객체만 읽는다(뒤에 설명·두 번째 객체가 붙는 경우가 있었다)
    out = json.JSONDecoder().raw_decode(text[start:])[0].get("items")
    if not isinstance(out, list):
        raise ValueError("items 배열이 없음")
    return [x for x in out if isinstance(x, dict)]


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
    images = []
    if image_name:
        full = (WORK / image_name).with_suffix(".full.jpg")
        images = [(image_name, full if full.exists() else WORK / image_name)] + [(t, WORK / t) for t in tiles]
    try:
        text = run_claude(build_prompt(job, image_name, formulas, tiles), model, images)
        return enforce_formulas(parse_result(text), formulas)
    finally:
        for _, path in images:
            path.unlink(missing_ok=True)
        if image_name:
            (WORK / image_name).unlink(missing_ok=True)


def main():
    env = load_env(ENV_FILE)
    api = Api(env["VOCAB_URL"], env["VOCAB_WORKER_TOKEN"])
    # 사진은 작은 볼펜 표시를 놓치지 않게 opus, 이미지 없는 작업은 더 가벼운 모델로 충분하다.
    models = {"photo": env.get("VOCAB_MODEL", "opus"),
              "text": env.get("VOCAB_MODEL_TEXT", "sonnet"),
              "enrich": env.get("VOCAB_MODEL_TEXT", "sonnet"),
              # 중요도는 sonnet이 specific·forecast 같은 단골 어휘를 낮게 매겨 opus로(2026-10-23, 같은 30장 비교)
              "rank": env.get("VOCAB_MODEL_RANK", "opus"),
              "grade": env.get("VOCAB_MODEL_GRADE", "sonnet"),
              "quiz": env.get("VOCAB_MODEL_QUIZ", "opus")}
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
        model = models.get(job["kind"], models["text"])
        LAST_USAGE.clear()
        try:
            if job["kind"] == "grade":
                grades = grade(job, model)
                _, raw = api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"grades": grades})
                log(f"job {job['id']} 채점 완료: {json.loads(raw)['graded']}문항{usage_note()}")
                BACKOFF_FILE.unlink(missing_ok=True)
                continue
            if job["kind"] == "enrich":
                related = enrich(job, model)
                _, raw = api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"related": related})
                res = json.loads(raw)
                log(f"job {job['id']} 채우기 완료: {res['filled']}개{usage_note()}")
                BACKOFF_FILE.unlink(missing_ok=True)
                continue
            if job["kind"] == "rank":
                freq = rank(job, model)
                _, raw = api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"freq": freq})
                log(f"job {job['id']} 중요도 매기기 완료: {json.loads(raw)['ranked']}/{len(freq)}개{usage_note()}")
                BACKOFF_FILE.unlink(missing_ok=True)
                continue
            if job["kind"] == "quiz":
                items = quiz(job, model)
                _, raw = api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"quiz": items})
                log(f"job {job['id']} 파트 5 문제 완료: {json.loads(raw)['quiz']}/{len(items)}개 저장{usage_note()}")
                BACKOFF_FILE.unlink(missing_ok=True)
                continue
            data = process(api, job, model)
            _, raw = api.req("POST", f"/api/worker/jobs/{job['id']}/result",
                             {"words": data["words"], "note": data.get("note") or None})
            res = json.loads(raw)
            log(f"job {job['id']} 완료: 추가 {res['added']}, 중복 {res['duplicates']}{usage_note()}")
            BACKOFF_FILE.unlink(missing_ok=True)
        except ClaudeUnavailable as e:
            log(f"job {job['id']} 보류 — {e} ({BACKOFF_SECONDS // 60}분 뒤 재시도)")
            api.req("POST", f"/api/worker/jobs/{job['id']}/result", {"error": str(e), "release": True})
            BACKOFF_FILE.touch()
            return 1
        except Exception as e:  # 작업 하나가 실패해도 다음 작업은 계속한다
            log(f"job {job['id']} 실패: {e}{usage_note()}")
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
