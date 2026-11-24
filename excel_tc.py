"""엑셀 TC를 읽어 실행하고, 결과(Pass/Fail·실패 사유·이슈 번호)를 엑셀 사본에 기록한다.

사용법: python excel_tc.py TC.xlsx          (실행)
        python excel_tc.py --check TC.xlsx  (실행하지 않고 어떻게 읽었는지만 확인)
        python excel_tc.py --convert TC.xlsx  (자연어 절차·기대 결과를 키워드로 바꾼 사본 만들기)
TC 시트는 첫 줄 제목으로 칸을 찾는다(순서 무관). 필수: TC ID, 유형, 테스트 절차
선택: 테스트 항목, 사전조건, 기대 결과, 실행(Y/N), 중요도. 결과/실패 사유/이슈 번호 칸은 없으면 새로 만든다.
절차/기대 결과는 한 줄에 하나씩 `키워드 | 대상 | 값` 형식 (키워드는 '키워드' 시트 참고).
'변수' 시트의 값은 ${이름}으로, '공통단계' 시트의 절차는 `공통 | 이름`으로 불러 쓴다.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter

import aris
import convert

TYPES = {"웹": "web", "앱": "app", "API": "api", "스크립트": "script"}
UI_KEYWORDS = {"이동": "goto", "입력": "fill", "클릭": "click", "키": "press", "확인": "expect_text", "대기": "wait",
               "보임": "expect_visible", "사라짐": "expect_hidden", "존재": "expect_exists", "없음": "expect_absent",
               "올리기": "hover", "체크": "check", "해제": "uncheck", "선택": "select", "프레임": "frame",
               "새창": "popup", "창닫기": "close", "뒤로": "back", "새로고침": "reload",
               "주소": "expect_url", "제목": "expect_title", "알림": "expect_alert", "이동됨": "expect_navigated",
               "스크롤": "scroll_to", "스와이프": "swipe", "키보드닫기": "hide_keyboard", "앱재시작": "restart_app"}
APP_ONLY_MISSING = {"이동", "키", "올리기", "체크", "해제", "선택", "프레임", "새창", "창닫기", "새로고침",
                    "주소", "제목", "알림", "이동됨", "없음"}  # 앱에서는 쓸 수 없는 키워드
WEB_MISSING = {"스크롤", "스와이프", "키보드닫기", "앱재시작"}  # 앱 전용 키워드
NO_ARG = {"뒤로", "새로고침", "창닫기", "이동됨", "키보드닫기", "앱재시작"}  # 대상 없이 한 단어로 쓰는 키워드
THREE_PART = {"입력": "입력 | 대상 | 값", "선택": "선택 | 목록 | 고를 항목"}
SETTINGS = {"API 주소": ("api", "base_url"), "웹 주소": ("web", "base_url"), "Appium 서버": ("app", "server")}
APP_SETTINGS = {"앱 파일": "appium:app", "앱 패키지": "appium:appPackage", "앱 시작 화면": "appium:appActivity",
                "기기 이름": "appium:deviceName"}  # 쉬운 이름 → Appium 설정 이름
WAITS = {"페이지 로딩 대기(초)": "nav_timeout_ms", "판정 대기(초)": "timeout_ms", "TC 간 대기(초)": "case_delay_ms"}
FILLS = {"PASS": PatternFill("solid", fgColor="C6EFCE"), "FAIL": PatternFill("solid", fgColor="FFC7CE"),
         "작성 오류": PatternFill("solid", fgColor="FFEDD5"), "미실행": PatternFill("solid", fgColor="E7E6E6"),
         "변환 필요": PatternFill("solid", fgColor="DDEBF7")}


def value(text):
    """엑셀에 쓴 값을 JSON 타입으로: 1 → 숫자, true → 불리언, 그 외는 문자열."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def lines(cell):
    return [[p.strip() for p in ln.split("|")] for ln in str(cell or "").splitlines() if ln.strip()]


