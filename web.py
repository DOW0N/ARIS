"""웹 화면: 엑셀 TC 업로드 → 실행 → 결과 대시보드.

실행: python web.py  →  브라우저에서 http://127.0.0.1:8000
"""
import json
import os
import queue
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import aris
import excel_tc

ROOT = aris.APP_DIR  # 사용자 폴더: jira.env, scripts/, 예전 runs/
RES = Path(getattr(sys, "_MEIPASS", ROOT))  # 화면·템플릿 (exe 안에 묶인 파일)
# 실행 기록은 프로그램 폴더 밖(사용자 폴더)에 둔다: 새 버전을 다른 폴더에 풀어도 이력이 그대로 보이게
RUNS = Path(os.environ.get("ARIS_RUNS") or Path.home() / "ARIS_runs")
RUNS.mkdir(parents=True, exist_ok=True)
# shortcut: 검토용 업로드는 지우지 않고 쌓인다. 많아지면 오래된 것부터 정리하는 기능 추가
REVIEWS = RUNS / "_review"  # '_'가 들어가 실행 ID(영문·숫자)와 겹치지 않음
MAX_UPLOAD_MB = 10

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
jobs = queue.Queue()
state_lock = threading.RLock()


def run_dir(run_id):
    # run_id는 서버가 만든 hex만 허용 (경로 조작 방지)
    if not run_id.isalnum():
        abort(404)
    d = RUNS / run_id
    if not (d / "run.json").is_file():
        abort(404)
    return d


def review_dir(review_id):
    if not review_id.isalnum():
        abort(404)
    d = REVIEWS / review_id
    if not (d / "input.xlsx").is_file():
        abort(404)
    return d


def load(d):
    # 화면 요청은 실행 스레드와 동시에 들어온다. 읽기도 잠금 안에서 해야
    # 윈도우에서 '읽는 중인 파일을 교체'하다 PermissionError가 나지 않는다 (RLock이라 update 안에서 다시 불러도 됨)
    with state_lock:
        return json.loads((d / "run.json").read_text(encoding="utf-8"))


def save(d, data):
    tmp = d / "run.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(d / "run.json")  # 화면이 쓰다 만 파일을 읽지 않도록 원자적 교체


def update(d, **fields):
    with state_lock:
        data = load(d)
        data.update(fields)
        save(d, data)


def worker():
    """실행은 한 번에 하나씩. aris.ARTIFACTS가 모듈 전역이고 브라우저·기기도 공유 자원이라서."""
    while True:
        d = jobs.get()
        try:
            aris.ARTIFACTS = d / "artifacts"
            update(d, status="running", started=datetime.now().isoformat(timespec="seconds"))

            def on_parsed(cases, invalid, skipped):
                update(d, total=len(cases), invalid=invalid, skipped=skipped,
                       cases=[{"id": c["id"], "type": c["type"], "name": c["name"]} for c in cases])

            def on_result(res):
                if res["screenshot"]:
                    res = {**res, "screenshot": Path(res["screenshot"]).name}
                with state_lock:
                    data = load(d)
                    data["results"].append(res)
                    save(d, data)

            _, _, out = excel_tc.run_file(str(d / "input.xlsx"), suite_dir=str(ROOT),
                                          on_result=on_result, on_parsed=on_parsed)
            update(d, status="done", output=out.name, finished=datetime.now().isoformat(timespec="seconds"))
        except Exception as e:  # 엑셀 형식 오류 등: 화면에 이유를 보여준다
            update(d, status="error", error=f"{type(e).__name__}: {e}",
                   finished=datetime.now().isoformat(timespec="seconds"))
        finally:
            jobs.task_done()


@app.get("/")
def index():
    return send_from_directory(RES / "static", "index.html")


@app.get("/template")
def template():
    return send_file(RES / "TC_template.xlsx", as_attachment=True, download_name="TC_템플릿.xlsx")


