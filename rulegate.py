#!/usr/bin/env python3
"""rulegate - make AI agents actually read your rules before they act.

Telling an agent "read the rules first" does not work reliably: it skims the headings and starts
working. rulegate turns reading into a gate:

  1. `rulegate quiz`   picks random sections from your rule files and asks what they require.
  2. `rulegate answer` checks each answer against the distinctive terms of that section
                       (terms the heading does not give away). Pass all -> a receipt token.
  3. `rulegate check`  is what your tooling calls before letting the agent change anything.
                       A receipt is only valid for the exact current version of the rules
                       (SHA-256), is signed (HMAC), and expires. Edit the rules -> everyone re-reads.

Integrations:
  * Claude Code hooks: `rulegate hook session-start` (injects the instructions into context) and
    `rulegate hook pre-tool` (blocks writing tools, shell commands and writing MCP tools until the
    session holds a receipt). `rulegate install-claude-hooks` wires both into ~/.claude/settings.json.
  * Any script: call `rulegate check --token <TOKEN>` and refuse to proceed on a non-zero exit.

Threat model (honest): this stops agents that *skip* or *skim* the rules - the common failure. It is
not a sandbox against an agent that deliberately attacks it: an agent running as your OS user can
read ~/.rulegate and forge a receipt if it tries hard. Every quiz, pass, fail and block is logged,
so such an attempt is visible afterwards.

Standard library only, Python 3.8+. MIT licence.
"""
import argparse, hashlib, hmac, json, math, os, random, re, secrets, sys, time, unicodedata
from pathlib import Path

VERSION = "0.2.0"
CONFIG_FILE = Path(os.path.expanduser("~/.rulegate/config.json"))   # the ONLY config source (no env, no cwd)
DEFAULTS = {
    "rules": [],                       # list of rule files (markdown)
    "heading": r"^#{2,3}\s+\S",        # which markdown headings start a section
    "min_section_words": 40,           # shorter sections are never asked
    "questions": 3,
    "keywords_per_section": 12,        # distinctive terms considered per section
    "min_hits": 3,                     # how many of them an answer must contain
    "min_answer_words": 12,
    "max_answer_words": 120,           # longer answers are rejected (no keyword dumping)
    "ttl_hours": 12,                   # receipt lifetime
    "cooldown_minutes": 5,             # after a failed quiz, wait before the next one
    "state_dir": "~/.rulegate",
    "gated_tools": ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"],
    "gate_mcp": True,                  # also block MCP tools unless their name looks read-only
    "mcp_read_only": r"(^|__)(read|list|get|search|find|view|fetch|stat|describe|show|query)[a-z_]*$",
    "command": "rulegate",             # how the agent should call rulegate (full path if not on PATH)
    "language": "en",
}
FLOOR = {"questions": 3, "min_hits": 3, "min_answer_words": 12}    # config may raise, never lower
READ_CMDS = r"(cat|less|head|tail|grep|rg|ls|wc|pwd)"
STOP = set("""
aber alle allem allen aller alles also auch auf aus bei beim bereits bevor bis bitte damit dann dass daher
davon dazu dem den denen der des deshalb die dies diese diesem diesen dieser dieses doch dort durch eine
einem einen einer eines einmal etwa etwas fuer gegen gibt habe haben hat hier ihre immer jede jedem jeden
jeder jedes jetzt kann kein keine keinen koennen machen mehr muss mussen muessen nach nicht nichts noch
nur oder ohne schon sein seine sich sind soll sollen sondern ueber unter und uns unsere vom von vor wann
warum was weil welche wenn werden wird wurde wurden zum zur zwei drei about after again against all also
and any are because been before being between both but can could does doing down during each few from
further have having here into itself just more most must never only other over same should some such than
that their them then there these they this those through under until very were what when where which while
with would your always every without within cannot doesnt dont isnt wont
""".split())


