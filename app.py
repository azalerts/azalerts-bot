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

def normalize_plaintext(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"<[^>]+>", "", s)               # HTML weg
    s = re.sub(r"^[ \t]+", "", s, flags=re.M)   # leading spaces
    s = re.sub(r"\n{3,}", "\n\n", s)            # te veel lege regels → 1
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

def _has_quotes(text: str) -> bool:
    return bool(re.search(r"[\"“”‘’'›‹«»].+[\"“”‘’'›‹«»]", text))

def _first_sentence_split(p: str):
    m = re.search(r"([.!?])(\s|$)", p)
    if not m:
        return p, "", ""
    end = m.end(1)  # include the punctuation
    return p[:end], p[end:], p[m.group(1)]

def ensure_alias_in_first_paragraph(result: dict, alias: str, source_text: str) -> dict:
    """
    Als attributie vereist is, zorg dat alias (bv 'VI') vloeiend in de 1e alinea staat:
    - Bij quotes/interview: voeg ', in gesprek met VI,' toe vóór de punt.
    - Anders: voeg ', zo meldt VI.' toe vóór de punt.
    """
    if not result or not result.get("attribution_required"):
        return result
    paras = result.get("body_paragraphs") or []
    if not paras:
        return result

    first = paras[0]
    # Als alias al aanwezig is, niets doen
    if re.search(rf"\b{re.escape(alias)}\b", first, flags=re.I):
        return result

    sent, tail, punct = _first_sentence_split(first.strip())
    if not sent:  # geen punt gevonden
        sent = first.strip()
        tail = ""
        punct = "."

    if _has_quotes(source_text) or re.search(r"\b(zegt|aldus|verklaart|vertelt|tegenover|in gesprek met)\b", source_text, flags=re.I):
        # interview/quotes
        # Vermijd dubbele komma: voeg netjes in voor de eindpunt
        if sent.endswith(","):
            new_first = f"{sent} in gesprek met {alias}{punct}{tail}"
        else:
            new_first = f"{sent[:-1]}, in gesprek met {alias}{punct}{tail}" if sent.endswith(punct) else f"{sent}, in gesprek met {alias}{punct}{tail}"
    else:
        # verslag/feit
        if sent.endswith(","):
            new_first = f"{sent} zo meldt {alias}{punct}{tail}"
        else:
            new_first = f"{sent[:-1]}, zo meldt {alias}{punct}{tail}" if sent.endswith(punct) else f"{sent}, zo meldt {alias}{punct}{tail}"

    paras[0] = new_first.strip()
    result["body_paragraphs"] = paras
    return result

# ---------------- single-pass: title + body (geen bullets) ----------------
def format_article_structured(client, source_title: str, source_text: str, source_name: str, source_url: str):
    """
    Eén LLM-call die AZ-perspectief afdwingt en JSON teruggeeft zonder bullets:
    { title, body_paragraphs[], attribution_required, attribution_line }
    """
    approx_words = len(source_text.split())
    # iets beknopter, minder alinea's
    target_words = max(120, int(approx_words * 0.8))
    brand_alias_str = brand_alias(source_name)
    attribution_required_hint = needs_attribution(source_title, source_text)

    system_msg = (
        "Je bent AZAlerts, een Nederlandse sportnieuwsredacteur. "
        "Schrijf altijd vanuit AZ-perspectief en blijf feitelijk correct. "
        "B1/B2, geen sensatie, geen uitroeptekens. "
        "Noteer een score met AZ eerst (bijv. 'AZ 2–1 PSV'). "
        "Titel zonder aanhalingstekens."
    )

    user_msg = f"""
Geef ALLEEN valide JSON terug, exact in dit schema:

{{
  "title": "string (AZ als onderwerp; één regel; score met AZ eerst indien van toepassing)",
  "body_paragraphs": ["string", "string"], 
  "attribution_required": true/false,
  "attribution_line": "string of lege string"
}}

Regels:
- Schrijf ALTIJD vanuit AZ-perspectief: AZ is onderwerp/focus.
- Pas de titel aan naar AZ-perspectief. Voorbeelden:
  - Bron: "PSV verliest van AZ" → "AZ wint van PSV".
  - Bron: "PSV – AZ eindigt in 1-1" → "AZ speelt gelijk tegen PSV (1-1)".
  - Bron: "AZ verliest van PSV" → "AZ verliest van PSV" (feiten blijven feiten).
- GEEN aanhalingstekens rondom de titel.
- body_paragraphs: 2–4 compacte alinea’s (intro → kern → context).
- Als de bron tekstuele citaten bevat, neem dan MINIMAAL één relevante quote op als echte citaatregel
  met spreker, bijvoorbeeld: “<quote>,” zei Maarten Martens tegen {brand_alias_str}.
- Beslis of bronvermelding nodig is:
  - JA bij transfer/geruchten, interviews/quotes of meningen/columns.
  - NEE bij wedstrijdverslag/stand/programmering/droge feiten.
- Bij attributie: verwerk de bron natuurlijk in de EERSTE alinea (bijv. "…, in gesprek met {brand_alias_str}" of "…, zo meldt {brand_alias_str}").
  én zet in "attribution_line": "Bron: {brand_alias_str} – {source_url}".
- Bij geen attributie: laat "attribution_line" leeg en zet "attribution_required": false.
- Geen URL's in de tekst.
- Streef naar ~{target_words} woorden in totaal.

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
        max_tokens=2200
    )
    raw = (resp.choices[0].message.content or "").strip()

    # JSON parse + fallback
    try:
        data = json.loads(raw)
        for k in ["title","body_paragraphs","attribution_required","attribution_line"]:
            if k not in data: raise ValueError("key missing: "+k)
        if not isinstance(data.get("body_paragraphs"), list):
            raise ValueError("body_paragraphs not list")
    except Exception:
        # Fallback: alles als platte tekst
        fallback_text = normalize_plaintext(raw)
        data = {
            "title": source_title or "AZ-update",
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

            # Single pass: title + body
            result = format_article_structured(
                client=client,
                source_title="",
                source_text=text,
                source_name=source_name,
                source_url=url
            )

            # Zorg dat alias vloeiend in alinea 1 staat (geen 'Volgens VI.' los)
            result = ensure_alias_in_first_paragraph(result, alias, text)

            # Samengestelde platte tekst (body + optionele bronregel)
            paragraphs = result.get("body_paragraphs") or []
            output_text = "\n\n".join(p.strip() for p in paragraphs if p.strip())
            if result.get("attribution_required") and result.get("attribution_line"):
                output_text = (output_text + "\n\n" + result["attribution_line"]).strip()

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
    print("[server] start op 127.0.0.1:8000 (AZAlerts strakker: quotes + bron, 2–4 alinea’s)")
    app.run(debug=True, host="127.0.0.1", port=8000, use_reloader=False)
