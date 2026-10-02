#!/usr/bin/env python3
"""
Daily Indian Bird Agent
-----------------------
1. Builds the bird universe from eBird (Cornell Lab) regional checklists + taxonomy.
2. Picks ONE species per day (never repeats; varies family/genus; season-aware).
3. Pulls the Wikipedia article + Wikimedia Commons photos (with credits) + IUCN status (GBIF).
4. Uses Claude to write a structured card STRICTLY from the fetched text, then a second
   Claude pass fact-checks the card against the same text and strips unsupported claims.
5. Emails a formatted HTML message and appends a row to a Google Sheet (your tracker).

Usage:  python agent.py            # real run
        python agent.py --dry-run  # builds the email to preview.html; no email, no sheet write
"""
import csv
import datetime as dt
import html
import json
import os
import random
import re
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote

import requests

# ----------------------------------------------------------------------------- config
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TODAY = dt.datetime.now(IST).date()

EBIRD_KEY = os.environ.get("EBIRD_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = (os.environ.get("OPENAI_MODEL") or "gpt-4o-mini")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER") or ("openai" if OPENAI_KEY and not os.environ.get("ANTHROPIC_API_KEY") else "anthropic")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", GMAIL_USER)
SHEET_ID = os.environ.get("SHEET_ID", "")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
SCOPE = os.environ.get("SCOPE", "auto").lower()  # south | india | auto
FORCE = os.environ.get("FORCE", "") == "1"
DRY_RUN = "--dry-run" in sys.argv or os.environ.get("DRY_RUN", "") == "1"

UA = {"User-Agent": f"DailyIndianBirdAgent/1.0 (personal educational project; {GMAIL_USER or 'no-contact'})"}

# eBird region codes: Kerala, Tamil Nadu, Karnataka, Andhra Pradesh, Telangana, Puducherry, Lakshadweep
SOUTH_REGIONS = ["IN-KL", "IN-TN", "IN-KA", "IN-AP", "IN-TG", "IN-PY", "IN-LD"]

# Families with many migrants: nudged up in the Sep-Apr migration season.
MIGRANT_FAMILIES = {
    "Scolopacidae", "Charadriidae", "Anatidae", "Laridae", "Sternidae", "Motacillidae",
    "Phylloscopidae", "Acrocephalidae", "Muscicapidae", "Accipitridae", "Falconidae",
    "Hirundinidae", "Locustellidae", "Cuculidae", "Oriolidae", "Threskiornithidae",
}

BOOKS = [
    "Birds of the Indian Subcontinent - Grimmett, Inskipp & Inskipp",
    "Birds of South Asia: The Ripley Guide - Rasmussen & Anderton",
    "The Book of Indian Birds - Salim Ali",
    "A Field Guide to the Birds of India - Krys Kazmierczak",
]

HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------------- eBird
def ebird(path, params=None):
    r = requests.get("https://api.ebird.org/v2" + path, params=params,
                     headers={"X-eBirdApiToken": EBIRD_KEY}, timeout=60)
    r.raise_for_status()
    return r.json()


def load_universe():
    tax = ebird("/ref/taxonomy/ebird", {"fmt": "json", "cat": "species", "locale": "en"})
    by_code = {t["speciesCode"]: t for t in tax}
    south = set()
    for reg in SOUTH_REGIONS:
        south |= set(ebird(f"/product/spplist/{reg}"))
    india = set(ebird("/product/spplist/IN"))
    south &= by_code.keys()
    india &= by_code.keys()
    return by_code, south, india


def family_of(t):
    return (t.get("familySciName") or "").split(" ")[0]


def genus_of(t):
    return t["sciName"].split()[0]


# ----------------------------------------------------------------------------- tracker (Google Sheet / CSV)
HEADERS = [
    "Date", "Day #", "Status", "Species Code", "Common Name", "Scientific Name", "Family", "Genus",
    "Scope", "Migratory Status", "IUCN", "Range in India", "Male vs Female", "Breeding Season",
    "Nesting", "Fun Facts", "Related Species Shown", "Wikipedia URL", "eBird URL", "Fact-check removed", "Note",
]


