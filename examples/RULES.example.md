# Example rules

## R1 — No invented data

Never generate synthetic candles, mock signals or "example data" and compute with them because real
data is missing — not even "just to test the logic". The numbers end up in a report anyway. If real
data is missing, report the blocker (R2) instead of improvising.

## R2 — Report blockers instead of improvising

If a source, an access or a file is missing, say exactly what is missing and what you already tried.
Do not replace it, do not work around it, do not continue "approximately". Set the task status to
blocked and name the blocker. The owner procures what you cannot procure.

## R3 — Test first, discard later

Every idea gets tested before it is discarded. "Not worth it" is not an allowed answer. If a tool or
data source is missing, the status is OPEN — PROCUREMENT and procuring it becomes a task; the idea is
never discarded for lack of tools. Only the part that was actually tested may be discarded, and it
must be named exactly that way.

## R4 — Independent review before "done"

Nothing counts as done until someone other than the author has checked it: numbers traced back to
their source file, sample size n of at least 30, drawdown below 50 percent, no 0 or 100 percent hit
rates, and the protocol entry exists before the change, not after.
