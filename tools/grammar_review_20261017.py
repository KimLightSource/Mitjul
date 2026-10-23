"""2026-10-17 문법 공식 전체 재검토(토익 출제 유형) — 일회용.

grammar-formulas.tsv(공식<TAB>포인트, 108줄)를 같은 순서로 읽어 6열 형식
(공식, 포인트, 유형, 문제, 보기, 정답)으로 다시 쓰고, 표기가 바뀐 공식은 old→new 대응표를
JSON으로 남긴다(DB 카드 이전용). 공식·포인트를 바꾸지 않는 줄은 None.

    python3 tools/grammar_review_20261017.py worker/grammar-formulas.tsv /tmp/formula-renames.json
"""
import json
import sys

# 번호(데이터 줄 순서, 1부터): (새 공식 또는 None, 새 포인트 또는 None, 유형, 문제, [보기], 정답)
R = {
 1: (None, None, "동사 수 일치", "The number of applicants for the position _____ increased sharply this year.", ["has", "have", "having", "to have"], "has"),
 2: (None, None, "수량 표현 a/the number of", "_____ number of customers have complained about the new billing system.", ["A", "The", "Each", "Much"], "A"),
 3: (None, None, "수량 형용사 + 단수명사", "_____ department manager must submit a budget proposal by Friday.", ["Every", "All", "Many", "Several"], "Every"),
 4: (None, None, "동사 수 일치", "One of the new printers on the third floor _____ out of order.", ["is", "are", "were", "have been"], "is"),
 5: (None, None, "동사 수 일치 — 수식어 건너뛰기", "The documents on the manager's desk _____ to be signed by noon.", ["need", "needs", "needing", "to need"], "need"),
 6: (None, None, "관계사절 동사 수 일치", "Employees who _____ to attend the workshop should register online.", ["wish", "wishes", "wishing", "to wish"], "wish"),
 7: ("either A or B / neither A nor B", "상관접속사는 짝이 정해져 있다 — 동사는 B에 수 일치", "상관접속사 짝", "Either Mr. Kim _____ Ms. Park will lead the training session.", ["or", "nor", "and", "but"], "or"),
 8: ("not only A but (also) B", "not only는 but (also)와 짝 — 동사는 B에 수 일치", "상관접속사 짝", "The new software is not only fast _____ also easy to use.", ["but", "and", "or", "so"], "but"),
 9: ("both A and B + 복수동사", "both는 and와 짝 — 두 개를 묶었으니 복수동사", "상관접속사 짝", "Both the CEO _____ the CFO will attend the shareholders' meeting.", ["and", "or", "nor", "but"], "and"),
 10: (None, None, "주어 자리 — 동명사", "_____ new clients is the top priority for the sales team this quarter.", ["Attracting", "Attract", "Attracted", "Attracts"], "Attracting"),
 11: (None, None, "사역동사 목적격 보어", "The manager had his assistant _____ the conference room.", ["prepare", "to prepare", "prepared", "preparing"], "prepare"),
 12: (None, None, "사역동사 목적격 보어 — 수동", "Ms. Lee had her car _____ at the service center.", ["repaired", "repair", "to repair", "repairing"], "repaired"),
 13: (None, None, "get 목적격 보어", "The director finally got the board _____ the proposal.", ["to approve", "approve", "approved", "approving"], "to approve"),
 14: (None, None, "help 목적격 보어", "The new system will help employees _____ their time more efficiently.", ["manage", "managing", "managed", "manages"], "manage"),
 15: (None, None, "지각동사 목적격 보어", "Visitors watched the technician _____ the new equipment.", ["install", "to install", "installs", "installed"], "install"),
 16: (None, None, "5형식 동사 목적격 보어", "The company encourages employees _____ part in volunteer activities.", ["to take", "take", "taking", "taken"], "to take"),
 17: (None, None, "수동태 뒤 to부정사", "Visitors are required _____ a valid ID at the front desk.", ["to present", "presenting", "present", "presented"], "to present"),
 18: (None, None, "목적격 보어 자리 — 형용사", "Regular maintenance keeps the machines _____.", ["operational", "operation", "operate", "operationally"], "operational"),
 19: (None, None, "주격 보어 자리 — 형용사", "Despite the downturn, sales remained _____ throughout the year.", ["stable", "stability", "stably", "stabilize"], "stable"),
 20: (None, None, "that절 동사원형(당위)", "The consultant recommended that the company _____ its marketing strategy.", ["revise", "revises", "revised", "revising"], "revise"),
 21: (None, None, "that절 동사원형(당위)", "It is essential that every participant _____ the form before the session.", ["complete", "completes", "completed", "completing"], "complete"),
 22: (None, None, "동사 뒤 동명사 vs to부정사", "The committee is considering _____ the deadline by one week.", ["extending", "to extend", "extend", "extended"], "extending"),
 23: (None, None, "동사 뒤 동명사 vs to부정사", "The firm plans _____ two new branches next year.", ["to open", "opening", "open", "opened"], "to open"),
 24: (None, None, "동사 뒤 준동사", "Sales of the new model have continued _____ steadily.", ["to rise", "rise", "risen", "rose"], "to rise"),
 25: (None, None, "관용 표현 + -ing", "Many users had difficulty _____ the new software.", ["installing", "to install", "install", "installed"], "installing"),
 26: (None, None, "to부정사의 의미상 주어", "It is difficult _____ new employees to learn all the procedures in a week.", ["for", "of", "to", "with"], "for"),
 27: (None, None, "전치사 to + -ing", "We look forward to _____ with you again.", ["working", "work", "be working", "have worked"], "working"),
 28: (None, None, "전치사 to + -ing", "Our company is committed to _____ high-quality service.", ["providing", "provide", "provided", "provides"], "providing"),
 29: (None, None, "be used to + -ing", "Ms. Chen is used to _____ long hours during the busy season.", ["working", "work", "worked", "works"], "working"),
 30: (None, None, "used to + 동사원형", "The building used to _____ a textile factory.", ["house", "housing", "housed", "houses"], "house"),
 31: (None, None, "전치사 뒤 동명사", "In addition to _____ discounts, the store provides free delivery.", ["offering", "offer", "offered", "offers"], "offering"),
 32: (None, None, "명사 뒤 to부정사", "Interns will have the opportunity _____ with senior managers.", ["to work", "working", "work", "worked"], "to work"),
 33: (None, None, "too ~ to부정사", "The package was _____ heavy to be shipped by regular mail.", ["too", "so", "very", "enough"], "too"),
 34: (None, None, "형용사 + enough to", "The conference room is large _____ to hold 200 people.", ["enough", "too", "so", "very"], "enough"),
 35: (None, None, "목적 — in order to", "In order to _____ costs, the company switched suppliers.", ["reduce", "reducing", "reduced", "reduction"], "reduce"),
 36: (None, None, "전치사 뒤 동명사", "You can reserve a seat by _____ the online form.", ["completing", "complete", "completed", "completion"], "completing"),
 37: (None, None, "동명사 vs 명사 — 뒤에 목적어", "The team is responsible for _____ the new product line.", ["launching", "launch", "launches", "launched"], "launching"),
 38: (None, None, "시간·조건 부사절 시제", "Please call me as soon as the shipment _____.", ["arrives", "will arrive", "arrived", "arriving"], "arrives"),
 39: (None, None, "시제 — since", "Mr. Park _____ for the company since 2019.", ["has worked", "works", "worked", "will work"], "has worked"),
 40: (None, None, "시제 — over the past", "Over the past five years, the company _____ its revenue.", ["has doubled", "doubles", "will double", "is doubling"], "has doubled"),
 41: (None, None, "시제 — by the time", "By the time the guests arrived, the staff _____ the hall.", ["had decorated", "decorate", "will decorate", "have decorated"], "had decorated"),
 42: (None, None, "가정법 과거", "If the budget were larger, we _____ more staff.", ["would hire", "will hire", "hire", "had hired"], "would hire"),
 43: (None, None, "가정법 과거완료", "If we had known about the delay, we _____ the meeting.", ["would have postponed", "will postpone", "postponed", "would postpone"], "would have postponed"),
 44: (None, None, "가정법 도치 — Should", "_____ you have any questions, please contact customer service.", ["Should", "If", "Were", "Had"], "Should"),
 45: (None, None, "가정법 도치 — Had", "_____ the team started earlier, the project would have been finished on time.", ["Had", "If", "Should", "Were"], "Had"),
 46: (None, None, "보어 도치 — Enclosed", "_____ is a copy of the signed contract.", ["Enclosed", "Enclose", "Enclosing", "Enclosure"], "Enclosed"),
 47: (None, None, "감정 분사 — 사람", "Customers were _____ with the quality of the service.", ["satisfied", "satisfying", "satisfy", "satisfaction"], "satisfied"),
 48: (None, None, "감정 분사 — 사물", "The keynote speaker gave a very _____ presentation.", ["interesting", "interested", "interest", "interestingly"], "interesting"),
 49: (None, None, "명사 뒤 분사", "The report _____ by the finance team was approved yesterday.", ["prepared", "preparing", "prepare", "preparation"], "prepared"),
 50: (None, None, "분사구문", "_____ in 1990, the company now has offices in ten countries.", ["Founded", "Founding", "Found", "To found"], "Founded"),
 51: (None, None, "접속사 + 분사", "When _____ by reporters, the spokesperson declined to comment.", ["asked", "asking", "ask", "to ask"], "asked"),
 52: ("목적어 있음 → 능동태 / 목적어 없음 → be p.p.", "빈칸 뒤에 목적어가 없으면 수동태(be p.p.)", "능동태 vs 수동태", "The new policy _____ at the next board meeting.", ["will be discussed", "will discuss", "discussed", "discussing"], "will be discussed"),
 53: ("자동사(occur/happen/arise/rise) → 수동태 불가", "목적어를 못 갖는 자동사는 수동태로 쓸 수 없다", "자동사 — 수동태 불가", "Several problems _____ during the installation.", ["arose", "were arisen", "arisen", "arising"], "arose"),
 54: (None, None, "소유격 관계대명사", "We hired a designer _____ work has won several awards.", ["whose", "who", "which", "whom"], "whose"),
 55: (None, None, "명사절 접속사 what", "_____ the customers want most is faster delivery.", ["What", "That", "Which", "Whether"], "What"),
 56: (None, None, "전치사 + 관계대명사", "The hotel at _____ we stayed offers free breakfast.", ["which", "that", "where", "what"], "which"),
 57: (None, None, "관계부사", "The town _____ the factory is located has a population of 20,000.", ["where", "which", "what", "whose"], "where"),
 58: ("콤마(,) + which/who — that 불가", "콤마 뒤 계속적 용법에는 that을 쓰지 않는다", "계속적 용법 관계사", "The new branch, _____ opened last month, has already exceeded its sales target.", ["which", "that", "what", "where"], "which"),
 59: (None, None, "whether + to부정사", "The board has not decided _____ to expand overseas.", ["whether", "if", "that", "about"], "whether"),
 60: (None, None, "전치사 vs 접속사", "_____ the heavy rain, the outdoor event was a success.", ["Despite", "Although", "Even though", "However"], "Despite"),
 61: (None, None, "접속사 vs 전치사", "_____ the product is expensive, it sells well.", ["Although", "Despite", "In spite of", "Nevertheless"], "Although"),
 62: (None, None, "전치사 vs 접속사", "The flight was delayed _____ bad weather.", ["due to", "because", "since", "so that"], "due to"),
 63: (None, None, "during vs while vs for", "Please turn off your phones _____ the presentation.", ["during", "while", "when", "as"], "during"),
 64: (None, None, "while vs during", "_____ waiting for the bus, she reviewed her notes.", ["While", "During", "For", "Despite"], "While"),
 65: (None, None, "until vs by", "The store will remain open _____ 9 P.M.", ["until", "by", "since", "within"], "until"),
 66: (None, None, "by vs until", "All reports must be submitted _____ Friday.", ["by", "until", "since", "during"], "by"),
 67: (None, None, "조건 접속사", "You may return the item _____ you keep the receipt.", ["provided", "unless", "despite", "although"], "provided"),
 68: (None, None, "형용사 자리 — 명사 앞", "The company offers a _____ selection of products.", ["wide", "widely", "width", "widen"], "wide"),
 69: (None, None, "부사 자리 — be와 p.p. 사이", "The event was _____ organized by the volunteers.", ["carefully", "careful", "care", "carefulness"], "carefully"),
 70: (None, None, "부사 자리 — have와 p.p. 사이", "Sales have _____ increased since the campaign began.", ["steadily", "steady", "steadiness", "steadied"], "steadily"),
 71: (None, None, "소유격 + own", "Participants must bring their _____ laptops.", ["own", "owner", "owned", "owning"], "own"),
 72: ("by + 재귀대명사(oneself) = 혼자서", None, "재귀대명사 관용 표현", "Ms. Kim completed the entire report by _____.", ["herself", "her", "hers", "she"], "herself"),
 73: (None, None, "비교급 + than", "The new model is _____ than the previous one.", ["lighter", "lightest", "light", "lightly"], "lighter"),
 74: (None, None, "최상급", "This is the _____ hotel in the city.", ["largest", "larger", "large", "largely"], "largest"),
 75: (None, None, "the 비교급, the 비교급", "The earlier you register, the _____ the discount.", ["greater", "great", "greatest", "greatly"], "greater"),
 76: (None, None, "as 원급 as", "The new printer is as _____ as the old one.", ["reliable", "more reliable", "most reliable", "reliably"], "reliable"),
 77: (None, None, "비교급 강조", "This year's profits are _____ higher than last year's.", ["much", "very", "more", "most"], "much"),
 78: (None, None, "so vs such", "The demand was _____ high that the product sold out in a day.", ["so", "such", "too", "very"], "so"),
 79: (None, None, "such vs so", "It was _____ a popular event that tickets sold out quickly.", ["such", "so", "too", "very"], "such"),
 80: (None, None, "수량 형용사 — 가산", "Only a _____ applicants were invited to the second interview.", ["few", "little", "much", "less"], "few"),
 81: (None, None, "수량 형용사 — 불가산", "There is _____ information about the merger available.", ["little", "few", "many", "a few"], "little"),
 82: (None, None, "another vs other", "Please submit _____ copy of the receipt.", ["another", "other", "others", "the others"], "another"),
 83: ("it → 단수 명사 / they·them → 복수 명사", "대명사는 가리키는 명사와 수를 맞춘다", "대명사 수 일치", "The new laptops sell well because _____ are lightweight.", ["they", "it", "them", "its"], "they"),
 84: ("명사 + 재귀대명사(myself/himself/themselves) — 강조", "강조 용법은 '직접, 그 자신이' — 빼도 문장이 성립한다", "재귀대명사 강조", "The CEO _____ will welcome the new employees.", ["himself", "him", "his", "he"], "himself"),
 85: ("no + 명사 = not a(n) + 명사", None, "no vs not", "_____ refunds will be given after 30 days.", ["No", "Not", "None", "Nothing"], "No"),
 86: ("most + 명사 / most of the + 명사 / almost all (of) the + 명사", "almost는 부사라 명사 앞에 못 온다 — 동사는 of 뒤 명사에 수 일치", "most / most of / almost", "_____ the employees attended the annual workshop.", ["Most of", "Almost", "Most", "The most"], "Most of"),
 87: ("동사·전치사 뒤 → 목적격(him) / 명사 앞 → 소유격(his) / 주어 = 목적어 → 재귀대명사(himself)", None, "대명사 격", "Ms. Ortiz will present _____ findings at the meeting.", ["her", "she", "hers", "herself"], "her"),
 88: (None, None, "those + 수식어", "_____ interested in the position should contact Ms. Wong.", ["Those", "They", "Them", "Whoever"], "Those"),
 89: (None, None, "부사 자리 — 동사 수식", "The technician _____ checked all the equipment before the inspection.", ["thoroughly", "thorough", "thoroughness", "more thorough"], "thoroughly"),
 90: (None, None, "부사 자리 — 형용사 수식", "The instructions were _____ clear.", ["remarkably", "remarkable", "remark", "remarked"], "remarkably"),
 91: (None, None, "부사 자리 — 숫자 앞", "The renovation will cost _____ $50,000.", ["approximately", "approximate", "approximation", "approximated"], "approximately"),
 92: (None, None, "부사 자리 — 문장 끝", "The staff handled the complaint _____.", ["professionally", "professional", "profession", "professionalism"], "professionally"),
 93: ("usually·often → 문장 앞·뒤 가능 / always → 동사 앞·be동사 뒤만", None, "빈도부사", "Mr. Davis _____ reviews the reports himself.", ["always", "ever", "yet", "soon"], "always"),
 94: (None, None, "부정 부사", "There were _____ any seats left at the concert.", ["hardly", "almost", "nearly", "mostly"], "hardly"),
 95: (None, None, "부정 부사 도치", "Rarely _____ the CEO attend department meetings.", ["does", "is", "has", "do"], "does"),
 96: (None, None, "시제 단서 — 빈도부사", "The shuttle bus _____ runs every 15 minutes.", ["usually", "soon", "recently", "lately"], "usually"),
 97: (None, None, "시제 단서 — 미래 부사", "The new branch will open _____.", ["shortly", "lately", "recently", "previously"], "shortly"),
 98: (None, None, "접속부사 vs 접속사", "Sales increased; _____, profits declined.", ["however", "although", "despite", "because"], "however"),
 99: (None, None, "only + 전치사구", "The discount is available _____ to members.", ["only", "alone", "single", "sole"], "only"),
 100: (None, None, "well + 전치사구", "Attendance was _____ below expectations.", ["well", "good", "very", "so"], "well"),
 101: (None, None, "강조 부사 even", "The software is easy to use, _____ for beginners.", ["even", "ever", "yet", "still"], "even"),
 102: (None, None, "quite a + 명사", "The merger was _____ a surprise to investors.", ["quite", "very", "too", "so"], "quite"),
 103: (None, None, "원급 강조", "The new laptop is _____ as fast as the desktop model.", ["almost", "most", "mostly", "more"], "almost"),
 104: (None, None, "최상급 강조", "This is _____ the best proposal we have received.", ["by far", "very", "much more", "so"], "by far"),
 105: (None, None, "also vs too vs either", "Ms. Ito speaks Spanish fluently. She speaks Japanese _____.", ["too", "also", "either", "neither"], "too"),
 106: ("부정문 뒤 → not ~ either / 긍정문 뒤 → ~ too", "부정문에 '~도 아니다'를 덧붙일 때는 문장 끝 either — too X", "either vs too", "The manager didn't attend the meeting, and her assistant didn't _____.", ["either", "too", "also", "neither"], "either"),
 107: ("시간 + later = ~ 후에 / thereafter = 그 이후에", "later는 시간 표현 뒤에서 '~ 후에' — '그 이후에'는 thereafter", "later vs ago", "The order was placed on Monday and arrived two days _____.", ["later", "ago", "since", "lately"], "later"),
 108: (None, None, "fewer vs less", "The new process requires _____ steps than the old one.", ["fewer", "less", "little", "much"], "fewer"),
}

src, renames_out = sys.argv[1], sys.argv[2]
out, renames, n = [], {}, 0
for line in open(src, encoding="utf-8").read().splitlines():
    if not line.strip() or line.startswith("#"):
        out.append(line)
        continue
    n += 1
    formula, point = line.split("\t")[:2]
    nf, np, typ, q, choices, ans = R[n]
    assert ans in choices and "_____" in q, n
    new_f, new_p = nf or formula, np or point
    if new_f != formula or new_p != point:
        renames[formula] = {"word": new_f, "meaning": new_p}
    out.append("\t".join([new_f, new_p, typ, q, " | ".join(choices), ans]))
assert n == len(R), (n, len(R))
open(src, "w", encoding="utf-8").write("\n".join(out) + "\n")
json.dump(renames, open(renames_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"{n}줄 갱신, 표기·포인트 변경 {len(renames)}개")
