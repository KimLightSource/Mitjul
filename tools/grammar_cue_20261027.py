"""2026-10-27 문법 공식 앞면 단서(cue) 채우기 — 일회용.

grammar-formulas.tsv의 줄마다 카드 앞면용 회상 단서(front: 핵심을 ?로 가린 공식, key: 가린 핵심)를
만든다. 공식을 통째로 앞면에 보여 주면 외웠는지 확인할 수 없다는 사용자 지적으로 시작했다.

    # 1) AI로 만들어 JSON으로 저장(목록은 안 바꿈) — 사람이 읽고 고친다
    python3 tools/grammar_cue_20261027.py gen /tmp/cues.json
    # 2) 검토한 JSON을 목록 7·8열(앞면, 정답)로 쓴다
    python3 tools/grammar_cue_20261027.py apply /tmp/cues.json

규칙 문구는 워커의 CUE_RULES(새 공식을 만들 때 쓰는 것)를 그대로 쓴다.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import vocab_worker as v  # noqa: E402

PROMPT = """너는 토익(TOEIC) 문법을 가르치는 강사다. 한국인 수험생의 단어장 앱에 있는 문법 공식 카드마다 앞면 단서를 만든다.
공식(formula)·포인트(point)·출제 유형(type)은 바꾸지 않는다.

{rules}
공식(JSON):
{items}

JSON 객체 하나만 출력한다. 설명이나 코드 펜스를 붙이지 않는다. 모든 공식에 대해 no를 그대로 써서 한 줄씩:
{{"cues": [{{"no": 1, "front": "... + ?", "key": "..."}}]}}
"""
BATCH = 36


def data_lines():
    lines = v.FORMULAS_FILE.read_text().split("\n")
    return lines, [i for i, l in enumerate(lines) if l.strip() and not l.startswith("#")]


def gen(out_path, model="opus"):
    lines, idx = data_lines()
    rows = []
    for no, i in enumerate(idx, 1):
        cols = lines[i].split("\t")
        rows.append({"no": no, "formula": cols[0], "point": cols[1], "type": cols[2] if len(cols) > 2 else ""})

    def run(batch):
        text = v.run_claude(PROMPT.format(rules=v.CUE_RULES, items=json.dumps(batch, ensure_ascii=False, indent=1)), model)
        s, e = text.find("{"), text.rfind("}")
        return json.loads(text[s : e + 1])["cues"]

    batches = [rows[i : i + BATCH] for i in range(0, len(rows), BATCH)]
    with ThreadPoolExecutor(len(batches)) as ex:
        cues = {c["no"]: c for part in ex.map(run, batches) for c in part}
    out = [{**r, **{k: (cues.get(r["no"]) or {}).get(k, "") for k in ("front", "key")}} for r in rows]
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"{len(out)}줄, 빠진 것 {[r['no'] for r in out if not v.clean_cue(r)]}")


def apply(in_path):
    lines, idx = data_lines()
    cues = {r["no"]: r for r in json.loads(Path(in_path).read_text())}
    for no, i in enumerate(idx, 1):
        cue = v.clean_cue(cues.get(no))
        cols = lines[i].split("\t")
        if not cue or cols[0] != cues[no]["formula"]:
            raise SystemExit(f"{no}번 줄이 안 맞음: {cols[0]}")
        cols = (cols + [""] * 6)[:6] + [cue["front"], cue["key"]]
        lines[i] = "\t".join(cols)
    v.FORMULAS_FILE.write_text("\n".join(lines))
    print(f"{len(idx)}줄에 앞면 단서를 썼다")


if __name__ == "__main__":
    {"gen": gen, "apply": apply}[sys.argv[1]](sys.argv[2])
