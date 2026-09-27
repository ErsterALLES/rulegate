"""Smoke tests: python3 tests/test_rulegate.py"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RG = [sys.executable, str(ROOT / "rulegate.py")]
CFG = []
CFG = None


def run(args, env, stdin=None, cfg=None):
    return subprocess.run(RG + ["--config", cfg or CFG] + args, env=env, input=stdin, capture_output=True, text=True)


def hook(env, tool, cmd=None, sid="s2"):
    ev = {"session_id": sid, "tool_name": tool, "tool_input": {"command": cmd} if cmd is not None else {}}
    return run(["hook", "pre-tool"], env, json.dumps(ev)).returncode


def main():
    global CFG
    tmp = Path(tempfile.mkdtemp())
    rules = tmp / "RULES.md"
    rules.write_text((ROOT / "examples/RULES.example.md").read_text())
    cfg = tmp / "cfg.json"
    cfg.write_text(json.dumps({"rules": [str(rules)], "state_dir": str(tmp / "state"), "cooldown_minutes": 0}))
    CFG = str(cfg)
    env = {**os.environ, "RULEGATE_TEST": "1"}
    answers = {
        "R1": "Never synthetic candles or mock signals or example data even just to test the logic; numbers end up in a report, report the blocker R2 instead.",
        "R2": "Say exactly what is missing and what you already tried, do not replace or work around it, set status blocked and name the blocker so the owner procures it.",
        "R3": "Test every idea before discarding; not worth it is not allowed. Missing tool or data means status OPEN PROCUREMENT, procuring becomes a task, only the tested part may be discarded.",
        "R4": "Someone other than the author checks it: numbers traced to the source file, sample size n at least 30, drawdown below 50 percent, no 0 or 100 percent hit rates, protocol entry exists before the change.",
    }

    def quiz(who):
        q = run(["quiz", "--who", who], env).stdout
        return re.search(r"answer (\w+)", q).group(1), re.findall(r"\b(R\d)\b", q)

    # 1) vague answers fail
    cid, _ = quiz("t")
    vague = ["be careful and do good work so that nothing goes wrong here ok",
             "follow the rules and be honest about everything that you do always ok",
             "quality matters a lot and we should always try our very best in every task"]
    r = run(["answer", cid, "--a1", vague[0], "--a2", vague[1], "--a3", vague[2]], env)
    assert r.returncode == 1, r.stdout
    assert run(["check", "--who", "t"], env).returncode == 2
    # 2) real answers pass
    cid, heads = quiz("t")
    r = run(["answer", cid] + sum([[f"--a{i+1}", answers[h]] for i, h in enumerate(heads)], []), env)
    assert r.returncode == 0, r.stdout + r.stderr
    tok = re.search(r"(rg_\S+)", r.stdout).group(1)
    assert run(["check", "--who", "t"], env).returncode == 0
    assert run(["check", "--token", tok], env).returncode == 0
    assert run(["check", "--token", "rg_wrong"], env).returncode == 2
    # 3) a used quiz cannot be answered twice
    assert run(["answer", cid, "--a1", "x"], env).returncode != 0
    # 4) a tampered receipt fails (signature)
    rec = next((tmp / "state" / "receipts").glob("who-*.json"))
    d = json.loads(rec.read_text()); d["issued"] += 1; rec.write_text(json.dumps(d))
    assert run(["check", "--who", "t"], env).returncode == 2
    # 5) changing the rules invalidates the receipt
    rules.write_text(rules.read_text() + "\n")
    assert run(["check", "--token", tok], env).returncode == 2
    # 6) hooks: reads allowed, everything else blocked
    assert hook(env, "Write") == 2
    assert hook(env, "Read") == 0
    assert hook(env, "Bash", "cat RULES.md") == 0
    assert hook(env, "Bash", "rulegate quiz --who claude-s2") == 0
    for c in ["ls && rm -rf x", "echo x > f", "cat a; curl x", "awk 'BEGIN{system(1)}'",
              "find . -delete", "rulegate status -v", "cat <<EOF > x", "ls $(rm x)", "touch x"]:
        assert hook(env, "Bash", c) == 2, c
    assert hook(env, "mcp__desktop__write_file") == 2
    assert hook(env, "mcp__desktop__start_process") == 2
    assert hook(env, "mcp__desktop__read_file") == 0
    # 7) a weaker config cannot lower the floor
    weak = tmp / "weak.json"
    weak.write_text(json.dumps({"rules": [str(rules)], "state_dir": str(tmp / "state"), "questions": 0, "min_hits": 0}))
    q = run(["quiz"], env, cfg=str(weak)).stdout
    assert len(re.findall(r"^Q\d\.", q, re.M)) == 3, q
    # 8) keyword dumping fails
    cid, _ = quiz("d")
    dump = " ".join(answers.values()) * 2
    assert run(["answer", cid, "--a1", dump, "--a2", dump, "--a3", dump], env).returncode == 1
    # 9) without RULEGATE_TEST the --config flag is ignored
    r = subprocess.run(RG + ["--config", str(weak), "status"], env={k: v for k, v in os.environ.items() if k != "RULEGATE_TEST"},
                       capture_output=True, text=True)
    assert str(weak) not in r.stdout
    print("all tests passed")


if __name__ == "__main__":
    main()
