# rulegate

**Make AI agents actually read your rules before they act.**

"Read the rules first" in a prompt or a skill does not work reliably. Agents skim the headings, feel
briefed, and start changing things. The rule they skipped is usually the one that mattered.
rulegate turns reading into a gate the agent cannot walk past quietly:

1. **`rulegate quiz`** picks random sections from your rule files and asks what each one requires.
2. **`rulegate answer`** checks every answer against the distinctive terms of that section: the
   numbers, conditions and words from its body that the heading does not give away. If all answers
   pass, you get a **receipt**.
3. **`rulegate check`** is what your tooling calls before it lets the agent change anything. A
   receipt is valid only for the **exact current version** of the rules (SHA-256) and only for a
   limited time. If you edit the rules, everyone has to read them again.

It ships ready-made **Claude Code hooks**:

- A `SessionStart` hook puts the instruction into the session's context.
- A `PreToolUse` hook blocks `Edit`, `Write`, `Bash` and similar tools until the session holds a
  receipt. Reading stays allowed, so the agent can still read the rules.

> Built after a real incident. An agent was told to "read the rules completely" and read only the
> headings. It then discarded a trading idea as "not testable" because a data source was missing,
> although rule R21 said exactly: *never discard for lack of tools, procure them.*

## What it can and cannot do

- ✅ An agent can no longer *skip* the rules without it showing: every quiz, pass, fail and block is
  written to `~/.rulegate/log.jsonl`.
- ✅ Receipts expire when the rules change or after `ttl_hours`.
- ✅ Random sections each time, one attempt per quiz, and the token is stored only as a hash.
- ⚠️ It cannot prove *understanding*. An agent that greps a section and paraphrases it passes, but
  then it has read that section. Three random sections make skimming expensive; they do not make
  it impossible.
- ⚠️ Grading is lexical (term overlap), in English and German, with umlauts normalised. Tune
  `min_hits` and `keywords_per_section` for your rules.

## Install

It is a single file and uses only the Python standard library (3.8+):

```bash
curl -O https://raw.githubusercontent.com/ErsterALLES/rulegate/main/rulegate.py
chmod +x rulegate.py && sudo ln -s "$PWD/rulegate.py" /usr/local/bin/rulegate
```

Create `~/.rulegate/config.json` or a `.rulegate.json` in your project:

```json
{
  "rules": ["~/project/RULES.md"],
  "heading": "^#{2,3}\\s+\\S",
  "questions": 3,
  "min_hits": 3,
  "ttl_hours": 12,
  "language": "en"
}
```

See what it will ask:

```bash
rulegate status -v
```

## Use with Claude Code

```bash
rulegate install-claude-hooks          # merges into ~/.claude/settings.json, makes a backup first
```

From then on, every new session:

1. starts with a notice that the rules must be read and a receipt obtained,
2. is blocked on `Edit`/`Write`/`Bash`/… with a clear message (`exit 2`, the message goes to the agent),
3. reads the rules, runs `rulegate quiz --who claude-<session>`, and answers,
4. is unblocked for the rest of the receipt's lifetime.

## Use with anything else

Gate your own tools on a receipt token:

```bash
rulegate check --token "$RULEGATE_TOKEN" --quiet || exit 2
```

Example: a task logger that refuses `start` without a token.

```python
import subprocess, sys
if subprocess.run(["rulegate", "check", "--token", args.token, "--quiet"]).returncode != 0:
    sys.exit(2)   # rulegate already told the agent what to do
```

## Commands

| command | what it does |
|---|---|
| `rulegate quiz [--who ID]` | starts a quiz with N random sections |
| `rulegate answer QUIZ --a1 … --a2 … --a3 …` | grades the answers; on a pass, prints the receipt token |
| `rulegate check --token T` / `--who ID` | exit 0 if the receipt is valid, 2 otherwise (with instructions on stderr) |
| `rulegate status [-v]` | shows the configuration, the sections and a keyword preview |
| `rulegate hook session-start` / `pre-tool` | entry points for Claude Code hooks |
| `rulegate install-claude-hooks` | writes both hooks into your settings |

## Deutsch in Kürze

`rulegate` zwingt KI-Agenten, Regeln wirklich zu lesen, bevor sie etwas ändern.

- **Quiz:** Das Werkzeug stellt Fragen zu zufälligen Abschnitten. Die Antworten müssen die Begriffe
  und Zahlen aus dem Text des Abschnitts enthalten. Aus der Überschrift allein lassen sie sich nicht
  ableiten.
- **Quittung:** Wer besteht, bekommt eine Quittung. Sie gilt nur für den aktuellen Stand der Regeln.
- **Sperre:** Ohne Quittung sperrt der Claude-Code-Hook alle Schreib- und Änderungswerkzeuge.
- **Sprache:** Mit `"language": "de"` erscheinen die Meldungen auf Deutsch.

## License

MIT
