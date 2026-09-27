"""Smoke tests: python3 tests/test_rulegate.py"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RG = [sys.executable, str(ROOT / "rulegate.py")]


def run(args, env, stdin=None):
    return subprocess.run(RG + args, env=env, input=stdin, capture_output=True, text=True)


def main():
    tmp = Path(tempfile.mkdtemp())
    rules = tmp / "RULES.md"
    rules.write_text((ROOT / "examples/RULES.example.md").read_text())
    cfg = tmp / "cfg.json"
    cfg.write_text(json.dumps({"rules": [str(rules)], "state_dir": str(tmp / "state"), "questions": 2}))
    env = {**os.environ, "RULEGATE_CONFIG": str(cfg)}
    answers = {
        "R1": "Never synthetic candles or mock signals or example data even just to test the logic; numbers end up in a report, report the blocker R2 instead.",
        "R2": "Say exactly what is missing and what you already tried, do not replace or work around it, set status blocked and name the blocker so the owner procures it.",
        "R3": "Test every idea before discarding; not worth it is not allowed. Missing tool or data means status OPEN PROCUREMENT, procuring becomes a task, only the tested part may be discarded.",
        "R4": "Someone other than the author checks it: numbers traced to the source file, sample size n at least 30, drawdown below 50 percent, no 0 or 100 percent hit rates, protocol entry exists before the change.",
    }
    # 1) vague answers fail
    q = run(["quiz", "--who", "t"], env).stdout
    cid = re.search(r"answer (\w+)", q).group(1)
    r = run(["answer", cid, "--a1", "be careful and do good work so that nothing goes wrong here ok", "--a2", "follow the rules and be honest about everything that you do always ok"], env)
    assert r.returncode == 1, r.stdout
    assert run(["check", "--who", "t"], env).returncode == 2
    # 2) real answers pass
    q = run(["quiz", "--who", "t"], env).stdout
    cid = re.search(r"answer (\w+)", q).group(1)
    heads = re.findall(r"\b(R\d)\b", q)
    r = run(["answer", cid, "--a1", answers[heads[0]], "--a2", answers[heads[1]]], env)
    assert r.returncode == 0, r.stdout + r.stderr
    tok = re.search(r"(rg_\S+)", r.stdout).group(1)
    assert run(["check", "--who", "t"], env).returncode == 0
    assert run(["check", "--token", tok], env).returncode == 0
    assert run(["check", "--token", "rg_wrong"], env).returncode == 2
    # 3) a used quiz cannot be answered twice
    assert run(["answer", cid, "--a1", "x"], env).returncode != 0
    # 4) changing the rules invalidates the receipt
    rules.write_text(rules.read_text() + "\n")
    assert run(["check", "--token", tok], env).returncode == 2
    # 5) hooks
    ev = json.dumps({"session_id": "s1", "tool_name": "Write", "tool_input": {}})
    assert run(["hook", "pre-tool"], env, ev).returncode == 2
    ev = json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "cat RULES.md"}})
    assert run(["hook", "pre-tool"], env, ev).returncode == 0
    ev = json.dumps({"session_id": "s1", "tool_name": "Read", "tool_input": {}})
    assert run(["hook", "pre-tool"], env, ev).returncode == 0
    print("all tests passed")


if __name__ == "__main__":
    main()
