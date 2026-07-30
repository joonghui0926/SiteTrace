# SiteTrace repository guidance

SiteTrace is an evidence investigation product, not a hazard detector.

## Product invariant

Every factual finding must carry one or more evidence clip IDs. Missing video
evidence is `UNVERIFIABLE`; it is never proof that a control was not performed.
Do not infer organizational root cause from video alone.

## Sponsor boundaries

- TwelveLabs finds and cites what happened in video.
- OpenAI maps observations to JHA meaning and drafts structured findings.
- Neo4j stores and compares planned and observed relationships.
- AWS Strands orchestrates tools, validation, sessions, approval, and reports.

## Verification

- Web: `npm run build && npm test`
- Backend: `python -m pytest backend/tests`
- Never commit `.env`, API keys, uploaded footage, generated reports, or AWS
  session credentials.