def parse_ui(kind, cell_lines):
    steps = []
    for parts in cell_lines:
        kw = parts[0]
        if kw not in UI_KEYWORDS or (kind == "app" and kw in APP_ONLY_MISSING) or (kind == "web" and kw in WEB_MISSING):
            raise ValueError(f"{'앱' if kind == 'app' else '웹'}에서 쓸 수 없는 키워드: {kw}")
        if kw in THREE_PART:
            if len(parts) < 3:
                raise ValueError(f"{kw}은 '{THREE_PART[kw]}' 형식")
            steps.append({UI_KEYWORDS[kw]: parts[1], "text": parts[2]})
        elif kw in NO_ARG:
            steps.append({UI_KEYWORDS[kw]: ""})
        else:
            if len(parts) < 2:
                raise ValueError(f"'{kw}' 뒤에 대상이 없음")
            steps.append({UI_KEYWORDS[kw]: parts[1]})
    return steps


def parse_api(proc, expect):
    case, exp = {}, {}
    for parts in proc:
        if parts[0] == "요청" and len(parts) >= 3:
            case["method"], case["path"] = parts[1].upper(), parts[2]
        elif parts[0] == "본문" and len(parts) >= 2:
            try:
                case["body"] = json.loads("|".join(parts[1:]))
            except ValueError:
                raise ValueError("본문은 JSON 형식이어야 함 (예: {\"title\": \"a\"})")
        elif parts[0] == "헤더" and len(parts) >= 3:
            case.setdefault("headers", {})[parts[1]] = parts[2]
        else:
            raise ValueError(f"알 수 없는 API 절차: {' | '.join(parts)}")
    for parts in expect:
        if parts[0] == "상태" and len(parts) >= 2:
            exp["status"] = int(parts[1])
        elif parts[0] == "시간" and len(parts) >= 2:
            exp["max_ms"] = int(parts[1])
        elif parts[0] == "값" and len(parts) >= 3:
            exp.setdefault("json", {})[parts[1]] = value(parts[2])
        else:
            raise ValueError(f"알 수 없는 API 기대 결과: {' | '.join(parts)}")
    if "path" not in case:
        raise ValueError("API 절차에 '요청 | 메서드 | 경로'가 없음")
    if not exp:
        raise ValueError("API 기대 결과가 비어 있음")
    return {**case, "expect": exp}


# 제목으로 칸을 찾는다. 공백·대소문자는 무시하고, 팀마다 다른 흔한 이름(한글·영어)도 받아준다.
COLUMNS = {
    "id": ["TCID", "TC번호", "케이스ID", "테스트ID", "TestCaseID", "CaseID", "TestID", "ID"],
    "type": ["유형", "테스트유형", "Type", "TestType"],
    "name": ["테스트항목", "항목", "테스트명", "제목", "TC명", "Title", "TestCaseName", "Summary", "Scenario", "TestItem"],
    "pre": ["사전조건", "전제조건", "Precondition", "Preconditions", "Pre-condition", "Prerequisite"],
    "proc": ["테스트절차", "절차", "수행절차", "테스트단계", "TestStep", "TestSteps", "Steps", "Step", "Procedure"],
    "expect": ["기대결과", "예상결과", "ExpectedOutput", "ExpectedResult", "ExpectedResults", "Expected"],
    # 사람용 문장 칸 옆에 두는 ARIS 실행용 칸. 값이 있으면 이 칸을 우선 실행한다.
    "auto_proc": ["자동화절차", "자동화단계", "AutomationStep", "AutoStep"],
    "auto_expect": ["자동화기대결과", "AutomationExpected", "AutoExpected"],
    "run": ["실행", "실행여부", "Run", "Execute"],
    "severity": ["중요도", "우선순위", "심각도", "Priority", "Severity"],
    "result": ["결과", "판정", "실행결과", "ResultStatus", "Result", "TestResult", "Status"],
    "reason": ["실패사유", "ARIS실패사유"],
    "issue": ["이슈번호", "ARIS이슈"],
    "convert": ["ARIS변환"],
}
LABELS = {"id": "TC ID(Test Case ID)", "proc": "테스트 절차(Test Step)"}
TOOL_SHEETS = {"설정", "변수", "공통단계", "키워드"}
HEADER_SCAN = 10  # 제목 줄은 위에서 이 줄 수 안에 있다고 본다(표 위에 문서 제목이 있는 양식 대응)
ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*\d$")  # mz-001, TC_001, SAM1-10, LOGIN01
# 결과 칸에 이미 쓰인 표현을 보고 같은 말로 기록한다: (통과, 실패, 미실행·작성 오류)
VOCABS = [("PASS", "FAIL", "N/T"), ("OK", "NG", "N/T"), ("P", "F", "N/T"), ("성공", "실패", "미실행"),
          ("합격", "불합격", "미실행")]