@app.get("/api/info")
def info():
    jira = all(os.environ.get(k) for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_PROJECT"))
    return {"jira": jira, "jira_url": os.environ.get("JIRA_URL", "").rstrip("/") if jira else None}


def new_run(write_input, filename):
    run_id = uuid.uuid4().hex[:12]
    d = RUNS / run_id
    d.mkdir()
    write_input(d / "input.xlsx")  # 사용자 파일명은 저장 경로에 쓰지 않는다
    save(d, {"id": run_id, "filename": filename, "status": "queued",
             "created": datetime.now().isoformat(timespec="seconds"),
             "total": None, "cases": [], "results": [], "invalid": []})
    jobs.put(d)
    return {"id": run_id}, 201


def uploaded_xlsx():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        abort(400, ".xlsx 파일을 올려주세요")
    return f


@app.post("/api/runs")
def create_run():
    f = uploaded_xlsx()
    return new_run(f.save, f.filename)


# ---- TC 검토: 실행하지 않고 읽은 결과와 변환 제안을 보여주고, 확정한 변환을 사본에 써 넣는다 ----
def analyze(d, path):
    try:
        return {"id": d.name, "filename": json.loads((d / "meta.json").read_text(encoding="utf-8"))["filename"],
                "applied": (d / "converted.xlsx").is_file(), **excel_tc.review(str(path))}
    except ValueError as e:  # 제목 줄을 못 찾는 등 양식 문제
        return {"error": str(e)}, 400


@app.post("/api/reviews")
def create_review():
    f = uploaded_xlsx()
    d = REVIEWS / uuid.uuid4().hex[:12]
    d.mkdir(parents=True)
    f.save(d / "input.xlsx")
    (d / "meta.json").write_text(json.dumps({"filename": f.filename}, ensure_ascii=False), encoding="utf-8")
    return analyze(d, d / "input.xlsx")


@app.post("/api/reviews/<review_id>/apply")
def apply_review(review_id):
    d = review_dir(review_id)
    edits = (request.get_json(silent=True) or {}).get("edits")
    if not isinstance(edits, dict) or not all(str(k).isdigit() and isinstance(v, dict) for k, v in edits.items()):
        return {"error": "적용할 변환 형식이 올바르지 않아요"}, 400
    empty = [k for k, v in edits.items() if not str(v.get("expect") or "").strip()]
    if empty:  # 판정이 없으면 이동만 하고 항상 통과로 나와 가짜 통과가 됨
        return {"error": f"행 {', '.join(map(str, empty))}: 자동화 기대 결과가 비어 있어 적용하지 않았어요. "
                         "확인·보임·주소 같은 판정을 한 줄 이상 넣어 주세요"}, 400
    # 이미 적용한 행이 사라지지 않게, 두 번째부터는 앞서 만든 사본 위에 이어서 쓴다
    src = d / "converted.xlsx" if (d / "converted.xlsx").is_file() else d / "input.xlsx"
    excel_tc.apply_auto(str(src), edits, str(d / "converted.xlsx"))
    return analyze(d, d / "converted.xlsx")


@app.get("/reviews/<review_id>/file")
def review_file(review_id):
    d = review_dir(review_id)
    if not (d / "converted.xlsx").is_file():
        abort(404)
    name = Path(json.loads((d / "meta.json").read_text(encoding="utf-8"))["filename"]).stem
    return send_file(d / "converted.xlsx", as_attachment=True, download_name=f"{name}_검토.xlsx")


@app.post("/api/reviews/<review_id>/run")
def run_review(review_id):
    d = review_dir(review_id)
    src = d / "converted.xlsx" if (d / "converted.xlsx").is_file() else d / "input.xlsx"
    filename = json.loads((d / "meta.json").read_text(encoding="utf-8"))["filename"]
    return new_run(lambda dst: shutil.copyfile(src, dst), filename)


@app.get("/api/runs")
def list_runs():
    runs = []
    for d in RUNS.iterdir():
        if (d / "run.json").exists():
            r = load(d)
            res, inv = r["results"], r["invalid"]
            runs.append({k: r.get(k) for k in ("id", "filename", "status", "created", "started", "finished", "total")} |
                        {"passed": sum(x["passed"] for x in res), "done": len(res),
                         "issues": sum(bool(x.get("issue")) for x in res),
                         "invalid": len(inv), "convert": sum(bool(b.get("convert")) for b in inv),
                         "skipped": len(r.get("skipped", []))})
    return jsonify(sorted(runs, key=lambda r: r["created"], reverse=True))


@app.get("/api/runs/<run_id>")
def get_run(run_id):
    return load(run_dir(run_id))


@app.delete("/api/runs/<run_id>")
def delete_run(run_id):
    d = run_dir(run_id)
    with state_lock:
        if load(d)["status"] in ("queued", "running"):
            return {"error": "실행 중인 기록은 지울 수 없어요. 끝난 뒤에 지워 주세요"}, 409
        shutil.rmtree(d)
    return {"deleted": run_id}


@app.get("/runs/<run_id>/output")
def download_output(run_id):
    d = run_dir(run_id)
    name = load(d).get("output")
    if not name:
        abort(404)
    return send_file(d / name, as_attachment=True)


@app.get("/runs/<run_id>/shot/<name>")
def screenshot(run_id, name):
    return send_from_directory(run_dir(run_id) / "artifacts", name)  # send_from_directory가 경로 조작을 막음


@app.errorhandler(400)
def bad_request(e):
    return {"error": e.description if isinstance(e.description, str) else "요청이 올바르지 않아요"}, 400


@app.errorhandler(413)
def too_large(_):
    return {"error": f"파일은 {MAX_UPLOAD_MB}MB 이하만 올릴 수 있어요"}, 413


def migrate_old_runs():
    """예전 버전처럼 프로그램 폴더 안 runs/에 있던 기록을 새 위치로 복사한다(원본은 그대로 둠)."""
    old = ROOT / "runs"
    if not old.is_dir() or old.resolve() == RUNS.resolve():
        return
    for d in old.iterdir():
        if (d / "run.json").exists() and not (RUNS / d.name).exists():
            shutil.copytree(d, RUNS / d.name)


def recover():
    """서버가 실행 중에 꺼졌던 실행은 중단 처리한다."""
    for d in RUNS.iterdir():
        if (d / "run.json").exists() and load(d)["status"] in ("queued", "running"):
            update(d, status="error", error="서버 재시작으로 중단됨")


if __name__ == "__main__":
    migrate_old_runs()
    recover()
    print(f" * 실행 기록 위치: {RUNS}")
    port = int(os.environ.get("PORT", 8000))
    if getattr(sys, "frozen", False):  # exe는 더블클릭으로 켜므로 화면을 자동으로 연다
        import webbrowser
        threading.Timer(1.5, webbrowser.open, [f"http://127.0.0.1:{port}"]).start()
        print(" * 창을 닫으면 ARIS가 종료돼요")
    threading.Thread(target=worker, daemon=True).start()
    # 기본은 내 PC에서만 접속. 팀에서 같이 쓰려면 HOST=0.0.0.0 (스크립트 TC가 서버에서 실행되므로 내부망에서만)
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, threaded=True)
