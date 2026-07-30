---
name: construction-safety-review
description: Review construction incident evidence against an approved JHA without making unsupported safety or root-cause claims.
---

# Construction safety evidence review

Use this skill whenever SiteTrace compares observed work with a Job Hazard
Analysis (JHA) or drafts an incident or near-miss investigation report.

## Evidence rules

1. Every factual event, finding, and report claim must include one or more
   `evidence_clip_ids`.
2. Preserve the source camera, video, and start/end timestamps supplied by
   TwelveLabs. Never invent or broaden a clip window.
3. Treat missing footage or inadequate camera coverage as `UNVERIFIABLE`.
   Absence from video is never proof that a control was not performed.
4. Do not merge people, vehicles, tools, or materials across cameras unless
   supplied visual, temporal, spatial, or movement evidence supports the link.
5. Separate direct observations from graph-supported comparisons. A Neo4j path
   can establish sequence or shared entities; it does not establish causation.

## Allowed finding statuses

Use exactly one:

- `COMPLIANT`
- `CONFIRMED_DEVIATION`
- `REQUIRED_CONTROL_NOT_OBSERVED`
- `UNVERIFIABLE`

Every `UNVERIFIABLE` finding must state an `evidence_gap_reason`.

## Safety boundaries

- Do not infer organizational root cause from video alone.
- Do not assign blame, discipline, legal liability, or regulatory disposition.
- Do not identify workers by face. Use observable, temporary descriptors.
- Do not claim a corrective action has been completed unless completion has
  its own cited evidence and a human reviewer confirms it.
- Draft corrective actions as proposals for a safety manager to review.

## Publication

An investigation report is always a draft until the Strands publication tool
receives an affirmative human-approval interrupt response. Never bypass the
approval step or imply that an unapproved draft has been published.
