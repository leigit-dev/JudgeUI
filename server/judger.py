import threading
import uuid
import time
import shutil
import os
import subprocess
from pathlib import Path
from typing import Dict, Any, List

JOB_STORE: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

DEFAULT_MAX_RUNNING = 3
CWD = Path.cwd()

LANG_C = 0
LANG_CPP = 1
LANG_PY = 4


def _ensure_run_dirs(max_running: int):
    for i in range(max_running):
        d = CWD / f"run{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "compile").mkdir(parents=True, exist_ok=True)
        (d / "python").mkdir(parents=True, exist_ok=True)
        (d / "standalone").mkdir(parents=True, exist_ok=True)


def _write_source(workdir: Path, solution: Dict[str, Any]):
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


def _compile_solution(workdir: Path, solution: Dict[str, Any], timeout_sec: int = 60) -> (bool, str):
    lang = solution.get("language")
    ce = ""
    if lang == LANG_PY:
        # no compile
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
            # move binary to workdir root for execution convenience
            bin_src = cwd / "Main"
            bin_dst = workdir / "Main"
            if bin_src.exists():
                shutil.move(str(bin_src), str(bin_dst))
            return True, ce
    except subprocess.TimeoutExpired:
        return False, "Compile timeout"
    except FileNotFoundError as e:
        return False, f"Compiler not found: {e}"


def _compare_files(file1: Path, file2: Path) -> (str, int):
    """
    Return verdict and score (100 or 0). verdict: AC/WA/PE/RE
    Simplified comparison: exact match -> AC; otherwise try whitespace-insensitive -> PE; else WA
    """
    try:
        a = file1.read_text(encoding="utf-8", errors="ignore").splitlines()
        b = file2.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return "RE", 0

    if a == b:
        return "AC", 100

    # whitespace-insensitive compare
    def norm(lines: List[str]):
        return [" ".join(l.split()) for l in lines if l.strip() != ""]

    if norm(a) == norm(b):
        return "PE", 0

    return "WA", 0


def _run_one_test(workdir: Path, solution: Dict[str, Any], testcase: str, data_dir: Path, time_limit_ms: int):
    result = {"name": testcase, "verdict": "", "score": 0, "time_used": 0, "memory_used": 0}
    language = solution.get("language")
    # prepare files
    in_file = data_dir / f"{testcase}.in"
    ans_file = data_dir / f"{testcase}.ans"
    user_out = workdir / "user.out"

    if not in_file.exists():
        result.update({"verdict": "RE", "score": 0})
        return result

    # copy input
    shutil.copy2(str(in_file), str(workdir / "data.in"))

    start = time.perf_counter()
    try:
        if language == LANG_PY:
            # run python Main.py in python dir
            exec_cwd = workdir / "python"
            src = exec_cwd / "Main.py"
            cmd = ["python", str(src)]
            with (exec_cwd / "data.in").open("rb") as inf, (user_out).open("wb") as outf:
                proc = subprocess.run(cmd, stdin=inf, stdout=outf, stderr=subprocess.PIPE, timeout=max(1, time_limit_ms // 1000))
        else:
            exec_cwd = workdir
            # ensure Main exists
            exe = exec_cwd / "Main"
            if os.name == 'nt':
                exe = exec_cwd / "Main.exe"
            if not exe.exists():
                result.update({"verdict": "RE", "score": 0})
                return result
            with (workdir / "data.in").open("rb") as inf, (user_out).open("wb") as outf:
                proc = subprocess.run([str(exe)], stdin=inf, stdout=outf, stderr=subprocess.PIPE, timeout=max(1, time_limit_ms // 1000))
        end = time.perf_counter()
        result["time_used"] = int((end - start) * 1000)
        # compare
        verdict, score = _compare_files(ans_file, user_out)
        result.update({"verdict": verdict, "score": score})
    except subprocess.TimeoutExpired:
        result.update({"verdict": "TLE", "score": 0, "time_used": time_limit_ms})
    except Exception as e:
        result.update({"verdict": "RE", "score": 0})
    finally:
        # cleanup per-test temporary files in workdir
        try:
            for p in [workdir / "data.in"]:
                if p.exists():
                    p.unlink()
        except Exception:
            pass
    return result


def _select_slot(max_running: int) -> int:
    _ensure_run_dirs(max_running)
    for i in range(max_running):
        lockfile = CWD / f"run{i}" / ".lock"
        if not lockfile.exists():
            # claim slot
            try:
                lockfile.write_text(str(os.getpid()))
                return i
            except Exception:
                continue
    return -1


def _release_slot(slot: int):
    try:
        lockfile = CWD / f"run{slot}" / ".lock"
        if lockfile.exists():
            lockfile.unlink()
    except Exception:
        pass


def _run_job(job_id: str, solution: Dict[str, Any], problem: Dict[str, Any], max_running: int = DEFAULT_MAX_RUNNING):
    JOB_STORE[job_id]["status"] = "running"
    slot = _select_slot(max_running)
    if slot == -1:
        JOB_STORE[job_id]["status"] = "queued"
        return
    workdir = CWD / f"run{slot}"
    JOB_STORE[job_id]["slot"] = slot
    JOB_STORE[job_id]["workdir"] = str(workdir)

    try:
        src = _write_source(workdir, solution)
        ok, ce = _compile_solution(workdir, solution)
        if not ok:
            JOB_STORE[job_id]["status"] = "finished"
            JOB_STORE[job_id]["result"] = {"status": "CE", "ce": ce}
            return

        # prepare testcases
        data_dir = Path(problem.get("data_dir", "data"))
        testcases = problem.get("testcases", [])
        if not testcases:
            # auto-discover .in files
            if data_dir.exists():
                testcases = [p.stem for p in sorted(data_dir.glob("*.in"))]

        solution_result = {"id": solution.get("id"), "score": 0, "testcases": []}
        time_limit_ms = problem.get("timelimit", 1000)

        for tc in testcases:
            tcres = _run_one_test(workdir, solution, tc, data_dir, time_limit_ms)
            solution_result["testcases"].append(tcres)
            solution_result["score"] += tcres.get("score", 0)

        JOB_STORE[job_id]["status"] = "finished"
        JOB_STORE[job_id]["result"] = solution_result
    finally:
        _release_slot(slot)


def start_solution_run(solution: Dict[str, Any], problem: Dict[str, Any], max_running: int = DEFAULT_MAX_RUNNING) -> str:
    job_id = str(uuid.uuid4())
    JOB_STORE[job_id] = {"status": "pending", "solution": solution, "problem": problem}
    t = threading.Thread(target=_run_job, args=(job_id, solution, problem, max_running), daemon=True)
    t.start()
    return job_id


def get_job(job_id: str) -> Dict[str, Any]:
    return JOB_STORE.get(job_id, {"status": "unknown"})


def list_jobs() -> Dict[str, Any]:
    return JOB_STORE
