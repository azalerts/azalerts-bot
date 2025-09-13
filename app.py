import os
import re
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
RESULT_TPL = "az_result.html" if _has_tpl("az_result.html") else "index.html"

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

def split_into_chunks(text: str, max_words: int = 900):
    words = text.split()
    for i in range(0, len(words), max_words):
        yield " ".join(words[i:i+max_words])

def normalize_plaintext(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"<[^>]+>", "", s)               # HTML tags weghalen
    s = re.sub(r"^[ \t]+", "", s, flags=re.M)   # leading spaces
    s = re.sub(r"\n{3,}", "\n\n", s)            # teveel lege regels terug naar 1
    return s.strip()

def needs_attribution(source_title: str, source_text: str) -> bool:
    """
    Heuristiek: True als het gaat om transfernieuws/gerucht/interview/quote/mening.
    False bij wedstrijdverslag/stand/programmering/'droge' feiten.
    """
    s = f"{source_title}\n{source_text}".lower()

    # Signalen voor interviews/quotes/mening
    quote_signals = [
        "zegt ", "aldus ", "vertelt ", "verklaart ", "volgens ", "laat weten",
        "in gesprek met", "tegenover", "citeert", "interview", "column", "opinie",
        "‘", "’", "“", "”", "\""
    ]

    # Signalen voor transfers/geruchten
    transfer_signals = [
        "transfer", "gerucht", "in gesprek met", "interesse in", "in de belangstelling",
        "bod", "bieding", "akkoord", "persoonlijk akkoord", "medische keuring",
        "tekent", "contract", "huurdeal", "gehuurd", "clausule", "transfersom",
        "overstap", "komt over van", "gaat naar"
    ]

    # Signalen voor 'droge' feiten (als tegengas)
    factual_signals = [
        "eindigt in", "speelschema", "programma", "speelronde", "stand ",
        "ranglijst", "samenvatting", "wedstrijdverslag", "score", "uitslag",
        "verslaat", "wint", "verliest", "gelijk", "1-0", "2-1", "3-2", "0-0"
    ]

    # Als duidelijke transfer/quote-signalen → attributie
    if any(k in s for k in transfer_signals):
        return True
    if any(k in s for k in quote_signals):
        return True

    # Als het vooral feitelijk wedstrijdgerelateerd is → geen attributie
    if any(k in s for k in factual_signals):
        return False

    # Default: geen attributie (conservatief)
    return False

# 1) Parafrase per chunk (neutraal en feitelijk houden)
def paraphrase_chunk(client, chunk: str, brand_alias_str: str) -> str:
    approx_tokens = min(int(len(chunk.split()) * 1.4), 2000)
    prompt_user = (
        "Parafraseer de onderstaande tekst in het Nederlands, behoud alle feiten en nuance, "
        "en houd de lengte ongeveer gelijk aan de input (±10%). "
        "Schrijf in een neutrale, nieuwswaardige toon. "
        "Gebruik geen URL's en geen losse bronregel.\n\n"
        "TEKST:\n" + chunk
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content":"Je bent een redacteur die complete, feitelijke Nederlandstalige nieuwsartikelen schrijft zonder sensatie."},
            {"role":"user","content": prompt_user}
        ],
        temperature=0.2,
        max_tokens=approx_tokens
    )
    return (resp.choices[0].message.content or "").strip()