# ------------------------------------------------------------------ config / state
def load_config(path=None):
    """Config comes only from ~/.rulegate/config.json (or --config for tests when RULEGATE_TEST=1).
    Environment variables and files in the working directory are deliberately ignored: an agent must
    not be able to hand rulegate an easier config."""
    cfg = dict(DEFAULTS)
    src = Path(path).expanduser() if (path and os.environ.get("RULEGATE_TEST") == "1") else CONFIG_FILE
    if src.is_file():
        cfg.update(json.loads(src.read_text(encoding="utf-8")))
        cfg["_config_file"] = str(src)
    for k, v in FLOOR.items():
        cfg[k] = max(int(cfg.get(k, v)), v)
    cfg["rules"] = [str(Path(r).expanduser()) for r in cfg["rules"]]
    return cfg


def state(cfg, *parts):
    p = Path(cfg["state_dir"]).expanduser().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log(cfg, **ev):
    ev["time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with state(cfg, "log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def key(cfg):
    p = state(cfg, ".key")
    if not p.is_file():
        p.write_text(secrets.token_hex(32))
        os.chmod(p, 0o600)
    return p.read_text().strip().encode()


def sign(cfg, rec):
    body = json.dumps({k: rec[k] for k in sorted(rec) if k != "sig"}, sort_keys=True)
    return hmac.new(key(cfg), body.encode(), hashlib.sha256).hexdigest()


def rules_hash(cfg):
    h = hashlib.sha256()
    for r in sorted(cfg["rules"]):
        h.update(r.encode()); h.update(Path(r).read_bytes())
    return h.hexdigest()


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ------------------------------------------------------------------ sections and keywords
def norm(s):
    s = s.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def tokens(s):
    out = []
    for t in re.findall(r"[a-z0-9][a-z0-9_.,%-]*[a-z0-9%]|[a-z0-9]", norm(s)):
        if re.search(r"\d", t):
            t = t.strip(".,")
            if len(t) >= 2 and not re.fullmatch(r"(19|20)\d\d([-.]\d\d){0,2}", t):   # numbers yes, dates no
                out.append(t)
        elif len(t) >= 5 and t not in STOP:
            out.append(t)
    return out


def sections(cfg):
    pat = re.compile(cfg["heading"])
    secs = []
    for r in cfg["rules"]:
        cur = None
        in_code = False
        for line in Path(r).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("```"):
                in_code = not in_code
            if not in_code and pat.match(line):
                cur = {"file": r, "heading": line.lstrip("#").strip(), "body": []}
                secs.append(cur)
            elif cur is not None:
                cur["body"].append(line)
    for s in secs:
        s["body"] = "\n".join(s["body"]).strip()
        s["words"] = len(s["body"].split())
        s["id"] = sha(s["file"] + "|" + s["heading"])[:10]
    return [s for s in secs if s["words"] >= cfg["min_section_words"]]


def keywords(cfg, secs):
    docs = [set(tokens(s["body"])) for s in secs]
    n = len(secs)
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    for s in secs:
        head = set(tokens(s["heading"]))
        tf = {}
        for t in tokens(s["body"]):
            if t not in head:
                tf[t] = tf.get(t, 0) + 1
        score = {t: c * math.log((n + 1) / df.get(t, 1)) for t, c in tf.items()}
        s["kw"] = [t for t, _ in sorted(score.items(), key=lambda x: (-x[1], x[0]))[:cfg["keywords_per_section"]]]
    return secs


def grade(cfg, sec, answer, all_kw):
    words = answer.split()
    toks = tokens(answer)
    tset = set(toks)
    hits = [k for k in sec["kw"] if k in tset or any(k in t for t in tset if len(k) >= 6)]
    if not (cfg["min_answer_words"] <= len(words) <= cfg["max_answer_words"]):
        return False, len(hits)
    # keyword dumping: an answer that carries more terms of OTHER sections than of the asked one
    # was not written from reading this section
    fremd = {t for t in tset if t in all_kw and t not in sec["kw"]}
    if len(fremd) > len(hits) + 3:
        return False, len(hits)
    return len(hits) >= min(cfg["min_hits"], len(sec["kw"])), len(hits)


# ------------------------------------------------------------------ texts
T = {
    "en": {
        "q": "Q{i}. What does the section \"{h}\" ({f}) require? Answer in your own words with the specific terms, numbers and conditions it uses ({w1}-{w2} words).",
        "how": "Answer with: {cmd} answer {cid} --a1 \"...\" --a2 \"...\" --a3 \"...\"",
        "pass": "PASSED. Receipt token (valid {h} h, only for this version of the rules):\n{tok}",
        "fail": "NOT PASSED: {bad} of {n} answers do not show that the section was read. This quiz is used up - read the rules and run `{cmd} quiz` again (earliest in {m} min).",
        "cool": "Cooldown after a failed quiz: next quiz in {s} s. Use the time to read the rules.",
        "nocheck": "rulegate: no valid rules receipt{w}. Read the rules completely ({r}), then run `{cmd} quiz{who}` and answer it.",
        "block": "Until then, file changes, shell commands (other than simple reads) and writing tools are blocked by a hook.",
    },
    "de": {
        "q": "F{i}. Was verlangt der Abschnitt „{h}“ ({f})? Antworte in eigenen Worten mit den konkreten Begriffen, Zahlen und Bedingungen daraus ({w1}–{w2} Wörter).",
        "how": "Antworten mit: {cmd} answer {cid} --a1 \"...\" --a2 \"...\" --a3 \"...\"",
        "pass": "BESTANDEN. Quittung (gültig {h} Std., nur für diesen Stand der Regeln):\n{tok}",
        "fail": "NICHT BESTANDEN: {bad} von {n} Antworten zeigen nicht, dass der Abschnitt gelesen wurde. Das Quiz ist verbraucht – Regeln lesen und `{cmd} quiz` neu starten (frühestens in {m} Min.).",
        "cool": "Sperrzeit nach nicht bestandenem Quiz: nächstes Quiz in {s} s. Die Zeit zum Lesen der Regeln nutzen.",
        "nocheck": "rulegate: keine gültige Regel-Quittung{w}. Erst die Regeln vollständig lesen ({r}), dann `{cmd} quiz{who}` ausführen und beantworten.",
        "block": "Bis dahin sind Datei-Änderungen, Befehle (außer einfachem Lesen) und schreibende Werkzeuge per Hook gesperrt.",
    },
}


def tr(cfg, k, **kw):
    kw.setdefault("cmd", cfg.get("command", "rulegate"))
    return T.get(cfg.get("language"), T["en"])[k].format(**kw)


# ------------------------------------------------------------------ commands
def cmd_quiz(cfg, a):
    who = a.who or ""
    cool = state(cfg, "cooldown", sha(who or "-")[:16])
    if cool.is_file():
        left = float(cool.read_text()) - time.time()
        if left > 0:
            print(tr(cfg, "cool", s=int(left)), file=sys.stderr); sys.exit(4)
    secs = keywords(cfg, sections(cfg))
    if len(secs) < cfg["questions"]:
        sys.exit("rulegate: not enough rule sections found - check `rules` and `heading` in the config.")
    pick = random.SystemRandom().sample(secs, cfg["questions"])
    cid = secrets.token_hex(4)
    ch = {"id": cid, "who": who, "rules_hash": rules_hash(cfg), "created": time.time(),
          "sections": [{"id": s["id"], "heading": s["heading"], "file": Path(s["file"]).name} for s in pick]}
    state(cfg, "challenges", cid + ".json").write_text(json.dumps(ch), encoding="utf-8")
    log(cfg, event="quiz", challenge=cid, who=who)
    for i, s in enumerate(pick, 1):
        print(tr(cfg, "q", i=i, h=s["heading"], f=Path(s["file"]).name, w1=cfg["min_answer_words"], w2=cfg["max_answer_words"]))
    print("\n" + tr(cfg, "how", cid=cid))


def cmd_answer(cfg, a):
    if not re.fullmatch(r"[0-9a-f]{8}", a.challenge or ""):
        sys.exit("rulegate: invalid quiz id.")
    p = state(cfg, "challenges", a.challenge + ".json")
    if not p.is_file():
        sys.exit("rulegate: unknown or already used quiz id.")
    ch = json.loads(p.read_text(encoding="utf-8"))
    p.unlink()                                                 # one attempt per quiz
    if ch["rules_hash"] != rules_hash(cfg):
        sys.exit("rulegate: the rules changed since this quiz - run `rulegate quiz` again.")
    answers = [a.a1, a.a2, a.a3, a.a4, a.a5]
    if a.json:
        answers = json.loads(Path(a.json).read_text(encoding="utf-8"))
    allsecs = keywords(cfg, sections(cfg))
    secs = {s["id"]: s for s in allsecs}
    all_kw = {k for s in allsecs for k in s["kw"]}
    res = []
    for i, q in enumerate(ch["sections"]):
        ans = answers[i] if i < len(answers) and isinstance(answers[i], str) else ""
        ok, hits = grade(cfg, secs[q["id"]], ans, all_kw) if q["id"] in secs else (False, 0)
        res.append({"section": q["heading"], "ok": ok, "hits": hits})
    bad = sum(1 for r in res if not r["ok"])
    who = ch["who"]
    log(cfg, event="answer", challenge=ch["id"], who=who, passed=not bad,
        results=[{"section": r["section"], "ok": r["ok"]} for r in res])
    if bad:
        state(cfg, "cooldown", sha(who or "-")[:16]).write_text(str(time.time() + 60 * cfg["cooldown_minutes"]))
        for i, r in enumerate(res, 1):
            print(f"  {'OK ' if r['ok'] else 'NO '} {i}. {r['section']}")
        print(tr(cfg, "fail", bad=bad, n=len(res), m=cfg["cooldown_minutes"]))
        sys.exit(1)
    tok = "rg_" + secrets.token_urlsafe(18)
    rec = {"token_sha": sha(tok), "who": who, "rules_hash": ch["rules_hash"], "issued": time.time(),
           "sections": [r["section"] for r in res]}
    rec["sig"] = sign(cfg, rec)
    state(cfg, "receipts", sha(tok)[:16] + ".json").write_text(json.dumps(rec), encoding="utf-8")
    if who:
        state(cfg, "receipts", "who-" + sha(who)[:16] + ".json").write_text(json.dumps(rec), encoding="utf-8")
    print(tr(cfg, "pass", h=cfg["ttl_hours"], tok=tok))


def valid_receipt(cfg, token=None, who=None, max_age=None):
    if token:
        p = state(cfg, "receipts", sha(token)[:16] + ".json")
    elif who:
        p = state(cfg, "receipts", "who-" + sha(who)[:16] + ".json")
    else:
        return False, "no token/who given"
    if not p.is_file():
        return False, "no receipt"
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False, "unreadable receipt"
    if not hmac.compare_digest(str(rec.get("sig", "")), sign(cfg, rec)):
        return False, "receipt signature invalid"
    if token and rec.get("token_sha") != sha(token):
        return False, "token mismatch"
    if who and not token and rec.get("who") != who:
        return False, "receipt belongs to someone else"
    if rec.get("rules_hash") != rules_hash(cfg):
        return False, "rules changed since the receipt"
    age_h = (time.time() - float(rec.get("issued", 0))) / 3600
    if age_h > min(max_age or cfg["ttl_hours"], cfg["ttl_hours"]):
        return False, f"receipt expired ({age_h:.1f} h old)"
    return True, "ok"


def cmd_check(cfg, a):
    ok, why = valid_receipt(cfg, a.token, a.who, a.max_age)
    if ok:
        if not a.quiet:
            print("rulegate: receipt valid.")
        return
    print(tr(cfg, "nocheck", w=f" ({why})", r=", ".join(cfg["rules"]), who=f" --who {a.who}" if a.who else ""), file=sys.stderr)
    sys.exit(2)


def cmd_status(cfg, a):
    secs = sections(cfg)
    print(f"rulegate {VERSION} · config: {cfg.get('_config_file', 'defaults')}")
    print(f"rules: {len(cfg['rules'])} file(s), {len(secs)} askable sections, hash {rules_hash(cfg)[:12]}")
    if a.verbose:                                  # headings only - never the grading keywords
        for s in secs:
            print(f"  - {s['heading']}  ({s['words']} words)")


# ------------------------------------------------------------------ Claude Code hooks
def bash_is_read_only(cmd):
    """Allow only single, simple read commands or rulegate quiz/answer/check - no chaining, redirection,
    substitution or here-docs. Anything else waits for the receipt."""
    if re.search(r"[;&|<>`$\n\\]|\(|\)", cmd):
        return False
    c = cmd.strip()
    if re.fullmatch(r"(\S*/)?(rulegate(\.py)?|python3?)(\s+\S*rulegate(\.py)?)?\s+(quiz|answer|check)(\s+.*)?", c):
        return True
    return bool(re.fullmatch(READ_CMDS + r"(\s+[^\s]+)*", c)) and not re.search(r"\s-(-?)(exec|delete|fprint)", c)


def hook_session_start(cfg, a):
    ev = json.loads(sys.stdin.read() or "{}")
    who = "claude-" + (ev.get("session_id") or "unknown")
    ok, _ = valid_receipt(cfg, who=who)
    if ok:
        return
    print(tr(cfg, "nocheck", w="", r=", ".join(cfg["rules"]), who=f" --who {who}") + "\n" + tr(cfg, "block"))


def hook_pre_tool(cfg, a):
    ev = json.loads(sys.stdin.read() or "{}")
    tool = ev.get("tool_name", "")
    gated = tool in cfg["gated_tools"] or (cfg.get("gate_mcp") and tool.startswith("mcp__")
                                           and not re.search(cfg["mcp_read_only"], tool.split("__")[-1]))
    if not gated:
        return
    if tool == "Bash" and bash_is_read_only((ev.get("tool_input") or {}).get("command", "")):
        return
    who = "claude-" + (ev.get("session_id") or "unknown")
    ok, why = valid_receipt(cfg, who=who)
    if ok:
        return
    log(cfg, event="blocked", who=who, tool=tool)
    print(tr(cfg, "nocheck", w=f" ({why})", r=", ".join(cfg["rules"]), who=f" --who {who}"), file=sys.stderr)
    sys.exit(2)                                                 # exit 2 = block, stderr goes to the agent


def cmd_install(cfg, a):
    sp = Path(a.settings).expanduser()
    data = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
    if sp.is_file():
        sp.with_suffix(".json.rulegate-backup").write_text(sp.read_text(encoding="utf-8"), encoding="utf-8")
    me = f"{sys.executable} {Path(__file__).resolve()}"
    hooks = data.setdefault("hooks", {})
    for ev in ("SessionStart", "PreToolUse"):                      # replace older rulegate entries
        hooks[ev] = [e for e in hooks.get(ev, []) if "rulegate" not in json.dumps(e)]
    hooks["SessionStart"].append({"hooks": [{"type": "command", "command": f"{me} hook session-start"}]})
    matcher = "|".join(cfg["gated_tools"]) + ("|mcp__.*" if cfg.get("gate_mcp") else "")
    hooks["PreToolUse"].append({"matcher": matcher, "hooks": [{"type": "command", "command": f"{me} hook pre-tool"}]})
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"rulegate hooks installed in {sp} (backup: {sp.with_suffix('.json.rulegate-backup').name}).")


