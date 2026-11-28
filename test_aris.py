"""네트워크 없이 로컬 가짜 서버로 실행기를 검증한다. 실행: python test_aris.py"""
import json
import os
import tempfile
import threading
import time as _time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import aris

PAGE = b"""<html><body><input id=q><button onclick="out.textContent='hello '+q.value">go</button>
<p id=out></p></body></html>"""
SITE = """<html><head><style>nav{display:none} @media (min-width:1400px){nav{display:block}}</style></head><body>
<nav><a href="#" onclick="out.textContent='AI 페이지'">AI</a></nav>
<h1 onclick="out.textContent='잘못 누름'">AI Technology of the Future</h1>
<label for=mail>E-mail</label><input id=mail oninput="echo.textContent=this.value"><p id=echo></p><p id=out></p>
</body></html>""".encode()
# 이카운트 흉내: 메가 메뉴(올려야 펼쳐짐), 숨은 모바일 메뉴 중복, 모달 속 iframe 폼, 필수 동의 알림, 목록, 새 창
EC = {
    "/ec": """<html><head><title>ERP 모든 기능 | 이카운트</title><style>.depth2{display:none} li:hover>.depth2{display:block}</style></head>
<body><header><ul class=depth1><li><a href=/ec-join>가입하기</a><ul class=depth2><li><a href=/ec-pricing>가격정책</a></li></ul></li></ul>
<a href="javascript:void(0)" onclick="modal.style.display='block'">영업팀<br>문의</a>
<a href="javascript:void(0)" onclick="window.open('/ec-popup')">새창열기</a></header>
<nav style=display:none><a href=/ec-pricing>가격정책</a></nav>
<div id=modal style=display:none><a class=close-modal onclick="modal.style.display='none'">닫기</a><iframe id=iFrameECK src=/ec-form></iframe></div>
<input type=checkbox id=agree><label for=agree>위의 사항에 대해 동의합니다.(필수)</label>
<button id=join onclick="if(!agree.checked){alert('필수 약관에 동의해 주세요.')}else{location='/ec-done'}">가입하기</button>
<label for=nation>Global Sites</label><select id=nation onchange="sel.textContent=this.value"><option>한국(한국어)</option><option>United States(English)</option></select><p id=sel></p>
</body></html>""",
    "/ec-slide": """<html><body><div style="position:relative;height:200px">
<div id=s1 style="position:absolute;opacity:1"><h1>유연한 ERP 이카운트</h1></div>
<div id=s2 style="position:absolute;opacity:0"><h1>모든 기능, 사용자 무제한 월 4만원</h1></div></div>
<span role=button aria-label="Go to slide 2" onclick="s1.style.opacity=0;s2.style.opacity=1">2</span></body></html>""",
    "/ec-form": "<html><body><h2>상담문의</h2><label for=c>회사명</label><input id=c oninput='echo.textContent=this.value'><p id=echo></p></body></html>",
    "/ec-popup": "<html><head><title>팝업</title></head><body><h1>새 창 내용</h1></body></html>",
    # 실제 사이트 구조: 접힌(높이 0) 모바일 메뉴에 같은 문구가 먼저 있고, 푸터 사이트맵에도 또 있음
    "/ec-dup": """<html><head><style>.depth2{display:none} li:hover>.depth2{display:block}</style></head><body style="margin:0">
<div style="height:0;overflow:hidden"><a href=/ec-join>가입하기</a><a href=/ec-pricing>가격정책</a></div>
<header><ul class=depth1><li><a href=/ec-join>가입하기</a><ul class=depth2><li><a href=/ec-pricing>가격정책</a></li></ul></li></ul></header>
<div style="height:3000px"></div><footer><a href=/ec-pricing>가격정책</a></footer></body></html>""",
    "/ec-pricing": "<html><body><h1>가격정책 페이지</h1></body></html>",
    "/frames": "<html><body>" + "".join(f'<iframe srcdoc="<p>광고 {i}</p>" width=200 height=80></iframe>' for i in range(6)) + '<iframe src="/never" width=200 height=80></iframe></body></html>',  # 끝나지 않는 iframe
    "/cover": """<html><body style="margin:0"><div style="position:fixed;top:0;left:0;width:100%;height:60px;background:#fff;z-index:9">헤더</div>
<h2 style="margin:40px 0 0;font-size:40px;line-height:60px">AI Wallpad, 일상의 모든 순간을 잇다</h2>
<p style="position:relative;margin-top:30px">덮인 문구</p><div style="position:absolute;top:110px;left:0;width:100%;height:80px;background:#000"></div>
<div style="position:absolute;top:300px;left:0;height:120px"><img width=600 height=120 style="position:absolute;left:0;top:0;background:#ccc">
<h3 style="position:absolute;left:0;top:20px;z-index:30;margin:0;pointer-events:none">클릭 통과 제목</h3></div></body></html>""",
    "/ec-icon": """<html><body><a href=/ec-pricing aria-label="AI Parking"><svg width=40 height=40></svg></a>
<button title="닫기 버튼" onclick="this.remove()">X</button><a href=/ec-pricing><img alt="뒤로가기" width=30 height=30></a><a href=/ec-done>&lt; 이전으로</a></body></html>""",
    "/ec-done": "<html><body><h1>가입 정보 입력</h1></body></html>",
}
# 스크롤 이동·팝업 닫힘 검증용: 아래쪽 섹션, 닫기 버튼 있는 팝업
LONG = """<html><body style="margin:0"><div id=popup>공지 팝업 <button id=close onclick="popup.remove()">X</button></div>
<a href="#sec">STT 보기</a><div style="height:3000px"></div><h2 id=sec>Solution 01 음성인식</h2></body></html>""".encode()


class Fake(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/page":
            code, ctype, body = 200, "text/html", PAGE
        elif self.path == "/heavy":  # 이미지 하나가 늦게 오는 실제 사이트 흉내
            code, ctype, body = 200, "text/html; charset=utf-8", "<html><body><h1>메인</h1><img src=/slow.png></body></html>".encode()
        elif self.path == "/lateload":  # 상상인처럼 메뉴 동작을 window load 때 붙이고, 느린 이미지가 load를 늦춤
            code, ctype, body = 200, "text/html; charset=utf-8", ("""<html><body><button id=b>전체메뉴</button><div id=m hidden>하위 메뉴</div>
<img src=/slow.png><script>addEventListener('load', () => b.onclick = () => m.hidden = false)</script></body></html>""").encode()
        elif self.path == "/never":  # 응답이 끝나지 않는 광고·추적 iframe 흉내
            _time.sleep(20)
            code, ctype, body = 200, "text/html", b""
        elif self.path == "/slow.png":
            _time.sleep(3)
            code, ctype, body = 200, "image/png", b""
        elif self.path in EC:
            code, ctype, body = 200, "text/html; charset=utf-8", EC[self.path].encode()
        elif self.path == "/site":  # 미디어젠 메인 흉내: 넓은 화면에서만 상단 메뉴, 히어로에 'AI Technology'
            code, ctype, body = 200, "text/html; charset=utf-8", SITE
        elif self.path == "/long":
            code, ctype, body = 200, "text/html; charset=utf-8", LONG
        elif self.path == "/blocked":  # CloudFront 차단 화면 흉내
            code, ctype, body = 403, "text/html", b"<html><head><title>ERROR: The request could not be satisfied</title></head><body>403</body></html>"
        elif self.path == "/item":
            code, ctype, body = 200, "application/json", json.dumps({"id": 1, "name": "ok"}).encode()
        else:
            code, ctype, body = 404, "application/json", b"{}"
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:  # 테스트가 끝나 브라우저가 먼저 닫힌 경우 (윈도우는 ConnectionAbortedError)
            pass

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)  # 느린 이미지 요청이 다른 요청을 막지 않게
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_PROJECT"):
    os.environ.pop(k, None)  # dry-run 강제
