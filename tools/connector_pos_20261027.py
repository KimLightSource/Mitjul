"""2026-10-27 연결어(접속사·접속부사·전치사) 품사 세분화 — 일회용.

`접속사` 하나로 뭉쳐 있던 품사를 명사절 접속사 / 부사절 접속사 / 접속부사 / 등위·상관접속사 / 전치사로 나누고,
역할이나 뜻이 갈리는 단어(once, since, while…)는 줄을 나눠 줄마다 예문을 단다. 사용자 요청으로 시작했다.
새 카드는 worker/prompt.md의 "연결어 품사" 규칙으로 만들어진다 — 이 도구는 이미 있는 카드용이다.

    # 1) AI로 바꿀 카드만 골라 JSON으로 저장(DB는 안 바꿈) — 사람이 읽고 고친다
    python3 tools/connector_pos_20261027.py gen /tmp/conn.json < words.json
    # 2) 검토한 JSON을 DB에 쓴다(백업 먼저). DB가 컨테이너 볼륨에 있으면 앱을 멈추고 실행한다
    python3 tools/connector_pos_20261027.py apply /path/to/vocab.db < /tmp/conn.json

words.json은 [{id, word, pos, meaning, example, senses}] — 접속사·전치사·부사·구 카드.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROMPT = """너는 토익(TOEIC) 문법·어휘를 가르치는 강사다. 한국인 수험생의 단어장 카드 중 **문장을 잇는 연결어**
(접속사·접속부사, 그리고 같은 뜻을 가진 전치사)의 품사 표기를 역할별로 고친다.

{rule}

## 할 일

아래 카드 중 연결어 역할이 있는 카드만 고친다. 연결어가 아닌 카드(hastily, be eligible for…)와 이미 규칙대로인 카드는
출력하지 않는다.

- `pos`: 첫 줄의 품사(위 이름). 첫 줄은 지금 카드의 첫 줄 뜻(책 문맥)을 유지한다 — 이름만 바꾸거나, 같은 역할 안에서
  뜻이 갈리면 나눈다.
- `meaning`: 줄바꿈(\\n)으로 나눈 전체 뜻. 둘째 줄부터 `[부사절 접속사] ~ 때문에`처럼 품사를 앞에 붙인다.
- `example`, `example_ko`: 지금 첫 예문이 새 첫 줄 뜻에 맞으면 **그대로** 복사한다. 안 맞을 때만 새로 쓴다.
- `senses`: 둘째 줄부터 줄마다 하나씩 {{pos, example, example_ko}}. 지금 senses에 그 줄에 맞는 예문이 있으면 그대로 쓰고,
  없으면 그 역할·뜻으로 쓰인 토익 파트 5·6·7 스타일 새 예문(비즈니스 맥락, 10~20단어, 표제어 포함)을 쓴다.
  예문은 반드시 그 줄의 역할로 문법에 맞게 쓰여야 한다(부사절 접속사 줄이면 뒤에 주어+동사, 전치사 줄이면 뒤에 명사).

카드(JSON):
{items}

JSON 객체 하나만 출력한다. 설명이나 코드 펜스를 붙이지 않는다. 고칠 카드만:
{{"cards": [{{"id": 1, "word": "...", "pos": "...", "meaning": "...", "example": "...", "example_ko": "...", "senses": [{{"pos": "...", "example": "...", "example_ko": "..."}}], "why": "무엇을 왜 바꿨는지 한 줄"}}]}}
"""
BATCH = 45


def rule():
    """prompt.md의 연결어 품사 규칙 문단을 그대로 쓴다(새 카드와 같은 기준)."""
    text = (Path(__file__).resolve().parent.parent / "worker" / "prompt.md").read_text()
    m = re.search(r"^- \*\*연결어 품사\*\*.*?(?=^- `example`)", text, re.S | re.M)
    return m.group(0)


def gen(out_path, model="opus"):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
    import vocab_worker as v

    rows = json.load(sys.stdin)

    def run(batch):
        text = v.run_claude(PROMPT.format(rule=rule(), items=json.dumps(batch, ensure_ascii=False, indent=1)), model)
        s, e = text.find("{"), text.rfind("}")
        return json.loads(text[s : e + 1])["cards"]

    batches = [rows[i : i + BATCH] for i in range(0, len(rows), BATCH)]
    with ThreadPoolExecutor(len(batches)) as ex:
        cards = [c for part in ex.map(run, batches) for c in part]
    Path(out_path).write_text(json.dumps(sorted(cards, key=lambda c: c["id"]), ensure_ascii=False, indent=1))
    print(f"{len(rows)}장 중 {len(cards)}장 고침")


def apply(db_path):
    """컨테이너 안에서: stdin의 검토한 JSON을 DB에 쓴다. related의 다른 키(바꿔 말하기 등)는 그대로 둔다."""
    import sqlite3

    cards = json.load(sys.stdin)
    con = sqlite3.connect(db_path)
    for c in cards:
        (rel,) = con.execute("select related from words where id = ?", (c["id"],)).fetchone()
        rel = json.loads(rel) if rel else {}
        lines = c["meaning"].count("\n")
        if len(c["senses"]) != lines:
            raise SystemExit(f"{c['id']} {c['word']}: 뜻 {lines + 1}줄인데 예문 {len(c['senses'])}개")
        rel.pop("senses", None)
        if c["senses"]:
            rel["senses"] = [{k: s[k] for k in ("pos", "example", "example_ko")} for s in c["senses"]]
        con.execute("update words set pos = ?, meaning = ?, example = ?, example_ko = ?, related = ? where id = ?",
                    (c["pos"], c["meaning"], c["example"], c["example_ko"],
                     json.dumps(rel, ensure_ascii=False) if rel else "", c["id"]))
    con.commit()
    print(f"{len(cards)}장 고침")


if __name__ == "__main__":
    {"gen": gen, "apply": apply}[sys.argv[1]](sys.argv[2])
