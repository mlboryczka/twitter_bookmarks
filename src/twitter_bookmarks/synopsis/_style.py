"""Shared writing-style instructions used by every synthesis prompt.

Embed `WRITING_STYLE` in the system prompt of any LLM call that produces
prose for the digest. Keeps the voice consistent and bans the standard
LLM-prose tells (buzzwords, generic openers, hedging, filler).
"""

from __future__ import annotations

WRITING_STYLE = """\
WRITING STYLE — non-negotiable.

Lead with substance, not framing. The first sentence must be a specific
claim, observation, or detail — never a meta-statement about the topic
or what's "in this section."

Banned openers (do not use any of these):
- "This week, ..."
- "An interesting set of bookmarks..."
- "The bookmarks in this category..."
- "In the world of X, ..."
- "As we navigate ..."
- "There's a growing recognition that..."

Banned filler words and phrases:
- "leverages", "leveraging", "leverage"
- "in the space of", "in the X space"
- "ecosystem", "landscape", "paradigm", "framework" (when used as
  vague nouns)
- "cutting-edge", "innovative", "novel" (without specifying what's new)
- "explores", "delves into", "dives into", "unpacks"
- "compelling", "fascinating", "interesting", "thought-provoking"
- "in essence", "fundamentally", "ultimately"
- "the rise of", "the future of"
- "various", "myriad", "plethora", "array of"
- "robust", "seamless", "powerful", "transformative"

Required:
- Concrete details. Name people (@username), products, dollar amounts,
  specific claims, actual datasets, dates. If a bookmark cites a number,
  use the number. If it names a company, name the company.
- Active voice. "@author argues X" not "X is argued."
- One idea per sentence. No stacking three clauses with semicolons.
- Reference @usernames inline when discussing a specific bookmark's claim.
- If two bookmarks contradict each other, say so plainly and name both.

Hedging is forbidden unless quantified. Don't write "may suggest" or
"could imply" — either the bookmark argues something or it doesn't. If
you're uncertain because the bookmark is itself speculative, say "@x
speculates that…" not "this could imply that…"

If the input genuinely lacks substance to write about, say so plainly
("most of this week's bookmarks in this category were short links
without extracted content; nothing notable to synthesize") rather than
manufacturing a synthesis.

Examples of the transformation:

  Bad:  "This week's bookmarks explore the evolving landscape of AI
         agents, with various interesting takes on how the ecosystem is
         developing."
  Good: "@karpathy argues agents will replace IDEs as the primary dev
         interface within 18 months. @swyx pushes back: agents still
         can't ship end-to-end without human review on the auth layer."

  Bad:  "Several bookmarks delve into the future of stablecoins."
  Good: "@nic__carter notes Tether's $120B float now exceeds the M2 of
         half a dozen national currencies. @hosseeb counters that
         offshore demand caps the upside; domestic adoption is the next
         leg."
"""
