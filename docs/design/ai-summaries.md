# Generating AI summaries

How Alexandria should get a summary written for a paper, given that
it cannot write one itself.

## TL;DR

* Alexandria has **no model and no API key**, and MCP *sampling* —
  which would let the server ask the client's model — is not
  available. So a "Summarise" button cannot generate; it can only
  ask something that can.
* Shell out to **`claude -p`**, piping the paper's text in on
  **stdin** and reading the summary back from `--output-format json`.
* **Do not route this through the MCP.** Alexandria already has the
  text; it does not need an agent to fetch it.
* Each `claude -p` invocation costs about **2.5 cents of fixed
  session overhead** before it reads a word of the paper. That, not
  the paper length, is what shapes the batch design.

## Why not the MCP

The obvious design is: the app asks a connected assistant to
summarise paper *N*, and the assistant calls `get_full_texts` to
read it. That works, and it is how a human at the terminal would do
it, but as an internal mechanism it is all cost and no benefit:

* It needs an MCP server registered for whatever directory the
  spawned process runs in.
* It needs tool-use permission for a read the app could do itself.
* It inherits the server's failure modes. A server left running for
  six days served a stale tool list and a mismatched catalogue
  (fixed 2026-09-04) — entirely invisible until someone asked it
  where it was pointed.

Alexandria does not need Claude to *fetch* anything. It needs Claude
to *read and condense*. The text is already in hand:
`jats.body_text()` for the publisher's own full text, the PDF
extraction otherwise, the abstract for a BibTeX-only ghost — the
same ladder `alexandria_mcp.server._best_full_text` uses. Pipe it
in, take the prose out.

## The interface

Measured 2026-09-05:

```
$ printf 'The mitochondrion is the powerhouse...' \
    | claude -p --output-format json \
             --model claude-haiku-4-5-20251001 \
             "Summarise the text on stdin in one sentence."
```

The JSON carries everything needed:

| field | use |
| --- | --- |
| `result` | the summary prose |
| `modelUsage` | exact model string, for `set_summary(model=…)` |
| `total_cost_usd` | what it cost |
| `usage` | token counts |

So the whole job is: build the text, spawn, capture, and store
through the same path `set_summary` uses — recording `source` as
`jats` / `pdf` / `abstract` according to which tier the text came
from, and the model as reported rather than as configured.

## The number that shapes the design

That one-sentence summary of two sentences of input reported:

```
in=9   out=186   cache_create=11186   cache_read=13615   $0.0247
```

Nine tokens of content against roughly **25,000 tokens of session
overhead** — the system prompt and tooling `claude -p` sets up each
time. The paper itself is nearly free by comparison: a 32,000-
character JATS body is about 8,000 tokens.

Consequences:

* **Per-paper spawning is the expensive shape.** Forty papers as
  forty invocations is about £1 of pure overhead before any reading.
* **Batch into as few invocations as possible.** One session handed
  several papers amortises the overhead across all of them.
* **The model matters more than anything else at scale.** Haiku
  summarised competently in the test; for a bulk pass over a library
  that is the setting worth exposing.
* **Show the cost before starting.** It is now a number we can
  compute — papers × overhead + text tokens — so a bulk action
  should state it rather than surprise the user afterwards.

## Where the control goes

**Not next to the JATS chip**, despite the obvious appeal:

* It would appear only on papers that have JATS, when a PDF paper
  can be summarised too and a ghost can be summarised from its
  abstract — and the text ladder already picks the best source, so
  the control need not know which.
* The JATS chip asserts "we hold the publisher's full text", which
  is a different fact from "this has, or wants, a summary".

**Put it where the summary lives.** `browse.make_summary_chip`
already draws a chip when a summary exists; when one does not, draw
the same chip dimmed, reading *Summarise*. One place on the card for
the state of a paper's summary — absent, requested, present.

**The bulk case is the more valuable one.** Summarising the paper
you are looking at is the rare case; "summarise the forty I have not
done" is the workflow, and `summary_overview` already returns
exactly that list as `missing_ids`. A hamburger item —
*Summarise unsummarised papers…* — stating the count and the
estimated cost.

## Open questions

* **Configurability.** Not everyone has `claude`. A prefs entry for
  the command and model keeps it general; the existing backlog item
  says "Summarise with \<configured-AI-agent\>".
* **Batching mechanics.** One invocation per paper is simplest and
  most expensive; one invocation handed *n* papers needs the prompt
  to keep the summaries separable and attributable to the right ID.
* **Failure.** A spawn that fails, times out or returns prose that
  is obviously not a summary should leave the sidecar untouched and
  say so, not store something wrong.
* **Recording intent.** A `summary_requested` block in the sidecar
  would make a request durable across restarts and let
  `summary_overview` report requested-but-not-done. Worth having if
  summarising ever becomes asynchronous or queued.
