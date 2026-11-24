"""사람이 쓴 자연어 TC 문장('1. 메인 접속 / 2. 상단 [가입하기] 클릭')을 ARIS 키워드로 바꾼다.

규칙 기반이라 바뀐 결과마다 확신도(높음/보통/검토 필요)를 붙인다. 못 바꾼 문장은 이유와 함께 돌려준다.
사용: python excel_tc.py --convert TC.xlsx → 자동화 칸을 채운 사본(TC_변환_날짜.xlsx)
"""
import re

HIGH, MID, LOW = "높음", "보통", "검토 필요"
RANK = {HIGH: 0, MID: 1, LOW: 2}
URL = re.compile(r"https?://[^\s'\"\])]+")
NUM = re.compile(r"^\s*\d+\s*[.)]\s*")
BRACKET = re.compile(r"\[([^\]]+)\]")
QUOTED = re.compile(r"[‘'\"“]([^‘’'\"“”]+)[’'\"”]")
PHONE = re.compile(r"\b\d{2,4}-\d{3,4}(?:-\d{4})?\b")
SHOW = re.compile(r"노출|표시|보여|보이|펼쳐|나타나|있어야|출력")
GO = re.compile(r"(?:접속|진입|이동|열기|방문)(?:한다|함)?$")


def clean(text):
    text = re.sub(r"\s*등$", "", text.strip().strip("-•·").strip())
    if re.search(r"\([^)]*[,/][^)]*\)", text):  # '월패드 (24" / 13")', '연락 방법(전화/이메일)' → 부연 설명은 버림
        text = re.sub(r"\s*\([^)]*\)", "", text)
    text = text.split("|")[-1]  # '로그인 | 바로가기 생성' 처럼 화면 문구에 | 가 있으면 뒤쪽만
    text = re.sub(r"\s*버튼$", "", text.strip())
    return BRACKET.sub(r"\1", text).strip().strip("'\"‘’“”").strip()


def items(text):
    """'A, B, … C 등' → [A, B, C]. 괄호 안 쉼표는 나누지 않는다."""
    out, depth, cur = [], 0, ""
    for ch in clean_parens(text):
        depth += ch == "(" and 1 or ch == ")" and -1 or 0
        if ch in ",…、" and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [x for x in (clean(i) for i in out) if x and x not in ("…", "...")]


def clean_parens(text):
    return text if not re.search(r"\([^)]*[,/][^)]*\)", text) else re.sub(r"\s*\([^)]*[,/][^)]*\)", "", text)


def relative(url, base):
    return url[len(base.rstrip("/")):] or "/" if base and url.startswith(base.rstrip("/")) else url


def page_path(name, pages):
    name = re.sub(r"\s*(?:관련|상세|소개)$", "", clean(name))
    return pages.get(name) or pages.get(f"{name} 페이지") or pages.get(name.replace(" 페이지", ""))


