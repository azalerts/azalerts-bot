import os
import re
import json
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, render_template, request, flash
import trafilatura

# ---------------- app & template setup ----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev")

TEMPLATES_DIR = Path(app.template_folder or "templates")

def _has_tpl(name: str) -> bool:
    return (TEMPLATES_DIR / name).is_file()

FORM_TPL   = "az_form.html"   if _has_tpl("az_form.html")   else "index.html"
RESULT_TPL = "az_result.html" if _has_tpl("az_result.html") else "az_result.html"

def render_form(**ctx):
    base = dict(error=None, used_openai=False, output_text=None, result=None)
    base.update(ctx or {})
    return render_template(FORM_TPL, **base)

def render_result(**ctx):
    base = dict(error=None, used_openai=True, output_text=None, result=None)
    base.update(ctx or {})
    return render_template(RESULT_TPL, **base)

# ---------------- helpers ----------------
def source_brand_from_url(url: str) -> str:
    from urllib.parse import urlparse
    netloc = urlparse(url).netloc.lower().replace("www.","")
    mapping = {
        "vi.nl": "Voetbal International",
        "voetbalinternational.nl": "Voetbal International",
        "nu.nl": "NU.nl",
        "nos.nl": "NOS",
        "ad.nl": "AD",
        "telegraaf.nl": "De Telegraaf",
        "parool.nl": "Het Parool",
        "volkskrant.nl": "de Volkskrant",
        "nrc.nl": "NRC",
        "rtlnieuws.nl": "RTL Nieuws",
        "rtl.nl": "RTL Nieuws",
        "bbc.com": "BBC",
        "espn.nl": "ESPN",
        "voetbalprimeur.nl": "VoetbalPrimeur",
        "voetbalzone.nl": "Voetbalzone",
        "az.nl": "AZ",
        "fcupdate.nl": "FCUpdate",
        "soccernews.nl": "SoccerNews",
    }
    if netloc in mapping:
        return mapping[netloc]
    base = netloc.split(".")[0]
    return base.capitalize() if base else netloc

def brand_alias(brand: str) -> str:
    aliases = {
        "Voetbal International": "VI",
        "VoetbalPrimeur": "VoetbalPrimeur",
        "RTL Nieuws": "RTL Nieuws",
        "De Telegraaf": "De Telegraaf",
    }
    return aliases.get(brand, brand)

def split_into_chunks(text: str, max_words: int = 1200):
    words = text.split()
    for i in range(0, len(words), max_words):
        yield " ".join(words[i:i+max_words])

def normalize_plaintext(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"<[^>]+>", "", s)               # HTML weghalen
    s = re.sub(r"^[ \t]+", "", s, flags=re.M)   # leading spaces
    s = re.sub(r"\n{3,}", "\n\n", s)            # teveel lege regels → 1
    return s.strip()

def needs_attribution(source_title: str, source_text: str) -> bool:
    """
    True voor transfer/gerucht/interview/quote/mening; False voor wedstrijdverslag/stand/programmering/droge feiten.
    """
    s = f"{source_title}\n{source_text}".lower()

    quote_signals = [
        "zegt ", "aldus ", "vertelt ", "verklaart ", "volgens ", "laat weten",
        "in gesprek met", "tegenover", "citeert", "interview", "column", "opinie",
        "‘", "’", "“", "”", "\""
    ]
    transfer_signals = [
        "transfer", "gerucht", "interesse in", "in de belangstelling",
        "bod", "bieding", "akkoord", "persoonlijk akkoord", "medische keuring",
        "tekent", "contract", "huurdeal", "gehuurd", "clausule", "transfersom",
        "overstap", "komt over van", "gaat naar"
    ]
    factual_signals = [
        "eindigt in", "speelschema", "programma", "speelronde", "stand ",
        "ranglijst", "samenvatting", "wedstrijdverslag", "score", "uitslag",
        "verslaat", "wint", "verliest", "gelijk", "1-0", "2-1", "3-2", "0-0"
    ]
    if any(k in s for k in transfer_signals): return True
    if any(k in s for k in quote_signals):    return True
    if any(k in s for k in factual_signals):  return False
    return False