os.chdir(tempfile.mkdtemp())

# API 판정
api = {"base_url": base}
ok = {"id": "A1", "name": "ok", "path": "/item", "expect": {"status": 200, "json": {"id": 1}}}
bad = {"id": "A2", "name": "bad", "path": "/item", "expect": {"json": {"name": "nope"}}}
assert aris.run_api(ok, api, {})[0] == []
assert aris.run_api(bad, api, {})[0] == ["json.name: 기대 'nope', 실제 'ok'"]
assert "status" in aris.run_api({**ok, "path": "/none"}, api, {})[0][0]
assert "요청 실패" in aris.run_api(ok, {"base_url": "http://127.0.0.1:1"}, {})[0][0]

# 스크립트
open("s_ok.py", "w", encoding="utf-8").write("def run(case): assert 1 + 1 == 2\n")
open("s_bad.py", "w", encoding="utf-8").write("def run(case): assert False, '값 불일치'\n")
assert aris.run_script({"script": "s_ok.py"}, {}, {"suite_dir": "."})[0] == []
assert aris.run_script({"script": "s_bad.py"}, {}, {"suite_dir": "."})[0] == ["AssertionError: 값 불일치"]

# 전체 흐름: API + 웹 + 스크립트 + 잘못된 type
web_ok = {"id": "W1", "type": "web", "name": "입력 후 결과", "steps": [
    {"goto": "/page"}, {"fill": "#q", "text": "도원"}, {"click": "button"}, {"expect_text": "hello 도원"}]}
web_bad = {"id": "W2", "type": "web", "name": "없는 텍스트", "steps": [
    {"goto": "/page"}, {"expect_text": "없는 문구"}]}
suite = {"api": api, "web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    ok, bad, web_ok, web_bad,
    {"id": "S1", "type": "script", "name": "스크립트", "script": "s_ok.py"},
    {"id": "X1", "type": "nope", "name": "잘못된 유형"},
]}
with open("suite.yaml", "w", encoding="utf-8") as f:
    json.dump(suite, f)  # JSON은 YAML의 부분집합
assert aris.main("suite.yaml") == 1
res = {r["id"]: r for r in json.load(open("results.json", encoding="utf-8"))}
assert [r["passed"] for r in res.values()] == [True, False, True, False, True, False]
assert res["W2"]["errors"][0].startswith("2단계 확인 실패: 페이지에 '없는 문구' 문구가 없음 (현재 페이지: http") and os.path.exists(res["W2"]["screenshot"])

# 이슈 본문 양식
f = aris.issue_fields(web_bad, {}, res["W2"]["errors"], "", "DEMO")
assert "# goto: /page" in f["description"] and f["labels"] == ["aris", "aris-W2"]
# 엑셀 TC: 파싱 → 실행 → 결과 사본 기록
import glob
from openpyxl import Workbook, load_workbook
import excel_tc

wb = Workbook()
ws = wb.active
ws.title = "TC"
ws.append(["TC ID", "유형", "테스트 항목", "사전조건", "테스트 절차", "기대 결과"])
ws.append(["E1", "api", "조회", "", "요청 | GET | /item", "상태 | 200\n값 | id | 1"])
ws.append(["E2", "웹", "입력", "", "이동 | /page\n입력 | #q | 도원\n클릭 | button", "확인 | hello 도원"])
ws.append(["E3", "웹", "실패", "", "이동 | /page", "확인 | 없는 문구"])
ws.append(["E4", "앱", "앱에서 이동 사용", "", "이동 | /x", "확인 | a"])
ws.append(["E1", "API", "중복 ID", "", "요청 | GET | /item", "상태 | 200"])
ws.append(["E5", "모름", "잘못된 유형", "", "", ""])
st = wb.create_sheet("설정")
st.append(["항목", "값"])
st.append(["API 주소", base])
st.append(["웹 주소", base])
wb.save("tc.xlsx")

suite, rows, invalid, skipped = excel_tc.parse("tc.xlsx")
assert suite["cases"][0]["expect"] == {"status": 200, "json": {"id": 1}}  # "1" → 숫자 1
assert [b["row"] for b in invalid] == [5, 6, 7] and skipped == []
assert excel_tc.main("tc.xlsx") == 1
out = load_workbook(glob.glob("tc_결과_*.xlsx")[0])["TC"]
assert [out.cell(r, 7).value for r in range(2, 8)] == ["PASS", "PASS", "FAIL", "작성 오류", "작성 오류", "작성 오류"]
assert "앱에서 쓸 수 없는" in out.cell(5, 8).value and out.cell(6, 8).value == "TC ID 중복"

# 실무형 엑셀: 칸 순서가 달라도 제목으로 찾음 + 실행 Y/N + 중요도 + 변수 + 공통단계 + 비밀번호 가림
wb = Workbook()
ws = wb.active
ws.title = "TC"
ws.append(["절차", "ID", "실행", "테스트 유형", "중요도", "기대결과", "전제조건", "TC명"])  # 다른 팀 양식 흉내
ws.append(["공통 | 접속\n입력 | #q | ${비밀번호}\n클릭 | button", "V1", "Y", "웹", "상", "확인 | hello ${비밀번호}", "로그인 상태", "변수+공통"])
ws.append(["요청 | GET | ${경로}", "V2", "N", "API", "", "상태 | 200", "", "실행 제외"])
ws.append(["공통 | 없는단계", "V3", "", "웹", "", "확인 | a", "", "없는 공통"])
ws.append(["이동 | ${없는변수}", "V4", "", "웹", "", "확인 | a", "", "없는 변수"])
ws.append(["요청 | GET | ${경로}", "V5", "y", "API", "하", "값 | name | 틀림", "", "API 변수"])
ws.append(["없는파일.py", "V6", "Y", "스크립트", "", "", "", "없는 스크립트"])
wb.create_sheet("설정").append(["항목", "값"])
wb["설정"].append(["웹 주소", base])
wb["설정"].append(["API 주소", base])
for sheet, rows_ in {"변수": [["경로", "/item"], ["비밀번호", "pw1234"]], "공통단계": [["접속", "이동 | /page"]]}.items():
    wb.create_sheet(sheet).append(["이름", "값"])
    for r_ in rows_:
        wb[sheet].append(r_)
wb.save("tc2.xlsx")

suite, rows, invalid, skipped = excel_tc.parse("tc2.xlsx")
v1 = suite["cases"][0]
assert [c["id"] for c in suite["cases"]] == ["V1", "V5"] and [s["id"] for s in skipped] == ["V2"]
assert v1["steps"][0] == {"goto": "/page"} and v1["steps"][1] == {"fill": "#q", "text": "pw1234"}
assert v1["severity"] == "상" and v1["precondition"] == "로그인 상태" and v1["secrets"] == ["pw1234"]
assert {b["id"]: b["error"] for b in invalid} == {"V3": "'공통단계' 시트에 없는 공통단계: 없는단계",
                                                  "V4": "'변수' 시트에 없는 변수: ${없는변수}",
                                                  "V6": "스크립트 파일이 없음: 없는파일.py"}