def proc_line(text, ctx):
    """절차 한 줄 → ([키워드 줄...], 확신도, 메모)."""
    t = NUM.sub("", text).strip()
    url = URL.search(t)
    if url and GO.search(t):
        return [f"이동 | {relative(url[0], ctx['base'])}"], HIGH, ""
    if re.search(r"(?:메인|홈)\s*(?:페이지|화면)?\s*(?:에\s*)?(?:접속|진입|이동)", t):
        if ctx["main"]:
            return [f"이동 | {ctx['main']}"], HIGH if "모바일" not in t else MID, \
                "모바일 화면이면 '설정' 시트 화면 크기를 390x844로" if "모바일" in t else ""
        return ["이동 | /"], MID, "메인 주소를 몰라 '웹 주소'로 이동"
    m = re.search(r"(.+?)\s*(?:페이지|화면)\s*(?:에\s*)?(?:접속|진입|이동)", t)
    if m and not BRACKET.search(t):
        path = page_path(m[1], ctx["pages"])
        if path:
            return [f"이동 | {path}"], HIGH, ""
        return [], LOW, f"'{clean(m[1])} 페이지' 주소를 '변수' 시트에 등록하면 자동 변환돼요 (이름: {clean(m[1])} 페이지)"
    if "뒤로" in t and "브라우저" in t:
        return ["뒤로"], HIGH, ""
    if "새로고침" in t:
        return ["새로고침"], HIGH, ""
    targets = [b for b in BRACKET.findall(t)] or QUOTED.findall(t)
    target = clean(targets[-1]) if targets else ""
    v = QUOTED.sub("", BRACKET.sub("", t))  # 동작은 대상 문구 밖에서 찾는다('선택할까요?' 영역 확인 ≠ 선택)
    if re.search(r"마우스\s*오버|오버|호버|마우스를\s*올", v):
        return ([f"올리기 | {target}"], HIGH, "") if target else ([], LOW, "마우스를 올릴 메뉴를 [메뉴명]으로 적어 주세요")
    if re.search(r"입력(?:한다|함|하기)?$", v) and "미입력" not in v:
        q = QUOTED.findall(t)
        if BRACKET.search(t) and q:
            return [f"입력 | {clean(BRACKET.findall(t)[-1])} | {q[-1]}"], HIGH, ""
        return [], LOW, "입력은 [칸 이름]에 '값' 입력 형식으로 적어 주세요"
    if re.search(r"체크\s*해제|해제", v) and target:
        return [f"해제 | {target}"], HIGH, ""
    if re.search(r"체크", v) and target:
        return [f"체크 | {target}"], HIGH, ""
    if re.search(r"클릭|누름|누른다|누르기|선택|터치|탭한다|탭$", v):
        if target:
            icon = re.search(r"아이콘|이미지|로고", v)
            return [f"클릭 | {target}"], MID if icon else HIGH, "아이콘이면 글자 대신 aria-label로 찾아요" if icon else ""
        return [], LOW, "누를 대상을 [버튼명]으로 적거나, 아이콘·로고는 선택자(예: header .logo)로 적어 주세요"
    if re.search(r"확인|스크롤|살펴|본다$", v):
        return [], HIGH, ""  # 눈으로 보는 단계: 판정은 기대 결과에서
    return [], LOW, "어떤 동작인지 알 수 없는 문장"


def page_moved(h, ctx):
    """'X 페이지로 이동되어야 함' → 주소 판정. 주소를 모르면 페이지가 바뀌었는지만(이동됨)."""
    go = re.search(r"(.+?)\s*(?:페이지|화면|사이트)\s*(?:로|으로|가|이)?\s*(?:이동|열려|열림|돌아와|전환|진입)", h)
    if not go:
        return [], HIGH, ""
    name = re.sub(r"^(?:새\s*창에서|다른 .*?으로)\s*", "", go[1]).strip()
    path = page_path(name, ctx["pages"])
    if path:
        return [f"주소 | {path}"], HIGH, ""
    if re.search(r"메인|홈", name) and ctx["main"]:
        return [f"주소 | {ctx['main']}"], HIGH, ""
    return ["이동됨"], MID, f"'{clean(name)} 페이지' 주소를 '변수' 시트에 등록하면 주소까지 확인해요 (지금은 페이지가 바뀌었는지만)"