# ---------------- single-pass: direct structured output ----------------
def format_article_structured(client, source_title: str, source_text: str, source_name: str, source_url: str):
    """
    Eén LLM-call die:
    - AZ-perspectief afdwingt (titel en tekst)
    - besluit of attributie nodig is
    - gestructureerde JSON-velden teruggeeft
    """
    approx_words = len(source_text.split())
    target_words = max(120, int(approx_words * 0.9))
    brand_alias_str = brand_alias(source_name)
    attribution_required_hint = needs_attribution(source_title, source_text)

    system_msg = (
        "Je bent AZAlerts, een Nederlandse sportnieuwsredacteur. "
        "Schrijf altijd vanuit AZ-perspectief en blijf feitelijk correct. "
        "Gebruik B1/B2-zinnen, geen sensatie, geen uitroeptekens. "
        "Noteer een score met AZ eerst (bijv. 'AZ 2–1 PSV'). "
        "Titel zonder aanhalingstekens."
    )

    # We vragen expliciet om JSON. (Geen tool-calls nodig; we parsen de string.)
    user_msg = f"""
Geef ALLEEN valide JSON terug, exact in dit schema:

{{
  "title": "string (AZ als onderwerp; één regel; score met AZ eerst indien van toepassing)",
  "intro": "string (2–3 korte zinnen, samenvatting vanuit AZ)",
  "bullets": ["string", "string", "string"],
  "body_paragraphs": ["string", "string", "string"],
  "attribution_required": true/false,
  "attribution_line": "string of lege string"
}}

Regels:
- Schrijf ALTIJD vanuit AZ-perspectief: AZ is onderwerp/focus.
- Pas de titel aan naar AZ-perspectief. Voorbeelden:
  - Bron: "PSV verliest van AZ" → Titel: "AZ wint van PSV".
  - Bron: "PSV – AZ eindigt in 1-1" → Titel: "AZ speelt gelijk tegen PSV (1-1)".
  - Bron: "AZ verliest van PSV" → Titel blijft feitelijk: "AZ verliest van PSV".
- GEEN aanhalingstekens rondom de titel (alleen echte citaten in de tekst).
- GEEN URL's in tekst.
- Bullets: 3–5 korte punten.
- body_paragraphs: 3–6 alinea’s, korte zinnen, logisch opgebouwd.
- Beslis of bronvermelding nodig is:
  - JA bij transfer/geruchten, interviews/quotes of meningen/columns.
  - NEE bij wedstrijdverslag/stand/programmering/droge feiten.
- Bij attributie: verwerk een korte bronvermelding IN de tekst (niet als losse regel) en zet in "attribution_line" exact: "Bron: {brand_alias_str} – {source_url}"
- Bij geen attributie: laat "attribution_line" leeg en zet "attribution_required": false.
- Streef naar ~{target_words} woorden in de body.

INVOER
SOURCE_TITLE: {source_title}
SOURCE_NAME: {source_name}
SOURCE_URL:  {source_url}

SOURCE_TEXT:
{source_text}

HINT (mag je negeren als het niet klopt): attribution_required = {str(attribution_required_hint).lower()}
"""

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content": system_msg},
            {"role":"user","content": user_msg}
        ],
        temperature=0.2,
        max_tokens=2400
    )
    raw = (resp.choices[0].message.content or "").strip()

    # Probeer JSON te parsen; bij fout → simpele fallback
    try:
        data = json.loads(raw)
        # minimale sanity
        for k in ["title","intro","bullets","body_paragraphs","attribution_required","attribution_line"]:
            if k not in data: raise ValueError("key missing: "+k)
        if not isinstance(data.get("bullets"), list): raise ValueError("bullets not list")
        if not isinstance(data.get("body_paragraphs"), list): raise ValueError("body_paragraphs not list")
    except Exception:
        # Fallback: alles in 1 tekstblok, zodat de site blijft werken
        fallback_text = normalize_plaintext(raw)
        data = {
            "title": source_title or "AZ-update",
            "intro": "",
            "bullets": [],
            "body_paragraphs": [fallback_text] if fallback_text else [],
            "attribution_required": False,
            "attribution_line": ""
        }
    return data

# ---------------- routes ----------------
@app.route("/", methods=["GET","POST"])
def index():
    if request.method == "POST":
        url = (request.form.get("url") or "").strip()
        if not url:
            flash("Vul een URL in.")
            return render_form()

        try:
            downloaded = trafilatura.fetch_url(url)
            text = trafilatura.extract(downloaded) if downloaded else ""
        except Exception:
            app.logger.exception("Trafilatura-fout")
            flash("Kon de tekst niet ophalen van deze URL.")
            return render_form()

        if not text or len(text.split()) < 50:
            flash("Te weinig tekst gevonden in dit artikel.")
            return render_form()

        source_name = source_brand_from_url(url)
        alias = brand_alias(source_name)

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            flash("OPENAI_API_KEY ontbreekt (zet deze in Render → Environment).")
            return render_form()

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)

            # SINGLE-PASS: direct structured output (minder tokens dan 2-pass)
            result = format_article_structured(
                client=client,
                source_title="",
                source_text=text,
                source_name=source_name,
                source_url=url
            )

            # Bouw ook een plain-text voor de Kopieer/Download-knop (samengesteld)
            pieces = []
            if result.get("title"): pieces.append(result["title"])
            if result.get("intro"): pieces.append(result["intro"])
            if result.get("bullets"):
                pieces.append("\n".join(f"• {b}" for b in result["bullets"]))
            if result.get("body_paragraphs"):
                pieces.extend(result["body_paragraphs"])
            if result.get("attribution_required") and result.get("attribution_line"):
                pieces.append(result["attribution_line"])
            output_text = "\n\n".join([p for p in pieces if p]).strip()

        except Exception:
            app.logger.exception("OpenAI client/format-fout")
            flash("Er ging iets mis bij het genereren van de tekst.")
            return render_form()

        return render_result(output_text=output_text, result=result, used_openai=True)

    return render_form()

# ---------------- diagnostics ----------------
@app.get("/health")
def health():
    return {"status": "ok"}, 200

@app.get("/debug-env")
def debug_env():
    return {"OPENAI_API_KEY_present": bool(os.getenv("OPENAI_API_KEY"))}, 200

@app.errorhandler(500)
def handle_500(err):
    app.logger.exception("Onverwachte 500")
    return render_form(error="Er ging iets mis op de server."), 500

# ---------------- local run ----------------
if __name__ == "__main__":
    print("[server] start op 127.0.0.1:8000 (1-pagina app, structured output)")
    app.run(debug=True, host="127.0.0.1", port=8000, use_reloader=False)