desc = aris.issue_fields(v1, {"base_url": base}, ["실패 pw1234"], "", "DEMO")["description"]
assert "pw1234" not in desc and "# 입력 | #q | ****" in desc and "실패 ****" in desc
assert "중요도: 상" in desc and "h3. 사전조건\n로그인 상태" in desc and "실행 일시:" in desc and f"대상 주소: {base}" in desc
v5_desc = aris.issue_fields(suite["cases"][1], {"base_url": base}, ["x"], "", "DEMO")["description"]
assert "요청 본문" not in v5_desc and f"GET {base}/item" in v5_desc  # 본문 없으면 'null' 줄 안 씀
results, invalid, out_path = excel_tc.run_file("tc2.xlsx")
assert [r["passed"] for r in results] == [True, False]
assert aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    {**v1, "steps": v1["steps"][:3] + [{"expect_text": "nope pw1234"}]}]}, ".")[0]["errors"][0].count("****") == 1
o = load_workbook(out_path)["TC"]
assert [c.value for c in o[1]][8:] == ["결과", "ARIS 실패 사유", "ARIS 이슈"]  # 결과 칸은 맨 뒤에 새로 생김
assert [o.cell(r, 9).value for r in range(2, 8)] == ["PASS", "미실행", "작성 오류", "작성 오류", "FAIL", "작성 오류"]
# 웹 화면: 업로드 → 백그라운드 실행 → 결과 조회/다운로드
import time
from pathlib import Path
import web

web.RUNS = Path(tempfile.mkdtemp())
web.REVIEWS = web.RUNS / "_review"
threading.Thread(target=web.worker, daemon=True).start()
client = web.app.test_client()
assert client.post("/api/runs", data={"file": (open("s_ok.py", "rb"), "a.py")}).status_code == 400
with open("tc.xlsx", "rb") as f:
    run_id = client.post("/api/runs", data={"file": (f, "tc.xlsx")}).get_json()["id"]
for _ in range(60):
    run = client.get(f"/api/runs/{run_id}").get_json()
    if run["status"] in ("done", "error"):
        break
    time.sleep(0.5)
assert run["status"] == "done", run
assert [r["passed"] for r in run["results"]] == [True, True, False] and len(run["invalid"]) == 3
assert run["results"][2]["screenshot"] == "E3.png"
assert client.get(f"/runs/{run_id}/shot/E3.png").status_code == 200
assert client.get(f"/runs/{run_id}/output").status_code == 200
assert client.get("/api/runs/..%2F..").status_code == 404
assert client.get("/api/runs").get_json()[0]["passed"] == 2
# 기록 삭제: 끝난 실행은 지워지고, 실행 중인 것은 거부
web.save(web.RUNS / run_id, {**run, "status": "running"})
assert client.delete(f"/api/runs/{run_id}").status_code == 409
web.save(web.RUNS / run_id, run)
assert client.delete(f"/api/runs/{run_id}").status_code == 200 and not (web.RUNS / run_id).exists()
assert client.delete("/api/runs/..%2F..").status_code == 404
# 예전 폴더(runs/)의 기록을 새 위치로 옮겨 옴
old_root = Path(tempfile.mkdtemp()); (old_root / "runs" / "abc123").mkdir(parents=True)
(old_root / "runs" / "abc123" / "run.json").write_text("{}", encoding="utf-8")
web.ROOT, saved_root = old_root, web.ROOT
web.migrate_old_runs(); web.migrate_old_runs()  # 두 번 불러도 안전
assert (web.RUNS / "abc123" / "run.json").exists()
web.ROOT = saved_root
import shutil as _sh; _sh.rmtree(web.RUNS / "abc123")

# 새 키워드: 보임(화면 안), 사라짐, 존재
kw = {"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    {"id": "K1", "type": "web", "name": "스크롤 전엔 안 보임", "steps": [{"goto": "/long"}, {"expect_visible": "Solution 01 음성인식"}]},
    {"id": "K2", "type": "web", "name": "클릭 후 스크롤", "steps": [
        {"goto": "/long"}, {"expect_exists": "Solution 01 음성인식"}, {"click": "text=STT 보기"},
        {"expect_visible": "Solution 01 음성인식"}]},
    {"id": "K3", "type": "web", "name": "팝업 닫힘", "steps": [
        {"goto": "/long"}, {"expect_visible": "공지 팝업"}, {"click": "#close"}, {"expect_hidden": "공지 팝업"}]},
    {"id": "K4", "type": "web", "name": "닫기 전엔 사라짐 실패", "steps": [{"goto": "/long"}, {"expect_hidden": "공지 팝업"}]},
    {"id": "K5", "type": "web", "name": "없는 요소", "steps": [{"goto": "/long"}, {"expect_exists": "#nope"}]},
]}
assert [r["passed"] for r in aris.run_suite(kw, ".")] == [False, True, True, False, False]
# 실제 사이트 대응: PC 화면 크기, 정확한 문구 클릭, 칸 이름으로 입력, 한글 실패 사유
site = [{"goto": "/site"}, {"click": "AI"}, {"expect_text": "AI 페이지"}, {"fill": "E-mail", "text": "a@b.c"},
        {"expect_text": "a@b.c"}]
dup = lambda cid, steps: {"id": cid, "type": "web", "name": cid, "steps": [{"goto": "/ec-dup"}] + steps}
assert [r["passed"] for r in aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    dup("D1", [{"expect_hidden": "가격정책"}, {"hover": "가입하기"}, {"expect_visible": "가격정책"}, {"click": "가격정책"},
               {"expect_url": "ec-pricing"}]),
    dup("D2", [{"expect_visible": "가격정책"}]),  # 펼치지 않으면 접힌 메뉴·푸터 것만 있어 실패해야 함
]}, ".")] == [True, False]

r_pc, r_narrow, r_msgs = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    {"id": "S1", "type": "web", "name": "PC", "steps": site},
    {"id": "S2", "type": "web", "name": "좁은 화면", "steps": site[:2]},
]}, ".") + aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500, "viewport": (1280, 720)}, "cases": [
    {"id": "S3", "type": "web", "name": "1280", "steps": site[:2]}]}, ".")
assert r_pc["passed"] and r_narrow["passed"]  # 기본 1920x1080이면 상단 메뉴가 보임
assert r_msgs["errors"] == ["2단계 클릭 실패: 'AI'는 페이지에 있지만 숨겨져 있음 (메뉴라면 '올리기'로 먼저 펼치거나 화면 크기 설정을 확인하세요)"]
msgs = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1000}, "cases": [
    {"id": "M1", "type": "web", "name": "화면 밖", "steps": [{"goto": "/long"}, {"expect_visible": "Solution 01 음성인식"}]},
    {"id": "M2", "type": "web", "name": "없음", "steps": [{"goto": "/long"}, {"expect_visible": "없는 제목"}]},
    {"id": "M3", "type": "web", "name": "안 닫힘", "steps": [{"goto": "/long"}, {"expect_hidden": "공지 팝업"}]},
]}, ".")
assert [m["errors"][0] for m in msgs] == ["2단계 보임 실패: 'Solution 01 음성인식'은 페이지에 있지만 지금 화면 밖에 있거나 다른 요소에 가려짐",
                                         f"2단계 보임 실패: '없는 제목'을 페이지에서 찾지 못함 (현재 페이지: {base}/long)",
                                         "2단계 사라짐 실패: '공지 팝업'이 아직 화면에 보임"]

