# Launch posts

Link for every post (UTM so the referrer survives LinkedIn's redirect):

    https://desk.sved.net/?utm_source=linkedin&utm_medium=social&utm_campaign=launch

If you move to a custom domain, swap the host — the UTM stays.

Before posting: run the URL through https://www.linkedin.com/post-inspector/ once. LinkedIn caches the card; inspect first, post second.

---

## LinkedIn — main post

A stock on my watchlist fell 91% in a single day.

Every indicator in every brokerage app said the same thing: oversold, buy the dip.

My system said nothing.

It had read the 8-K filed that same day, tied it to a failed trial, and blocked the signal before it reached my phone. That was its first week live.

I built an unattended research desk for small-cap biotech. Every morning at 09:00, Tue-Sat, it reads SEC filings, insider trades, short interest and trial records for ~60 names, computes signals, and pushes an alert only when something changes tier. Most mornings it sends nothing. That is the feature.

Three things I learned that have nothing to do with code:

1. Ask why it's cheap before you ask how cheap. A priced offering, a delisting notice, an auditor change, or under 1.5 quarters of cash explains a low price. It doesn't make it an opportunity.

2. Insider buying is informative. Insider selling mostly isn't. One filing showed an executive exercising options at $0.99 and selling at $5.06 minutes later. Counted carelessly, that's "insider buying."

3. Measure against the alternative you actually have. Scored against simply owning the XBI ETF, two of my three starting rules were worthless or harmful. "Buy near the yearly low" lost 2.68%. "More oversold is better" lost 9.59%. Only oversold confirmed by capitulation volume survived, at +1.72%.

I published the losing numbers alongside the winning one, because a tool that only shows you what worked is a tool you can't trust.

It's open source, MIT, Python standard library only, and every data source is free. It places no orders and it is not financial advice — it's the filter I wanted and couldn't buy.

Site, source and the full write-up: https://desk.sved.net/?utm_source=linkedin&utm_medium=social&utm_campaign=launch

#biotech #trading #python #opensource #sec #quant

---

## LinkedIn — short follow-up (a week later, engineering angle)

The hardest part of building a system that runs at 09:00 while nobody watches wasn't the finance. It was that "nothing to report" and "the run broke" look identical.

So there are two processes. One does the work. The other only checks whether the first one produced anything, and alarms after three silent weekdays.

Four more decisions like that — zero dependencies, byte-for-byte verified refactors, a test for every data trap that once produced a wrong number — are in the write-up. Same approach works for any pipeline where most of the input is noise.

https://desk.sved.net/brief.html?utm_source=linkedin&utm_medium=social&utm_campaign=launch-eng

---

## X / Bluesky

A stock fell 91% in one day. Every indicator said "oversold, buy."

My nightly research desk read the 8-K, found the failed trial, and said nothing.

Open source, stdlib Python, free data, publishes the rules that lost.

https://desk.sved.net/?utm_source=x&utm_medium=social&utm_campaign=launch

---

## Notes

- The numbers above come from `docs/capability-brief.html` and the README. If you re-run `score_alerts.py` before posting and they've moved, update the post, not the page — or both.
- Don't name the −91% company in the post. It's in the brief for anyone who clicks, and naming a real ticker in a social post next to the word "buy" is the kind of thing that ages badly.
- First comment on the LinkedIn post: paste the GitHub URL. LinkedIn suppresses reach on posts with external links in the body less than it used to, but the first-comment link still helps.
