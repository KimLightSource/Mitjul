#!/usr/bin/env python3
"""단어장 AI 워커 — claude CLI가 로그인된 머신의 cron에서 1분마다 실행한다.

앱 서버에서 대기 중인 작업을 가져가(pull) claude CLI로 처리하고 결과를 돌려보낸다.
워커 쪽은 포트를 열 필요가 없다.
- photo: 책 사진에서 파란 볼펜 표시 → 어휘, 빨간 볼펜 표시 → 문법 공식으로 정리
- text : 사용자가 직접 입력한 단어의 뜻·품사·예문 생성

설정은 ~/.config/vocab/worker.env (권한 0600, 저장소 밖)에서 읽는다:
  VOCAB_URL=http://<앱 서버>:8000
  VOCAB_WORKER_TOKEN=...
  VOCAB_MODEL=sonnet            (선택)
claude 인증은 ~/.config/claude-headless.env 의 CLAUDE_CODE_OAUTH_TOKEN(`claude setup-token`으로
발급한 1년짜리 토큰)을 쓴다. 파일이 없으면 ~/.claude의 로그인 세션을 그대로 쓴다.
표준 라이브러리만 쓴다 — pip/venv 없이 python3만 있으면 된다.
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path.home() / ".config/vocab/worker.env"
CLAUDE_ENV_FILE = Path.home() / ".config/claude-headless.env"
# claude는 이 디렉터리에서 실행한다. 저장소 안에서 돌리면 그 저장소의 CLAUDE.md가 프롬프트에 딸려 들어온다.
WORK = Path.home() / ".cache/vocab-worker"
CLAUDE = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
MAX_JOBS_PER_RUN = 10
CLAUDE_TIMEOUT = 600
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


def build_prompt(job, image_name, existing_grammar):
    template = (HERE / "prompt.md").read_text()
    if job["kind"] == "photo":
        task = (
            f"현재 디렉터리의 `{image_name}` 파일은 사용자가 공부 중인 토익 교재 페이지를 찍은 사진이다. "
            "Read 도구로 이 이미지를 열어라.\n\n"
            "사용자는 볼펜 색으로 표시를 구분한다(밑줄, 동그라미, 덧칠 모두 해당):\n"
            "- **파란색** = 모르는 어휘 → 어휘 항목. 밑줄이 숙어의 일부에 걸쳤으면 의미가 통하는 덩어리로 잡는다.\n"
            "- **빨간색** = 문법 → 표시된 부분과 그 문장이 보여 주는 문법 규칙을 **공식 한 줄**로 정리해 "
            "문법 공식 항목으로 만든다. 공식으로 정리할 만한 규칙이 없는 표시(단순 어휘 등)는 건너뛰고 note에 적는다. "
            "한 문장에 규칙이 여럿 표시돼 있으면 각각 만든다.\n"
            "인쇄된 강조(굵은 글씨, 인쇄된 색 글자, 인쇄된 밑줄), 검정 펜, 연필 표시는 무시한다. "
            "파란·빨간 표시가 하나도 없으면 words를 빈 배열로 하고 note에 그 사실을 적는다."
        )
    else:
        words = "\n".join(f"- {w}" for w in job["payload"]["words"])
        task = (
            "사용자가 직접 입력한 아래 항목 각각에 대해 카드를 만들어라. 단어·숙어면 어휘 항목, "
            "'사역동사 + 목적어 + 동사원형'처럼 문법 규칙이면 문법 공식 항목으로 만든다. "
            f"철자가 틀렸으면 바로잡은 형태로 쓴다.\n\n{words}"
        )
    existing = "\n".join(f"- {g}" for g in existing_grammar) or "(아직 없음)"
    return template.replace("{TASK}", task).replace("{EXISTING_GRAMMAR}", existing)


def parse_result(text):
    # 모델이 규칙을 어기고 코드 펜스나 설명을 붙여도 JSON 객체 부분만 뽑는다.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("응답에 JSON이 없음: " + text[:300])
    data = json.loads(text[start : end + 1])
    if not isinstance(data.get("words"), list):
        raise ValueError("words 배열이 없음")
    return data


def process(api, job, model):
    WORK.mkdir(parents=True, exist_ok=True)
    image_name = None
    if job["kind"] == "photo":
        _, raw = api.req("GET", f"/api/worker/jobs/{job['id']}/image")
        ext = Path(job["filename"]).suffix or ".jpg"
        image_name = f"job-{job['id']}{ext}"
        (WORK / image_name).write_bytes(raw)
    _, raw = api.req("GET", "/api/words?kind=grammar")
    existing_grammar = [w["word"] for w in json.loads(raw)]
    try:
        cmd = [
            CLAUDE, "-p", build_prompt(job, image_name, existing_grammar),
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
        return parse_result(out.get("result", ""))
    finally:
        if image_name:
            (WORK / image_name).unlink(missing_ok=True)


def main():
    env = load_env(ENV_FILE)
    api = Api(env["VOCAB_URL"], env["VOCAB_WORKER_TOKEN"])
    model = env.get("VOCAB_MODEL", "sonnet")
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
