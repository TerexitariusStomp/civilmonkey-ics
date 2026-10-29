#!/usr/bin/env python3
"""Generate an .ics feed from the public Telegram channel t.me/civilmonkey.

Usage:
    python3 civilmonkey_ics.py build                    # scrape + write civilmonkey.ics
    python3 civilmonkey_ics.py build --pages 15         # how far back to paginate
    python3 civilmonkey_ics.py build --output out.ics   # custom output path

Events are extracted heuristically from post text. A state file
(.civilmonkey_state.json) persists events for posts that scroll out of
Telegram's ~20-message public preview, so run it on a schedule (cron /
systemd timer) and subscribe your calendar to the generated file or URL.

Times without an explicit zone are assumed UTC. Zones like "CET", "PT",
or "Portugal time" are resolved via IANA tz where possible.
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from html import unescape
from zoneinfo import ZoneInfo

CHANNEL = "civilmonkey"
BASE = f"https://t.me/s/{CHANNEL}"
POST_URL = f"https://t.me/{CHANNEL}/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) civilmonkey-ics/1.0"}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
ORD = r"(?:ths|st|nd|rd|th)?"

WEEKDAYS = {d.lower(): i for i, d in enumerate(
    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])}

# tz abbreviations / phrases -> IANA zone (abbrev meaning varies; these are the
# most plausible for this channel's EU/US-centric content)
TZMAP = {
    "utc": "UTC", "gmt": "UTC", "z": "UTC",
    "cet": "Europe/Berlin", "cest": "Europe/Berlin", "met": "Europe/Berlin",
    "bst": "Europe/London", "wet": "Europe/Lisbon", "west": "Europe/Lisbon",
    "est": "America/New_York", "edt": "America/New_York", "et": "America/New_York",
    "cst": "America/Chicago", "cdt": "America/Chicago", "ct": "America/Chicago",
    "mst": "America/Denver", "mdt": "America/Denver", "mt": "America/Denver",
    "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles", "pt": "America/Los_Angeles",
    "ist": "Asia/Kolkata",
    "urc": "UTC", "uct": "UTC", "uts": "UTC",  # common typos seen in posts
}
TZ_PHRASES = {
    "portugal time": "Europe/Lisbon", "lisbon time": "Europe/Lisbon",
    "berlin time": "Europe/Berlin", "german time": "Europe/Berlin",
    "uk time": "Europe/London", "london time": "Europe/London",
    "pacific time": "America/Los_Angeles",
    "eastern time": "America/New_York",
}

DAYPART = {"morning": 10, "noon": 12, "midday": 12, "afternoon": 15,
           "evening": 19, "tonight": 20, "night": 21}

STOP_LINE = re.compile(r"^\s*(https?://|\[|@|see |via |announcement)", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def strip_tags(html):
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", "", html)
    return unescape(html)


def scrape_posts(max_pages=8):
    """Yield dicts {id, dt (UTC), text, links} newest-first."""
    posts = []
    before = None
    for _ in range(max_pages):
        url = BASE + (f"?before={before}" if before else "")
        page = fetch(url)
        # split on message containers
        chunks = re.split(r'<div class="tgme_widget_message_wrap[^"]*">', page)
        seen = 0
        for chunk in chunks[1:]:
            mpost = re.search(r'data-post="' + CHANNEL + r'/(\d+)"', chunk)
            mtext = re.search(
                r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                chunk, re.S)
            mtime = re.search(r'<time datetime="([^"]+)"', chunk)
            if not mpost:
                continue
            pid = int(mpost.group(1))
            links = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*rel="?noopener', chunk)
            if not links:
                links = re.findall(r'href="(https?://(?!t\.me|telegram)[^"]+)"', chunk)
            posts.append({
                "id": pid,
                "dt": mtime.group(1) if mtime else None,
                "text": strip_tags(mtext.group(1)) if mtext else "",
                "links": [l for l in links if "t.me/" not in l],
            })
            seen += 1
        if not seen:
            break
        before = min(p["id"] for p in posts)
        time.sleep(0.4)
    posts.sort(key=lambda p: p["id"])
    return posts


def month_num(m):
    return MONTHS[m.lower().rstrip(".")]


def infer_year(month, day, post_date, explicit_year=None):
    if explicit_year:
        return explicit_year
    cand = date(post_date.year, month, day)
    if cand < post_date - timedelta(days=45):
        cand = date(post_date.year + 1, month, day)
    return cand.year


def get_tz(tzname, when):
    """Return tzinfo; when is a date/datetime used only for logging fallbacks."""
    return ZoneInfo(TZMAP.get(tzname.lower(), "UTC"))


def parse_times(line, ref_date):
    """Return (start_dt_aware_or_None, end_dt_or_None, matched_spans)."""
    spans = []
    # 24h range "9:00-18:00" / "09:00 to 18:00"
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(?:-|–|to)\s*([01]?\d|2[0-3]):([0-5]\d)\b", line)
    if m:
        s = datetime.combine(ref_date, dtime(int(m.group(1)), int(m.group(2))), ZoneInfo("UTC"))
        e = datetime.combine(ref_date, dtime(int(m.group(3)), int(m.group(4))), ZoneInfo("UTC"))
        spans.append(m.span())
        return s, e, spans
    # "7-9pm" / "7 - 9 pm CET"
    m = re.search(
        rf"\b(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)\s*({'|'.join(TZMAP)})?\b",
        line, re.I)
    if m:
        h1, h2 = int(m.group(1)), int(m.group(2))
        if m.group(4).lower() == "pm":
            h2 = h2 % 12 + 12
            if h1 < 12:
                h1 += 12
        if h1 > 23 or h2 > 23:
            m = None
    if m:
        tz = get_tz(m.group(5) or "utc", ref_date)
        s = datetime.combine(ref_date, dtime(h1, 0), tz).astimezone(ZoneInfo("UTC"))
        e = datetime.combine(ref_date, dtime(h2, int(m.group(3) or 0)), tz).astimezone(ZoneInfo("UTC"))
        spans.append(m.span())
        return s, e, spans
    # "11:00am to 6:00pm"
    m = re.search(
        rf"\b(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)\s*(?:to|until|-)\s*(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)\s*({'|'.join(TZMAP)})?\b",
        line, re.I)
    if m:
        def h12(h, mm, ap):
            h = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
            return h, int(mm or 0)
        tz = get_tz(m.group(7) or "utc", ref_date)
        h, mi = h12(m.group(1), m.group(2), m.group(3))
        s = datetime.combine(ref_date, dtime(h, mi), tz).astimezone(ZoneInfo("UTC"))
        h, mi = h12(m.group(4), m.group(5), m.group(6))
        e = datetime.combine(ref_date, dtime(h, mi), tz).astimezone(ZoneInfo("UTC"))
        spans.append(m.span())
        return s, e, spans
    # "4 30pm" (hour + minutes space-separated; hour <= 12 so day-of-month
    # like "28 10pm" can't misfire)
    m = re.search(
        rf"\b(1[0-2]|0?[1-9])\s+([0-5]\d)\s*(am|pm)\s*({'|'.join(TZMAP)})?\b",
        line, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        if h <= 23:
            tz = get_tz(m.group(4) or "utc", ref_date)
            s = datetime.combine(ref_date, dtime(h, int(m.group(2))), tz).astimezone(ZoneInfo("UTC"))
            spans.append(m.span())
            return s, s + timedelta(hours=1), spans
    # single "5pmUTC" / "3pm" / "at 15:00"
    m = re.search(
        rf"\b(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)\s*({'|'.join(TZMAP)})?\b",
        line, re.I)
    if not m:
        m2 = re.search(rf"\b([01]?\d|2[0-3]):([0-5]\d)\s*({'|'.join(TZMAP)})\b", line, re.I)
        if m2:
            tz = get_tz(m2.group(3), ref_date)
            s = datetime.combine(ref_date, dtime(int(m2.group(1)), int(m2.group(2))), tz)
            spans.append(m2.span())
            return s.astimezone(ZoneInfo("UTC")), s.astimezone(ZoneInfo("UTC")) + timedelta(hours=1), spans
        # 24h "at 15:00" without tz -> UTC
        m2 = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", line)
        if m2:
            s = datetime.combine(ref_date, dtime(int(m2.group(1)), int(m2.group(2))), ZoneInfo("UTC"))
            spans.append(m2.span())
            return s, s + timedelta(hours=1), spans
        # tz phrase only, e.g. "7-9pm Portugal time" handled above; dayparts
        for word, hr in DAYPART.items():
            m3 = re.search(rf"\b{word}\b", line, re.I)
            if m3:
                s = datetime.combine(ref_date, dtime(hr), ZoneInfo("UTC"))
                spans.append(m3.span())
                return s, s + timedelta(hours=2), spans
        return None, None, spans
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    tz = get_tz(m.group(4) or "utc", ref_date)
    s = datetime.combine(ref_date, dtime(h, int(m.group(2) or 0)), tz).astimezone(ZoneInfo("UTC"))
    spans.append(m.span())
    return s, s + timedelta(hours=1), spans


def clean_title(line, spans, post_id):
    """Remove matched date/time text and boilerplate; return title + location."""
    keep = line
    for a, b in sorted(spans, reverse=True):
        keep = keep[:a] + " " + keep[b:]
    # location hints before we mangle the line further
    loc = None
    ml = re.search(r"\birl\s+in\s+([A-Z][A-Za-z' .]+?)(?=[,:;.]|$|\s+in\s)", keep)
    if not ml:
        ml = re.search(r"\bin\s+(Paris|Berlin|Barcelona|Munich|Brussels|NYC|New York|Lisbon|Portugal|Maine|London|Elseworth)\b", keep)
    if ml:
        loc = ml.group(1).strip(" ,.:;")
    keep = re.sub(r"https?://\S+|t\.me/\S+", " ", keep, flags=re.I)
    keep = re.sub(rf"\b({MONTH_RE})\.?\s*\d{{1,2}}{ORD}", " ", keep, flags=re.I)
    keep = re.sub(rf"\b\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)\s*({'|'.join(TZMAP)})?\b", " ", keep, flags=re.I)
    keep = re.sub(rf"\b({'|'.join(WEEKDAYS)})\b", " ", keep, flags=re.I)
    keep = re.sub(r"\b(tomorrow|today|tonight|yesterday|live now|happening|online|irl|recorded|starting|starts|starts now)\b", " ", keep, flags=re.I)
    keep = re.sub(r"\bin\s+half\s+an\s+hour\b", " ", keep, flags=re.I)
    keep = re.sub(r"\b(this|next|coming|on|at|in|the|a|an|and|until|by)\b", " ", keep, flags=re.I)
    keep = re.sub(r"[^\w '&/+@.\u2013-]+", " ", keep)
    keep = re.sub(r"\s+", " ", keep).strip(" -–—,:;.@")
    title = keep[:90] if keep else ""
    return title, loc


def next_title(lines, i, post_id):
    """First meaningful line after i, cleaned, for date-only lines like 'live now:'."""
    for j in range(i + 1, min(i + 3, len(lines))):
        cand = lines[j].strip(" -–—,:;.")
        if cand and not STOP_LINE.match(cand) and len(cand) > 3:
            title, _ = clean_title(cand, [], post_id)
            return (title or cand)[:90]
    return ""


def line_dates(line, post_dt):
    """Extract (start_date_or_dt, end, all_day, spans) candidates from one line."""
    found = []
    post_date = post_dt.date()

    # cross-month range "Oct22-Nov22" / "Oct 22 - Nov 22"
    for m in re.finditer(
        rf"\b({MONTH_RE})\.?\s*(\d{{1,2}}){ORD}\s*[-–]\s*({MONTH_RE})\.?\s*(\d{{1,2}}){ORD}",
        line, re.I):
        y1 = infer_year(month_num(m.group(1)), int(m.group(2)), post_date)
        d1 = date(y1, month_num(m.group(1)), int(m.group(2)))
        y2 = infer_year(month_num(m.group(3)), int(m.group(4)), post_date)
        d2 = date(y2, month_num(m.group(3)), int(m.group(4)))
        if d2 < d1:
            d2 = date(d1.year + 1, d2.month, d2.day)
        found.append((d1, d2, m.span()))
    if found:
        return found

    # same-month range / single "Month D[-D2][, YYYY]" or "D Month"
    pat = re.compile(
        rf"(?:\b({MONTH_RE})\.?\s*(\d{{1,2}}){ORD}(?:\s*[-–]\s*(\d{{1,2}}){ORD})?(?:\s*,?\s*(20\d{{2}}))?"
        rf"|\b(\d{{1,2}}){ORD}\s+({MONTH_RE})(?:\s*,?\s*(20\d{{2}}))?)",
        re.I)
    for m in pat.finditer(line):
        if m.group(1):
            mo = month_num(m.group(1))
            d1 = int(m.group(2))
            d2 = int(m.group(3)) if m.group(3) else None
            yr = int(m.group(4)) if m.group(4) else None
        else:
            d1 = int(m.group(5))
            mo = month_num(m.group(6))
            d2 = None
            yr = int(m.group(7)) if m.group(7) else None
        try:
            y = infer_year(mo, d1, post_date, yr)
            start = date(y, mo, d1)
            end = date(y, mo, d2) if d2 else start
            if end < start:
                start, end = end, start
        except ValueError:
            continue
        found.append((start, end, m.span()))
    if found:
        return found

    # weekday + day number: "Wednesday 29"
    m = re.search(
        rf"\b({'|'.join(WEEKDAYS)})\s+(\d{{1,2}}){ORD}\b", line, re.I)
    if m:
        wd = WEEKDAYS[m.group(1).lower()]
        day = int(m.group(2))
        for delta in range(-45, 46):
            cand = post_date + timedelta(days=delta)
            if cand.day == day and cand.weekday() == wd:
                return [(cand, cand, m.span())]

    # bare weekday with a time on the line: "thursday 12pmEST"
    if re.search(r"\d{1,2}\s*(?::\d{2})?\s*(?:am|pm)\b|\b[01]?\d:[0-5]\d\b", line, re.I):
        m = re.search(rf"\b({'|'.join(WEEKDAYS)})s?\b", line, re.I)
        if m:
            wd = WEEKDAYS[m.group(1).lower()]
            delta = (wd - post_date.weekday()) % 7 or 7
            if delta <= 10:
                cand = post_date + timedelta(days=delta)
                return [(cand, cand, m.span())]

    # bare month: "in November" -> 1st of that month
    m = re.search(rf"\bin\s+({MONTH_RE})\b(?!\s*\d)", line, re.I)
    if m:
        mo = month_num(m.group(1))
        y = infer_year(mo, 1, post_date)
        return [(date(y, mo, 1), date(y, mo, 1), m.span())]
    return found


def relative_events(lines, post_dt):
    """'live now', 'tomorrow 7pm', 'in half an hour', '35 hours left', 'today'.

    A single line can carry several cues ("tonight: co-working - tomorrow
    4pmEST ..."), so each matching cue produces its own candidate."""
    out = []
    for i, line in enumerate(lines):
        ll = line.lower()
        cands = []  # (base: date|datetime, all_day, deadline)
        if re.search(r"\blive now\b|\bhappening live\b", ll):
            cands.append((post_dt, False, False))
        if re.search(r"\bin\s+half\s+an\s+hour\b", ll):
            cands.append((post_dt + timedelta(minutes=30), False, False))
        for m in re.finditer(r"\bin\s+(\d+)\s+minutes?\b", ll):
            cands.append((post_dt + timedelta(minutes=int(m.group(1))), False, False))
        for m in re.finditer(r"(\d+)\s+(?:hours?|hrs?)\s+left\b", ll):
            cands.append((post_dt + timedelta(hours=int(m.group(1))), False, True))
        for m in re.finditer(r"(\d+)\s+min(?:utes?)?\s+left\b", ll):
            cands.append((post_dt + timedelta(minutes=int(m.group(1))), False, True))
        for m in re.finditer(r"(\d+)\s+days?\s+left\b", ll):
            cands.append((post_dt + timedelta(days=int(m.group(1))), False, True))
        for m in re.finditer(rf"\bend\s+of\s+({MONTH_RE})\b", ll):
            mo = month_num(m.group(1))
            y = infer_year(mo, 28, post_dt.date())
            cands.append((date(y + (1 if mo == 12 else 0), 1 if mo == 12 else mo + 1, 1)
                          - timedelta(days=1), True, True))
        for m in re.finditer(rf"\b(?:until|till|by)\s+(?:the\s+)?(\d{{1,2}}){ORD}\b", ll):
            day = int(m.group(1))
            try:
                d = date(post_dt.year, post_dt.month, day)
                if d < post_dt.date():
                    d = date(post_dt.year + (post_dt.month == 12),
                             1 if post_dt.month == 12 else post_dt.month + 1, day)
                cands.append((d, True, True))
            except ValueError:
                pass
        for m in re.finditer(rf"\b(?:until|till)\s+({'|'.join(WEEKDAYS)})s?\b", ll):
            delta = (WEEKDAYS[m.group(1)] - post_dt.weekday()) % 7 or 7
            cands.append((post_dt.date() + timedelta(days=delta), True, True))
        day_after = re.search(r"\bday after tomorrow\b", ll)
        if day_after:
            cands.append((post_dt.date() + timedelta(days=2), True, False))
        if re.search(r"\btomorrow\b", ll) and not day_after:
            cands.append((post_dt.date() + timedelta(days=1), True, False))
        if re.search(r"\btonight\b", ll):
            cands.append((datetime.combine(post_dt.date(), dtime(20), ZoneInfo("UTC")), False, False))
        if re.search(r"\bclosing today\b|\btoday\b", ll):
            cands.append((post_dt.date(), True,
                          "closing" in ll or "deadline" in ll))
        for base, allday, deadline in cands:
            if isinstance(base, datetime) and not allday:
                base = base if base.tzinfo else base.replace(tzinfo=ZoneInfo("UTC"))
                s, e, tspans = parse_times(line, base.date())
                start = s if s else base
                end = e if e and e > start else start + timedelta(hours=1)
                out.append({"start": start, "end": end, "all_day": False,
                            "line_i": i, "spans": [], "deadline": deadline})
            else:
                d = base.date() if isinstance(base, datetime) else base
                s, e, tspans = parse_times(line, d)
                # thin cue line ("tomorrow, June15th:") -> time+name on next line
                if not s and len(line) < 50 and i + 1 < len(lines):
                    s2, e2, _ = parse_times(lines[i + 1], d)
                    if s2:
                        s, e = s2, e2
                if s:
                    out.append({"start": s, "end": e, "all_day": False,
                                "line_i": i, "spans": [], "deadline": deadline})
                else:
                    out.append({"start": d, "end": d, "all_day": True,
                                "line_i": i, "spans": [], "deadline": deadline})
    return out


def extract_events(post):
    post_dt = datetime.fromisoformat(post["dt"]).astimezone(ZoneInfo("UTC"))
    lines = [l.strip() for l in post["text"].split("\n") if l.strip()]
    rel_lines = {e["line_i"] for e in relative_events(lines, post_dt)}
    events = []
    def fill_title(title, loc, i):
        if title and len(title) >= 12 and title != loc:
            return title
        nt = next_title(lines, i, post["id"])
        if not nt:
            return title
        return nt if not title or title == loc else f"{title} — {nt}"

    for ev in relative_events(lines, post_dt):
        line = lines[ev["line_i"]]
        title, loc = clean_title(line, ev["spans"], post["id"])
        title = fill_title(title, loc, ev["line_i"])
        events.append({**ev, "title": title, "loc": loc, "line": line})
    for i, line in enumerate(lines):
        if i in rel_lines or STOP_LINE.match(line):
            continue
        for (d1, d2, span) in line_dates(line, post_dt):
            s, e, tspans = parse_times(line, d1)
            spans = [span] + tspans
            title, loc = clean_title(line, spans, post["id"])
            title = fill_title(title, loc, i)
            if s:
                events.append({"start": s, "end": e if e > s else s + timedelta(hours=1),
                               "all_day": False, "line_i": i, "line": line,
                               "title": title, "loc": loc, "deadline": False})
            else:
                # date-only line ("Oct 28", "June15th") -> time+name often sit
                # on the following line ("9am-1pm: SheFi Berlin")
                remainder = re.sub(rf"\b({'|'.join(WEEKDAYS)})s?\b|\W+", " ",
                                   line[:span[0]] + line[span[1]:], flags=re.I).strip()
                if len(remainder) < 10 and i + 1 < len(lines):
                    s2, e2, tspans2 = parse_times(lines[i + 1], d1)
                    if s2:
                        t2, loc2 = clean_title(lines[i + 1], tspans2, post["id"])
                        events.append({
                            "start": s2, "end": e2 if e2 > s2 else s2 + timedelta(hours=1),
                            "all_day": False, "line_i": i, "line": lines[i + 1],
                            "title": t2 or title, "loc": loc2 or loc, "deadline": False})
                        break
                events.append({"start": d1, "end": d2, "all_day": True,
                               "line_i": i, "line": line, "title": title,
                               "loc": loc, "deadline": False})
            break  # one date expression per line is enough
    # dedupe same (start, title-ish)
    seen, dedup = set(), []
    for ev in events:
        key = (str(ev["start"]), re.sub(r"\W+", "", ev["title"].lower())[:40])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(ev)
    return post_dt, dedup


def ev_uid(post_id, idx, ev):
    h = hashlib.sha1(f"{post_id}|{ev['start']}|{ev['title']}".encode()).hexdigest()[:10]
    return f"civilmonkey-{post_id}-{idx}-{h}@telegram"


def esc(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    """RFC 5545 75-octet folding (UTF-8 aware)."""
    out, cur = [], ""
    for ch in line:
        if len(cur.encode()) + len(ch.encode()) > 74:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def fmt_dt(v, all_day, is_end=False):
    if all_day:
        d = v if isinstance(v, date) and not isinstance(v, datetime) else v.date()
        return f"VALUE=DATE:{d.strftime('%Y%m%d')}"
    return ":" + v.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def build_ics(store):
    now = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//civilmonkey-ics//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:civil notes (t.me/civilmonkey)",
        "X-WR-CALDESC:Events extracted from t.me/civilmonkey channel posts",
        "X-WR-TIMEZONE:UTC",
        "X-PUBLISHED-TTL:PT1H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
    ]
    for pid in sorted(store["posts"], key=int):
        p = store["posts"][pid]
        for i, ev in enumerate(p["events"]):
            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:{ev['uid']}")
            lines.append(f"DTSTAMP:{now}")
            if ev["all_day"]:
                d1 = date.fromisoformat(ev["start"][:10])
                d2 = date.fromisoformat(ev["end"][:10])
                lines.append(f"DTSTART;VALUE=DATE:{d1:%Y%m%d}")
                lines.append(f"DTEND;VALUE=DATE:{d2 + timedelta(days=1):%Y%m%d}")
            else:
                s = datetime.fromisoformat(ev["start"])
                e = datetime.fromisoformat(ev["end"])
                lines.append(f"DTSTART:{s.astimezone(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}")
                lines.append(f"DTEND:{e.astimezone(ZoneInfo('UTC')):%Y%m%dT%H%M%SZ}")
            title = ev["title"] or "(see post)"
            if ev.get("deadline"):
                title = "Deadline: " + title
            lines.append(fold("SUMMARY:" + esc(title)))
            desc = ev.get("line", "")
            if p.get("text"):
                body = p["text"].strip()
                if len(body) > 1200:
                    body = body[:1200] + "…"
                desc = desc + "\\n\\n" + body if desc else body
            desc += f"\\n\\nSource: {POST_URL}{pid}"
            lines.append(fold("DESCRIPTION:" + esc(desc)))
            lines.append(f"URL:{POST_URL}{pid}")
            if ev.get("loc"):
                lines.append(fold("LOCATION:" + esc(ev["loc"])))
            lines.append(f"CATEGORIES:{esc('civil notes')}")
            lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def ev_to_json(ev, uid):
    def ser(v):
        return v.isoformat()
    return {"uid": uid, "start": ser(ev["start"]), "end": ser(ev["end"]),
            "all_day": ev["all_day"], "title": ev["title"],
            "loc": ev["loc"], "line": ev["line"], "deadline": ev["deadline"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--pages", type=int, default=15)
    b.add_argument("--output", default="civilmonkey.ics")
    b.add_argument("--state", default=".civilmonkey_state.json")
    b.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        store = json.load(open(args.state))
    except (OSError, json.JSONDecodeError):
        store = {"posts": {}}

    posts = scrape_posts(args.pages)
    n_new = 0
    for post in posts:
        pid = str(post["id"])
        post_dt, events = extract_events(post)
        old = store["posts"].get(pid, {})
        if old.get("text") == post["text"] and "events" in old:
            continue  # unchanged
        store["posts"][pid] = {
            "text": post["text"],
            "posted": post["dt"],
            "events": [ev_to_json(ev, ev_uid(post["id"], i, ev))
                       for i, ev in enumerate(events)],
        }
        n_new += 1

    ics = build_ics(store)
    with open(args.output, "w", newline="") as f:
        f.write(ics)
    with open(args.state, "w") as f:
        json.dump(store, f, indent=1)

    n_events = sum(len(p["events"]) for p in store["posts"].values())
    if not args.quiet:
        print(f"scraped {len(posts)} posts ({n_new} new/changed), "
              f"{len(store['posts'])} posts tracked, {n_events} events -> {args.output}")


if __name__ == "__main__":
    main()