def expect_block(head, subs, ctx):
    """기대 결과 한 덩어리(머리 줄 + '- 항목' 줄들) → ([키워드 줄...], 확신도, 메모)."""
    h = NUM.sub("", head).strip()
    kw = "보임" if ctx["hovered"] or re.search(r"메뉴|팝업|창", h) else "확인"
    out, conf, notes = [], HIGH, []
    if re.search(r"새\s*창", h):
        ctx["popup"] = True
    moved = page_moved(h, ctx)
    for s in subs:  # '- 브라우저 탭 제목에 'X' 노출', '- 제목: X'
        s = s.strip().lstrip("-•·").strip()
        if "탭 제목" in s or "탭제목" in s:
            q = QUOTED.findall(s) or [s.split(":")[-1]]
            out.append(f"제목 | {clean(q[-1])}")
        else:
            out += [f"{kw} | {x}" for x in items(s.split(":", 1)[-1] if re.match(r"^[^:]{1,6}:", s) else s)]
    if subs:
        if not out:
            return moved[0], LOW, "확인할 문구를 찾지 못함"
        return moved[0] + out, moved[1] if moved[0] else conf, moved[2]
    if moved[0]:
        return moved
    pop = re.search(r"(.+?)\s*(?:팝업|레이어|모달|신청 화면|창)(?:이|가)?\s*(노출|표시|열려|나타|보여|닫혀|사라)", h)
    if pop and not BRACKET.search(h):  # '상담문의 팝업이 노출/닫혀야 함' → 팝업 제목으로 판정
        name = clean(pop[1])
        return [f"{'사라짐' if pop[2] in ('닫혀', '사라') else '보임'} | {name}"], MID, \
            f"'{name}' 문구로 팝업을 확인해요 (팝업이 iframe이어도 자동으로 찾아요)"
    if re.search(r"로드|열려야|정상\s*접속", h) and not SHOW.search(h.replace("로드", "")):
        return [], HIGH, ""  # 이동이 성공했으면 통과
    found = [*re.findall(r"\(([^)]+)\)", h)] if re.search(r"\([^)]*,[^)]*\)", h) else []
    vals = [x for f in found for x in items(f)]
    vals += [clean(b) for b in BRACKET.findall(h)] + [q for q in QUOTED.findall(h)] + PHONE.findall(h)
    vals = list(dict.fromkeys(v for v in vals if v))
    if vals and SHOW.search(h):
        return [f"{kw} | {v}" for v in vals], HIGH if not found else MID, ""
    return [], LOW, "확인할 대상을 '문구'나 [버튼명]으로 적어 주세요"


def blocks(text):
    """기대 결과 칸을 (머리 줄, ['- 항목', ...]) 묶음으로."""
    out = []
    for ln in str(text or "").splitlines():
        if not ln.strip():
            continue
        if ln.strip().startswith(("-", "•", "·")) and out:
            out[-1][1].append(ln)
        else:
            out.append((ln, []))
    return out


def convert(proc, expect, pre="", base="", pages=None):
    """→ {"proc": [줄], "expect": [줄], "confidence": 높음/보통/검토 필요, "notes": [메모]}"""
    pages = pages or {}
    m = re.search(r"메인\s*[:：]\s*(" + URL.pattern + ")", str(pre or ""))
    main = relative(m[1], base) if m else page_path("메인", pages) or ""
    ctx = {"base": base, "pages": pages, "main": main, "hovered": False, "popup": False}
    out_p, out_e, notes, conf = [], [], [], HIGH

    def take(lines, c, note, src):
        nonlocal conf
        conf = max(conf, c, key=RANK.get)
        if note:
            notes.append(f"{NUM.sub('', src).strip()} → {note}")
        return lines

    for ln in [x for x in str(proc or "").splitlines() if x.strip()]:
        got = take(*proc_line(ln, ctx), ln)
        ctx["hovered"] = bool(got) and got[-1].startswith("올리기") or ctx["hovered"] and not got
        out_p += got
    for head, subs in blocks(expect):
        out_e += take(*expect_block(head, subs, ctx), head)
    if ctx["popup"]:  # '새 창에서 열려야 함' → 마지막 클릭을 새창으로
        for i in range(len(out_p) - 1, -1, -1):
            if out_p[i].startswith("클릭 | "):
                out_p[i] = "새창 | " + out_p[i][5:]
                break
    if not out_e:
        conf = LOW
        notes.append("판정할 기대 결과가 없어 실행해도 검증이 안 돼요")
    return {"proc": out_p, "expect": out_e, "confidence": conf, "notes": notes}