SECRET_HINTS = ("비밀번호", "비번", "password", "passwd", "pw", "token", "토큰", "secret", "apikey", "api_key")
VAR = re.compile(r"\$\{([^}]+)\}")


class NeedsConversion(ValueError):
    """절차가 키워드가 아닌 자연어 문장이라 실행 전에 변환이 필요한 TC."""


def norm(text):
    return re.sub(r"\s+", "", str(text or "")).upper()


def guess_id_column(ws, header_row):
    """제목으로 못 찾으면 값의 대부분이 영문+숫자 ID 패턴인 칸을 ID 칸으로 본다."""
    for col in range(1, ws.max_column + 1):
        vals = [str(ws.cell(r, col).value).strip() for r in range(header_row + 1, min(ws.max_row, header_row + 50) + 1)
                if ws.cell(r, col).value not in (None, "")]
        if vals and sum(bool(ID_PATTERN.match(v)) for v in vals) / len(vals) >= 0.8:
            return col
    return None


def locate(wb):
    """TC 표가 있는 (시트, 제목 줄 번호, {칸 이름: 열 번호})를 찾는다. 시트 이름·제목 줄 위치는 자유."""
    best = None
    for ws in wb.worksheets:
        if ws.title in TOOL_SHEETS:
            continue
        for r in range(1, min(ws.max_row, HEADER_SCAN) + 1):
            header = {}
            for c in ws[r]:
                if c.value is not None:
                    header.setdefault(norm(c.value), c.column)
            cols = {k: next((header[norm(n)] for n in names if norm(n) in header), None) for k, names in COLUMNS.items()}
            cols["id"] = cols["id"] or guess_id_column(ws, r)
            if not (cols["id"] and (cols["proc"] or cols["auto_proc"])):
                continue
            score = sum(1 for v in cols.values() if v) + (0.5 if ws.title == "TC" else 0)
            if best is None or score > best[0]:
                best = (score, ws, r, cols)
    if not best:
        raise ValueError(f"TC 표를 찾지 못했어요. 위쪽 {HEADER_SCAN}줄 안에 제목 줄이 있고, "
                         f"{LABELS['id']}와 {LABELS['proc']} 칸이 있어야 해요.")
    return best[1:]


def summary_of(cell):
    """테스트 항목 칸이 없으면 절차 첫 줄을 제목으로 쓴다(번호 제거, 40자)."""
    first = next((ln.strip() for ln in str(cell or "").splitlines() if ln.strip()), "")
    first = re.sub(r"^\d+[.)]\s*", "", first)
    return first[:40] + ("…" if len(first) > 40 else "")


def read_pairs(wb, sheet):
    """'변수', '공통단계'처럼 [이름 | 값] 두 칸짜리 시트를 dict로. 시트가 없으면 빈 dict."""
    if sheet not in wb.sheetnames:
        return {}
    return {str(k).strip(): ("" if v is None else str(v)) for k, v, *_ in
            wb[sheet].iter_rows(min_row=2, values_only=True) if k is not None and str(k).strip()}