class Tracker:
    def __init__(self):
        self.ws = None
        self.csv_path = os.path.join(HERE, "data", "log.csv")
        os.makedirs(os.path.dirname(self.csv_path), exist_ok=True)
        if SHEET_ID and SA_JSON:
            import gspread
            gc = gspread.service_account_from_dict(json.loads(SA_JSON))
            self.ws = gc.open_by_key(SHEET_ID).sheet1
            if self.ws.row_values(1) != HEADERS:
                if not self.ws.get_all_values():
                    self.ws.append_row(HEADERS)
                else:
                    self.ws.insert_row(HEADERS, 1)
        elif not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(HEADERS)

    def rows(self):
        if self.ws:
            return self.ws.get_all_records()
        with open(self.csv_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def append(self, row: dict):
        vals = [str(row.get(h, "")) for h in HEADERS]
        if self.ws:
            self.ws.append_row(vals, value_input_option="USER_ENTERED")
        else:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(vals)


# ----------------------------------------------------------------------------- selection
def load_common_names():
    p = os.path.join(HERE, "data", "familiar_birds.txt")
    if not os.path.exists(p):
        print("Note: data/familiar_birds.txt not found in repo; continuing without it.")
        return set()
    with open(p, encoding="utf-8") as f:
        return {l.strip().lower() for l in f if l.strip() and not l.startswith("#")}


def ranked_candidates(by_code, south, india, rows):
    done = {r["Species Code"] for r in rows if r["Status"] in ("sent", "skipped")}
    south_left = south - done
    if SCOPE == "south":
        pool, phase = south, "South India"
    elif SCOPE == "india":
        pool, phase = india, "All India"
    else:
        if len(south_left) >= 15:
            pool, phase = south, "South India"
        else:
            pool, phase = india | south, "All India"
    pool = pool - done

    familiar = load_common_names()
    fresh = [c for c in pool if by_code[c]["comName"].lower() not in familiar]
    candidates = fresh if fresh else list(pool)  # familiar birds only when nothing else is left

    recent = [r for r in rows if r["Status"] == "sent"]
    fam_recent = {r["Family"] for r in recent[-14:]}
    gen_recent = {r["Genus"] for r in recent[-30:]}
    month = TODAY.month
    migrating = month >= 9 or month <= 4
    boost = 2.0 if month in (10, 11, 12, 1, 2, 3) else 1.5

    weights = []
    for c in candidates:
        t = by_code[c]
        w = 1.0
        if family_of(t) in fam_recent:
            w *= 0.1
        if genus_of(t) in gen_recent:
            w *= 0.05
        if migrating and family_of(t) in MIGRANT_FAMILIES:
            w *= boost
        weights.append(w)

    rng = random.Random(TODAY.toordinal())
    order = []
    cands, ws = candidates[:], weights[:]
    while cands and len(order) < 12:
        i = rng.choices(range(len(cands)), weights=ws)[0]
        order.append(cands.pop(i))
        ws.pop(i)
    return order, phase


# ----------------------------------------------------------------------------- sources: Wikipedia / Commons / GBIF
def wp(params):
    r = requests.get("https://en.wikipedia.org/w/api.php",
                     params={**params, "format": "json", "formatversion": 2}, headers=UA, timeout=40)
    r.raise_for_status()
    return r.json()


def fetch_page(title):
    d = wp({"action": "query", "prop": "extracts|info", "explaintext": 1, "exsectionformat": "plain",
            "inprop": "url", "titles": title, "redirects": 1})
    p = d["query"]["pages"][0]
    if p.get("missing") or not p.get("extract"):
        return None
    return {"title": p["title"], "url": p["fullurl"], "text": p["extract"]}


def find_page(sci, com):
    """Return a Wikipedia page only if its text contains the scientific name AND mentions India."""
    for q in (f'"{sci}"', f"{com} bird"):
        hits = wp({"action": "query", "list": "search", "srsearch": q, "srlimit": 3})["query"]["search"]
        for h in hits:
            page = fetch_page(h["title"])
            if not page:
                continue
            low = page["text"].lower()
            if sci.lower() in low and ("india" in low or "south asia" in low):
                return page
    return None


BAD_IMG = re.compile(r"map|range|distribution|status|iucn|symbol|logo|icon|\.svg|\.ogg|\.png$|\.gif|"
                     r"wikispecies|commons-|question_book|skull|egg|track", re.I)


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def get_images(title, n=3):
    try:
        r = requests.get("https://en.wikipedia.org/api/rest_v1/page/media-list/" + quote(title.replace(" ", "_")),
                         headers=UA, timeout=40)
        r.raise_for_status()
        files = []
        for i in r.json().get("items", []):
            if i.get("type") != "image":
                continue
            t = i.get("title", "")
            t = t if t.startswith("File:") else "File:" + t
            if not BAD_IMG.search(t):
                files.append(t)
        if not files:
            return []
        d = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=40, params={
            "action": "query", "titles": "|".join(files[:10]), "prop": "imageinfo",
            "iiprop": "url|extmetadata", "iiurlwidth": 800, "format": "json", "formatversion": 2}).json()
        out = []
        for p in d.get("query", {}).get("pages", []):
            ii = (p.get("imageinfo") or [{}])[0]
            if not ii.get("thumburl"):
                continue
            md = ii.get("extmetadata", {})
            out.append({
                "src": ii["thumburl"],
                "page": ii.get("descriptionurl", ""),
                "artist": strip_tags(md.get("Artist", {}).get("value", "Unknown"))[:80],
                "license": strip_tags(md.get("LicenseShortName", {}).get("value", "")),
            })
        return out[:n]
    except Exception as e:  # images are nice-to-have
        print("  image fetch failed:", e, flush=True)
        return []


