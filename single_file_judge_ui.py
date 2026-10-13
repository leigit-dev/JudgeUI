#!/usr/bin/env python3
"""
single_file_judge_ui.py

A single-file local judge UI and judge runner.
- No sandboxing (for local/personal use only)
- Supports C, C++, Python
- Uses current directory and creates run0..runN directories
- Serves a small HTML/JS UI and provides REST API endpoints

Usage:
    python single_file_judge_ui.py
Then open http://127.0.0.1:5000

"""
from flask import Flask, jsonify, request, send_file, Response
import threading
import uuid
import time
import shutil
import os
import subprocess
from pathlib import Path
import json
import sys

# Configuration
DEFAULT_MAX_RUNNING = 3
CWD = Path.cwd()
RUN_PREFIX = "run"
LOG_PATH = CWD / "judgeui.log"
DATA_DIR = CWD / "data"

LANG_C = 0
LANG_CPP = 1
LANG_PY = 4

app = Flask(__name__)

# In-memory job store
JOB_STORE = {}
STORE_LOCK = threading.Lock()

# Utilities
def append_log(line: str):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except Exception:
        pass

def ensure_run_dirs(max_running: int):
    for i in range(max_running):
        d = CWD / f"{RUN_PREFIX}{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "compile").mkdir(parents=True, exist_ok=True)
        (d / "python").mkdir(parents=True, exist_ok=True)
        (d / "standalone").mkdir(parents=True, exist_ok=True)

def select_slot(max_running: int) -> int:
    ensure_run_dirs(max_running)
    for i in range(max_running):
        lockfile = CWD / f"{RUN_PREFIX}{i}" / ".lock"
        if not lockfile.exists():
            try:
                lockfile.write_text(str(os.getpid()))
                return i
            except Exception:
                continue
    return -1

def release_slot(slot: int):
    try:
        lockfile = CWD / f"{RUN_PREFIX}{slot}" / ".lock"
        if lockfile.exists():
            lockfile.unlink()
    except Exception:
        pass

# Judge core functions

def write_source(workdir: Path, solution: dict):
    lang = solution.get("language")
    code = solution.get("code", "")
    if lang == LANG_C:
        src = workdir / "compile" / "Main.c"
    elif lang == LANG_CPP:
        src = workdir / "compile" / "Main.cc"
    elif lang == LANG_PY:
        src = workdir / "python" / "Main.py"
    else:
        raise ValueError("Unsupported language")
    with src.open("w", encoding="utf-8") as fh:
        fh.write(code)
    return src

def compile_solution(workdir: Path, solution: dict, timeout_sec: int = 60):
    lang = solution.get("language")
    ce = ""
    if lang == LANG_PY:
        return True, ce
    elif lang == LANG_C:
        cmd = ["gcc", "Main.c", "-o", "Main", "-O2", "-std=c99"]
        cwd = workdir / "compile"
    elif lang == LANG_CPP:
        cmd = ["g++", "Main.cc", "-o", "Main", "-O2", "-std=c++14"]
        cwd = workdir / "compile"
    else:
        return False, "Unsupported language"
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_sec)
        if proc.returncode != 0:
            ce = proc.stderr or proc.stdout
            return False, ce
        else:
            bin_src = cwd / "Main"
            if os.name == 'nt' and not bin_src.exists():
                # On Windows, mingw produces Main.exe
                bin_src = cwd / "Main.exe"
            bin_dst = workdir / (bin_src.name if bin_src.exists() else "Main")
            if bin_src.exists():
                shutil.move(str(bin_src), str(bin_dst))
            return True, ce
    except subprocess.TimeoutExpired:
        return False, "Compile timeout"
    except FileNotFoundError as e:
        return False, f"Compiler not found: {e}"

def compare_files(file1: Path, file2: Path):
    try:
        a = file1.read_text(encoding="utf-8", errors="ignore").splitlines()
        b = file2.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return "RE", 0
    if a == b:
        return "AC", 100
    def norm(lines):
        return [" ".join(l.split()) for l in lines if l.strip() != ""]
    if norm(a) == norm(b):
        return "PE", 0
    return "WA", 0