def fill_vars(text, variables):
    def sub(m):
        name = m.group(1).strip()
        if name not in variables:
            raise ValueError(f"'변수' 시트에 없는 변수: ${{{name}}}")
        return variables[name]
    return VAR.sub(sub, str(text or ""))


def expand_common(cell_lines, commons, variables):
    """'공통 | 로그인' 줄을 공통단계 시트의 절차로 바꾼다."""
    out = []
    for parts in cell_lines:
        if parts[0] != "공통":
            out.append(parts)
            continue
        name = parts[1] if len(parts) > 1 else ""
        if name not in commons:
            raise ValueError(f"'공통단계' 시트에 없는 공통단계: {name or '(이름 없음)'}")
        sub = lines(fill_vars(commons[name], variables))
        # shortcut: 공통단계 안에서 다른 공통단계 호출은 미지원, 필요해지면 깊이 제한 재귀로 확장
        if any(p[0] == "공통" for p in sub):
            raise ValueError(f"공통단계 '{name}' 안에서는 '공통'을 쓸 수 없음")
        out.extend(sub)
    return out


def in_range(tc_id, spec):
    """'실행 범위' 설정: 'EC-022~EC-036, EC-045'처럼 범위·개별 ID를 쉼표로. 비어 있으면 전부."""
    if not spec:
        return True
    key = lambda x: [int(t) if t.isdigit() else t for t in re.findall(r"\d+|\D+", x.strip().upper())]
    for part in str(spec).split(","):
        lo, _, hi = part.partition("~")
        if (key(lo) <= key(tc_id) <= key(hi)) if hi.strip() else key(lo) == key(tc_id):
            return True
    return False


