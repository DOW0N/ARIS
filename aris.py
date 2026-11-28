"""웹·앱·API·스크립트 테스트 자동화 실행기. 실패하면 Jira Cloud 이슈를 자동 등록한다.

사용법: python aris.py [testcases.yaml]
Jira 정보(JIRA_URL, JIRA_EMAIL, JIRA_TOKEN, JIRA_PROJECT)는 jira.env 파일 또는 환경변수로 넣는다.
없으면 dry-run(등록 내용만 출력).
"""
import getpass
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

ARTIFACTS = Path("artifacts")  # 실패 스크린샷 저장 위치


def load_env(path):
    """KEY=값 형식의 설정 파일을 환경변수로 읽는다. 이미 설정된 환경변수와 빈 값은 건드리지 않는다."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():  # 메모장 저장 시 붙는 BOM 제거
        key, sep, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if sep and key and not key.startswith("#") and val and not os.environ.get(key):
            os.environ[key] = val


# exe로 묶으면 __file__이 임시 폴더라서, 사용자 파일(jira.env, scripts/)은 exe 옆에서 찾는다
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
load_env(APP_DIR / "jira.env")
if getattr(sys, "frozen", False) and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    # exe 안의 Playwright는 기본으로 exe 내부에서 크롬을 찾는다. 이 PC에 설치된 크롬을 쓰게 함
    # shortcut: 크롬이 설치된 PC에서만 동작, v0.8 배포판에서 크롬을 함께 묶음
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright")


HTML_TAGS = {"a", "button", "input", "select", "textarea", "label", "form", "img", "nav", "header", "footer", "main",
             "section", "div", "span", "p", "ul", "ol", "li", "table", "tr", "td", "th", "option",
             "h1", "h2", "h3", "h4", "h5", "h6"}
# 실패 메시지에 엑셀에 쓴 키워드 이름으로 보여준다
ACTION_KO = {"goto": "이동", "click": "클릭", "fill": "입력", "press": "키", "expect_text": "확인", "wait": "대기",
             "expect_visible": "보임", "expect_hidden": "사라짐", "expect_exists": "존재", "expect_absent": "없음", "hover": "올리기",
             "check": "체크", "uncheck": "해제", "select": "선택", "frame": "프레임", "popup": "새창", "close": "창닫기",
             "back": "뒤로", "reload": "새로고침", "expect_url": "주소", "expect_title": "제목", "expect_alert": "알림",
             "expect_navigated": "이동됨", "scroll_to": "스크롤", "swipe": "스와이프", "hide_keyboard": "키보드닫기",
             "restart_app": "앱재시작"}


FRAME_RETRY = {"click", "fill", "check", "uncheck", "select", "popup", "expect_text", "expect_visible", "expect_exists"}


def run_steps(case, do, screenshot, pass_shot=False):
    """steps를 순서대로 실행. 첫 실패에서 멈추고 스크린샷을 남긴다. 각 step의 첫 키가 동작 이름.
    pass_shot이면 통과한 TC도 마지막 화면을 남긴다(통과 증거)."""
    def take():
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        shot = str(ARTIFACTS / f"{case['id']}.png")
        try:
            screenshot(shot)
            return shot
        except Exception:
            return None
    for n, step in enumerate(case["steps"], 1):
        action, arg = next(iter(step.items()))
        try:
            do(action, arg, step)
        except Exception as e:
            return [f"{n}단계 {ACTION_KO.get(action, action)} 실패: {str(e).splitlines()[0]}"], "", take()
    return [], "", take() if pass_shot else None


# ---------------- API ----------------
def check_api(case, resp):
    exp = case.get("expect", {})
    errors = []
    if "status" in exp and resp.status_code != exp["status"]:
        errors.append(f"status: 기대 {exp['status']}, 실제 {resp.status_code}")
    if "max_ms" in exp:
        ms = resp.elapsed.total_seconds() * 1000
        if ms > exp["max_ms"]:
            errors.append(f"응답시간: 기대 ≤{exp['max_ms']}ms, 실제 {ms:.0f}ms")
    if "json" in exp:
        try:
            body = resp.json()
        except ValueError:
            return errors + ["응답 본문이 JSON이 아님"]
        if not isinstance(body, dict):
            return errors + ["응답 본문이 JSON 객체가 아님"]
        for key, want in exp["json"].items():
            if body.get(key) != want:
                errors.append(f"json.{key}: 기대 {want!r}, 실제 {body.get(key)!r}")
    return errors


def run_api(case, cfg, ctx):
    try:
        resp = requests.request(
            case.get("method", "GET"), cfg.get("base_url", "").rstrip("/") + case["path"],
            json=case.get("body"), headers=case.get("headers"), timeout=10,
        )
    except requests.RequestException as e:
        return [f"요청 실패: {e}"], "응답 없음", None
    return check_api(case, resp), f"HTTP {resp.status_code}\n{resp.text[:1000]}", None


# ---------------- 웹 (Playwright) ----------------
class Why(AssertionError):
    """이미 QA용 한글 이유가 담긴 실패. 다시 해석하지 않는다."""


# 요소와 모든 상위 요소의 투명도를 곱한 값. 페이드 슬라이드·팝업은 화면에 있어도 opacity 0이라 사람 눈엔 안 보인다.
OPACITY_JS = "el => { let o = 1; for (let e = el; e; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity); return o; }"
# 요소 가운데 점이 화면 안이고, 그 점을 실제로 이 요소가 차지하는지(접힌 메뉴에 잘리거나 다른 요소에 가려지지 않았는지)
# 가운데 한 점만 보면 고정 헤더·장식 레이어가 일부만 덮어도 '가려짐'이 돼서, 요소 안 여러 점 중 하나라도 보이면 보임
# pointer-events:none(클릭을 통과시키는 제목·장식 글자)은 elementFromPoint에 안 잡혀서, 확인하는 동안만 잠깐 켠다
COVER_JS = """el => { el.scrollIntoView({block: "center"}); const r = el.getBoundingClientRect();
  const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!t || t === el || el.contains(t) || t.contains(el)) return "";
  const cls = typeof t.className === "string" ? t.className.trim().split(/\\s+/).slice(0, 2).join(".") : "";
  const txt = (t.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 20);
  return t.tagName.toLowerCase() + (t.id ? "#" + t.id : "") + (cls ? "." + cls : "") + (txt ? ` '${txt}'` : ""); }"""
IN_VIEW_JS = """el => { const r = el.getBoundingClientRect(); if (!r.width || !r.height) return false;
  const pe = el.style.pointerEvents; el.style.pointerEvents = "auto";
  try { for (const fx of [0.5, 0.15, 0.85]) for (const fy of [0.5, 0.2, 0.8]) {
    const x = r.left + r.width * fx, y = r.top + r.height * fy;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    const hit = document.elementFromPoint(x, y); if (hit && (hit === el || el.contains(hit))) return true; }
  return false; } finally { el.style.pointerEvents = pe; } }"""


def josa(word, batchim, plain):
    """'가격정책'은 / '메뉴'는 처럼 받침에 맞는 조사. 한글이 아니면 받침 없는 쪽."""
    c = word[-1:]
    return batchim if "가" <= c <= "힣" and (ord(c) - 0xAC00) % 28 else plain


def run_web(case, cfg, ctx):
    from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright

    if "browser" not in ctx:  # 브라우저는 한 번만 띄우고 케이스마다 새 세션
        ctx["pw"] = sync_playwright().start()
        ctx["browser"] = ctx["pw"].chromium.launch(headless=cfg.get("headless", True))
        probe = ctx["browser"].new_page()  # 화면 없는 실행은 브라우저 이름에 'HeadlessChrome'이 붙어 봇으로 차단하는 사이트가 있음
        ctx["ua"] = probe.evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
        probe.close()
    elif cfg.get("case_delay_ms"):  # 실제 사이트는 짧은 시간에 접속이 몰리면 차단하기도 해서 TC 사이에 쉬어 감
        time.sleep(cfg["case_delay_ms"] / 1000)
    w, h = cfg.get("viewport", (1920, 1080))  # 기본은 PC 화면. 좁으면 반응형 사이트가 모바일 메뉴(☰)로 바뀜
    # 케이스마다 새 세션: 쿠키·로그인·닫은 팝업 기록이 다음 TC에 영향을 주지 않게
    session = ctx["browser"].new_context(viewport={"width": w, "height": h}, user_agent=ctx["ua"])
    session.set_default_timeout(cfg.get("timeout_ms", 5000))  # 클릭·판정 대기
    session.set_default_navigation_timeout(cfg.get("nav_timeout_ms", 30000))  # 실제 사이트는 첫 로딩이 느림
    base = cfg.get("base_url", "").rstrip("/")
    timeout_ms = cfg.get("timeout_ms", 5000)
    dialogs = []  # 알림창(alert/confirm) 내용. 확인을 눌러 닫고 기록만 남긴다

    ready = set()  # 문서가 열린(commit된) iframe만 자동 탐색: 로딩이 안 끝나는 광고·추적 iframe에서 무한 대기 방지

    def watch(pg):
        pg.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        pg.on("framenavigated", lambda f: ready.add(f))
        return pg

    pages = [watch(session.new_page())]  # 새창을 열면 뒤에 쌓이고, 창닫기 하면 이전 창으로
    state = {"scope": pages[-1]}  # 지금 찾는 범위: 현재 창 또는 그 안의 프레임

    def page():
        return pages[-1]

    def is_selector(arg):
        """'#id', 'text=..', 'button', 'input[name=q]', 'div.menu a'는 선택자, 'AI', 'Contact us'는 화면 문구."""
        if arg.startswith(("#", ".", "[", "//", "text=", "css=", "xpath=", "role=")):
            return True
        return re.split(r"[\s#.\[:>]", arg, maxsplit=1)[0].lower() in HTML_TAGS

    def every(arg, exact=False):
        sc = state["scope"]
        if is_selector(arg):
            return sc.locator(arg)
        # 글자 없는 아이콘 버튼은 aria-label·title로 이름이 붙어 있어서 같이 찾는다
        return (sc.get_by_text(arg, exact=exact).or_(sc.get_by_label(arg, exact=exact))
                .or_(sc.get_by_title(arg, exact=exact)).or_(sc.get_by_alt_text(arg, exact=exact)))  # 이미지 버튼은 alt

    def target(arg, exact=False):
        """같은 메뉴가 접힌 모바일 메뉴·푸터에도 있는 사이트가 많아서, 지금 화면에 보이는 것을 먼저 고른다.
        화면에 보이는 게 없으면 보이는 것 중 첫 번째(필요하면 Playwright가 스크롤해서 누름)."""
        loc = every(arg, exact).filter(visible=True)
        if exact and not is_selector(arg) and every(arg, exact).count() == 0:  # '< 뒤로가기', '신청하기 →'처럼 기호가 붙은 버튼
            loc = every(arg).filter(visible=True)
        try:
            i = loc.evaluate_all(f"els => els.findIndex({IN_VIEW_JS})")
        except PlaywrightError:
            i = -1
        return loc.nth(i) if i >= 0 else loc.first

    def field(arg):
        """입력칸·체크박스·목록: 선택자가 아니면 칸 이름(label)이나 안내 문구(placeholder)로 찾는다."""
        sc = state["scope"]
        loc = sc.locator(arg) if is_selector(arg) else sc.get_by_label(arg).or_(sc.get_by_placeholder(arg))
        return loc.filter(visible=True).first

    def explain(action, arg):
        """Playwright 영어 에러 대신 QA가 바로 알 수 있는 이유."""
        if action == "goto":
            return f"페이지를 열지 못함 (시간 초과 또는 주소 오류): {arg}"
        q = f"'{arg}'"
        here = lambda: " ".join(x for x in (page().title(), page().url) if x)
        if action == "expect_text":
            return f"페이지에 {q} 문구가 없음 (현재 페이지: {here()})"
        if action == "expect_hidden":
            return f"{q}{josa(arg, '이', '가')} 아직 화면에 보임"
        if action == "expect_url":
            return f"주소에 '{arg}'가 없음 (현재 주소: {page().url})"
        if action == "expect_title":
            return f"페이지 제목에 '{arg}'가 없음 (현재 제목: {page().title()})"
        if action == "expect_alert":
            return f"'{arg}' 알림창이 뜨지 않음" + (f" (뜬 알림: {' / '.join(dialogs)})" if dialogs else "")
        if action == "frame":
            return f"프레임을 찾지 못함: {arg}"
        if action == "popup":
            return f"{q}{josa(arg, '을', '를')} 눌러도 새 창이 열리지 않음"
        if action == "select":
            return f"{q} 목록에서 '{state['select_value']}'를 고를 수 없음"
        form = action in ("fill", "check", "uncheck")
        all_ = (state["scope"].locator(arg) if is_selector(arg) else
                state["scope"].get_by_label(arg).or_(state["scope"].get_by_placeholder(arg))) if form \
            else every(arg, exact=action in ("click", "hover", "popup"))
        visible = all_.filter(visible=True)
        if all_.count() == 0:  # 주소·제목을 같이 남겨야 차단 화면·다른 페이지로 간 경우를 바로 알 수 있음
            return f"{q}{josa(arg, '을', '를')} 페이지에서 찾지 못함 (현재 페이지: {here()})"
        is_ = josa(arg, "은", "는")
        hint = " (마우스를 올려도 펼쳐지지 않는 메뉴일 수 있어요. 클릭해야 이동하는 메뉴인지 확인하세요)" \
            if action == "expect_visible" and state.get("prev") == "hover" else ""
        if visible.count() == 0:
            return f"{q}{is_} 페이지에 있지만 숨겨져 있음" + (" (메뉴라면 '올리기'로 먼저 펼치거나 화면 크기 설정을 확인하세요)" if action == "click" else hint)
        if action == "expect_visible":
            return f"{q}{is_} 페이지에 있지만 지금 화면 밖에 있거나 다른 요소에 가려짐{hint}"
        if action in ACTION_KO:
            try:  # 무엇이 덮었는지 남겨야 팝업·플로팅 버튼·쿠키 배너 중 어느 것인지 바로 앎
                by = visible.first.evaluate(COVER_JS)
            except PlaywrightError:
                by = ""
            return f"{q}{is_} 있지만 다른 요소에 가려져 {ACTION_KO[action]}할 수 없음" + (f" (가린 요소: {by})" if by else "")
        return None

    def other_scopes():
        """팝업 속 iframe처럼 지금 범위 밖에 있을 수 있는 곳. '프레임'으로 직접 들어간 경우는 손대지 않는다."""
        if state["scope"] is not page() and not state.get("auto"):
            return []
        return ([page()] if state["scope"] is not page() else []) + loaded_frames()

    def loaded_frames():
        found = []
        for f in page().frames[1:]:
            try:
                if f in ready and not f.is_detached() and f.frame_element().is_visible():
                    found.append(f)
            except PlaywrightError:
                pass
        return found

    def do(action, arg, step):
        state["prev"] = state.get("last")  # 실패 이유를 설명할 때 바로 앞 동작이 필요함
        url_before = page().url
        try:
            try:
                act(action, str(arg), step)
            except Why:
                raise
            except (AssertionError, PlaywrightError):
                # 클릭은 됐는데 페이지가 바로 넘어가면서 Playwright가 오류를 내는 경우: 이동했으면 클릭은 성공
                if action == "click" and page().url != url_before:
                    return
                if action not in FRAME_RETRY:
                    raise
                prev, auto = state["scope"], state.get("auto")
                for sc in other_scopes():  # 상담문의 팝업처럼 iframe 안에 있으면 자동으로 들어가서 다시 시도
                    state["scope"], state["auto"] = sc, sc is not page()
                    # 대상이 없는 iframe(동영상·지도·광고)에서 판정 대기 시간을 다 쓰지 않도록 바로 세어 보고 건너뜀
                    probe = every(str(arg)).count() or (action in ("fill", "check", "uncheck", "select") and field(str(arg)).count())
                    if not probe:
                        continue
                    try:
                        act(action, str(arg), step)
                        return
                    except (AssertionError, PlaywrightError):
                        pass
                state["scope"], state["auto"] = prev, auto
                raise
            finally:
                state["last"] = action
        except Why:
            raise
        except (AssertionError, PlaywrightError) as e:
            try:
                why = explain(action, str(arg))
            except PlaywrightError:  # 페이지가 닫히는 등 이유를 확인할 수 없을 때는 원래 메시지
                why = None
            if "has been closed" in str(e):
                why = "실행 도중 브라우저 창이 닫힘 (실행 중인 창을 직접 닫았거나, 사이트가 창을 닫음)"
            if why:
                raise AssertionError(why) from e
            raise

    def wait_until(check):
        """판정 대기 시간 동안 조건이 참이 될 때까지 다시 확인 (애니메이션이 끝나기를 기다림)."""
        for _ in range(max(1, timeout_ms // 100)):
            if check():
                return True
            page().wait_for_timeout(100)
        return False

    def settle():
        # 메뉴·언어 선택·팝업 동작을 window load 때 붙이는 사이트(jQuery $(window).load)가 많다.
        # 화면이 보인다고 바로 누르면 아무 반응이 없으므로, load를 잠깐 기다리되 늦으면 그냥 진행한다.
        try:
            # shortcut: 끝나지 않는 광고 iframe이 있으면 이동마다 최대 5초가 더 든다. 문제되면 load_wait_ms로 줄임
            ms = min(cfg.get("nav_timeout_ms", 30000), cfg.get("load_wait_ms", 5000))
            if ms > 0:  # Playwright는 0을 '무한 대기'로 봄
                page().wait_for_load_state("load", timeout=ms)
        except PlaywrightError:
            pass

    def act(action, arg, step):
        if action in ("click", "popup", "press", "back", "check", "uncheck", "select"):
            state["before"] = page().url  # '이동됨' 판정용: 이 동작 전 주소
        if action == "goto":
            # 이미지·영상까지 다 받을 때까지(load) 기다리면 실제 사이트에서 시간 초과가 잦다.
            # 화면 구조가 준비되면 진행하고, 이후 클릭·판정은 요소가 나타날 때까지 알아서 기다린다.
            res = page().goto(base + arg if arg.startswith("/") else arg, wait_until="domcontentloaded")
            settle()
            state["scope"] = page()
            state["before"] = page().url  # 이동 직후 주소가 기준: 뒤에 클릭 없이 '이동됨'이면 '안 바뀜'으로 실패
            # 차단(403·429)·서버 오류면 화면 문구를 찾을 필요도 없이 여기서 멈춤. 404는 '없는 페이지 안내' TC가 있어서 통과시킴
            if res and (res.status in (403, 429) or res.status >= 500):
                raise Why(f"사이트가 응답을 거부함 (HTTP {res.status}{', 자동 접속 차단 가능성' if res.status in (403, 429) else ''}): {page().title() or page().url}")
        elif action == "click":
            target(arg, exact=True).click()  # 'AI' 메뉴를 누를 때 'AI Technology' 같은 글자가 눌리지 않게
        elif action == "hover":
            target(arg, exact=True).hover()
        elif action == "fill":
            field(arg).fill(str(step["text"]))
        elif action in ("check", "uncheck"):
            el = field(arg)
            if el.count() == 0:  # 디자인용 체크박스(진짜 input이 숨겨진 경우)는 글자를 눌러서 체크
                target(arg, exact=True).click()
            else:
                el.check() if action == "check" else el.uncheck()
        elif action == "select":
            state["select_value"] = str(step["text"])
            field(arg).select_option(label=state["select_value"])
        elif action == "press":
            page().keyboard.press(arg)
        elif action == "frame":
            state["auto"] = False
            state["scope"] = page() if arg in ("나가기", "") else state["scope"].frame_locator(arg)
            if arg not in ("나가기", ""):
                expect(state["scope"].locator("body")).to_be_attached()
        elif action == "popup":
            with page().expect_popup() as info:
                target(arg, exact=True).click()
            pages.append(watch(info.value))
            page().wait_for_load_state("domcontentloaded")
            settle()
            state["scope"] = page()
        elif action == "close":
            if len(pages) > 1:
                pages.pop().close()
            state["scope"] = page()
        elif action == "back":
            page().go_back(wait_until="domcontentloaded")
            settle()
            state["scope"] = page()
        elif action == "reload":
            page().reload(wait_until="domcontentloaded")
            settle()
            state["scope"] = page()
        elif action == "expect_text":
            expect(state["scope"].locator("body")).to_contain_text(arg)
        elif action == "expect_visible":  # 페이지 어딘가가 아니라 지금 화면 안에 보여야 함(스크롤 이동 검증)
            # 같은 문구가 푸터·접힌 메뉴에도 있을 수 있으니 '첫 번째'가 아니라 '하나라도' 화면에 보이면 통과
            seen = {"in_view": False}

            def shown():
                for el in every(arg).filter(visible=True).all():
                    if el.evaluate(IN_VIEW_JS):
                        seen["in_view"] = True
                        if el.evaluate(OPACITY_JS) >= 0.5:
                            return True
                return False
            if not wait_until(shown):
                if seen["in_view"]:
                    raise Why(f"'{arg}'{josa(arg, '은', '는')} 화면 위치에 있지만 투명해서 보이지 않음 (슬라이드·페이드 애니메이션의 숨은 화면일 수 있음)")
                raise AssertionError("not shown")
        elif action == "expect_hidden":
            # 화면에 실제로 보이는 것이 없으면(숨김·접힌 메뉴·페이드 아웃) 사라진 것으로 본다
            gone = lambda: not any(el.evaluate(IN_VIEW_JS) and el.evaluate(OPACITY_JS) >= 0.1
                                   for el in every(arg).filter(visible=True).all())
            if not wait_until(gone):
                raise Why(f"'{arg}'{josa(arg, '이', '가')} 아직 화면에 보임")
        elif action == "expect_navigated":
            before = state.get("before")
            if not wait_until(lambda: before is not None and page().url != before):
                raise Why(f"페이지가 바뀌지 않음 (현재 주소: {page().url})" if before else "'이동됨' 앞에 클릭·새창·뒤로 같은 동작이 없음")
        elif action == "expect_exists":
            expect(every(arg).first).to_be_attached()
        elif action == "expect_absent":  # 숨은 복제본·iframe 안까지 세어서, 하나라도 있으면 실패
            def total():
                prev, n = state["scope"], 0
                for sc in [page()] + [f for f in page().frames[1:] if f in ready and not f.is_detached()]:
                    state["scope"] = sc
                    n += every(arg).count()
                state["scope"] = prev
                return n
            if not wait_until(lambda: total() == 0):
                raise Why(f"'{arg}'{josa(arg, '이', '가')} 페이지에 있음 ({total()}개)")
        elif action == "expect_url":
            expect(page()).to_have_url(re.compile(re.escape(arg)))
        elif action == "expect_title":
            expect(page()).to_have_title(re.compile(re.escape(arg)))
        elif action == "expect_alert":
            for _ in range(max(1, timeout_ms // 100)):  # 알림창은 조금 늦게 뜨기도 해서 판정 대기 시간만큼 기다림
                if any(arg in m for m in dialogs):
                    return
                page().wait_for_timeout(100)
            raise AssertionError(f"알림 없음: {arg}")
        elif action == "wait":
            page().wait_for_timeout(float(arg) * 1000)
        else:
            raise ValueError(f"알 수 없는 웹 동작: {action}")

    try:
        return run_steps(case, do, lambda p: page().screenshot(path=p), cfg.get("pass_shot"))
    finally:
        session.close()


# ---------------- 앱 (Appium) ----------------
APP_DEFAULTS = {"platformName": "Android", "appium:automationName": "UiAutomator2",
                "appium:autoGrantPermissions": True,  # 위치·알림 같은 권한 팝업이 TC를 막지 않게 미리 허용
                "appium:newCommandTimeout": 300}
SWIPE = {"위": "up", "아래": "down", "왼쪽": "left", "오른쪽": "right"}  # 손가락이 움직이는 방향


def xlit(text):
    """xpath 문자열 리터럴. 따옴표가 섞인 화면 문구도 찾을 수 있게."""
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in text.split("'")) + ")"


def app_steps(case, cfg, driver):
    """앱 TC의 단계를 실행. driver는 Appium 세션(테스트에서는 가짜 드라이버)."""
    anr_seen = []  # 결과 메시지에 '에뮬레이터 응답 없음이 있었다'고 남겨 원인 판단을 돕는다

    def all_by_text(text, exact=True):
        t = xlit(text)
        attrs = ("@text", "@content-desc", "@hint", "@resource-id")
        cond = " or ".join(f"{a}={t}" if exact else f"contains({a},{t})" for a in attrs)
        return driver.find_elements("xpath", f"//*[{cond}]")

    def clear_anr():
        """에뮬레이터가 무거우면 'System UI isn't responding' 창이 앱을 가린다. 앱 결함이 아니므로 '기다리기'를 눌러 닫는다."""
        driver.implicitly_wait(0)  # 실패한 찾기마다 기다림이 두 배로 늘지 않게
        try:
            if not driver.find_elements("xpath", "//*[contains(@text, \"isn't responding\") or contains(@text, '응답하지 않')]"):
                return False
            for btn in driver.find_elements("xpath", "//*[@text='Wait' or @text='대기']"):
                btn.click()
                time.sleep(1)
                anr_seen.append(True)
                return True
            return False
        except Exception:
            return False
        finally:
            driver.implicitly_wait(cfg.get("timeout_ms", 5000) / 1000)

    def located(loc):
        found = _located(loc)
        if not found and clear_anr():
            found = _located(loc)
        return found

    def _located(loc):
        """~접근성ID / id= / xpath= / 화면 글자(정확히 같은 것 우선, 없으면 포함)"""
        if loc.startswith("~"):
            return driver.find_elements("accessibility id", loc[1:])
        if loc.startswith("id="):
            return driver.find_elements("id", loc[3:])
        if loc.startswith("xpath="):
            return driver.find_elements("xpath", loc[6:])
        return all_by_text(loc) or all_by_text(loc, exact=False)

    def find(loc):
        found = located(loc)
        if not found:
            raise AssertionError(f"'{loc}'를 화면에서 찾지 못함")
        shown = [el for el in found if el.is_displayed()]
        if not shown:
            raise AssertionError(f"'{loc}'는 있지만 지금 화면에 보이지 않음 (스크롤이 필요할 수 있어요)")
        return shown[0]

    def swipe(direction):
        size = driver.get_window_size()
        driver.execute_script("mobile: swipeGesture", {
            "left": int(size["width"] * 0.1), "top": int(size["height"] * 0.2),
            "width": int(size["width"] * 0.8), "height": int(size["height"] * 0.6),
            "direction": direction, "percent": 0.7})

    def do(action, arg, step):
        if action == "click":
            find(arg).click()
        elif action == "fill":
            el = find(arg)
            el.clear()
            el.send_keys(str(step["text"]))
        elif action == "expect_text":
            if not located(arg):
                raise AssertionError(f"'{arg}'가 화면에 없음")
        elif action == "expect_visible":
            find(arg)
        elif action == "expect_hidden":
            if any(el.is_displayed() for el in all_by_text(arg, exact=False)):
                raise AssertionError(f"'{arg}'가 아직 화면에 보임")
        elif action == "expect_exists":
            if not located(arg):
                raise AssertionError(f"'{arg}'를 화면에서 찾지 못함")
        elif action == "scroll_to":
            for _ in range(6):  # 아래로 넘기며 찾다가 끝까지 없으면 실패
                if any(el.is_displayed() for el in located(arg)):
                    return
                swipe("up")
            raise AssertionError(f"'{arg}'가 나올 때까지 스크롤했지만 찾지 못함")
        elif action == "swipe":
            if arg not in SWIPE:
                raise ValueError(f"스와이프 방향은 {'/'.join(SWIPE)} 중 하나")
            swipe(SWIPE[arg])
        elif action == "hide_keyboard":
            try:
                driver.hide_keyboard()
            except Exception:  # 키보드가 이미 닫혀 있으면 그대로 진행
                pass
        elif action == "restart_app":
            pkg = cfg.get("capabilities", {}).get("appium:appPackage") or driver.current_package
            driver.terminate_app(pkg)
            driver.activate_app(pkg)
        elif action == "back":
            driver.back()
        elif action == "wait":
            time.sleep(float(arg))
        else:
            raise ValueError(f"알 수 없는 앱 동작: {action}")

    errors, *rest = run_steps(case, do, driver.get_screenshot_as_file, cfg.get("pass_shot"))
    if errors and anr_seen:
        errors = errors + ["참고: 실행 중 에뮬레이터 'System UI 응답 없음' 창이 떠서 닫았어요. 앱 결함이 아니라 PC·에뮬레이터 부하일 수 있어요"]
    return (errors, *rest)