def run_one_test(workdir: Path, solution: dict, testcase: str, data_dir: Path, time_limit_ms: int):
    result = {"name": testcase, "verdict": "", "score": 0, "time_used": 0, "memory_used": 0}
    language = solution.get("language")
    in_file = data_dir / f"{testcase}.in"
    ans_file = data_dir / f"{testcase}.ans"
    user_out = workdir / "user.out"
    if not in_file.exists():
        result.update({"verdict": "RE", "score": 0})
        return result
    shutil.copy2(str(in_file), str(workdir / "data.in"))
    start = time.perf_counter()
    try:
        if language == LANG_PY:
            exec_cwd = workdir / "python"
            src = exec_cwd / "Main.py"
            cmd = [sys.executable, str(src)]
            with (exec_cwd / "data.in").open("rb") as inf, (user_out).open("wb") as outf:
                proc = subprocess.run(cmd, stdin=inf, stdout=outf, stderr=subprocess.PIPE, timeout=max(1, time_limit_ms // 1000))
        else:
            exec_cwd = workdir
            exe = exec_cwd / "Main"
            if os.name == 'nt' and not exe.exists():
                exe = exec_cwd / "Main.exe"
            if not exe.exists():
                result.update({"verdict": "RE", "score": 0})
                return result
            with (workdir / "data.in").open("rb") as inf, (user_out).open("wb") as outf:
                proc = subprocess.run([str(exe)], stdin=inf, stdout=outf, stderr=subprocess.PIPE, timeout=max(1, time_limit_ms // 1000))
        end = time.perf_counter()
        result["time_used"] = int((end - start) * 1000)
        verdict, score = compare_files(ans_file, user_out)
        result.update({"verdict": verdict, "score": score})
    except subprocess.TimeoutExpired:
        result.update({"verdict": "TLE", "score": 0, "time_used": time_limit_ms})
    except Exception:
        result.update({"verdict": "RE", "score": 0})
    finally:
        try:
            p = workdir / "data.in"
            if p.exists():
                p.unlink()
        except Exception:
            pass
    return result

def run_job(job_id: str, solution: dict, problem: dict, max_running: int = DEFAULT_MAX_RUNNING):
    JOB_STORE[job_id]["status"] = "running"
    slot = select_slot(max_running)
    if slot == -1:
        JOB_STORE[job_id]["status"] = "queued"
        append_log(f"job {job_id} queued (no free slots)")
        return
    workdir = CWD / f"{RUN_PREFIX}{slot}"
    JOB_STORE[job_id]["slot"] = slot
    JOB_STORE[job_id]["workdir"] = str(workdir)
    append_log(f"job {job_id} running in slot {slot}")
    try:
        write_source(workdir, solution)
        ok, ce = compile_solution(workdir, solution)
        if not ok:
            JOB_STORE[job_id]["status"] = "finished"
            JOB_STORE[job_id]["result"] = {"status": "CE", "ce": ce}
            append_log(f"job {job_id} compile error")
            return
        data_dir = Path(problem.get("data_dir", str(DATA_DIR / str(problem.get('id', 0)))))
        testcases = problem.get("testcases", [])
        if not testcases:
            if data_dir.exists():
                testcases = [p.stem for p in sorted(data_dir.glob("*.in"))]
        solution_result = {"id": solution.get("id"), "score": 0, "testcases": []}
        time_limit_ms = problem.get("timelimit", 1000)
        for tc in testcases:
            tcres = run_one_test(workdir, solution, tc, data_dir, time_limit_ms)
            solution_result["testcases"].append(tcres)
            solution_result["score"] += tcres.get("score", 0)
        JOB_STORE[job_id]["status"] = "finished"
        JOB_STORE[job_id]["result"] = solution_result
        append_log(f"job {job_id} finished")
    finally:
        release_slot(slot)

# API & UI
HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Single-file Judge UI</title>
  <style>
    body{font-family:Arial;background:#0b1220;color:#e5eefb;margin:0}
    .wrap{display:flex}
    .sidebar{width:220px;background:#111827;padding:20px}
    .content{flex:1;padding:20px}
    button{padding:8px 12px;border-radius:6px;border:none;background:#2563eb;color:white}
    pre{background:#020817;padding:12px;border-radius:6px}
    textarea{width:100%;min-height:200px}
    table{width:100%;border-collapse:collapse}
    th,td{padding:8px;border-bottom:1px solid #223}
  </style>
</head>
<body>
<div class="wrap">
  <div class="sidebar">
    <h2>JudgeUI</h2>
    <div><button onclick="show('dashboard')">Dashboard</button></div>
    <div style="margin-top:8px"><button onclick="show('run')">Run</button></div>
    <div style="margin-top:8px"><button onclick="show('jobs')">Jobs</button></div>
    <div style="margin-top:8px"><button onclick="show('logs')">Logs</button></div>
  </div>
  <div class="content">
    <div id="dashboard" class="panel">
      <h3>Dashboard</h3>
      <div>Status: <span id="state">-</span></div>
      <div>Jobs: <span id="jobcount">0</span></div>
      <div style="margin-top:12px"><button onclick="refresh()">Refresh</button></div>
    </div>

    <div id="run" class="panel" style="display:none">
      <h3>Run Solution</h3>
      <label>Solution ID: <input id="solid" value="123"/></label>
      <br/>
      <label>Language: <select id="lang"><option value="1">C++</option><option value="0">C</option><option value="4">Python</option></select></label>
      <h4>Code</h4>
      <textarea id="code">#include &lt;iostream&gt;\nint main(){int a,b; if(!(std::cin>>a>>b)) return 0; std::cout<<a+b<<"\n";}</textarea>
      <h4>Problem data dir (relative)</h4>
      <input id="probdir" value="data/101" style="width:60%"/>
      <div style="margin-top:8px"><button onclick="startRun()">Start</button></div>
      <div id="run-result" style="margin-top:12px"></div>
    </div>

    <div id="jobs" class="panel" style="display:none">
      <h3>Jobs</h3>
      <table><thead><tr><th>ID</th><th>Status</th><th>Slot</th><th>Action</th></tr></thead><tbody id="job-table"></tbody></table>
    </div>

    <div id="logs" class="panel" style="display:none">
      <h3>Logs</h3>
      <pre id="logpre">(no logs)</pre>
      <div style="margin-top:8px"><button onclick="refreshLogs()">Refresh</button></div>
    </div>
  </div>
</div>
<script>
function show(name){document.querySelectorAll('.panel').forEach(p=>p.style.display='none');document.getElementById(name).style.display='block'}
async function refresh(){
  const s=await fetch('/api/status').then(r=>r.json());
  document.getElementById('state').textContent=s.state;
  document.getElementById('jobcount').textContent=s.jobs;
}
async function startRun(){
  const sol=document.getElementById('solid').value;
  const lang=Number(document.getElementById('lang').value);
  const code=document.getElementById('code').value;
  const probdir=document.getElementById('probdir').value;
  const payload={language:lang,code:code,user:'local',}
  const res=await fetch(`/api/judge/${sol}/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,problem:{id:1,timelimit:1000,data_dir:probdir}})}).then(r=>r.json());
  document.getElementById('run-result').textContent='Started job '+res.job_id;
  refreshJobs();
}
async function refreshJobs(){
  const res=await fetch('/api/pending').then(r=>r.json());
  const rows=res.items.map(it=>`<tr><td>${it.job_id.slice(0,8)}</td><td>${it.status}</td><td>${it.slot||'-'}</td><td><button onclick="viewJob('`+it.job_id+`')">View</button></td></tr>`).join('');
  document.getElementById('job-table').innerHTML=rows||'<tr><td colspan=4>No jobs</td></tr>';
}
async function viewJob(id){
  const r=await fetch('/api/job/'+id).then(r=>r.json());
  const job=r.job||{};
  alert(JSON.stringify(job,null,2));
}
async function refreshLogs(){
  const r=await fetch('/api/logs').then(r=>r.json());
  document.getElementById('logpre').textContent=(r.lines||[]).join('\n');
}
setInterval(()=>{refresh();refreshJobs()},3000);
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return HTML_TEMPLATE

@app.route('/api/status')
def api_status():
    with STORE_LOCK:
        jobs = len(JOB_STORE)
    state = 'running' if any(j.get('status')=='running' for j in JOB_STORE.values()) else 'idle'
    return jsonify({"ok":True,"state":state,"jobs":jobs})

@app.route('/api/logs')
def api_logs():
    tail = int(request.args.get('tail', '200'))
    if not LOG_PATH.exists():
        return jsonify({"ok":True,"lines":[]})
    lines = LOG_PATH.read_text(encoding='utf-8',errors='ignore').splitlines()[-tail:]
    return jsonify({"ok":True,"lines":lines})

@app.route('/api/pending')
def api_pending():
    items = []
    with STORE_LOCK:
        for job_id, job in JOB_STORE.items():
            items.append({
                'job_id': job_id,
                'status': job.get('status'),
                'slot': job.get('slot'),
                'problem_id': job.get('problem',{}).get('id'),
            })
    return jsonify({"ok":True,"items":items})

@app.route('/api/judge/<int:solution_id>/start', methods=['POST'])
def api_judge_start(solution_id):
    payload = request.get_json(silent=True) or {}
    language = payload.get('language', 1)
    code = payload.get('code', '')
    user = payload.get('user', 'local')
    problem = payload.get('problem', {})
    solution = { 'id': solution_id, 'language': language, 'code': code, 'user': user }
    job_id = str(uuid.uuid4())
    with STORE_LOCK:
        JOB_STORE[job_id] = {'status':'pending','solution':solution,'problem':problem}
    t = threading.Thread(target=run_job, args=(job_id,solution,problem,DEFAULT_MAX_RUNNING), daemon=True)
    t.start()
    append_log(f"Started job {job_id} for solution {solution_id}")
    return jsonify({"ok":True,"job_id":job_id})

@app.route('/api/job/<job_id>')
def api_job(job_id):
    job = JOB_STORE.get(job_id)
    if not job:
        return jsonify({"ok":False,"message":"not found"}),404
    return jsonify({"ok":True,"job":job})

@app.route('/api/jobs')
def api_jobs():
    return jsonify({"ok":True,"jobs":JOB_STORE})

@app.route('/api/problem/<int:pid>/files')
def api_problem_files(pid):
    base = DATA_DIR / str(pid)
    if not base.exists():
        return jsonify({"ok":True,"files":[]})
    files = [p.name for p in sorted(base.iterdir())]
    return jsonify({"ok":True,"files":files})

@app.route('/api/problem/<int:pid>/file/<path:fn>')
def api_problem_file(pid, fn):
    base = DATA_DIR / str(pid)
    f = base / fn
    if not f.exists():
        return jsonify({"ok":False}),404
    return send_file(str(f), as_attachment=True)

if __name__ == '__main__':
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    append_log('Judge UI started')
    app.run(host='127.0.0.1', port=5000, debug=False)