# 사이트가 차단(403)하면 화면 탓이 아니라 차단이라고 바로 알려줌
blk = aris.run_suite({"web": {"base_url": base, "timeout_ms": 300}, "cases": [
    {"id": "B1", "type": "web", "name": "차단", "steps": [{"goto": "/blocked"}, {"click": "가입하기"}]}]}, ".")
assert blk[0]["issue"] is None and "이슈 등록 안 함" in blk[0]["issue_error"]  # 차단은 이슈로 올리지 않음
assert blk[0]["errors"][0].startswith("1단계 이동 실패: 사이트가 응답을 거부함 (HTTP 403"), blk[0]["errors"]

# 앱 TC가 있는데 Appium이 꺼져 있으면 실행 전에 멈춤 (가짜 실패·가짜 이슈 방지)
app_suite = {"app": {"server": "http://127.0.0.1:9"}, "cases": [{"id": "X1", "type": "app", "name": "서버 꺼짐", "steps": []}]}
try:
    aris.run_suite(app_suite, ".")
    raise AssertionError("Appium이 꺼져 있는데 실행됨")
except aris.EnvError as e:
    assert "appium을 켠 뒤" in str(e)
# 실행 도중 Appium이 끊긴 경우에도 앱 결함이 아니므로 이슈로 올리지 않음
real_app, real_up = aris.RUNNERS["app"], aris.appium_up
aris.RUNNERS["app"] = lambda case, cfg, ctx: (["Appium 연결 실패: 연결 거부 (Appium 서버와 에뮬레이터가 켜져 있는지 확인하세요)"], "", None)
aris.appium_up = lambda server: True
off = aris.run_suite(app_suite, ".")
aris.RUNNERS["app"], aris.appium_up = real_app, real_up
assert off[0]["issue"] is None and "이슈 등록 안 함" in off[0]["issue_error"], off

# 연속 실패가 쌓이면(사이트 차단 등) 이후 이슈 등록 보류 — 가짜 이슈가 쏟아지지 않게
fail_web = lambda cid: {"id": cid, "type": "web", "name": cid, "steps": [{"goto": "/long"}, {"expect_text": "없는 문구"}]}
paused = aris.run_suite({"web": {"base_url": base, "timeout_ms": 300}, "issue_pause": 2, "cases": [fail_web("P1"), fail_web("P2"), fail_web("P3")]}, ".")
assert [bool(r["issue_error"]) for r in paused] == [False, False, True] and "연속 3건 실패" in paused[2]["issue_error"]

# 실무 웹 키워드: 올리기·주소·뒤로·제목, 프레임, 알림·체크, 선택, 새창·창닫기
ec = lambda cid, steps: {"id": cid, "type": "web", "name": cid, "steps": [{"goto": "/ec"}] + steps}
ec_res = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    ec("E1", [{"expect_hidden": "가격정책"}, {"hover": "가입하기"}, {"expect_visible": "가격정책"}, {"click": "가격정책"},
              {"expect_url": "ec-pricing"}, {"back": ""}, {"expect_title": "이카운트"}]),
    ec("E2", [{"click": "가격정책"}]),
    ec("E3", [{"click": "영업팀문의"}, {"frame": "#iFrameECK"}, {"expect_visible": "상담문의"}, {"fill": "회사명", "text": "테스트"},
              {"expect_text": "테스트"}, {"frame": "나가기"}, {"click": "닫기"}, {"expect_hidden": "닫기"}]),
    ec("E4", [{"click": "#join"}, {"expect_alert": "필수 약관에 동의"}, {"check": "위의 사항에 대해 동의합니다.(필수)"},
              {"click": "#join"}, {"expect_text": "가입 정보 입력"}]),
    ec("E5", [{"select": "Global Sites", "text": "United States(English)"}, {"expect_text": "United States(English)"}]),
    ec("E6", [{"popup": "새창열기"}, {"expect_visible": "새 창 내용"}, {"expect_title": "팝업"}, {"close": ""},
              {"expect_visible": "새창열기"}]),
    ec("E7", [{"click": "#join"}, {"expect_alert": "없는 알림"}]),
    ec("E8", [{"expect_url": "nope"}]),
    {"id": "E9", "type": "web", "name": "숨은 슬라이드", "steps": [{"goto": "/ec-slide"}, {"expect_visible": "모든 기능, 사용자 무제한 월 4만원"}]},
    {"id": "E10", "type": "web", "name": "슬라이드 넘김", "steps": [
        {"goto": "/ec-slide"}, {"expect_hidden": "모든 기능, 사용자 무제한 월 4만원"}, {"click": '[aria-label="Go to slide 2"]'},
        {"expect_visible": "모든 기능, 사용자 무제한 월 4만원"}, {"expect_hidden": "유연한 ERP 이카운트"}]},
]}, ".")
assert [r["passed"] for r in ec_res] == [True, False, True, True, True, True, False, False, False, True], [r["errors"] for r in ec_res]
assert ec_res[8]["errors"] == ["2단계 보임 실패: '모든 기능, 사용자 무제한 월 4만원'은 화면 위치에 있지만 투명해서 보이지 않음 (슬라이드·페이드 애니메이션의 숨은 화면일 수 있음)"]
assert ec_res[1]["errors"] == ["2단계 클릭 실패: '가격정책'은 페이지에 있지만 숨겨져 있음 (메뉴라면 '올리기'로 먼저 펼치거나 화면 크기 설정을 확인하세요)"]
assert ec_res[6]["errors"] == ["3단계 알림 실패: '없는 알림' 알림창이 뜨지 않음 (뜬 알림: 필수 약관에 동의해 주세요.)"]
assert ec_res[7]["errors"][0].startswith("2단계 주소 실패: 주소에 'nope'가 없음 (현재 주소: http://127.0.0.1:")
# 아이콘 버튼(aria-label·title) 찾기, 주소를 모를 때 '이동됨'
icon = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    {"id": "I1", "type": "web", "name": "I1", "steps": [{"goto": "/ec-icon"}, {"click": "닫기 버튼"}, {"expect_hidden": "닫기 버튼"},
                                                        {"click": "AI Parking"}, {"expect_navigated": ""}]},
    {"id": "I2", "type": "web", "name": "I2", "steps": [{"goto": "/ec-icon"}, {"click": "닫기 버튼"}, {"expect_navigated": ""}]},
]}, ".")
# 팝업 속 iframe: '프레임' 없이도 자동으로 들어가서 찾고, 팝업 밖 버튼은 다시 페이지에서 찾음
auto_fr = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1000}, "cases": [
    ec("F1", [{"click": "영업팀문의"}, {"expect_visible": "상담문의"}, {"fill": "회사명", "text": "테스트"}, {"expect_text": "테스트"},
              {"click": "닫기"}, {"expect_hidden": "닫기"}]),
    ec("F2", [{"click": "영업팀문의"}, {"expect_visible": "없는 문구"}]),
    ec("F3", [{"hover": "가입하기"}, {"hover": "영업팀문의"}, {"expect_visible": "가격정책"}]),
]}, ".")
assert auto_fr[0]["passed"], auto_fr[0]["errors"]
assert not auto_fr[1]["passed"] and "찾지 못함" in auto_fr[1]["errors"][0], auto_fr[1]["errors"]
assert "클릭해야 이동하는 메뉴인지" in auto_fr[2]["errors"][0], auto_fr[2]["errors"]