def run_app(case, cfg, ctx):
    from appium import webdriver
    from appium.options.common import AppiumOptions

    opts = AppiumOptions()
    opts.load_capabilities({**APP_DEFAULTS, **cfg.get("capabilities", {})})
    try:  # 케이스마다 새 세션: 앱 데이터가 초기화돼 항상 같은 상태(비로그인)에서 시작
        driver = webdriver.Remote(cfg.get("server", APPIUM_SERVER), options=opts)
    except Exception as e:
        return [f"Appium 연결 실패: {str(e).splitlines()[0]} (Appium 서버와 에뮬레이터가 켜져 있는지 확인하세요)"], "", None
    driver.implicitly_wait(cfg.get("timeout_ms", 5000) / 1000)
    try:
        return app_steps(case, cfg, driver)
    finally:
        driver.quit()


# ---------------- 사용자 스크립트 ----------------
def run_script(case, cfg, ctx):
    """스크립트 파일의 run(case)를 호출. 예외(assert 실패 포함)가 나면 Fail."""
    base = Path(ctx["suite_dir"]).resolve()
    path = (base / case["script"]).resolve()
    if base not in path.parents:  # 엑셀 업로드로 임의 파일이 실행되지 않도록 TC 폴더 안의 파일만 허용
        return [f"스크립트는 {base} 안에 있어야 함: {case['script']}"], "", None
    try:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run(case)
    except Exception as e:
        return [f"{type(e).__name__}: {e}"], "", None
    return [], "", None


