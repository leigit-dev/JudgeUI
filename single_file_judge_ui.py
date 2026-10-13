#!/usr/bin/env python3
"""
single_file_judge_ui.py - Enhanced for competition use

Features added:
- Frontend accepts all sample inputs/answers from the browser (multiple testcases)
- Detects TLE, MLE (optional via psutil), RE, CE, PE (presentation error)
- Default equal-weight scoring per testcase (total 100)
- Dark / Light mode toggle
- Real-time job progress (frontend polls /api/job/<id>)

Security: still no sandbox. DO NOT run untrusted code on public machines.

Run:
  python -m venv .venv
  . .venv/bin/activate     # or .venv\Scripts\Activate.ps1 on Windows PowerShell
  pip install -r requirements.txt
  python single_file_judge_ui.py
  open http://127.0.0.1:5000

"""
from flask import Flask, jsonify, request, send_file
import threading
import uuid
import time
import shutil
import os
import subprocess
from pathlib import Path
import json
import sys

# Optional dependency for memory measurement
try:
    import psutil
    HAS_PSUTIL = True
except Exception:
    HAS_PSUTIL = False

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
            if bin_src.exists():
                bin_dst = workdir / bin_src.name
                shutil.move(str(bin_src), str(bin_dst))
            return True, ce
    except subprocess.TimeoutExpired:
        return False, "Compile timeout"
    except FileNotFoundError as e:
        return False, f"Compiler not found: {e}"


def normalize_whitespace(text: str) -> str:
    # Normalize whitespace: collapse runs of whitespace to single space and strip line endings
    return "\n".join([" ".join(line.split()) for line in text.splitlines() if line.strip() != ""]) 


def compare_files_by_path(ans_path: Path, out_path: Path):
    try:
        a = ans_path.read_text(encoding="utf-8", errors="ignore")
        b = out_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "RE", 0
    if a == b:
        return "AC", 100
    if normalize_whitespace(a) == normalize_whitespace(b):
        return "PE", 0
    return "WA", 0


def monitor_memory_and_kill(proc_pid: int, mem_limit_bytes: int):
    # Monitor process tree memory; if over limit, kill and return True
    try:
        p = psutil.Process(proc_pid)
    except Exception:
        return False
    try:
        while True:
            if not p.is_running():
                return False
            # sum rss of process and children
            rss = 0
            try:
                rss += p.memory_info().rss
                for c in p.children(recursive=True):
                    try:
                        rss += c.memory_info().rss
                    except Exception:
                        pass
            except Exception:
                pass
            if rss > mem_limit_bytes:
                try:
                    for c in p.children(recursive=True):
                        try:
                            c.kill()
                        except Exception:
                            pass
                    p.kill()
                except Exception:
                    pass
                return True
            time.sleep(0.05)
    except Exception:
        return False