ps = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1000, "pass_shot": True}, "cases": [ec("PS1", [{"expect_text": "가입하기"}])]}, ".")
assert ps[0]["passed"] and ps[0]["screenshot"] and os.path.exists(ps[0]["screenshot"]), ps  # 통과 증거 화면

t0 = time.time()
fr = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1000, "load_wait_ms": 300}, "cases": [
    {"id": "FR", "type": "web", "name": "FR", "steps": [{"goto": "/frames"}, {"click": "없는 버튼"}]}]}, ".")
assert not fr[0]["passed"] and time.time() - t0 < 6, time.time() - t0  # 대상 없는 iframe 6개에서 대기 시간을 쓰지 않음

ab = aris.run_suite({"web": {"base_url": base, "timeout_ms": 600}, "cases": [
    {"id": "AB1", "type": "web", "name": "AB1", "steps": [{"goto": "/ec-icon"}, {"expect_absent": "a[href='/nope']"}]},
    {"id": "AB2", "type": "web", "name": "AB2", "steps": [{"goto": "/ec-icon"}, {"expect_absent": "a[href='/ec-pricing']"}]}]}, ".")
assert ab[0]["passed"] and "페이지에 있음" in ab[1]["errors"][0], [a["errors"] for a in ab]  # '없음': 하나라도 있으면 실패

cov = aris.run_suite({"web": {"base_url": base, "timeout_ms": 800}, "cases": [
    {"id": "C1", "type": "web", "name": "C1", "steps": [{"goto": "/cover"}, {"expect_visible": "일상의 모든 순간을 잇다"}]},
    {"id": "C2", "type": "web", "name": "C2", "steps": [{"goto": "/cover"}, {"expect_visible": "덮인 문구"}]},
    {"id": "C3", "type": "web", "name": "C3", "steps": [{"goto": "/cover"}, {"expect_visible": "클릭 통과 제목"}]},
    {"id": "C4", "type": "web", "name": "C4", "steps": [{"goto": "/cover"}, {"click": "덮인 문구"}]}]}, ".")
assert "가려져" in cov[3]["errors"][0] and "가린 요소: div" in cov[3]["errors"][0], cov[3]["errors"]  # 무엇이 덮었는지 알려줌
assert cov[2]["passed"], cov[2]["errors"]  # pointer-events:none 제목(CVnet 메인 문구)도 보이면 보임
assert cov[0]["passed"] and not cov[1]["passed"], [c["errors"] for c in cov]  # 고정 헤더가 위쪽 일부만 덮으면 보임, 전부 덮이면 가려짐

icon4 = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    {"id": "I4", "type": "web", "name": "I4", "steps": [{"goto": "/ec-icon"}, {"click": "뒤로가기"}, {"expect_url": "ec-pricing"}]}]}, ".")
icon5 = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [
    {"id": "I5", "type": "web", "name": "I5", "steps": [{"goto": "/ec-icon"}, {"click": "이전으로"}, {"expect_url": "ec-done"}]}]}, ".")
assert icon5[0]["passed"], icon5  # 정확히 같은 글자가 없으면 기호가 붙은 버튼도 찾음
assert icon4[0]["passed"], icon4  # 글자 없는 이미지 버튼은 alt로
icon3 = aris.run_suite({"web": {"base_url": base, "timeout_ms": 300}, "cases": [
    {"id": "I3", "type": "web", "name": "I3", "steps": [{"goto": "/ec"}, {"hover": "가입하기"}, {"expect_navigated": ""}]}]}, ".")
assert icon3[0]["errors"][0].startswith("3단계 이동됨 실패: 페이지가 바뀌지 않음"), icon3  # 올리기는 페이지를 바꾸지 않음
assert [r["passed"] for r in icon] == [True, False] and icon[1]["errors"][0].startswith("3단계 이동됨 실패: 페이지가 바뀌지 않음"), icon

# 자연어 TC → 키워드 자동 변환
import convert
c = convert.convert("1. 메인 접속\n2. 상단 [가입하기]에 마우스 오버\n3. 하위 메뉴 [가격정책] 클릭",
                    "3. 가격정책 페이지로 이동되어야 함", "메인: https://x.com/kr/", "https://x.com")
assert c["proc"] == ["이동 | /kr/", "올리기 | 가입하기", "클릭 | 가격정책"] and c["expect"] == ["이동됨"] and c["confidence"] == "보통"
c = convert.convert("1. 메인 접속\n2. 상단 [ERP 주요기능]에 마우스 오버",
                    "2. 하위 메뉴가 펼쳐져야 함\n- 재고/유통, 생산/제조, … 웹메일 등", "메인: https://x.com/kr/", "https://x.com")
assert c["expect"] == ["보임 | 재고/유통", "보임 | 생산/제조", "보임 | 웹메일"] and c["confidence"] == "높음"
c = convert.convert("1. 메인 접속\n2. '왜 많은 기업이 선택할까요?' 영역 확인", "2. 다음 카드가 노출되어야 함\n- 연락 방법(전화/이메일)\n- [동영상 보기] 버튼\n- 로그인 | 즐겨찾기 등록",
                    "", "", {"메인 페이지": "/m"})
assert c["proc"] == ["이동 | /m"] and c["expect"] == ["확인 | 연락 방법", "확인 | 동영상 보기", "확인 | 즐겨찾기 등록"], c
c = convert.convert("1. 가입신청 페이지 접속\n2. [회사명]에 '테스트' 입력\n3. AI 솔루션 영역 [커뮤니티] 카드 클릭",
                    "3. 새 창에서 웰니스 페이지가 열려야 함", "", "", {"가입신청 페이지": "/join", "웰니스 페이지": "wellness"})
assert c["proc"] == ["이동 | /join", "입력 | 회사명 | 테스트", "새창 | 커뮤니티"] and c["expect"] == ["주소 | wellness"], c
c = convert.convert("1. 모든 기능 페이지 접속\n2. 상단 우측 로그인 아이콘 클릭", "2. 상담문의 팝업이 노출되어야 함")
assert c["confidence"] == "검토 필요" and c["expect"] == ["보임 | 상담문의"] and len(c["notes"]) == 3, c
# 엑셀 사본: 자연어 행만 채우고, 자동화 칸이 있는 행·검토 필요 행은 건드리지 않음
wb = Workbook()
ws = wb.active
ws.append(["TC ID", "테스트 절차", "기대 결과", "자동화 절차"])
ws.append(["N1", "1. 메인 접속\n2. 상단 [가입하기]에 마우스 오버", "2. 하위 메뉴가 펼쳐져야 함\n- 가격정책", ""])
ws.append(["N2", "1. 메인 접속\n2. 로고 클릭", "2. 메인 페이지로 이동되어야 함", ""])
ws.append(["N3", "1. 메인 접속", "1. 정상", "이동 | /ec"])
wb.create_sheet("설정").append(["항목", "값"])
wb["설정"].append(["웹 주소", base])
wb.create_sheet("변수").append(["이름", "값"])
wb["변수"].append(["메인 페이지", "/ec"])
wb.save("nl.xlsx")
# 실행 범위: 범위 밖 TC는 실행 대상·미실행 목록 어디에도 넣지 않아 결과 칸을 건드리지 않음
assert excel_tc.in_range("EC-9", "EC-1~EC-10") and not excel_tc.in_range("EC-037", "EC-022~EC-036, EC-045")
wb["설정"].append(["실행 범위", "N2~N3"])
wb.save("nl_range.xlsx")
_, rg_rows, _, rg_skip = excel_tc.parse("nl_range.xlsx")
assert sorted(rg_rows) == ["N3"] and rg_skip == [], (rg_rows, rg_skip)  # N2는 변환 필요라 작성 오류 쪽, N1은 범위 밖
next(r for r in wb["설정"].iter_rows() if r[0].value == "실행 범위")[1].value = "ZZ-1"
wb.save("nl_range.xlsx")
try:
    excel_tc.parse("nl_range.xlsx")
    raise AssertionError("범위에 맞는 TC가 없으면 알려야 함")
