# www/ — the public site

One HTML file, self-hosted fonts, no build tooling beyond `tools/build_site.py`
(stdlib). Deployed to GitHub Pages by `.github/workflows/pages.yml` on every push
that touches `www/`, the brief, or the workflow.

    make www            # build to _site/ and serve on :8000 — what Pages will serve
    make og             # rebuild og.png (dev-only: pillow + fonts in tools/fonts/)

## What is in here

| file | role |
|---|---|
| `index.html` | the whole landing page: CSS inline, JSON-LD in `<head>` |
| `og.png` | 1200x630 share card; LinkedIn renders nothing without one |
| `llms.txt` | the same facts as the page, in one file for AI answer engines |
| `fonts/` | Inter (variable) and JetBrains Mono, Latin subset, woff2, OFL licences alongside |
| `site.toml` | canonical URL and the `[author]` block (title, blurb, LinkedIn) shown in byline, footer and schema |
| `stats.toml` | operating record written by `tools/site_stats.py`; commit it — `reports/` is gitignored so CI cannot compute it. Band omitted when absent |
| `sample-report.html` | not committed by default — see below |

`robots.txt`, `sitemap.xml`, `brief.html` and `CNAME` are generated at build time, not committed.

## Things only you can do, in the order that pays off

**1. Publish one real night.** The page currently shows an illustrative composite and says so. A real report converts better and closes the question.

    python3 tools/make_sample_report.py reports/2026-09-08.md --dry-run     # read it first
    python3 tools/make_sample_report.py reports/2026-09-08.md --anonymize   # writes www/sample-report.html
    make www

The script drops whole sections about paper positions, sizing and routing, blanks every figure on lines that mention entry zones, invalidation, stops, size or brokers, and strips anything between `<!-- private -->` markers. It leaves inline broker *names* alone — if a line says "via IBKR" and you'd rather it didn't, wrap it in the private markers before publishing. It is a filter, not a guarantee: read the dry run. Once the file exists the build adds it to the sitemap and you can link it from the tape panel (`<div class="pf">` in `index.html`) with one `<a>`.

**1b. Show the operating record.** The strongest single line for a senior engineer is how long it has run unattended, and it is computed, not typed:

    python3 tools/site_stats.py     # writes www/stats.toml from reports/ and tests/
    make www

Commit the file: `reports/` never reaches GitHub, so the Pages build can only read what you committed. Re-run before each release. Missed weeknights include US market holidays; the page says so.

**1c. Paste your LinkedIn URL** into `[author].linkedin` in `site.toml`. Until then the byline shows sved.net and GitHub only, and the schema `sameAs` has GitHub alone.

**2. Repo About box and topics** — GitHub topic pages rank, and it is where technical traders actually find tools.

    tools/repo-metadata.sh          # needs gh auth login; idempotent

**3. Enable Pages once.** Settings → Pages → Source: *GitHub Actions*. Then push.

**4. Search Console.** After the first deploy, add the property, submit `https://svedbg.github.io/pharma/sitemap.xml`, and request indexing on the root URL. Without this a Pages site can wait weeks to be discovered.

**5. LinkedIn Post Inspector.** `https://www.linkedin.com/post-inspector/` — paste the URL *before* your first post; LinkedIn caches the card. Post copy with UTM links is in `docs/launch-post.md`.

**6. Terminal recording.** A real, unedited `--no-llm` sweep, as an animated SVG:

    pip install asciinema && npm i -g svg-term-cli
    tools/record-demo.sh            # writes www/demo.svg

then remove the comment wrapper around the demo `<section>` in `index.html`. The slot is commented out on purpose: a fabricated run would contradict the page.

**7. Custom domain** (e.g. `desk.sved.net`) — steps are in `site.toml`. The build rewrites every self-URL and writes `CNAME` from that one setting. Re-do steps 4 and 5 afterwards; the canonical changes.

**8. Analytics, if you want them.** Nothing is installed. If you want to know whether the LinkedIn post worked, the two privacy-respecting options that need no cookie banner in the EU are [GoatCounter](https://www.goatcounter.com) (free, one `<script>` tag) and [Plausible](https://plausible.io) (paid, one tag). Either goes just before `</body>` in `index.html`. The UTM parameters in the launch posts are already set up for whichever you pick.

## Editing

- The test count is counted from `tests/` at every build (`{{tests}}`); it was hard-coded at 87 from the brief and had drifted to 267. Don't type it again.
- Every other number on the page traces to `README.md` or `docs/capability-brief.html`. If you re-run `score_alerts.py` and the −2.68 / −9.59 / +1.72 figures move, change them in three places: the measured table, the FAQ answer, and `llms.txt`. The JSON-LD FAQ answer in `<head>` is the fourth.
- Dark palette and type live in `:root` at the top of `index.html`. Muted text is `--fg-4`; it sits at 4.9:1 on panels — don't take it darker.
- The tape at the top is a CSS marquee. `prefers-reduced-motion` stops it; hover pauses it.
- `og.png` is generated, not drawn. Edit `tools/make_og.py`, run `make og`, and commit the PNG — the workflow does not regenerate it.
