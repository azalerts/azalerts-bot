import os
from flask import Flask, render_template, request, flash
import trafilatura
from urllib.parse import urlparse

app = Flask(__name__)
app.secret_key = "dev"

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
    # Korte varianten die vaak in NL-kopij gebruikt worden
    aliases = {
        "Voetbal International": "VI",
        "VoetbalPrimeur": "VoetbalPrimeur",   # soms 'VP', maar voluit is veilig
        "RTL Nieuws": "RTL Nieuws",
        "De Telegraaf": "De Telegraaf",
    }
    return aliases.get(brand, brand)

def split_into_chunks(text: str, max_words: int = 900):
    words = text.split()
    for i in range(0, len(words), max_words):
        yield " ".join(words[i:i+max_words])

# 1) Parafrase per chunk (lengte ~ gelijk)
def paraphrase_chunk(client, chunk: str, brand_alias_str: str) -> str:
    approx_tokens = min(int(len(chunk.split()) * 1.4), 2000)
    prompt_user = (
        "Parafraseer de onderstaande tekst in het Nederlands, behoud alle feiten en nuance, "
        "en houd de lengte ongeveer gelijk aan de input (±10%). "
        "Schrijf in een neutrale, nieuwswaardige toon. "
        "Verwerk eventueel een korte bronzin in de lopende tekst wanneer logisch, "
        f"bijv. '..., zo meldt {brand_alias_str}.' "
        "Voeg geen URL's toe en geen losse bronregel.\n\n"
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
    return resp.choices[0].message.content.strip()

# 2) Finale format-stap: titel, eerste zin = hoofdboodschap + vroege bron, alinea-indeling
def format_article(client, full_text: str, brand_alias_str: str, approx_words: int) -> str:
    target_words = max(120, int(approx_words*0.9))  # ondergrens en ± zelfde lengte
    prompt_user = (
        "Zet de onderstaande tekst om naar een AZAlerts-waardig nieuwsartikel met deze eisen:\n"
        "1) Bovenaan één titel, tussen ENKELE aanhalingstekens: '...'\n"
        "2) Eerste zin = de hoofdboodschap. Plaats VROEG in die zin een bronvermelding in de lopende tekst, "
        f"bijv. '..., zo meldt {brand_alias_str}.' of '..., schrijft {brand_alias_str}.'\n"
        "3) Deel daarna op in korte alinea's: introductie (1 alinea) → kernpunten (1-3 alinea's) → context/achtergrond (1-2 alinea's).\n"
        "4) Geen URL's, geen losse bronregel onderaan, geen reclame en geen speculatie.\n"
        "5) Lengte: ongeveer gelijk aan de input (–10% tot +10%).\n"
        "6) Alles in het Nederlands en uitsluitend feiten uit de input.\n\n"
        f"Streef naar ~{target_words} woorden.\n\n"
        "INPUT:\n" + full_text
    )
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role":"system","content":"Je bent een ervaren Nederlandse nieuwsredacteur. Je houdt je strikt aan instructies en hallucineert niet."},
            {"role":"user","content": prompt_user}
        ],
        temperature=0.2,
        max_tokens=2500
    )
    return resp.choices[0].message.content.strip()

# ---------------- routes ----------------
@app.route("/", methods=["GET","POST"])
def index():
    if request.method == "POST":
        url = request.form.get("url","").strip()
        if not url:
            flash("Vul een URL in.")
            return render_template("az_form.html")

        downloaded = trafilatura.fetch_url(url)
        text = trafilatura.extract(downloaded) if downloaded else ""
        if not text or len(text.split()) < 50:
            flash("Te weinig tekst gevonden in dit artikel.")
            return render_template("az_form.html")

        brand = source_brand_from_url(url)
        alias = brand_alias(brand)
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            flash("OPENAI_API_KEY ontbreekt. Zet je sleutel in ~/.zshrc en herstart de server.")
            return render_template("az_form.html")

        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # Parafrase in chunks om lengte/limieten te bewaken
        chunks = list(split_into_chunks(text, max_words=1000))
        outputs = []
        for ch in chunks:
            try:
                outputs.append(paraphrase_chunk(client, ch, alias))
            except Exception as e:
                print("[OpenAI] Fout op chunk:", e)
                outputs.append(ch)  # fallback: originele chunk

        merged = "\n\n".join(outputs).strip()

        # Finale format-stap: structuur + vroege bron + titel
        try:
            total_words = len(text.split())
            final_text = format_article(client, merged, alias, approx_words=total_words)
        except Exception as e:
            print("[OpenAI] Format-fout:", e)
            final_text = merged  # fallback

        return render_template("az_result.html", output_text=final_text, used_openai=True)

    return render_template("az_form.html")

if __name__ == "__main__":
    print("[server] start op 127.0.0.1:8000 (1-pagina app)")
    app.run(debug=True, host="127.0.0.1", port=8000, use_reloader=False)
