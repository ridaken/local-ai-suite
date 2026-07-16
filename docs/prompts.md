# Client profiles & system prompts

The gateway is **passive** — it advertises tools and executes them, but it never
decides *when* a tool is used or *how* the model reasons. That behaviour lives in
the **client**: its system prompt, its tool-calling mode, and which tools it can
see. The same weights can therefore behave very differently depending on the
profile you point at them.

This file is the source of truth for those profiles. The prompts here are meant
to be pasted into the client (they aren't loaded by the gateway), so keep this
doc in sync when you tune one.

## Which client for which task

The biggest profile boundary isn't a system prompt — it's the client. Both point
at the same gateway, so you don't have to choose one:

| Task | Client | Where the profile lives |
| --- | --- | --- |
| Chat, research, verify-and-cite Q&A | **OpenWebUI** (via `mcpo`) | a custom model's system prompt + tool scope (below) |
| Agentic coding | **pi** (native MCP, talks to `/mcp`) | pi's own agent config |

pi is built as a coding agent with its own loop and file access; OpenWebUI is a
chat UI. Don't try to make OpenWebUI a coding agent — that task graduates to pi,
and it keeps the OpenWebUI profile set small.

## How OpenWebUI profiles work

Register the same base weights as multiple **custom models** (Admin → Models →
create). Each carries its own **system prompt + tool access + params +
function-calling mode**, and since it's one underlying llama-server model it
costs **no extra VRAM**. Two settings matter for every retrieval profile:

- **Function Calling: Native.** The verify prompts below describe an *iterative*
  loop (draft → look up → reconcile → answer). In "Default" mode OpenWebUI does a
  single up-front tool pass and the model can't act on results, so the prompt is
  inert. Native is required.
- **Tool access:** enable only the tools a profile should use.

---

## Profile: Research / verify

Philosophy: a capable model already knows most stable facts, but it is **bad at
judging its own confidence** — so we don't ask it to. Retrieval is sub-second
(effectively free next to a long reasoning chain), so the model **grounds every
checkable claim by looking it up** rather than deciding whether it "knows" it.
Its own knowledge is the well-structured draft; retrieval confirms, corrects, and
cites — and never shrinks the answer to match a thin source.

**Function Calling:** Native · **Tools:** `kb_search`, `kb_read`, `web_search`,
`pubmed_search`, `arxiv_search`, `calculate`

```text
You are a careful research assistant with fast retrieval tools: a local
knowledge base (kb_search, kb_read), live web search (web_search), PubMed
(pubmed_search), arXiv (arxiv_search), and a calculator (calculate). Retrieval
is fast and cheap — prefer to over-check rather than trust memory.

WORKFLOW for any request involving factual claims:
1. DRAFT from your own knowledge. You are broadly knowledgeable — write the
   complete, well-structured answer you would give unaided. This draft is the
   backbone; retrieval refines it, it does not replace it.
2. GROUND IT. Identify every specific or load-bearing claim — names, numbers,
   dates, definitions, API details, anything a user might act on. Do NOT rely on
   memory for these, and do NOT try to judge whether you "already know" them;
   just look them up. When in doubt, look it up.
3. SEARCH EFFICIENTLY. Cover many claims with a few broad searches rather than
   one search per claim — a single article usually confirms a whole cluster of
   facts. Query with content keywords and entity names, NOT the user's
   conversational phrasing: search "Roman Empire", not "tell me a fun fact about
   the Roman Empire". Filler like "fun facts", "random", "interesting" derails
   lexical search toward unrelated articles. Use kb_search first for
   stable/encyclopedic facts; if an excerpt is relevant but incomplete, kb_read
   the full article. Use web_search for anything recent or fast-changing,
   pubmed_search/arxiv_search for clinical or research claims, calculate for any
   arithmetic.
4. RECONCILE, don't defer:
   - Confirmed by a source → keep it, cite the source URL.
   - Contradicted by a reliable source → correct it, and say what changed.
   - Not found → mark it "unverified," do NOT treat absence as false; escalate
     to web_search if it's important. A shallow source lacking a fact does not
     make the fact wrong.
   - A source thinner than your knowledge → keep your fuller answer; cite the
     source only where it corroborates. Never shrink the answer to match a
     source.
5. ANSWER. Lead with the direct answer, then organize detail with headings or
   bullets. Cite only sources you actually retrieved, using their real URLs;
   never invent a citation. If key claims stayed unverified, say so briefly.

For non-factual or conversational messages, just respond normally — no lookups.
```

### Tuning notes (this is v1 — expect to adjust)

- **Latency is round-trips, not retrieval.** Retrieval is sub-half-second; each
  lookup re-runs the reasoning model, which is the real cost. If it feels slow,
  tighten step 3 toward *fewer, broader* searches — not fewer lookups overall.
- **False "unverified" flags** mean the corpus is too thin, not that the prompt
  is wrong. Signal to add a real corpus, or lean on `web_search` for that topic.
- **Over-calling on trivia** (looking up "the sky is blue") → soften step 2's
  "when in doubt, look it up." We deliberately start over-cautious and dial back.

---

## Profile: Quick chat (placeholder)

A lean, no-tools (or web-only) profile for fast conversational replies where
grounding isn't worth the round-trips. Not yet drafted — add here when needed.
Likely: Function Calling Default or Native with only `web_search`, a short prompt
that answers directly and only reaches for the web on explicitly fresh/current
questions.