def parse(path, suite_dir=None):
    """엑셀 → (suite, 행 번호 맵, 작성 오류 목록, 실행 제외 목록). suite_dir은 스크립트 경로의 기준 폴더."""
    suite_dir = Path(suite_dir or os.path.dirname(os.path.abspath(path)))
    wb = load_workbook(path, data_only=True)
    ws, header_row, cols = locate(wb)
    suite = {"web": {}, "api": {}, "app": {"capabilities": {}}, "cases": []}
    for key, val in read_pairs(wb, "설정").items():
        if val == "":
            continue
        if key in SETTINGS:
            section, field = SETTINGS[key]
            suite[section][field] = val
        elif key == "실행 범위":
            suite["range"] = val
        elif key == "이슈 등록 보류(연속 실패)":
            try:
                suite["issue_pause"] = int(float(val))
            except ValueError:
                raise ValueError(f"설정 '{key}'는 숫자여야 해요 (0이면 보류 안 함): {val}")
        elif key in WAITS:
            try:
                ms = int(float(val) * 1000)
            except ValueError:
                raise ValueError(f"설정 '{key}'는 숫자(초)여야 해요: {val}")
            suite["web"][WAITS[key]] = ms
            suite["app"][WAITS[key]] = ms
        elif key == "화면 크기":
            m = re.fullmatch(r"\s*(\d+)\s*[xX×*]\s*(\d+)\s*", val)
            if not m:
                raise ValueError(f"설정 '화면 크기'는 가로x세로 형식이어야 해요 (예: 1920x1080): {val}")
            suite["web"]["viewport"] = (int(m[1]), int(m[2]))
        elif key == "통과 화면 저장":
            suite["web"]["pass_shot"] = suite["app"]["pass_shot"] = val.upper() == "Y"
        elif key in APP_SETTINGS:
            suite["app"]["capabilities"][APP_SETTINGS[key]] = val
        elif key == "앱 초기화":  # Y(기본): TC마다 앱 데이터를 지워 비로그인 상태로 시작
            suite["app"]["capabilities"]["appium:noReset"] = val.upper() == "N"
        elif key == "브라우저 화면 표시":
            suite["web"]["headless"] = val.upper() != "Y"
        else:  # 그 외 항목은 앱(Appium) capability로 전달
            suite["app"]["capabilities"][key] = value(val)
    variables = read_pairs(wb, "변수")
    commons = read_pairs(wb, "공통단계")
    secrets = [v for k, v in variables.items() if v and any(h in k.lower() for h in SECRET_HINTS)]

    get = lambda row, key: row[cols[key] - 1].value if cols[key] and cols[key] <= len(row) else None
    rows, invalid, skipped, seen = {}, [], [], set()
    for row in ws.iter_rows(min_row=header_row + 1):
        r = row[0].row
        if not any(get(row, k) for k in ("id", "name", "pre", "proc", "expect", "auto_proc", "auto_expect")):
            continue
        tc_id = str(get(row, "id") or "").strip()
        auto = bool(str(get(row, "auto_proc") or "").strip())
        proc_raw = get(row, "auto_proc") if auto else get(row, "proc")
        expect_raw = get(row, "auto_expect") if auto else get(row, "expect")
        # 제목 칸이 없으면 무엇을 검증하는지 드러나게 사람용 기대 결과 첫 줄 → 절차 첫 줄 순으로 쓴다
        name = (str(get(row, "name") or "").strip() or summary_of(get(row, "expect")) or summary_of(get(row, "proc"))
                or summary_of(get(row, "auto_expect")) or tc_id)
        if not in_range(tc_id, suite.get("range")):  # 범위 밖은 결과 칸도 건드리지 않음 (이전 실행 결과 유지)
            continue
        if str(get(row, "run") or "Y").strip().upper() in ("N", "NO", "X", "아니오", "제외"):
            skipped.append({"id": tc_id or f"(행 {r})", "row": r, "name": name})
            continue
        try:
            if not tc_id:
                raise ValueError("TC ID가 비어 있음")
            if tc_id in seen:
                raise ValueError("TC ID 중복")
            k = str(get(row, "type") or "").strip() or "웹"  # 유형 칸이 없거나 비어 있으면 웹
            kind = TYPES.get(k) or TYPES.get(k.upper())  # 'api', 'Api'도 허용
            if not kind:
                raise ValueError(f"유형은 {'/'.join(TYPES)} 중 하나")
            proc = expand_common(lines(fill_vars(proc_raw, variables)), commons, variables)
            expect = lines(fill_vars(expect_raw, variables))
            if kind != "script" and proc and all(len(p) == 1 and p[0] not in NO_ARG for p in proc):
                raise NeedsConversion("자연어로 쓴 절차라 실행하려면 키워드 변환이 필요해요")
            case = {"id": tc_id, "name": name, "type": kind, "secrets": secrets,
                    "precondition": fill_vars(get(row, "pre"), variables).strip().strip("-").strip(),
                    "severity": str(get(row, "severity") or "").strip(),
                    # 이슈에는 자동화 키워드가 아니라 사람이 쓴 테스트 절차·기대 결과를 올린다
                    "human_steps": fill_vars(get(row, "proc"), variables).strip(),
                    "human_expect": fill_vars(get(row, "expect"), variables).strip()}
            if kind == "api":
                case.update(parse_api(proc, expect))
            elif kind == "script":
                case["script"] = str(proc_raw or "").strip()
                if not case["script"]:
                    raise ValueError("스크립트 유형은 절차에 스크립트 파일 경로를 적어야 함")
                if Path(case["script"]).is_absolute() or ".." in Path(case["script"]).parts:
                    raise ValueError("스크립트 경로는 툴 폴더 기준 상대 경로만 가능 (예: scripts/check.py)")
                if not (suite_dir / case["script"]).is_file():
                    raise ValueError(f"스크립트 파일이 없음: {case['script']}")
            else:
                case["steps"] = parse_ui(kind, proc + expect)
                if not case["steps"]:
                    raise ValueError("절차가 비어 있음")
                case["display_steps"] = [" | ".join(p) for p in proc + expect]
        except ValueError as e:
            invalid.append({"id": tc_id or f"(행 {r})", "row": r, "error": str(e),
                            "convert": isinstance(e, NeedsConversion)})
            continue
        seen.add(tc_id)
        rows[tc_id] = r
        suite["cases"].append(case)
    if suite.get("range") and not (rows or invalid or skipped):
        raise ValueError(f"'설정' 시트 실행 범위 '{suite['range']}'에 해당하는 TC가 없어요. TC ID를 확인하세요")
    return suite, rows, invalid, skipped