except ValueError as e:
    assert "ZZ-1" in str(e)
conv_out, conv_count = excel_tc.convert_file("nl.xlsx")
o = load_workbook(conv_out).active
assert conv_count == {"높음": 1, "검토 필요": 1} and o.cell(2, 4).value == "이동 | /ec\n올리기 | 가입하기"
assert o.cell(1, 6).value == "ARIS 변환" and o.cell(3, 4).value is None and "[제안]" in o.cell(3, 6).value and o.cell(4, 4).value == "이동 | /ec"
assert excel_tc.convert_file(conv_out)[1] == {"검토 필요": 1}  # 다시 돌려도 'ARIS 변환' 칸을 새로 만들지 않고 재사용
conv_suite, _, conv_inv, _ = excel_tc.parse(str(conv_out))
assert [r["passed"] for r in aris.run_suite(conv_suite, ".")] == [True, True] and conv_inv[0]["id"] == "N2" and conv_inv[0]["convert"]

# 웹 'TC 검토': 실행 없이 읽은 결과 + 변환 제안 → 고쳐서 적용 → 변환 엑셀 받기 → 그대로 실행
with open("nl.xlsx", "rb") as f:
    rvw = client.post("/api/reviews", data={"file": (f, "자연어.xlsx")}).get_json()
st = {x["id"]: x for x in rvw["rows"]}
assert [st[i]["state"] for i in ("N1", "N2", "N3")] == ["convert", "convert", "ready"] and not rvw["applied"]
assert st["N1"]["suggestion"]["confidence"] == "높음" and st["N2"]["suggestion"]["confidence"] == "검토 필요"
assert client.get(f"/reviews/{rvw['id']}/file").status_code == 404  # 적용 전엔 받을 파일 없음
assert client.post(f"/api/reviews/{rvw['id']}/apply", json={"edits": {"../x": {}}}).status_code == 400
assert "기대 결과가 비어" in client.post(f"/api/reviews/{rvw['id']}/apply", json={"edits": {"3": {"proc": "이동 | /ec", "expect": ""}}}).get_json()["error"]
edits = {st["N1"]["row"]: {"proc": "\n".join(st["N1"]["suggestion"]["proc"]), "expect": "\n".join(st["N1"]["suggestion"]["expect"])},
         st["N2"]["row"]: {"proc": "이동 | /ec", "expect": "주소 | /ec"}}  # 검토 필요 행은 사람이 고쳐서 적용
n1, n2 = list(edits.items())
rvw = client.post(f"/api/reviews/{rvw['id']}/apply", json={"edits": dict([n1])}).get_json()
rvw = client.post(f"/api/reviews/{rvw['id']}/apply", json={"edits": dict([n2])}).get_json()  # 나눠 적용해도 앞의 것이 유지
assert rvw["applied"] and all(x["state"] == "ready" for x in rvw["rows"]), rvw["rows"]
assert client.get(f"/reviews/{rvw['id']}/file").status_code == 200
assert load_workbook("nl.xlsx").active.cell(3, 4).value is None  # 원본은 그대로
rv_run = client.post(f"/api/reviews/{rvw['id']}/run").get_json()["id"]
for _ in range(60):
    run = client.get(f"/api/runs/{rv_run}").get_json()
    if run["status"] in ("done", "error"):
        break
    time.sleep(0.5)
assert run["filename"] == "자연어.xlsx" and [r["passed"] for r in run["results"]] == [True, True, True], run
assert client.post("/api/reviews/zz/apply", json={"edits": {}}).status_code == 404

assert excel_tc.parse_ui("web", [["올리기", "가입하기"], ["선택", "국가", "미국"], ["뒤로"], ["창닫기"]]) == [
    {"hover": "가입하기"}, {"select": "국가", "text": "미국"}, {"back": ""}, {"close": ""}]

# 리소스가 늦게 와도 화면이 준비되면 진행 (로딩 제한 1.5초 < 이미지 3초)
heavy = {"web": {"base_url": base, "timeout_ms": 1500, "nav_timeout_ms": 1500}, "cases": [
    {"id": "H1", "type": "web", "name": "무거운 페이지", "steps": [{"goto": "/heavy"}, {"expect_visible": "메인"}]}]}
assert aris.run_suite(heavy, ".")[0]["passed"]
late = {"web": {"base_url": base, "timeout_ms": 2000, "nav_timeout_ms": 10000}, "cases": [
    {"id": "L1", "type": "web", "name": "load 뒤에 붙는 메뉴", "steps": [{"goto": "/lateload"}, {"click": "전체메뉴"}, {"expect_visible": "하위 메뉴"}]}]}
assert aris.run_suite(late, ".")[0]["passed"]  # load 전에 누르면 메뉴가 안 열림

# 범용 양식: 문서 제목 줄 + 2번째 줄 영어 제목 + 유형 칸 없음 + ID 제목이 낯선 이름 + OK/NG 표현 + 병합·수식 유지
wb = Workbook()
ws = wb.active
ws.title = "Sheet1"
ws.append(["Test Case"])
ws.merge_cells("A1:E1")
ws.append(["번호", "Precondition", "Test Step", "Expected Output", "Result Status", "Notes", "", "OK", "NG", "N/T"])
ws.merge_cells("F2:G2")
ws.append(["LG-01", "URL: x", "이동 | /long\n클릭 | text=STT 보기", "보임 | Solution 01 음성인식", "OK", "메모 유지"])
ws.append(["LG-02", "", "1. 상단 [AI] 클릭", "2. AI 페이지 진입", "OK", ""])
ws.append(["LG-03", "", "이동 | /long", "사라짐 | 공지 팝업", "", ""])
ws["H3"], ws["I3"], ws["J3"] = '=COUNTIF(E3:E5,"OK")', '=COUNTIF(E3:E5,"NG")', '=COUNTIF(E3:E5,"N/T")'
wb.create_sheet("설정").append(["항목", "값"])
wb["설정"].append(["웹 주소", base])
wb["설정"].append(["페이지 로딩 대기(초)", 20])
wb["설정"].append(["판정 대기(초)", "2.5"])
wb["설정"].append(["화면 크기", "390 x 844"])
wb.save("generic.xlsx")
g_web = excel_tc.parse("generic.xlsx")[0]["web"]
assert (g_web["nav_timeout_ms"], g_web["timeout_ms"], g_web["viewport"]) == (20000, 2500, (390, 844))
g_ws, g_row, g_cols = excel_tc.locate(load_workbook("generic.xlsx"))
assert (g_ws.title, g_row, g_cols["id"], g_cols["proc"], g_cols["result"], g_cols["type"]) == ("Sheet1", 2, 1, 3, 5, None)
results, invalid, out_path = excel_tc.run_file("generic.xlsx")
assert [r["passed"] for r in results] == [True, False] and invalid[0]["convert"] is True
assert results[0]["name"] == "보임 | Solution 01 음성인식"  # 테스트 항목 칸이 없으면 기대 결과 첫 줄을 제목으로
assert results[0]["step_count"] == 3
o = load_workbook(out_path)["Sheet1"]
assert [o.cell(r, 5).value for r in (3, 4, 5)] == ["OK", "N/T", "NG"]  # 원래 쓰던 표현으로 기록
assert o.cell(3, 6).value == "메모 유지" and o.cell(3, 8).value == '=COUNTIF(E3:E5,"OK")' and len(o.merged_cells.ranges) == 2
assert [o.cell(2, c).value for c in (11, 12)] == ["ARIS 실패 사유", "ARIS 이슈"]  # 비고 칸은 덮어쓰지 않고 새 칸
assert o.cell(4, 11).value.startswith("[변환 필요]")
# 사람용 문장 칸 + 자동화 칸: 자동화 칸이 채워진 행은 그걸 실행, 비어 있으면 사람용 칸(→ 변환 필요)
wb = Workbook()
ws = wb.active
ws.append(["Test Case ID", "Test Step", "Expected Output", "자동화 절차", "자동화 기대 결과", "Result Status"])
ws.append(["AU-01", "1. 메인 접속\n2. STT 보기 클릭", "2. Solution 01 음성인식 영역으로 이동", "이동 | /long\n클릭 | STT 보기",
           "보임 | Solution 01 음성인식", ""])