RUNNERS = {"api": run_api, "web": run_web, "app": run_app, "script": run_script}


# ---------------- Jira ----------------
def mask(text, secrets):
    """비밀번호 같은 변수 값이 이슈·결과 엑셀·화면에 남지 않게 가린다."""
    for secret in secrets:
        text = text.replace(secret, "****")
    return text


# TC '중요도' 칸 → Jira 우선순위 (Jira 기본 우선순위 이름). 모르는 값이면 Jira 기본값(보통 Medium)
PRIORITY = {name: jira for jira, names in {
    "Highest": ("최상", "긴급", "치명", "blocker", "critical", "highest", "p0"),
    "High": ("상", "높음", "high", "major", "p1"),
    "Medium": ("중", "보통", "medium", "normal", "p2"),
    "Low": ("하", "낮음", "low", "minor", "p3"),
    "Lowest": ("최하", "사소", "trivial", "lowest", "p4"),
}.items() for name in names}


def issue_fields(case, cfg, errors, evidence, project):
    """QA 이슈 양식(재현 절차/기대 결과/실제 결과)으로 Jira 필드를 만든다."""
    kind = case.get("type", "api")
    if kind == "api":
        steps = [f"{case.get('method', 'GET')} {cfg.get('base_url', '')}{case['path']}"]
        if case.get("body") is not None:
            steps.append(f"요청 본문: {json.dumps(case['body'], ensure_ascii=False)}")
        expected = json.dumps(case.get("expect", {}), ensure_ascii=False)
    elif kind == "script":
        steps, expected = [f"스크립트 실행: {case['script']}"], "스크립트의 모든 검증 통과"
    else:  # 엑셀 TC는 사용자가 쓴 원문(공통단계 펼친 결과)을 그대로 보여준다
        steps = case.get("display_steps") or [", ".join(f"{k}: {v}" for k, v in s.items()) for s in case["steps"]]
        expected = "시나리오의 모든 단계 정상 수행"
    auto_steps = None
    if case.get("human_steps"):  # 엑셀의 사람용 절차가 있으면 그걸 재현 절차로, 자동화 단계는 참고로 맨 아래
        auto_steps, steps = steps, [re.sub(r"^\s*\d+\s*[.)]\s*", "", x) for x in case["human_steps"].splitlines() if x.strip()]
    if case.get("human_expect"):
        expected = case["human_expect"]
    env_lines = [f"실행 일시: {datetime.now():%Y-%m-%d %H:%M:%S}",
                 f"실행자: {os.environ.get('ARIS_TESTER') or getpass.getuser()}"]
    if cfg.get("base_url"):
        env_lines.append(f"대상 주소: {cfg['base_url']}")
    if case.get("severity"):
        env_lines.append(f"중요도: {case['severity']}")
    desc = "\n".join([
        f"*테스트 유형:* {kind}",
        "h3. 테스트 환경", *[f"* {x}" for x in env_lines],
        *(["h3. 사전조건", case["precondition"]] if case.get("precondition") else []),
        "h3. 재현 절차", *[f"# {s}" for s in steps],
        "h3. 기대 결과", expected,
        "h3. 실제 결과", *[f"* {e}" for e in errors],
        *([f"{{code}}{evidence}{{code}}"] if evidence else []),
        *(["h3. 참고: ARIS 실행 단계 (실패 사유의 'n단계' 번호)", "{noformat}",
           *[f"{i}. {x}" for i, x in enumerate(auto_steps, 1)], "{noformat}"] if auto_steps and auto_steps != steps else []),
    ])
    desc = mask(desc, case.get("secrets", []))
    fields = {
        "project": {"key": project},
        "issuetype": {"name": os.environ.get("JIRA_ISSUE_TYPE", "Bug")},
        "summary": f"[ARIS] {case['id']} {case['name']} 실패",
        "description": desc,
        "labels": ["aris", f"aris-{case['id']}"],
    }
    priority = PRIORITY.get(str(case.get("severity") or "").strip().lower())
    if priority:
        fields["priority"] = {"name": priority}
    return fields


