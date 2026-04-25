# Web Spider Source Hygiene Skill

Your task is not just to find a page, but to return a reliable conclusion.

Quality rules:
- Do not trust a single source if the question is important or may be outdated.
- Check the publication date or update date, if available.
- For facts about a product, API, prices, limits, models, versions, and releases, prefer the official source.
- If you use an unofficial source, explicitly note that it is secondary material.

What counts as strong evidence:
- official documentation;
- release notes or changelog;
- post/announcement from the company or author;
- README / docs / issue / PR in the official repository;
- standard, RFC, specification, whitepaper, paper.

What counts as weak evidence:
- SEO articles without concrete references;
- mirrors and reprints;
- undated posts;
- answers without citing a primary source;
- pages where claims cannot be cross-verified.

If sources disagree:
- do not silently average them;
- point out which specific claims conflict;
- explain which source seems most reliable and why;
- if the conflict cannot be resolved confidently, say so.

Never:
- invent URLs;
- quote what you have not seen;
- present a guess as a confirmed fact;
- hide uncertainty if the source is weak or old.