def write_results(path, rows, results, invalid, skipped=()):
    """원본 양식 그대로 사본을 만들어 결과를 쓴다. 결과 칸에 이미 OK/NG 같은 표현이 있으면 같은 말로 기록한다."""
    wb = load_workbook(path)
    ws, header_row, cols = locate(wb)
    vocab = None
    if cols["result"]:
        c = cols["result"]
        # 결과 칸에 이미 쓴 값, 없으면 그 칸의 드롭다운 목록(OK,NG,N/T 등)을 보고 같은 표현을 쓴다
        used = {norm(ws.cell(r, c).value) for r in range(header_row + 1, ws.max_row + 1)}
        for dv in ws.data_validations.dataValidation:
            if dv.formula1 and any(rng.min_col <= c <= rng.max_col for rng in dv.sqref.ranges):
                used |= {norm(x) for x in dv.formula1.strip('"').split(",")}
        vocab = next((v for v in VOCABS if norm(v[0]) in used or norm(v[1]) in used), None)
    last = ws.max_column
    for key, title in (("result", "결과"), ("reason", "ARIS 실패 사유"), ("issue", "ARIS 이슈")):
        if not cols[key]:  # 없는 칸은 표 오른쪽 끝에 새로 만든다(기존 비고 칸 등은 덮어쓰지 않음)
            last += 1
            cols[key] = last
            put(ws, header_row, last, title)

    def mark(r, status, fill_key, reason="", issue=""):
        put(ws, r, cols["result"], status, FILLS[fill_key])
        put(ws, r, cols["reason"], reason or None)
        put(ws, r, cols["issue"], issue or None)

    for res in results:
        ok = res["passed"]
        mark(rows[res["id"]], (vocab[0] if ok else vocab[1]) if vocab else ("PASS" if ok else "FAIL"),
             "PASS" if ok else "FAIL", "\n".join(res["errors"]), res["issue"] or res.get("issue_error") or "")
    for bad in invalid:
        label = "변환 필요" if bad.get("convert") else "작성 오류"
        mark(bad["row"], vocab[2] if vocab else label, label, f"[{label}] {bad['error']}" if vocab else bad["error"])
    for skip in skipped:
        mark(skip["row"], vocab[2] if vocab else "미실행", "미실행")
    out = Path(path).with_name(f"{Path(path).stem}_결과_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    wb.save(out)
    return out


def put(ws, r, c, val, fill=None):
    cell = ws.cell(r, c)
    if isinstance(cell, MergedCell):  # 병합된 칸의 안쪽은 쓸 수 없어서 건너뜀
        return
    cell.value = val
    if fill:
        cell.fill = fill


def run_file(path, suite_dir=None, on_result=None, on_parsed=None):
    """엑셀 TC 파싱 → 실행 → 결과 엑셀 저장. (results, invalid, 결과 파일 경로)를 돌려준다."""
    suite, rows, invalid, skipped = parse(path, suite_dir)
    if on_parsed:
        on_parsed(suite["cases"], invalid, skipped)
    results = aris.run_suite(suite, suite_dir or os.path.dirname(os.path.abspath(path)), on_result)
    return results, invalid, write_results(path, rows, results, invalid, skipped)


def file_exists(path):
    if Path(path).is_file():
        return True
    hint = f" 혹시 '{path}.xlsx'처럼 확장자가 두 번 붙지 않았는지 확인해 주세요." if Path(f"{path}.xlsx").is_file() else ""
    print(f"파일을 찾을 수 없어요: {path}{hint}")
    return False


def convert_file(path):
    """자연어로만 쓴 행의 자동화 칸을 채운 사본을 만든다. 이미 자동화 칸이 있는 행은 건드리지 않는다.
    → (사본 경로, {확신도: 건수})"""
    wb = load_workbook(path)
    ws, header_row, cols = locate(wb)
    base = read_pairs(wb, "설정").get("웹 주소", "")
    pages = read_pairs(wb, "변수")  # 'X 페이지' 이름으로 주소를 적어 두면 '[X] 페이지 접속'을 이동으로 바꾼다
    add_auto_columns(ws, header_row, cols)
    fills = {convert.HIGH: FILLS["PASS"], convert.MID: PatternFill("solid", fgColor="FFF2CC"), convert.LOW: FILLS["작성 오류"]}
    count = {}
    get = lambda r, k: ws.cell(r, cols[k]).value if cols.get(k) else None
    for r in range(header_row + 1, ws.max_row + 1):
        proc = str(get(r, "proc") or "").strip()
        kind = str(get(r, "type") or "웹").strip()
        if not proc or str(get(r, "auto_proc") or "").strip() or TYPES.get(kind, TYPES.get(kind.upper())) != "web":
            continue
        res = convert.convert(proc, get(r, "expect"), get(r, "pre"), base, pages)
        count[res["confidence"]] = count.get(res["confidence"], 0) + 1
        note = [res["confidence"], *res["notes"]]
        if res["confidence"] == convert.LOW:  # 반쪽 변환을 실행하면 가짜 통과가 나올 수 있어 제안만 메모로
            note += ["[제안]", *res["proc"], *res["expect"]]
        else:
            put(ws, r, cols["auto_proc"], "\n".join(res["proc"]))
            put(ws, r, cols["auto_expect"], "\n".join(res["expect"]))
        put(ws, r, cols["convert"], "\n".join(note), fills[res["confidence"]])
    out = Path(path).with_name(f"{Path(path).stem}_변환_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
    wb.save(out)
    return out, count


def add_auto_columns(ws, header_row, cols):
    """자동화 절차·자동화 기대 결과·ARIS 변환 칸이 없으면 표 오른쪽에 만든다."""
    last = ws.max_column
    for key, title in (("auto_proc", "자동화 절차"), ("auto_expect", "자동화 기대 결과"), ("convert", "ARIS 변환")):
        if not cols.get(key):
            last += 1
            cols[key] = last
            put(ws, header_row, last, title)


def review(path):
    """실행하지 않고 TC마다 어떻게 읽었는지와, 자연어 TC의 변환 제안을 돌려준다 (웹 'TC 검토' 화면용)."""
    suite, rows, invalid, skipped = parse(path)
    wb = load_workbook(path, data_only=True)
    ws, header_row, cols = locate(wb)
    base = read_pairs(wb, "설정").get("웹 주소", "")
    pages = read_pairs(wb, "변수")
    cell = lambda r, k: str(ws.cell(r, cols[k]).value or "").strip() if cols.get(k) else ""
    out = []
    for c in suite["cases"]:
        r = rows[c["id"]]
        out.append({"row": r, "id": c["id"], "name": c["name"], "type": c["type"], "state": "ready",
                    "steps": c.get("display_steps") or [c.get("script") or c.get("path") or ""],
                    "human_proc": cell(r, "proc"), "human_expect": cell(r, "expect")})
    for b in invalid:
        r = b["row"]
        item = {"row": r, "id": b["id"], "name": cell(r, "name") or summary_of(cell(r, "expect")) or summary_of(cell(r, "proc")) or b["id"],
                "state": "convert" if b["convert"] else "invalid", "error": b["error"],
                "human_proc": cell(r, "proc"), "human_expect": cell(r, "expect")}
        if b["convert"]:
            res = convert.convert(cell(r, "proc"), cell(r, "expect"), cell(r, "pre"), base, pages)
            item["suggestion"] = res
        out.append(item)
    for b in skipped:
        out.append({"row": b["row"], "id": b["id"], "name": b["name"], "state": "skipped"})
    out.sort(key=lambda x: x["row"])
    return {"sheet": ws.title, "header_row": header_row, "rows": out}


def apply_auto(path, edits, out):
    """검토 화면에서 확정한 자동화 절차·기대 결과를 원본 양식의 사본에 써 넣는다. edits: {행 번호: {"proc", "expect"}}"""
    wb = load_workbook(path)
    ws, header_row, cols = locate(wb)
    add_auto_columns(ws, header_row, cols)
    for r, e in edits.items():
        put(ws, int(r), cols["auto_proc"], str(e.get("proc") or "").strip())
        put(ws, int(r), cols["auto_expect"], str(e.get("expect") or "").strip())
        put(ws, int(r), cols["convert"], f"ARIS 검토 화면에서 적용 ({datetime.now():%m/%d %H:%M})", FILLS["PASS"])
    wb.save(out)
    return out


def check(path):
    """실행하지 않고 ARIS가 엑셀을 어떻게 읽었는지만 보여준다."""
    if not file_exists(path):
        return 1
    wb = load_workbook(path, data_only=True)
    ws, header_row, cols = locate(wb)
    names = {"id": "TC ID", "type": "유형", "name": "테스트 항목", "pre": "사전조건", "proc": "테스트 절차",
             "expect": "기대 결과", "auto_proc": "자동화 절차", "auto_expect": "자동화 기대 결과",
             "run": "실행", "severity": "중요도", "result": "결과",
             "reason": "실패 사유", "issue": "이슈 번호"}
    print(f"시트: {ws.title} / 제목 줄: {header_row}번째 줄")
    for key, label in names.items():
        c = cols[key]
        found = f"{get_column_letter(c)}열 '{ws.cell(header_row, c).value}'" if c else "없음"
        print(f"  {label}: {found}")
    suite, rows, invalid, skipped = parse(path)
    convert = [b for b in invalid if b.get("convert")]
    errors = [b for b in invalid if not b.get("convert")]
    total = len(suite["cases"]) + len(invalid) + len(skipped)
    print(f"\nTC {total}개: 바로 실행 {len(suite['cases'])} / 변환 필요 {len(convert)} / "
          f"작성 오류 {len(errors)} / 실행 제외 {len(skipped)}")
    for b in errors:
        print(f"  작성 오류 {b['id']} (행 {b['row']}): {b['error']}")
    return 0


def main(path):
    if not file_exists(path):
        return 1
    def on_parsed(cases, invalid, skipped):
        for b in invalid:
            print(f"작성 오류  {b['id']} (행 {b['row']}): {b['error']}")
        if skipped:
            print(f"실행 제외(실행=N) {len(skipped)}건: {', '.join(x['id'] for x in skipped)}")

    results, invalid, out = run_file(path, on_parsed=on_parsed)
    passed = sum(r["passed"] for r in results)
    print(f"\n결과: {passed}/{len(results)} 통과, 작성 오류 {len(invalid)}건 → {out}")
    return 0 if passed == len(results) and not invalid else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--convert":
        if len(args) < 2 or not file_exists(args[1]):
            sys.exit(1)
        out, count = convert_file(args[1])
        print(f"자동 변환: {', '.join(f'{k} {v}건' for k, v in count.items()) or '변환할 행 없음'} → {out}")
        print("'ARIS 변환' 칸의 메모를 확인하고, '검토 필요' 행은 제안을 보고 자동화 칸을 직접 채워 주세요.")
        sys.exit(0)
    if args and args[0] == "--check":
        sys.exit(check(args[1] if len(args) > 1 else "TC_template.xlsx"))
    sys.exit(main(args[0] if args else "TC_template.xlsx"))