def iucn_status(sci):
    names = {"LEAST_CONCERN": "Least Concern", "NEAR_THREATENED": "Near Threatened", "VULNERABLE": "Vulnerable",
             "ENDANGERED": "Endangered", "CRITICALLY_ENDANGERED": "Critically Endangered",
             "DATA_DEFICIENT": "Data Deficient", "EXTINCT_IN_THE_WILD": "Extinct in the Wild",
             "LC": "Least Concern", "NT": "Near Threatened", "VU": "Vulnerable", "EN": "Endangered",
             "CR": "Critically Endangered", "DD": "Data Deficient"}
    try:
        m = requests.get("https://api.gbif.org/v1/species/match", params={"name": sci, "kingdom": "Animalia"},
                         headers=UA, timeout=30).json()
        key = m.get("usageKey")
        r = requests.get(f"https://api.gbif.org/v1/species/{key}/iucnRedListCategory", headers=UA, timeout=30).json()
        return names.get(r.get("category"), r.get("category")) or None
    except Exception:
        return None


def thumb_for(sci):
    try:
        d = wp({"action": "query", "generator": "search", "gsrsearch": f'"{sci}"', "gsrlimit": 1,
                "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 240})
        pg = d.get("query", {}).get("pages", [])
        return pg[0]["thumbnail"]["source"] if pg and "thumbnail" in pg[0] else None
    except Exception:
        return None


def relatives(by_code, featured_code, india, south, limit=6):
    """Other Indian species of the same genus (else same family) -> 'different varieties' section."""
    t = by_code[featured_code]
    same = [c for c in india if c != featured_code and genus_of(by_code[c]) == genus_of(t)]
    label = f"other {genus_of(t)} species in India"
    if len(same) < 2:
        same = [c for c in india if c != featured_code and family_of(by_code[c]) == family_of(t)]
        label = f"cousins from the same family ({t.get('familyComName', family_of(t))})"
    same.sort(key=lambda c: by_code[c]["taxonOrder"])
    out = []
    for c in same[:limit]:
        x = by_code[c]
        out.append({"code": c, "name": x["comName"], "sci": x["sciName"], "in_south": c in south,
                    "thumb": thumb_for(x["sciName"])})
    return out, label


# ----------------------------------------------------------------------------- Claude: write + fact-check
def _chat_compat(url, key, model, system, user):
    """OpenAI-style chat call (works for Gemini's OpenAI-compatible endpoint and OpenAI). Retries on 429."""
    import time
    body = {"model": model, "temperature": 0.2, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    for attempt in range(3):
        print(f"    [LLM call attempt {attempt + 1}]", flush=True)
        r = requests.post(url, headers={"Authorization": "Bearer " + key}, json=body, timeout=60)
        if r.status_code == 400 and "response_format" in body:
            body.pop("response_format")  # some models reject JSON mode; prompt already demands JSON
            continue
        if r.status_code in (429, 503):
            print(f"    rate-limited ({r.status_code}), waiting 8s", flush=True)
            time.sleep(8)
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"LLM error {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def claude_json(system, user, max_tokens=2500):
    """Provider order: LLM_PROVIDER env, else Gemini (free) if key set, else OpenAI, else Anthropic."""
    env = os.environ.get
    provider = (env("LLM_PROVIDER") or "").lower()
    if not provider:
        provider = "gemini" if env("GEMINI_API_KEY") else "openai" if env("OPENAI_API_KEY") else "anthropic"
    if provider == "gemini":
        txt = _chat_compat("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                           env("GEMINI_API_KEY"), env("GEMINI_MODEL") or "gemini-flash-latest", system, user)
    elif provider == "openai":
        txt = _chat_compat("https://api.openai.com/v1/chat/completions", env("OPENAI_API_KEY"),
                           env("OPENAI_MODEL") or "gpt-4o-mini", system, user)
    else:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                     messages=[{"role": "user", "content": user}])
        txt = "".join(b.text for b in msg.content if b.type == "text")
    txt = re.sub(r"^```(?:json)?|```$", "", txt.strip(), flags=re.M).strip()
    return json.loads(txt)


SCHEMA = """{
 "tagline": "one vivid sentence introducing the bird",
 "migratory_status": "Resident | Winter visitor | Summer visitor | Passage migrant | Local/altitudinal migrant | Vagrant | Unknown",
 "status_note": "1 sentence on where/when it occurs in India (say so if source is about other regions)",
 "identification": "key field marks, size",
 "habitat": "...",
 "range_india": "where in India / south India it is found",
 "male_vs_female": "how males and females differ (or say they look alike) - null if not stated",
 "breeding_season": "when it breeds; if breeding happens outside India say so - null if not stated",
 "nesting": "nest type, site, eggs, who incubates - null if not stated",
 "diet": "...",
 "voice": "...",
 "fun_facts": ["3 to 5 interesting facts, each clearly stated in the source"]
}"""

WRITER_SYS = (
    "You are a careful ornithology editor for a birding newsletter for an Indian enthusiast. "
    "Use ONLY the SOURCE TEXT provided. Never use outside knowledge and never guess. "
    "If the source does not state something, output null for that field. "
    "If the source describes something for another region (e.g. breeding in Europe), say that explicitly "
    "rather than presenting it as Indian. Write in your own words, warm and clear, no long verbatim quotes. "
    "Return ONLY valid JSON matching the schema."
)

CHECK_SYS = (
    "You are a strict fact-checker. You get a SOURCE TEXT and a DRAFT JSON card. Remove or null out every claim "
    "in the draft that is not directly supported by the source. Do not add new information. Fix claims that "
    "overstate the source. Return ONLY JSON: {\"card\": <same schema as draft>, \"removed\": [\"short description of each removed/changed claim\"]}."
)


def build_card(page, common, sci):
    text = page["text"][:20000]
    user = f"Bird: {common} ({sci})\n\nSCHEMA:\n{SCHEMA}\n\nSOURCE TEXT (Wikipedia: {page['title']}):\n{text}"
    draft = claude_json(WRITER_SYS, user)
    checked = claude_json(CHECK_SYS, f"SOURCE TEXT:\n{text}\n\nDRAFT:\n{json.dumps(draft)}")
    return checked["card"], checked.get("removed", [])


# ----------------------------------------------------------------------------- email
NA = "<i style='color:#888'>Not well documented in our sources.</i>"


def sec(title, body, emoji=""):
    if not body:
        return ""
    return (f"<tr><td style='padding:10px 0 2px;font-weight:700;color:#1b5e20'>{emoji} {html.escape(title)}</td></tr>"
            f"<tr><td style='padding:0 0 8px;line-height:1.55'>{body}</td></tr>")


def render_email(meta, card, images, rel, rel_label, removed, sources):
    e = html.escape
    imgs = "".join(
        f"<div style='margin:8px 0'><img src='{e(i['src'])}' style='width:100%;max-width:560px;border-radius:8px'>"
        f"<div style='font-size:11px;color:#777'>Photo: {e(i['artist'])} · {e(i['license'])} · "
        f"<a href='{e(i['page'])}' style='color:#777'>Wikimedia Commons</a></div></div>" for i in images
    ) or "<i style='color:#888'>No freely-licensed photo found; see the eBird/Macaulay links below.</i>"

    facts = "".join(f"<li style='margin-bottom:4px'>{e(f)}</li>" for f in (card.get("fun_facts") or []))
    facts = f"<ul style='margin:4px 0 0 18px;padding:0'>{facts}</ul>" if facts else ""

    def val(k):
        v = card.get(k)
        return e(v) if v else None

    rel_html = "".join(
        f"<td style='padding:6px;width:33%;vertical-align:top;font-size:12px;text-align:center'>"
        + (f"<img src='{e(r['thumb'])}' style='width:100%;border-radius:6px'><br>" if r["thumb"] else "")
        + f"<a href='https://ebird.org/species/{e(r['code'])}' style='color:#1b5e20;font-weight:600'>{e(r['name'])}</a>"
        f"<br><i>{e(r['sci'])}</i>" + ("<br>📍 seen in South India" if r["in_south"] else "") + "</td>"
        for r in rel
    )
    rel_rows = ""
    cells = re.findall(r"<td.*?</td>", rel_html, flags=re.S)
    for i in range(0, len(cells), 3):
        rel_rows += "<tr>" + "".join(cells[i:i + 3]) + "</tr>"
    rel_block = (f"<div style='font-weight:700;color:#1b5e20;margin-top:14px'>🔍 Know the family: {e(rel_label)}</div>"
                 f"<table width='100%' style='margin-top:4px'>{rel_rows}</table>") if rel else ""

    src = "".join(f"<li><a href='{e(u)}'>{e(n)}</a></li>" for n, u in sources)
    books = "".join(f"<li>{e(b)}</li>" for b in BOOKS)
    fc = f"{len(removed)} unsupported statement(s) removed by the fact-check pass." if removed else "Fact-check pass: nothing removed."

    body = f"""<html><body style="margin:0;background:#f3f6f1;font-family:Segoe UI,Arial,sans-serif;color:#222">
<table width="100%"><tr><td align="center" style="padding:16px">
<table width="600" style="max-width:600px;background:#fff;border-radius:12px;padding:22px">
<tr><td style="font-size:12px;color:#777">Bird of the Day #{meta['day']} · {meta['date']} · Phase: {e(meta['phase'])}</td></tr>
<tr><td style="font-size:26px;font-weight:800;padding-top:4px">{e(meta['common'])}</td></tr>
<tr><td style="font-style:italic;color:#555">{e(meta['sci'])} · Family {e(meta['family'])}</td></tr>
<tr><td style="padding:8px 0">
 <span style="background:#e8f5e9;color:#1b5e20;border-radius:12px;padding:3px 10px;font-size:12px">{e(card.get('migratory_status') or 'Status unknown')}</span>
 {'<span style="background:#fff3e0;color:#e65100;border-radius:12px;padding:3px 10px;font-size:12px">IUCN: ' + e(meta['iucn']) + '</span>' if meta.get('iucn') else ''}
</td></tr>
<tr><td style="font-size:15px;padding-bottom:6px">{e(card.get('tagline') or '')}</td></tr>
<tr><td>{imgs}</td></tr>
{sec('Where & when in India', ' '.join(filter(None, [val('range_india'), val('status_note')])), '🗺️')}
{sec('How to identify', val('identification'), '👀')}
{sec('Habitat', val('habitat'), '🌿')}
{sec('Male vs Female', val('male_vs_female') or NA, '♂️♀️')}
{sec('Breeding season', val('breeding_season') or NA, '💞')}
{sec('Nesting', val('nesting') or NA, '🪺')}
{sec('Diet', val('diet'), '🍽️')}
{sec('Voice', val('voice'), '🎵')}
{sec('Interesting facts', facts, '✨')}
<tr><td>{rel_block}</td></tr>
<tr><td style="padding-top:14px;font-size:13px">
 <a href="https://ebird.org/species/{e(meta['code'])}">eBird species page</a> ·
 <a href="https://media.ebird.org/catalog?taxonCode={e(meta['code'])}&region=India">More photos (Macaulay Library)</a> ·
 <a href="https://xeno-canto.org/explore?query={quote(meta['sci'])}">Hear its calls (xeno-canto)</a></td></tr>
<tr><td style="padding-top:14px;font-size:11px;color:#777;border-top:1px solid #eee;margin-top:14px">
 <b>Sources:</b><ul style="margin:2px 0 6px 16px">{src}</ul>
 {e(fc)}<br><b>Go deeper (books):</b><ul style="margin:2px 0 0 16px">{books}</ul>
 Text is machine-written strictly from the sources above and auto-fact-checked; always confirm important details in a field guide.
</td></tr></table></td></tr></table></body></html>"""
    plain = f"Bird of the Day #{meta['day']}: {meta['common']} ({meta['sci']})\n{card.get('tagline') or ''}\nSee HTML version for details."
    return body, plain


def send_email(subject, html_body, plain):
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, EMAIL_TO
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_USER, [EMAIL_TO], msg.as_string())


# ----------------------------------------------------------------------------- main
def main():
    tracker = Tracker()
    rows = tracker.rows()
    today = TODAY.isoformat()
    if not FORCE and not DRY_RUN and any(r["Date"] == today and r["Status"] == "sent" for r in rows):
        print("Already sent today. Set FORCE=1 to override.", flush=True)
        return

    by_code, south, india = load_universe()
    order, phase = ranked_candidates(by_code, south, india, rows)
    day = sum(1 for r in rows if r["Status"] == "sent") + 1

    for i, code in enumerate(order):
        print(f"--- candidate {i + 1}/{len(order)} ---", flush=True)
        t = by_code[code]
        common, sci = t["comName"], t["sciName"]
        print("Trying", common, sci, flush=True)
        page = find_page(sci, common)
        if not page:
            print("  no reliable source page; skipping permanently", flush=True)
            if not DRY_RUN:
                tracker.append({"Date": today, "Status": "skipped", "Species Code": code, "Common Name": common,
                                "Scientific Name": sci, "Family": family_of(t), "Genus": genus_of(t),
                                "Note": "No reliable India-relevant source article found"})
            continue
        try:
            card, removed = build_card(page, common, sci)
        except Exception as e:
            print("  LLM step failed:", e, flush=True)
            continue
        if not card.get("fun_facts") and not card.get("identification"):
            print("  card too thin after fact-check; trying next species", flush=True)
            continue

        images = get_images(page["title"])
        rel, rel_label = relatives(by_code, code, india, south)
        iucn = iucn_status(sci)
        meta = {"day": day, "date": TODAY.strftime("%d %b %Y"), "phase": phase, "common": common, "sci": sci,
                "family": t.get("familyComName", family_of(t)), "code": code, "iucn": iucn}
        sources = [(f"Wikipedia: {page['title']}", page["url"]),
                   ("eBird / Cornell Lab of Ornithology (taxonomy & regional checklists)", f"https://ebird.org/species/{code}"),
                   ("GBIF (taxonomy, IUCN category)", "https://www.gbif.org/")]
        body, plain = render_email(meta, card, images, rel, rel_label, removed, sources)
        subject = f"🦜 Bird of the Day #{day}: {common} ({sci})"

        if DRY_RUN:
            with open(os.path.join(HERE, "preview.html"), "w", encoding="utf-8") as f:
                f.write(body)
            print("Dry run OK -> preview.html\nSubject:", subject, flush=True)
            return

        send_email(subject, body, plain)
        tracker.append({
            "Date": today, "Day #": day, "Status": "sent", "Species Code": code, "Common Name": common,
            "Scientific Name": sci, "Family": family_of(t), "Genus": genus_of(t), "Scope": phase,
            "Migratory Status": card.get("migratory_status") or "", "IUCN": iucn or "",
            "Range in India": card.get("range_india") or "", "Male vs Female": card.get("male_vs_female") or "",
            "Breeding Season": card.get("breeding_season") or "", "Nesting": card.get("nesting") or "",
            "Fun Facts": " | ".join(card.get("fun_facts") or []),
            "Related Species Shown": ", ".join(r["name"] for r in rel),
            "Wikipedia URL": page["url"], "eBird URL": f"https://ebird.org/species/{code}",
            "Fact-check removed": len(removed), "Note": "",
        })
        print("Sent:", subject, flush=True)
        return
    print("No suitable species found today (all candidates failed).", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