ws.append(["AU-02", "1. 메인 접속", "1. 팝업 노출", "", "", ""])
from openpyxl.worksheet.datavalidation import DataValidation
dv = DataValidation(type="list", formula1='"OK,NG,N/A,N/T"')  # 결과 칸이 비어 있어도 드롭다운 목록으로 표현을 맞춤
ws.add_data_validation(dv)
dv.add("F2:F50")
wb.create_sheet("설정").append(["항목", "값"])
wb["설정"].append(["웹 주소", base])
wb.save("auto.xlsx")
s_auto, _, inv_auto, _ = excel_tc.parse("auto.xlsx")
au1 = s_auto["cases"][0]
assert au1["steps"] == [{"goto": "/long"}, {"click": "STT 보기"}, {"expect_visible": "Solution 01 음성인식"}]
assert au1["name"] == "Solution 01 음성인식 영역으로 이동" and inv_auto[0]["id"] == "AU-02" and inv_auto[0]["convert"]
assert aris.run_suite(s_auto, ".")[0]["passed"]
au_desc = aris.issue_fields(au1, {}, ["x"], "", "DEMO")["description"]  # 이슈엔 사람이 쓴 절차·기대 결과, 자동화 단계는 참고로
assert "h3. 재현 절차\n# 메인 접속\n# STT 보기 클릭\nh3. 기대 결과\n2. Solution 01 음성인식 영역으로 이동" in au_desc
assert "{noformat}\n1. 이동 | /long\n2. 클릭 | STT 보기\n3. 보임 | Solution 01 음성인식\n{noformat}" in au_desc
o_auto = load_workbook(excel_tc.run_file("auto.xlsx")[2]).active
assert [o_auto.cell(r, 6).value for r in (2, 3)] == ["OK", "N/T"]

try:
    excel_tc.locate(Workbook())
    raise AssertionError("빈 엑셀은 오류여야 함")
except ValueError as e:
    assert "TC 표를 찾지 못했어요" in str(e)

# jira.env 읽기: 빈 값·주석은 무시, 이미 있는 환경변수는 덮어쓰지 않음, BOM 허용
open("t.env", "w", encoding="utf-8-sig").write('# 주석\nARIS_T1="값1"\nARIS_T2=\nARIS_T3=새값\n')
os.environ["ARIS_T3"] = "기존값"
aris.load_env("t.env")
assert os.environ["ARIS_T1"] == "값1" and "ARIS_T2" not in os.environ and os.environ["ARIS_T3"] == "기존값"

# 가짜 Jira 서버로 등록 흐름 검증: 생성 → 첨부 → 중복 생략 → 에러 본문 전달
jira = {"issues": {}, "attachments": [], "fail": False}


class FakeJira(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if jira["fail"]:
            code, out = 400, {"errors": {"issuetype": "이슈 유형을 찾을 수 없음"}}
        elif self.path == "/rest/api/3/search/jql":
            label = json.loads(body)["jql"].split('labels="')[1].split('"')[0]
            code, out = 200, {"issues": [{"key": k} for k, f in jira["issues"].items() if label in f["labels"]]}
        elif self.path == "/rest/api/2/issue" and jira.get("no_priority") and "priority" in json.loads(body)["fields"]:
            code, out = 400, {"errors": {"priority": "Field 'priority' cannot be set."}}
        elif self.path == "/rest/api/2/issue":
            key = f"QA-{len(jira['issues']) + 1}"
            jira["issues"][key] = json.loads(body)["fields"]
            code, out = 201, {"key": key}
        elif self.path.endswith("/attachments"):
            jira["attachments"].append(self.path)
            code, out = 200, []
        else:
            code, out = 404, {}
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out, ensure_ascii=False).encode())

    def do_GET(self):  # 환경 점검: 프로젝트 조회
        ok = self.path == "/rest/api/3/project/QA"
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"key": "QA", "name": "ARIS"} if ok else {}).encode())

    def log_message(self, *args):
        pass


jsrv = HTTPServer(("127.0.0.1", 0), FakeJira)
threading.Thread(target=jsrv.serve_forever, daemon=True).start()
os.environ.update(JIRA_URL=f"http://127.0.0.1:{jsrv.server_port}", JIRA_EMAIL="a@b.c",
                  JIRA_TOKEN="t", JIRA_PROJECT="QA", JIRA_ISSUE_TYPE="버그")
fail_suite = {"web": {"base_url": base, "timeout_ms": 1500}, "cases": [web_bad]}
r1 = aris.run_suite(fail_suite, ".")[0]
assert r1["issue"] == "QA-1" and jira["attachments"] == ["/rest/api/2/issue/QA-1/attachments"]
assert jira["issues"]["QA-1"]["issuetype"] == {"name": "버그"} and "aris-W2" in jira["issues"]["QA-1"]["labels"]
r2 = aris.run_suite(fail_suite, ".")[0]
assert r2["issue"] == "QA-1" and len(jira["issues"]) == 1  # 중복 등록 안 함
jira["fail"] = True
r3 = aris.run_suite({"api": api, "cases": [bad]}, ".")[0]
assert r3["issue"] is None and "HTTP 400" in r3["issue_error"] and "이슈 유형을 찾을 수 없음" in r3["issue_error"]
# 중요도 → 우선순위: 아는 값만 넣고, 우선순위 칸이 없는 프로젝트면 빼고 다시 등록
jira["fail"] = False
assert aris.issue_fields({**web_bad, "severity": "상"}, {}, ["x"], "", "QA")["priority"] == {"name": "High"}
assert aris.issue_fields({**web_bad, "severity": " Minor "}, {}, ["x"], "", "QA")["priority"] == {"name": "Low"}
assert "priority" not in aris.issue_fields({**web_bad, "severity": "애매"}, {}, ["x"], "", "QA")
jira["no_priority"] = True
r4 = aris.run_suite({"web": {"base_url": base, "timeout_ms": 1500}, "cases": [{**web_bad, "id": "W9", "severity": "최상"}]}, ".")[0]
assert r4["issue"] and "priority" not in jira["issues"][r4["issue"]], r4
# 환경 점검: Jira 연결·프로젝트 확인, Appium이 꺼져 있으면 경고
env = {i["name"]: i for i in aris.check_env("http://127.0.0.1:9")}
assert env["Jira"]["state"] == "ok" and "프로젝트 QA (ARIS)" in env["Jira"]["detail"], env["Jira"]
assert env["브라우저 (웹 TC)"]["state"] == "ok", env["브라우저 (웹 TC)"]
assert env["Appium (앱 TC)"]["state"] == "warn" and "appium을 켜" in env["Appium (앱 TC)"]["detail"]
assert env["Android 기기 (앱 TC)"]["state"] in ("ok", "warn")
os.environ["JIRA_PROJECT"] = "NOPE"
assert aris.check_env("http://127.0.0.1:9")[0]["state"] == "fail"
os.environ["JIRA_PROJECT"] = "QA"
for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_PROJECT", "JIRA_ISSUE_TYPE"):
    os.environ.pop(k)