def check(r):
    """실패 시 Jira가 보낸 이유(본문)까지 담아 예외를 낸다. raise_for_status는 상태 코드만 알려줘서 원인을 알 수 없다."""
    if not r.ok:
        raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:500]}")
    return r


# 연결을 재사용: TC마다 새 연결을 열면 윈도우에서 소켓 포트가 바닥나 등록이 실패함(WinError 10048)
JIRA = requests.Session()


def report_to_jira(fields, case_id, screenshot):
    """이슈 키를 돌려준다(이미 열린 이슈면 그 키, dry-run이면 None)."""
    env = {k: os.environ.get(k) for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_PROJECT")}
    if not all(env.values()):
        print(f"    [dry-run] 등록될 이슈: {fields['summary']}" + (f" (첨부: {screenshot})" if screenshot else ""))
        return None
    url, auth = env["JIRA_URL"].rstrip("/"), (env["JIRA_EMAIL"], env["JIRA_TOKEN"])

    # 같은 케이스로 열린 이슈가 있으면 중복 등록하지 않는다.
    jql = f'project="{env["JIRA_PROJECT"]}" AND labels="aris-{case_id}" AND statusCategory != Done'
    r = JIRA.post(f"{url}/rest/api/3/search/jql", auth=auth, timeout=10,
                      json={"jql": jql, "fields": ["key"], "maxResults": 1})
    check(r)
    if r.json().get("issues"):
        key = r.json()["issues"][0]["key"]
        print(f"    이미 열린 이슈 있음: {key} (등록 생략)")
        return key
    # v2 API는 description에 위키 마크업 문자열을 그대로 받는다(v3는 ADF 필요).
    r = JIRA.post(f"{url}/rest/api/2/issue", auth=auth, json={"fields": fields}, timeout=10)
    if not r.ok and "priority" in fields and "priority" in r.text:
        # 우선순위 칸이 없는 프로젝트(일부 팀 관리형)에서는 우선순위만 빼고 다시 등록
        print("    우선순위를 쓸 수 없는 프로젝트라 기본 우선순위로 등록")
        r = JIRA.post(f"{url}/rest/api/2/issue", auth=auth, timeout=10,
                      json={"fields": {k: v for k, v in fields.items() if k != "priority"}})
    check(r)
    key = r.json()["key"]
    if screenshot:
        try:  # 이슈는 이미 만들어졌으므로 첨부 실패가 등록 실패로 보이지 않게 따로 처리
            with open(screenshot, "rb") as f:
                check(JIRA.post(f"{url}/rest/api/2/issue/{key}/attachments", auth=auth, timeout=30,
                                    headers={"X-Atlassian-Token": "no-check"}, files={"file": f}))
        except (OSError, requests.RequestException) as e:
            print(f"    스크린샷 첨부 실패: {e}")
    print(f"    Jira 이슈 등록: {url}/browse/{key}")
    return key


# ---------------- 실행 ----------------
# ---------------- 환경 점검 ----------------
APPIUM_SERVER = "http://127.0.0.1:4723"


class EnvError(Exception):
    """테스트 환경이 준비되지 않아 실행을 시작하지 않음 (제품 결함 아님)."""


def appium_up(server):
    try:
        return requests.get(f"{server.rstrip('/')}/status", timeout=3).ok
    except requests.RequestException:
        return False


def find_adb():
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    local = Path(sdk, "platform-tools", "adb.exe" if os.name == "nt" else "adb") if sdk else None
    return shutil.which("adb") or (str(local) if local and local.exists() else None)


def check_env(server=APPIUM_SERVER):
    """실행 전 환경을 한 번에 확인한다. 항목마다 {name, state: ok/warn/fail, detail}."""
    out = []
    add = lambda name, state, detail: out.append({"name": name, "state": state, "detail": detail})

    env = {k: os.environ.get(k) for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_PROJECT")}
    if not all(env.values()):
        add("Jira", "warn", "jira.env가 없어 dry-run으로 동작해요. 실패해도 이슈는 등록되지 않아요")
    else:
        url, key = env["JIRA_URL"].rstrip("/"), env["JIRA_PROJECT"]
        try:
            r = JIRA.get(f"{url}/rest/api/3/project/{key}", auth=(env["JIRA_EMAIL"], env["JIRA_TOKEN"]), timeout=8)
            if r.ok:
                add("Jira", "ok", f"{url} · 프로젝트 {key} ({r.json().get('name', '')})")
            elif r.status_code in (401, 403):
                add("Jira", "fail", "로그인 실패: jira.env의 이메일과 API 토큰을 확인하세요")
            elif r.status_code == 404:
                add("Jira", "fail", f"프로젝트 {key}를 찾지 못함: jira.env의 프로젝트 키를 확인하세요")
            else:
                add("Jira", "fail", f"Jira 응답 {r.status_code}")
        except requests.RequestException as e:
            add("Jira", "fail", f"Jira 주소에 접속하지 못함 ({type(e).__name__}): jira.env의 주소와 인터넷 연결을 확인하세요")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            version = b.version
            b.close()
        add("브라우저 (웹 TC)", "ok", f"Chromium {version} 실행 가능")
    except Exception as e:
        add("브라우저 (웹 TC)", "fail", f"브라우저를 열지 못함: {str(e).splitlines()[0][:120]}")

    if appium_up(server):
        add("Appium (앱 TC)", "ok", f"{server} 응답함")
    else:
        add("Appium (앱 TC)", "warn", f"{server} 응답 없음 → 앱 TC를 돌리려면 터미널에서 appium을 켜 주세요")

    adb = find_adb()
    if not adb:
        add("Android 기기 (앱 TC)", "warn", "adb를 찾지 못함 → Android SDK의 platform-tools를 PATH에 넣어 주세요")
    else:
        try:
            lines = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=8).stdout.splitlines()[1:]
            rows = [l.split() for l in lines if l.strip()]
            ready = [r[0] for r in rows if len(r) > 1 and r[1] == "device"]
            other = [f"{r[0]}({r[1]})" for r in rows if len(r) > 1 and r[1] != "device"]
            if ready:
                add("Android 기기 (앱 TC)", "ok", f"연결됨: {', '.join(ready)}" + (f" · 확인 필요: {', '.join(other)}" if other else ""))
            else:
                add("Android 기기 (앱 TC)", "warn", "연결된 기기 없음" + (f" ({', '.join(other)})" if other else "") + " → 에뮬레이터를 켜고 adb devices로 확인하세요")
        except subprocess.TimeoutExpired:
            add("Android 기기 (앱 TC)", "warn", "adb가 응답하지 않음 → taskkill /F /IM adb.exe 후 adb devices를 다시 실행하세요")
    return out


def run_suite(suite, suite_dir, on_result=None):
    """케이스를 모두 실행하고 결과 목록을 돌려준다. YAML/엑셀 입력 모두 이 함수를 쓴다.
    on_result(result)는 케이스 하나가 끝날 때마다 호출된다(웹 화면 진행률용)."""
    ctx = {"suite_dir": suite_dir}
    server = suite.get("app", {}).get("server", APPIUM_SERVER)
    if any(c.get("type") == "app" for c in suite["cases"]) and not appium_up(server):
        # 꺼진 채로 돌리면 모든 앱 TC가 연결 실패로 끝나므로 시작 전에 멈춘다
        raise EnvError(f"Appium 서버({server})가 응답하지 않아 실행하지 않았어요. 터미널에서 appium을 켠 뒤 다시 실행하세요")
    project = os.environ.get("JIRA_PROJECT", "DEMO")
    results = []
    pause_after = suite.get("issue_pause", 5)  # 연속 실패가 이만큼 쌓이면 이후 이슈 등록을 보류 (0이면 끄기)
    streak = 0
    try:
        for case in suite["cases"]:
            kind = case.get("type", "api")
            if kind not in RUNNERS:  # 케이스 작성 오류는 제품 버그가 아니므로 Jira에 올리지 않음
                errors, evidence, shot = [f"알 수 없는 type: {kind} (api/web/app/script)"], "", None
            else:
                errors, evidence, shot = RUNNERS[kind](case, suite.get(kind, {}), ctx)
                errors = [mask(e, case.get("secrets", [])) for e in errors]
            print(f"{'PASS' if not errors else 'FAIL'}  [{kind}] {case['id']}  {case['name']}")
            for e in errors:
                print(f"    - {e}")
            issue = issue_error = None
            streak = streak + 1 if errors and kind in RUNNERS else 0
            if errors and "사이트가 응답을 거부함" in errors[0]:  # 차단·서버 오류는 제품 결함이 아니라 환경 문제
                issue_error = "사이트 접속 거부(차단·서버 오류)라 이슈 등록 안 함. 잠시 뒤 다시 실행하세요"
                print(f"    {issue_error}")
            elif errors and errors[0].startswith("Appium 연결 실패"):  # 앱을 열지도 못함: 테스트 환경 문제
                issue_error = "Appium·에뮬레이터 연결 실패라 이슈 등록 안 함. 서버를 켠 뒤 다시 실행하세요"
                print(f"    {issue_error}")
            elif errors and kind in RUNNERS and pause_after and streak > pause_after:
                # 사이트 차단·서버 장애·로그인 만료면 모든 TC가 줄줄이 실패해 가짜 이슈가 쏟아짐
                issue_error = f"연속 {streak}건 실패로 이슈 자동 등록 보류 (사이트 차단·환경 문제 가능성, 원인 확인 후 다시 실행)"
                print(f"    {issue_error}")
            elif errors and kind in RUNNERS:
                try:
                    issue = report_to_jira(issue_fields(case, suite.get(kind, {}), errors, evidence, project),
                                   case["id"], shot)
                except requests.RequestException as e:
                    issue_error = f"Jira 등록 실패: {e}"
                    print(f"    {issue_error}")
            results.append({"id": case["id"], "type": kind, "name": case["name"],
                            "passed": not errors, "errors": errors, "screenshot": shot, "issue": issue,
                            "issue_error": issue_error, "step_count": len(case.get("steps", []))})
            if on_result:
                on_result(results[-1])
    finally:
        if "browser" in ctx:
            ctx["browser"].close()
            ctx["pw"].stop()
    return results


def main(path):
    with open(path, encoding="utf-8") as f:
        suite = yaml.safe_load(f)
    results = run_suite(suite, os.path.dirname(os.path.abspath(path)))
    passed = sum(r["passed"] for r in results)
    print(f"\n결과: {passed}/{len(results)} 통과")
    with open("results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    return 0 if passed == len(results) else 1  # CI에서 실패를 감지하도록 종료 코드 반환


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "testcases.yaml"))