# 2) Finale format-stap met AZ-perspectief en conditionele bron
def format_article(client, full_text: str, brand_alias_str: str, approx_words: int, attribution_required: bool) -> str:
    target_words = max(120, int(approx_words * 0.9))

    # Instructies afhankelijk van bronvermelding
    if attribution_required:
        attribution_rule = (
            f"- Verwerk vroeg in de EERSTE alinea een korte bronvermelding in de lopende tekst "
            f"(bijv. '..., aldus {brand_alias_str}.'). "
            f"Gebruik GEEN losse bronregel onderaan."
        )
    else:
        attribution_rule = (
            "- Neem GEEN bronvermelding op (geen merknamen, geen 'volgens ...', geen losse bronregel)."
        )

    prompt_user = (
        "Zet de onderstaande tekst om naar een AZAlerts-waardig nieuwsartikel als PLATTE TEKST met ALLEEN alinea's.\n\n"
        "Regels:\n"
        "- Schrijf ALTIJD vanuit AZ-perspectief: AZ is het onderwerp of de focus.\n"
        "- Pas de titel aan naar AZ-perspectief. Voorbeelden:\n"
        "  Bron: 'PSV verliest van AZ' → Titel: 'AZ wint van PSV'.\n"
        "  Bron: 'PSV – AZ eindigt in 1-1' → Titel: 'AZ speelt gelijk tegen PSV (1-1)'.\n"
        "  Bron: 'AZ verliest van PSV' → Titel blijft feitelijk: 'AZ verliest van PSV'.\n"
        "- Noteer een eventuele score met AZ eerst (bijv. 'AZ 2–1 PSV').\n"
        "- Korte, duidelijke zinnen (B1/B2); geen sensatie, geen uitroeptekens.\n"
        "- Gebruik GEEN aanhalingstekens rondom de titel.\n"
        "- Citeer alleen echte uitspraken in de lopende tekst, met spreker.\n"
        f"{attribution_rule}\n"
        "- GEEN opsommingstekens, GEEN Markdown, GEEN HTML, GEEN URL's.\n"
        "- Scheid alinea’s met precies ÉÉN lege regel.\n"
        "- Opbouw: 1) Titel (één regel). 2) Eerste alinea = hoofdboodschap (AZ-perspectief). "
        "3) Daarna korte alinea’s met kern en context.\n"
        f"\nStreef naar ~{target_words} woorden.\n"
        "\nINPUT:\n" + full_text
    )

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "Je bent AZAlerts, een Nederlandse sportnieuwsredacteur. "
                    "Schrijf altijd vanuit AZ-perspectief en blijf feitelijk correct. "
                    "Gebruik platte tekst met alinea’s, geen opmaak."
                ),
            },
            {"role": "user", "content": prompt_user}
        ],
        temperature=0.2,
        max_tokens=2500,
    )
    out = (resp.choices[0].message.content or "").strip()
    return normalize_plaintext(out)

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

        brand = source_brand_from_url(url)
        alias = brand_alias(brand)

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            flash("OPENAI_API_KEY ontbreekt (zet deze in Render → Environment).")
            return render_form()

        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)

            # 1) Chunk-parafrase (neutraal)
            chunks = list(split_into_chunks(text, max_words=1000))
            outputs = []
            for ch in chunks:
                try:
                    outputs.append(paraphrase_chunk(client, ch, alias))
                except Exception:
                    app.logger.exception("OpenAI-fout op chunk")
                    outputs.append(ch)  # fallback

            merged = "\n\n".join(outputs).strip()

            # 2) Heuristiek: wel/geen bron afhankelijk van type artikel
            try:
                attribution_required = needs_attribution(source_title="", source_text=text)
            except Exception:
                attribution_required = False

            # 3) Finale format: AZ-perspectief + conditionele bron
            try:
                total_words = len(text.split())
                final_text = format_article(
                    client,
                    merged,
                    alias,
                    approx_words=total_words,
                    attribution_required=attribution_required
                )
            except Exception:
                app.logger.exception("OpenAI format-fout")
                final_text = merged

        except Exception:
            app.logger.exception("OpenAI client/init-fout")
            flash("Er ging iets mis bij het aanroepen van OpenAI.")
            return render_form()

        return render_result(output_text=final_text, result={"body": final_text}, used_openai=True)

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
    print("[server] start op 127.0.0.1:8000 (1-pagina app)")
    app.run(debug=True, host="127.0.0.1", port=8000, use_reloader=False)