assert aris.check_env("http://127.0.0.1:9")[0]["state"] == "warn"  # jira.env 없음 = dry-run
# 앱: 가짜 Appium 드라이버(화면 XML + xpath)로 로그인/비로그인 흐름 검증
from lxml import etree


class FakeEl:
    def __init__(self, drv, node):
        self.drv, self.node = drv, node

    def is_displayed(self):
        return self.node.get("displayed", "true") == "true"

    def click(self):
        self.drv.tap(self.node.get("resource-id") or self.node.get("text"))

    def clear(self):
        self.node.set("text", "")

    def send_keys(self, v):
        self.node.set("text", v)
        self.drv.kb = True


class FakeDriver:
    def __init__(self):
        self.screen, self.kb, self.calls, self.scrolled = "login", False, [], 0
        self.user = self.pw = ""

    anr = 0  # 남은 'System UI 응답 없음' 창 표시 횟수

    def implicitly_wait(self, sec):
        pass

    def xml(self):
        if self.anr:
            return """<r><n text="System UI isn't responding"/><n text="Wait"/></r>"""
        if self.screen == "login":
            return f"""<r><n resource-id="user" hint="Username" text="{self.user}"/><n resource-id="pw" hint="Password" text="{self.pw}"/>
                <n text="Login" content-desc="login button"/>{'<n text="Provided credentials do not match"/>' if self.screen_err else ''}</r>"""
        return f"""<r><n text="Products"/><n text="Log Out"/><n text="Sauce Lab's Bottom Item" displayed="{'true' if self.scrolled else 'false'}"/></r>"""

    screen_err = False

    def find_elements(self, by, value):
        root = etree.fromstring(self.xml())
        if by == "xpath":
            nodes = root.xpath(value)
        elif by == "id":
            nodes = root.xpath(f"//*[@resource-id='{value}']")
        else:
            nodes = root.xpath(f"//*[@content-desc='{value}']")
        return [FakeEl(self, n) for n in nodes]

    def tap(self, what):
        self.calls.append(what)
        if what == "Wait":
            self.anr -= 1
        elif what == "Login":
            ok = (self.user, self.pw) == ("bob@example.com", "10203040")
            self.screen, self.screen_err = ("home", False) if ok else ("login", True)
        elif what == "Log Out":
            self.screen, self.user, self.pw = "login", "", ""

    def get_window_size(self):
        return {"width": 1080, "height": 2400}

    def execute_script(self, name, args):
        self.calls.append((name, args["direction"]))
        if args["direction"] == "up":
            self.scrolled += 1

    def hide_keyboard(self):
        if not self.kb:
            raise RuntimeError("키보드 없음")
        self.kb = False

    def get_screenshot_as_file(self, path):
        Path(path).write_bytes(b"png")


def fill_hook(drv):  # FakeEl.send_keys가 노드만 바꾸므로 드라이버 상태에도 반영
    orig = FakeEl.send_keys
    def sk(self, v):
        orig(self, v)
        setattr(drv, "user" if self.node.get("resource-id") == "user" else "pw", v)
    FakeEl.send_keys = sk


def app_case(cid, lines):
    return {"id": cid, "name": cid, "type": "app", "steps": excel_tc.parse_ui("app", excel_tc.lines(lines))}


login = "입력 | Username | bob@example.com\n입력 | Password | 10203040\n키보드닫기\n클릭 | Login"
cases = {
    "A1": (login + "\n보임 | Products\n스크롤 | Sauce Lab's Bottom Item", True),       # 로그인 + 따옴표 문구 스크롤
    "A2": ("입력 | Username | bob@example.com\n입력 | Password | 틀림\n클릭 | Login\n보임 | Provided credentials", True),  # 네거티브
    "A3": (login + "\n클릭 | Log Out\n보임 | Login\n사라짐 | Products", True),          # 로그아웃 → 비로그인
    "A4": ("클릭 | 없는 버튼", False),
    "A5": ("클릭 | ~login button\n키보드닫기\n스와이프 | 왼쪽", True),                    # 접근성 ID, 키보드 없어도 통과
}
for cid, (src, want) in cases.items():
    drv = FakeDriver(); fill_hook(drv)
    errs, _, _ = aris.app_steps(app_case(cid, src), {}, drv)
    assert (not errs) == want, (cid, errs)
    if cid == "A4":
        assert "'없는 버튼'를 화면에서 찾지 못함" in errs[0], errs
    if cid == "A1":
        assert ("mobile: swipeGesture", "up") in drv.calls
# 에뮬레이터 'System UI 응답 없음' 창: 닫고 다시 찾아 통과, 끝내 실패하면 원인 참고 문구를 붙임
drv = FakeDriver(); fill_hook(drv); drv.anr = 1
errs, _, _ = aris.app_steps(app_case("A6", login + "\n보임 | Products"), {}, drv)
assert not errs and "Wait" in drv.calls, errs
drv = FakeDriver(); drv.anr = 1
errs, _, _ = aris.app_steps(app_case("A7", "클릭 | 없는 버튼"), {}, drv)
assert len(errs) == 2 and "System UI 응답 없음" in errs[1], errs
assert aris.xlit("a'b\"c") == "concat('a', \"'\", 'b\"c')"
try:
    excel_tc.parse_ui("web", [["스와이프", "위"]])
    raise AssertionError("웹에서 앱 전용 키워드는 막아야 함")
except ValueError as e:
    assert "웹에서 쓸 수 없는 키워드" in str(e)
# 설정 시트: 쉬운 이름 → Appium 설정, 판정 대기는 앱에도 적용
wb = Workbook(); ws = wb.active
ws.append(["TC ID", "유형", "테스트 절차", "기대 결과"]); ws.append(["A1", "앱", "클릭 | Login", "보임 | Products"])
st = wb.create_sheet("설정"); st.append(["항목", "값"])
for r in [["앱 패키지", "com.saucelabs.mydemoapp.android"], ["앱 초기화", "Y"], ["판정 대기(초)", 7], ["기기 이름", "emulator-5554"]]:
    st.append(r)
wb.save("app_tc.xlsx")
app_suite = excel_tc.parse("app_tc.xlsx")[0]["app"]
assert app_suite["capabilities"] == {"appium:appPackage": "com.saucelabs.mydemoapp.android", "appium:noReset": False,
                                     "appium:deviceName": "emulator-5554"} and app_suite["timeout_ms"] == 7000, app_suite

print("\n모든 셀프 테스트 통과")