def run_one_test(workdir: Path, solution: dict, testcase: dict, time_limit_ms: int, memory_limit_mb: int):
    # testcase: {name: str, input: str, answer: str}
    name = testcase.get('name')
    result = {"name": name, "verdict": "", "score": 0, "time_used": 0, "memory_used": 0, "stderr": ""}
    language = solution.get("language")
    # write input and answer to files in workdir/data
    data_dir = workdir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    in_file = data_dir / f"{name}.in"
    ans_file = data_dir / f"{name}.ans"
    out_file = workdir / "user.out"
    in_file.write_text(testcase.get('input', ''), encoding='utf-8')
    ans_file.write_text(testcase.get('answer', ''), encoding='utf-8')

    mem_limit_bytes = int(memory_limit_mb * 1024 * 1024)

    # prepare command
    if language == LANG_PY:
        exec_cwd = workdir / "python"
        exec_cwd.mkdir(parents=True, exist_ok=True)
        src = exec_cwd / "Main.py"
        # copy source if not there
        if not src.exists():
            # source was written previously by write_source
            # fallback: look at workdir/compile/Main.py
            possible = workdir / "python" / "Main.py"
            if not possible.exists():
                # if source in compile dir, move
                possible = workdir / "compile" / "Main.py"
            try:
                if possible.exists():
                    shutil.copy2(str(possible), str(src))
            except Exception:
                pass
        cmd = [sys.executable, str(src)]
        cwd = str(exec_cwd)
    else:
        exec_cwd = workdir
        exe = exec_cwd / "Main"
        if os.name == 'nt' and not exe.exists():
            exe = exec_cwd / "Main.exe"
        if not exe.exists():
            result.update({"verdict": "RE", "score": 0})
            return result
        cmd = [str(exe)]
        cwd = str(exec_cwd)

    start_ts = time.perf_counter()
    proc = None
    mem_kb_peak = 0
    try:
        inf = open(str(workdir / "data.in"), "wb")
        inf.write(in_file.read_bytes())
        inf.close()
        inf = open(str(workdir / "data.in"), "rb")
        outf = open(str(out_file), "wb")
        stderr_pipe = subprocess.PIPE
        proc = subprocess.Popen(cmd, stdin=inf, stdout=outf, stderr=stderr_pipe, cwd=cwd)

        mem_thread = None
        mem_killed = False
        if HAS_PSUTIL and memory_limit_mb > 0:
            # start monitor thread
            def memmon():
                nonlocal mem_killed
                killed = monitor_memory_and_kill(proc.pid, mem_limit_bytes)
                mem_killed = killed
            mem_thread = threading.Thread(target=memmon, daemon=True)
            mem_thread.start()

        # wait with timeout checking
        elapsed = 0.0
        interval = 0.05
        timeout_sec = max(1, time_limit_ms / 1000)
        while True:
            try:
                rc = proc.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                elapsed = time.perf_counter() - start_ts
                # check time
                if elapsed > timeout_sec:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    result.update({"verdict": "TLE", "score": 0, "time_used": time_limit_ms})
                    append_log(f"Job: TLE on testcase {name}")
                    return result
                # continue loop
                continue
        end_ts = time.perf_counter()
        result["time_used"] = int((end_ts - start_ts) * 1000)

        # read stderr
        stderr = b""
        try:
            stderr = proc.stderr.read() if proc.stderr is not None else b""
        except Exception:
            pass
        if stderr:
            try:
                result['stderr'] = stderr.decode('utf-8', errors='ignore')
            except Exception:
                result['stderr'] = str(stderr)

        # if mem monitor found kill
        if HAS_PSUTIL and mem_thread is not None and mem_thread.is_alive() is False and mem_killed:
            result.update({"verdict": "MLE", "score": 0})
            append_log(f"Job: MLE on testcase {name}")
            return result

        # check return code
        if proc.returncode != 0:
            # Treat non-zero as RE
            result.update({"verdict": "RE", "score": 0})
            append_log(f"Job: RE on testcase {name} rc={proc.returncode}")
            return result

        # compare outputs
        verdict, score = compare_files_by_path(ans_file, out_file)
        result.update({"verdict": verdict, "score": score})
        return result

    except Exception as e:
        append_log(f"Exception during run_one_test: {e}")
        result.update({"verdict": "RE", "score": 0})
        return result
    finally:
        try:
            if proc and proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        try:
            if inf:
                inf.close()
        except Exception:
            pass
        try:
            if outf:
                outf.close()
        except Exception:
            pass


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
    JOB_STORE[job_id]["testcases"] = []
    append_log(f"job {job_id} running in slot {slot}")
    try:
        # write source
        write_source(workdir, solution)
        ok, ce = compile_solution(workdir, solution)
        if not ok:
            JOB_STORE[job_id]["status"] = "finished"
            JOB_STORE[job_id]["result"] = {"status": "CE", "ce": ce}
            append_log(f"job {job_id} compile error")
            return

        # Prepare testcases
        provided = problem.get('testcases')
        if provided and isinstance(provided, list) and len(provided) > 0:
            tcs = []
            for i, t in enumerate(provided):
                name = t.get('name') or f'tc{i+1}'
                tcs.append({'name': name, 'input': t.get('input',''), 'answer': t.get('answer',''), 'weight': t.get('weight', 1)})
        else:
            data_dir = Path(problem.get("data_dir", str(DATA_DIR / str(problem.get('id', 0)))))
            tcs = []
            if data_dir.exists():
                ins = sorted([p for p in data_dir.glob('*.in')])
                for p in ins:
                    stem = p.stem
                    ansf = data_dir / f"{stem}.ans"
                    if ansf.exists():
                        tcs.append({'name': stem, 'input': p.read_text(encoding='utf-8',errors='ignore'), 'answer': ansf.read_text(encoding='utf-8',errors='ignore'), 'weight':1})

        n = len(tcs)
        if n == 0:
            JOB_STORE[job_id]["status"] = "finished"
            JOB_STORE[job_id]["result"] = {"status":"NO_TESTS"}
            return

        solution_result = {"id": solution.get("id"), "score": 0, "testcases": []}
        time_limit_ms = int(problem.get("timelimit", 1000))
        memory_limit_mb = int(problem.get("memory_limit", 256))

        # equal weight scoring total 100
        per_case_full = 100.0 / n

        for idx, tc in enumerate(tcs):
            JOB_STORE[job_id]["testcases"].append({'name': tc['name'], 'status': 'running'})
            res = run_one_test(workdir, solution, tc, time_limit_ms, memory_limit_mb)
            # score: if verdict AC -> full for that case, else 0. (PE gives 0 score but marked PE)
            score = per_case_full if res['verdict'] == 'AC' else 0
            solution_result['testcases'].append({'name': res['name'], 'verdict': res['verdict'], 'score': int(score), 'time_used': res.get('time_used',0), 'memory_used': res.get('memory_used',0), 'stderr': res.get('stderr','')})
            solution_result['score'] += int(score)
            JOB_STORE[job_id]['testcases'][idx]['status'] = res['verdict']
            JOB_STORE[job_id]['progress'] = int((idx+1)/n*100)

        # cap score to 100
        if solution_result['score'] > 100:
            solution_result['score'] = 100

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
  <title>Local Contest Judge UI</title>
  <style>
    :root{
      --bg:#0b1220; --fg:#e5eefb; --card:#111827; --accent:#2563eb;
    }
    body.light{--bg:#f8fafc; --fg:#0b1220; --card:#fff; --accent:#2563eb}
    body{font-family:Arial;background:var(--bg);color:var(--fg);margin:0}
    .wrap{display:flex}
    .sidebar{width:240px;background:var(--card);padding:20px}
    .content{flex:1;padding:20px}
    button{padding:8px 12px;border-radius:6px;border:none;background:var(--accent);color:white}
    pre{background:#020817;padding:12px;border-radius:6px}
    textarea{width:100%;min-height:120px}
    table{width:100%;border-collapse:collapse}
    th,td{padding:8px;border-bottom:1px solid rgba(255,255,255,0.06)}
    .tc-block{border:1px solid rgba(255,255,255,0.06);padding:8px;border-radius:6px;margin-bottom:8px;background:var(--card)}
    .row{display:flex;gap:12px}
    .col{flex:1}
  </style>
</head>
<body class="dark">
<div style="padding:8px;display:flex;justify-content:space-between;align-items:center;background:var(--card);">
  <div style="font-weight:bold">Local Contest Judge</div>
  <div>
    <button id="toggle-theme">Toggle Theme</button>
  </div>
</div>
<div style="display:flex;gap:16px;padding:16px;">
  <div style="width:360px;background:var(--card);padding:12px;border-radius:8px;">
    <h3>Run</h3>
    <label>Solution ID: <input id="solid" value="1"/></label><br/>
    <label>Language: <select id="lang"><option value="1">C++</option><option value="0">C</option><option value="4">Python</option></select></label>
    <h4>Code</h4>
    <textarea id="code">#include &lt;iostream&gt;\nint main(){int a,b; if(!(std::cin>>a>>b)) return 0; std::cout<<a+b<<"\n";}</textarea>
    <h4>Testcases</h4>
    <div id="testcases-container"></div>
    <div style="margin-top:8px;">
      <button onclick="addTestcase()">Add Testcase</button>
      <button onclick="startRun()">Start</button>
    </div>
    <div style="margin-top:8px;color:#aaa;font-size:13px">Memory limit (MB): <input id="memlimit" value="256" style="width:70px"/></div>
    <div style="margin-top:8px;color:#aaa;font-size:13px">Time limit (ms): <input id="timelimit" value="1000" style="width:70px"/></div>
  </div>

  <div style="flex:1">
    <h3>Jobs</h3>
    <div id="jobs-list"></div>
  </div>
</div>
<script>
// Theme toggle
(function(){
  const body = document.body;
  const btn = document.getElementById('toggle-theme');
  btn.addEventListener('click', ()=>{ body.classList.toggle('light');});
})();

function makeTestcaseHtml(name='', inputText='', answerText=''){
  const id = 'tc-' + Math.random().toString(36).slice(2,9);
  const div = document.createElement('div');
  div.className='tc-block';
  div.id = id;
  div.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center"><strong>Testcase</strong><button onclick="this.closest('.tc-block').remove()">Remove</button></div>
    <div><label>Name: <input class="tc-name" value="${name}"/></label></div>
    <div><label>Input</label><textarea class="tc-in">${inputText}</textarea></div>
    <div><label>Answer</label><textarea class="tc-ans">${answerText}</textarea></div>
  `;
  return div;
}
function addTestcase(){ document.getElementById('testcases-container').appendChild(makeTestcaseHtml('','')) }
// add one default
addTestcase();

async function startRun(){
  const sol=document.getElementById('solid').value;
  const lang=Number(document.getElementById('lang').value);
  const code=document.getElementById('code').value;
  const mem = Number(document.getElementById('memlimit').value)||256;
  const tlim = Number(document.getElementById('timelimit').value)||1000;
  const tcs = [];
  document.querySelectorAll('.tc-block').forEach((b,idx)=>{
    const name = b.querySelector('.tc-name').value || ('tc'+(idx+1));
    const input = b.querySelector('.tc-in').value || '';
    const ans = b.querySelector('.tc-ans').value || '';
    tcs.push({name:name,input:input,answer:ans});
  });
  const payload = {language:lang,code:code,user:'local',problem:{timelimit:tlim,memory_limit:mem,testcases:tcs}};
  const res = await fetch(`/api/judge/${sol}/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
  if(res.ok){ addJobCard(res.job_id); pollJob(res.job_id); }
}

function addJobCard(job_id){
  const c = document.createElement('div');
  c.id = 'jobcard-'+job_id;
  c.style.border='1px solid rgba(255,255,255,0.06)'; c.style.padding='8px'; c.style.marginBottom='8px';
  c.innerHTML = `<div><strong>Job ${job_id.slice(0,8)}</strong> <span id="job-${job_id}-status">pending</span></div><div id="job-${job_id}-progress"></div><div id="job-${job_id}-cases"></div>`;
  document.getElementById('jobs-list').prepend(c);
}

async function pollJob(job_id){
  const elStatus = document.getElementById(`job-${job_id}-status`);
  const elProgress = document.getElementById(`job-${job_id}-progress`);
  const elCases = document.getElementById(`job-${job_id}-cases`);
  let finished = false;
  while(!finished){
    const res = await fetch(`/api/job/${job_id}`).then(r=>r.json());
    if(!res.ok){ break; }
    const job = res.job;
    elStatus.textContent = job.status;
    const prog = job.progress || (job.status==='finished'?100:0);
    elProgress.innerHTML = `<div style="background:rgba(255,255,255,0.06);height:12px;border-radius:6px;overflow:hidden;width:100%"><div style="height:12px;background:var(--accent);width:${prog}%"></div></div>`;
    elCases.innerHTML = '';
    if(job.testcases){
      job.testcases.forEach(tc=>{
        const d = document.createElement('div'); d.style.padding='6px'; d.style.borderBottom='1px solid rgba(255,255,255,0.03)';
        d.innerHTML = `<strong>${tc.name}</strong> - ${tc.status||tc.verdict||'pending'} ${tc.verdict?('('+tc.verdict+')'):''} ${tc.score?(' score:'+tc.score):''}`;
        elCases.appendChild(d);
      })
    }
    if(job.status==='finished') finished=true;
    await new Promise(r=>setTimeout(r,800));
  }
}

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
    return jsonify({"ok":True,"state":state,"jobs":jobs, "psutil": HAS_PSUTIL})

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
        JOB_STORE[job_id] = {'status':'pending','solution':solution,'problem':problem,'progress':0}
    t = threading.Thread(target=run_job, args=(job_id,solution,problem,DEFAULT_MAX_RUNNING), daemon=True)
    t.start()
    append_log(f"Started job {job_id} for solution {solution_id}")
    return jsonify({"ok":True,"job_id":job_id})

@app.route('/api/job/<job_id>')
def api_job(job_id):
    job = JOB_STORE.get(job_id)
    if not job:
        return jsonify({"ok":False,"message":"not found"}),404
    # prepare lightweight job view
    view = {k: job.get(k) for k in ('status','slot','workdir','progress')}
    view['testcases'] = []
    for tc in job.get('testcases', []):
        # normalize testcase view
        if isinstance(tc, dict):
            view['testcases'].append({ 'name': tc.get('name'), 'status': tc.get('status') or tc.get('verdict'), 'verdict': tc.get('verdict'), 'score': tc.get('score')})
        else:
            view['testcases'].append(tc)
    view['result'] = job.get('result')
    return jsonify({"ok":True,"job":view})

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
    append_log('Judge UI v2 started')
    if not HAS_PSUTIL:
        append_log('psutil not found: MLE detection disabled. For MLE enable, pip install psutil')
    app.run(host='127.0.0.1', port=5000, debug=False)