def main():
    ap = argparse.ArgumentParser(prog="rulegate", description="Make AI agents actually read your rules before they act.")
    ap.add_argument("--config", help=argparse.SUPPRESS)          # only honoured with RULEGATE_TEST=1
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("quiz", help="start a quiz"); s.add_argument("--who")
    s = sub.add_parser("answer", help="answer a quiz"); s.add_argument("challenge")
    for i in range(1, 6):
        s.add_argument(f"--a{i}")
    s.add_argument("--json", help="file with a JSON list of answers")
    s = sub.add_parser("check", help="exit 0 if a valid receipt exists, 2 otherwise")
    s.add_argument("--token"); s.add_argument("--who"); s.add_argument("--max-age", type=float); s.add_argument("--quiet", action="store_true")
    s = sub.add_parser("status"); s.add_argument("-v", "--verbose", action="store_true")
    s = sub.add_parser("hook", help="Claude Code hook entry points"); s.add_argument("event", choices=["session-start", "pre-tool"])
    s = sub.add_parser("install-claude-hooks"); s.add_argument("--settings", default="~/.claude/settings.json")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if not cfg["rules"] and a.cmd not in ("install-claude-hooks",):
        sys.exit(f"rulegate: no rule files configured ({CONFIG_FILE} -> \"rules\").")
    {"quiz": cmd_quiz, "answer": cmd_answer, "check": cmd_check, "status": cmd_status,
     "install-claude-hooks": cmd_install,
     "hook": lambda c, x: (hook_session_start if x.event == "session-start" else hook_pre_tool)(c, x)}[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
