#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review_bridge.py - review.html 의 '에이전트에 바로 반영' 버튼용 로컬 다리(bridge) 서버. (선택)

브라우저(file://)는 CLI 를 실행할 수 없으므로, 이 서버를 켜 두면 버튼이 localhost 로
첨삭 요청(.md)을 보내고 → 서버가 revisions/<rev>.md 로 저장 → 헤드리스 `claude -p` 가
prompts/apply_revisions.md 절차대로 원고를 고친 뒤 → 서버가 build_review.py 로 다시 빌드한다.
브릿지 없이도 킷은 동작한다(내보낸 .md 를 Claude Code 에 "반영해줘"라고 주면 된다).

구조는 상위 킷의 tools/qa_bridge.py 를 따른다: 접수(202 + job_id)와 조회(GET /job) 분리,
프로젝트당 동시 작업 1개, 끊긴 연결은 트레이스백 없이 한 줄 로그.

엔드포인트
---------
- GET  /ping            → {ok, version, project_id, title}
- POST /save  {name, md, project_id}  → revisions/<name>.md 저장
- POST /apply {name, md, project_id}  → 저장 + 반영 job 시작 → 202 {job_id}
- GET  /job?id=<id>     → {status:"running", elapsed, log_tail} | 완료 결과

요청 본문은 JSON 문자열이면 Content-Type 과 무관하게 받는다(브라우저는 preflight 를 피하려고 text/plain 으로 보낸다).

사용법
------
    python review_bridge.py                       # 포트 = review.json 의 bridge_port (기본 8788)
    python review_bridge.py path/to/project --port 9001
    REVIEW_BRIDGE_MODEL=sonnet python review_bridge.py     # 헤드리스 모델 지정
    REVIEW_BRIDGE_SKIP_PERMS=1 python review_bridge.py     # 권한 우회 (편집이 막힐 때만)
    REVIEW_BRIDGE_CLAUDE=/path/to/claude python review_bridge.py
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_review as br  # noqa: E402

VERSION = "1.0"
NAME_RE = re.compile(r"^rev_[A-Za-z0-9_.-]{1,80}$")
JOB_TTL = 30 * 60
LOG_TAIL = 14

PROJECT = HERE
CFG = {}
PROJECT_ID = ""
_jobs = {}
_guard = threading.Lock()

PROMPT_TEMPLATE = """너는 원고 첨삭 반영 에이전트다. 사람이 review.html 에서 남긴 수정 요청을 원고에 반영한다.

- 절차 정본: {prompt_path} — 먼저 이 파일 전체를 읽고 그대로 따른다.
- 이번 요청 파일: {rev_path}
- 프로젝트 폴더(review.json 위치): {project}
- 원고 파일: {sources}
- 반영 후 다시 빌드: python "{build}" "{project}"

규칙 요약 (정본이 우선):
1. 요청 파일을 끝까지 읽고, 항목마다 원문 블록/대상 구절로 원고 위치를 확인한 뒤 요청 범위만 고친다.
2. 질문(question) 유형은 원고를 고치지 않고 note 에 답한다.
3. 인용·참조·수식·수치는 요청이 없으면 건드리지 않는다. 근거 없는 내용(가짜 인용·결과)을 만들지 않는다.
4. revisions/{name}.applied.json 을 정본 스키마로 쓰고, revisions/CHANGELOG.md 에 한 줄 요약을 덧붙인다.
5. 빌드 명령을 실행한다. review.html · versions/ 는 직접 수정하지 않는다.
6. 마지막 줄에 다음 형식을 정확히 한 줄 출력한다:
REVISION_RESULT: {{"status":"ok","rev":"{name}","applied":0,"partial":0,"skipped":0,"answered":0}}
"""


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def rev_dir():
    d = PROJECT / "revisions"
    d.mkdir(exist_ok=True)
    return d


def save_md(name, md):
    if not NAME_RE.match(name or ""):
        raise ValueError(f"허용되지 않는 파일 이름: {name!r}")
    if not (md or "").strip():
        raise ValueError("내용이 비어 있습니다.")
    p = rev_dir() / f"{name}.md"
    p.write_text(md, encoding="utf-8")
    return p


def claude_args():
    exe = os.environ.get("REVIEW_BRIDGE_CLAUDE") or shutil.which("claude") or "claude"
    args = [exe, "-p", "--add-dir", str(PROJECT), "--add-dir", str(HERE)]
    for rel in CFG.get("sources", []):
        parent = (PROJECT / rel).resolve().parent
        if parent != PROJECT and PROJECT not in parent.parents:
            args += ["--add-dir", str(parent)]  # 원고가 프로젝트 밖(예: ../paper/main.tex)에 있을 때
    if os.environ.get("REVIEW_BRIDGE_SKIP_PERMS", "").strip().lower() in ("1", "true", "yes"):
        args += ["--dangerously-skip-permissions"]
    else:
        args += ["--permission-mode", "acceptEdits"]
    model = os.environ.get("REVIEW_BRIDGE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    args += ["--allowedTools", "Read", "Edit", "MultiEdit", "Write", "Glob", "Grep", "Bash"]
    return args


def parse_result(lines):
    rx = re.compile(r"REVISION_RESULT:\s*(\{.*\})\s*$")
    for line in reversed(lines):
        m = rx.search(line)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                continue
    return None


def run_apply(name, jlog):
    rev_path = rev_dir() / f"{name}.md"
    prompt = PROMPT_TEMPLATE.format(
        prompt_path=HERE / "prompts" / "apply_revisions.md",
        rev_path=rev_path, project=PROJECT, name=name,
        sources=", ".join(str((PROJECT / s).resolve()) for s in CFG.get("sources", [])),
        build=HERE / "build_review.py",
    )
    args = claude_args()
    jlog("[claude] spawn: " + " ".join(args[:6]) + " …")
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                cwd=str(PROJECT), env=env)
    except FileNotFoundError:
        return {"status": "error", "message": "claude CLI 를 찾을 수 없습니다 (REVIEW_BRIDGE_CLAUDE 로 경로 지정 가능)."}
    proc.stdin.write(prompt.encode("utf-8"))
    proc.stdin.close()
    lines = []
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        lines.append(line)
        jlog("[claude] " + line)
    proc.wait()
    result = parse_result(lines)

    # 에이전트가 빌드를 빠뜨려도 결과가 보이도록 한 번 더 빌드 (내용이 같으면 새 버전을 만들지 않음)
    jlog("[build] build_review.py")
    b = subprocess.run([sys.executable, str(HERE / "build_review.py"), str(PROJECT)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (b.stdout + b.stderr).strip().splitlines():
        jlog("[build] " + line)

    applied_json = rev_dir() / f"{name}.applied.json"
    if proc.returncode != 0:
        return {"status": "error", "message": f"claude 종료 코드 {proc.returncode}", "result": result}
    if not applied_json.is_file():
        return {"status": "error", "message": f"{applied_json.name} 이 만들어지지 않았습니다 — 로그를 확인하세요.", "result": result}
    # 카운트는 에이전트가 출력한 줄이 아니라 applied.json 의 items 에서 센다 (정본)
    try:
        data = json.loads(applied_json.read_text(encoding="utf-8"))
        counted = {"status": "ok", "rev": name, "applied": 0, "partial": 0, "skipped": 0, "answered": 0}
        for it in data.get("items", []):
            st = it.get("status", "")
            counted[st] = counted.get(st, 0) + 1
        result = counted
    except Exception as e:
        return {"status": "error", "message": f"{applied_json.name} 을 읽을 수 없습니다: {e}", "result": result}
    if b.returncode != 0:
        return {"status": "error", "message": "원고 반영은 끝났지만 빌드가 실패했습니다.", "result": result}
    return {"status": "ok", "message": "반영 완료", "result": result}


def start_job(name):
    with _guard:
        now = time.time()
        for k in [k for k, j in _jobs.items() if j["status"] != "running" and j["finished"] and now - j["finished"] > JOB_TTL]:
            _jobs.pop(k, None)
        for j in _jobs.values():
            if j["status"] == "running":
                return j, False
        job = {"id": uuid.uuid4().hex[:16], "name": name, "status": "running", "created": now,
               "finished": None, "result": None, "log": []}
        _jobs[job["id"]] = job

    def jlog(msg):
        log(msg)
        job["log"].append(msg)
        if len(job["log"]) > 400:
            del job["log"][:200]

    def runner():
        try:
            res = run_apply(name, jlog)
        except Exception as e:
            traceback.print_exc()
            res = {"status": "error", "message": f"서버 예외: {e}"}
        job["result"] = res
        job["status"] = res.get("status", "error")
        job["finished"] = time.time()
        log(f"[bridge] job {job['id']} → {job['status']} · {res.get('message', '')}")

    threading.Thread(target=runner, daemon=True).start()
    return job, True


def job_view(job):
    tail = [re.sub(r"^\[claude\] ", "", l) for l in job["log"][-LOG_TAIL:]]
    if job["status"] == "running":
        return {"status": "running", "job_id": job["id"], "name": job["name"],
                "elapsed": time.time() - job["created"], "log_tail": tail}
    out = dict(job["result"] or {})
    out.update(job_id=job["id"], name=job["name"], log_tail=tail)
    return out


DISCONNECT = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class Handler(BaseHTTPRequestHandler):
    server_version = "review_bridge/" + VERSION

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except DISCONNECT + (OSError,):
            self.close_connection = True

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except DISCONNECT + (OSError,):
            self.close_connection = True

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if path in ("", "/ping"):
            self._json(200, {"ok": True, "version": VERSION, "project_id": PROJECT_ID, "title": CFG.get("title", "")})
        elif path == "/job":
            jid = (parse_qs(u.query).get("id") or [""])[0]
            with _guard:
                job = _jobs.get(jid)
            if not job:
                self._json(404, {"status": "unknown", "message": "그 작업을 찾을 수 없습니다 (브릿지 재시작 또는 보관 만료)."})
            else:
                self._json(200, job_view(job))
        else:
            self._json(404, {"ok": False, "message": "unknown path"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if path not in ("/save", "/apply"):
            self._json(404, {"status": "error", "message": "unknown path"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n).decode("utf-8") if n else "{}")
        except Exception as e:
            self._json(400, {"status": "error", "message": f"잘못된 요청: {e}"})
            return
        pid = data.get("project_id")
        if pid and pid != PROJECT_ID:
            self._json(409, {"status": "error", "message": f"이 브릿지는 다른 프로젝트({CFG.get('title')})를 서비스 중입니다."})
            return
        name, md = data.get("name", ""), data.get("md", "")
        try:
            p = save_md(name, md)
        except Exception as e:
            self._json(400, {"status": "error", "message": str(e)})
            return
        log(f"[bridge] saved {p.relative_to(PROJECT)} ({len(md)} chars)")
        if path == "/save":
            self._json(200, {"status": "ok", "path": str(p.relative_to(PROJECT))})
            return
        job, created = start_job(name)
        if not created:
            self._json(200, {"status": "busy", "job_id": job["id"], "name": job["name"],
                             "message": f"이미 진행 중인 반영 작업이 있습니다 ({job['name']}). 요청 파일은 저장했습니다."})
            return
        self._json(202, {"status": "accepted", "job_id": job["id"], "name": name})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], DISCONNECT):
            return
        super().handle_error(request, client_address)


def main():
    global PROJECT, CFG, PROJECT_ID
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="첨삭 반영 로컬 브릿지")
    ap.add_argument("project", nargs="?", default=str(HERE))
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    PROJECT = Path(args.project).resolve()
    CFG = br.load_config(PROJECT)
    PROJECT_ID = br.doc_id(CFG)
    port = args.port or int(os.environ.get("REVIEW_BRIDGE_PORT") or CFG.get("bridge_port") or 8788)
    try:
        srv = Server((args.host, port), Handler)
    except OSError as e:
        print(f"포트 {port} 를 열 수 없습니다 (이미 실행 중일 수 있습니다): {e}")
        return
    print(f"review_bridge {VERSION} - http://{args.host}:{port}")
    print(f"  project : {PROJECT}  ({CFG.get('title')}, id={PROJECT_ID})")
    print(f"  claude  : {os.environ.get('REVIEW_BRIDGE_CLAUDE') or shutil.which('claude') or '(PATH 에 없음)'}")
    print(f"  model   : {os.environ.get('REVIEW_BRIDGE_MODEL') or '(inherit)'}")
    print("  review.html 의 '에이전트에 바로 반영' 버튼이 이 서버로 요청합니다. Ctrl+C 로 종료.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
        srv.shutdown()


if __name__ == "__main__":
    main()
