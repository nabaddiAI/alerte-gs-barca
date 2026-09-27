#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot d'alerte — billets Galatasaray vs FC Barcelona
Mardi 13 octobre 2026, RAMS Park (Istanbul)

Surveille :
  - la page officielle « Bilet Duyuruları » de galatasaray.org,
  - le flux RSS officiel de galatasaray.org,
  - la presse turque via Google Actualités (source secondaire).
Envoie une alerte Telegram dès qu'une annonce de billetterie apparaît.

Il n'achète rien et ne touche pas à Passo : il te prévient, tu achètes toi-même.

Utilisation :
  python bot_billets.py --test              teste les sources + message Telegram de test
  python bot_billets.py --simulation        envoie une fausse alerte pour voir à quoi elle ressemble
  python bot_billets.py --once              une seule vérification
  python bot_billets.py --duree 20          vérifie en boucle pendant 20 minutes (GitHub Actions)
  python bot_billets.py                     boucle continue sur ton ordinateur
  python bot_billets.py --trouver-chat-id   récupère ton identifiant Telegram
  python bot_billets.py --reset             efface l'historique des alertes
"""

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import unicodedata
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

DOSSIER = os.path.dirname(os.path.abspath(__file__))
CHEMIN_CONFIG = os.path.join(DOSSIER, "config.json")
CHEMIN_ETAT = os.path.join(DOSSIER, "state.json")
CHEMIN_SECRETS = os.path.join(DOSSIER, "secrets.local.json")
FUSEAU = ZoneInfo("Europe/Istanbul")
FUSEAU_MAROC = ZoneInfo("Africa/Casablanca")
ENTETES = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8,fr;q=0.7",
}


# ---------------------------------------------------------------- utilitaires

def log(message):
    horodatage = datetime.now(FUSEAU).strftime("%d/%m %H:%M:%S")
    print(f"[{horodatage}] {message}", flush=True)


def normaliser(texte):
    """Minuscules, sans accents ni lettres turques spéciales (İ, ı, ş, ç, ğ, ö, ü)."""
    texte = texte.replace("İ", "i").replace("I", "ı").lower()
    texte = unicodedata.normalize("NFKD", texte)
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    texte = texte.replace("ı", "i")
    return re.sub(r"\s+", " ", texte).strip()


def html_vers_texte(source):
    source = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", source)
    source = re.sub(r"(?s)<[^>]+>", " ", source)
    return re.sub(r"\s+", " ", html.unescape(source)).strip()


def groupes(mots_cles):
    """Chaque groupe = liste d'alternatives. Tous les groupes doivent être présents."""
    return [[normaliser(a) for a in (g if isinstance(g, list) else [g])] for g in mots_cles]


def contient_tous(texte_norm, groupes_norm):
    return all(any(alt in texte_norm for alt in g) for g in groupes_norm)


def exclu(texte_norm, source):
    return any(normaliser(m) in texte_norm for m in source.get("exclure", []))


def charger_json(chemin, defaut):
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return defaut


def sauver_json(chemin, donnees):
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=2)
    os.replace(tmp, chemin)


def date_rss(texte):
    try:
        d = parsedate_to_datetime(texte)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def double_heure(d=None):
    d = d or datetime.now(FUSEAU)
    return f"{d.astimezone(FUSEAU):%d/%m %H:%M} Istanbul · {d.astimezone(FUSEAU_MAROC):%H:%M} Maroc"


# ---------------------------------------------------------------- traduction

_CACHE_TRADUCTION = {}
LANGUE_CIBLE = "fr"


def traduire(texte):
    """Traduit un titre en français (service gratuit Google, sans clé).
    En cas d'échec, renvoie None : l'alerte part quand même, en turc."""
    texte = (texte or "").strip()
    if not texte or not LANGUE_CIBLE:
        return None
    if texte in _CACHE_TRADUCTION:
        return _CACHE_TRADUCTION[texte]
    traduction = None
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": LANGUE_CIBLE, "dt": "t", "q": texte},
            headers=ENTETES,
            timeout=10,
        )
        r.raise_for_status()
        morceaux = r.json()[0]
        traduction = "".join(m[0] for m in morceaux if m and m[0]).strip() or None
        if traduction and normaliser(traduction) == normaliser(texte):
            traduction = None  # déjà en français / rien à traduire
    except Exception as e:
        log(f"[traduction] impossible : {e}")
    _CACHE_TRADUCTION[texte] = traduction
    return traduction


def avec_traduction(titre):
    """Titre original + ligne traduite en dessous."""
    traduction = traduire(titre)
    return f"{titre}\n🇫🇷 {traduction}" if traduction else titre


# ---------------------------------------------------------------- Telegram

def identifiants_telegram():
    secrets = charger_json(CHEMIN_SECRETS, {})
    token = os.environ.get("TELEGRAM_TOKEN") or secrets.get("telegram_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or secrets.get("telegram_chat_id", "")
    return token.strip(), str(chat_id).strip()


def envoyer(message, silencieux=False):
    log("Message : " + message.replace("\n", " | "))
    token, chat_id = identifiants_telegram()
    if not token or not chat_id:
        log("[!] Telegram non configuré — message affiché ici seulement.")
        return False
    for essai in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": message[:4000],
                    "disable_notification": "true" if silencieux else "false",
                },
                timeout=20,
            )
            if r.ok:
                return True
            log(f"[!] Telegram a refusé le message : {r.status_code} {r.text[:200]}")
            if r.status_code in (400, 401, 403, 404):
                return False  # erreur d'identifiants : inutile de réessayer
        except requests.RequestException as e:
            log(f"[!] Envoi Telegram impossible : {e}")
        time.sleep(3 * (essai + 1))
    return False


def trouver_chat_id():
    token, _ = identifiants_telegram()
    if not token:
        token = input("Colle le token de ton bot Telegram (donné par @BotFather) : ").strip()
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    donnees = r.json()
    if not donnees.get("ok"):
        print("Token invalide :", donnees)
        return 1
    ids = {}
    for maj in donnees.get("result", []):
        chat = (maj.get("message") or maj.get("channel_post") or {}).get("chat")
        if chat:
            ids[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not ids:
        print("Aucun message trouvé. Envoie « bonjour » à ton bot sur Telegram, puis relance.")
        return 1
    for cid, nom in ids.items():
        print(f"Chat ID : {cid}  ({nom})")
    chat_id = next(iter(ids))
    sauver_json(CHEMIN_SECRETS, {"telegram_token": token, "telegram_chat_id": str(chat_id)})
    print(f"Identifiants enregistrés dans {os.path.basename(CHEMIN_SECRETS)} (usage local).")
    return 0


# ---------------------------------------------------------------- sources

def telecharger(url):
    r = requests.get(url, headers=ENTETES, timeout=25)
    r.raise_for_status()
    if (r.encoding or "").lower() in ("iso-8859-1", ""):
        r.encoding = "utf-8"
    return r


def verifier_rss(source):
    """Flux RSS : titre + description doivent contenir tous les groupes de mots-clés."""
    reponse = telecharger(source["url"])
    racine = ET.fromstring(reponse.content)
    g = groupes(source["mots_cles"])
    age_max = source.get("age_max_heures")
    maintenant = datetime.now(timezone.utc)
    trouves = []
    for item in racine.iter("item"):
        titre = html_vers_texte(item.findtext("title") or "")
        lien = (item.findtext("link") or "").strip().replace("http://", "https://", 1)
        description = html_vers_texte(item.findtext("description") or "")
        publie = date_rss(item.findtext("pubDate") or "")
        if age_max and publie and (maintenant - publie).total_seconds() > age_max * 3600:
            continue
        texte = normaliser(f"{titre} {description} {lien}")
        if contient_tous(texte, g) and not exclu(texte, source):
            trouves.append({"id": lien or titre, "titre": titre or "(sans titre)",
                            "lien": lien, "publie": publie})
    return trouves


def verifier_liens(source):
    """Page HTML : chaque lien <a> (texte + adresse) est testé séparément."""
    reponse = telecharger(source["url"])
    g = groupes(source["mots_cles"])
    motif = source.get("filtre_lien", "")
    trouves = {}
    for m in re.finditer(r'(?is)<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                         reponse.text):
        lien = urljoin(source["url"], html.unescape(m.group(1).strip()))
        if motif and motif not in lien:
            continue
        titre = html_vers_texte(m.group(2))
        texte = normaliser(f"{titre} {lien}")
        if contient_tous(texte, g) and not exclu(texte, source):
            # plusieurs <a> pointent souvent vers le même article : on garde le titre le plus long
            if lien not in trouves or len(titre) > len(trouves[lien]["titre"]):
                trouves[lien] = {"id": lien, "titre": titre or lien, "lien": lien, "publie": None}
    return list(trouves.values())[:10]


def verifier_source(source):
    if source.get("type") == "rss":
        return verifier_rss(source)
    return verifier_liens(source)


# ---------------------------------------------------------------- logique

def etat_initial():
    return {"alertes": {}, "vus": {}, "erreurs": {}, "sources_amorcees": [],
            "dernier_quotidien": "", "fin_annoncee": False, "derniere_verif": {}}


def cle_de(source, resultat):
    # Clé = l'adresse de l'article : la même annonce vue dans la page ET le flux RSS
    # ne déclenche qu'une seule alerte.
    ident = resultat["id"].lower().replace("http://", "https://").rstrip("/")
    return hashlib.sha1(ident.encode()).hexdigest()


def message_alerte(source, resultat):
    if source.get("officielle"):
        entete = "🚨🚨 BILLETS GALATASARAY – BARCELONA (SITE OFFICIEL)"
        action = ("➡️ Ouvre l'annonce pour les horaires de chaque phase, puis Passo : "
                  "connecté, carte Passo Taraftar/Passolig active, paiement prêt.")
    else:
        entete = "📰 Info presse — billets Galatasaray – Barcelona"
        action = "ℹ️ Source non officielle : vérifie l'annonce sur galatasaray.org ou dans l'appli Passo."
    publie = f"Publié : {double_heure(resultat['publie'])}\n" if resultat.get("publie") else ""
    return (
        f"{entete}\n\n"
        f"{avec_traduction(resultat['titre'])}\n"
        f"{resultat['lien']}\n\n"
        f"Source : {source['nom']}\n"
        f"{publie}"
        f"Détecté : {double_heure()}\n\n"
        f"{action}"
    )


def date_fin(config):
    try:
        return datetime.fromisoformat(config.get("fin_surveillance", "")).replace(tzinfo=FUSEAU)
    except ValueError:
        return None


def verifier(config, etat, local=False):
    maintenant = time.time()
    seuil_erreurs = int(config.get("erreurs_avant_alerte", 3))
    actives = [s for s in config["sources"] if s.get("active", True)]

    for source in actives:
        nom = source["nom"]
        try:
            resultats = verifier_source(source)
        except Exception as e:  # réseau, blocage, XML invalide...
            n = etat["erreurs"].get(nom, 0) + 1
            etat["erreurs"][nom] = n
            etat["derniere_verif"][nom] = f"échec ({str(e)[:80]})"
            log(f"[erreur] {nom} : {e}")
            if n == seuil_erreurs:
                envoyer(
                    f"⚠️ Le bot n'arrive plus à lire « {nom} » ({n} échecs d'affilée).\n"
                    f"Erreur : {str(e)[:200]}\nJe continue d'essayer.",
                    silencieux=True,
                )
            continue

        if etat["erreurs"].get(nom, 0) >= seuil_erreurs:
            envoyer(f"✅ « {nom} » est de nouveau accessible.", silencieux=True)
        etat["erreurs"][nom] = 0
        etat["derniere_verif"][nom] = "OK"
        log(f"{nom} : OK ({len(resultats)} correspondance(s))")

        # Première lecture d'une source non officielle : on mémorise l'existant sans alerter
        # (évite une rafale d'articles « quand les billets seront-ils en vente ? »).
        premiere_fois = nom not in etat["sources_amorcees"]
        if premiere_fois:
            etat["sources_amorcees"].append(nom)
            if not source.get("officielle"):
                for r in resultats:
                    etat["vus"][cle_de(source, r)] = r["titre"][:120]
                if resultats:
                    envoyer(
                        f"ℹ️ Démarrage : {len(resultats)} article(s) déjà publié(s) sur « {nom} » "
                        "ont été mémorisés sans alerte. Plus récent :\n"
                        + "\n".join(f"· {avec_traduction(r['titre'][:150])}" for r in resultats[:3]),
                        silencieux=True,
                    )
                continue

        for resultat in resultats:
            cle = cle_de(source, resultat)
            if cle in etat["alertes"] or cle in etat["vus"]:
                continue
            message = message_alerte(source, resultat)
            envoyer(message)
            etat["alertes"][cle] = {
                "message": message,
                "derniere": maintenant,
                "rappels": 0 if source.get("rappels", True) else 999,
            }
            if local:
                print("\a", end="", flush=True)
                if resultat["lien"]:
                    webbrowser.open(resultat["lien"])

    # rappels (sources officielles), au cas où la première notification passe inaperçue
    rappels = config.get("rappels", {})
    nombre, minutes = int(rappels.get("nombre", 3)), float(rappels.get("minutes", 15))
    for alerte in etat["alertes"].values():
        if alerte["rappels"] < nombre and maintenant - alerte["derniere"] >= minutes * 60:
            envoyer(f"🔁 RAPPEL {alerte['rappels'] + 1}/{nombre}\n\n" + alerte["message"])
            alerte["rappels"] += 1
            alerte["derniere"] = maintenant

    # message quotidien pour confirmer que le bot tourne
    maintenant_ist = datetime.now(FUSEAU)
    aujourd_hui = maintenant_ist.date().isoformat()
    if (maintenant_ist.hour >= int(config.get("heure_message_quotidien", 9))
            and etat.get("dernier_quotidien") != aujourd_hui):
        etats_sources = "\n".join(
            f"· {s['nom']} : {etat['derniere_verif'].get(s['nom'], '—')}" for s in actives
        )
        jours = (datetime(2026, 10, 13, tzinfo=FUSEAU).date() - maintenant_ist.date()).days
        envoyer(
            f"🟢 Bot actif — J-{jours} avant le match.\n"
            f"{len(etat['alertes'])} alerte(s) envoyée(s) jusqu'ici.\n{etats_sources}\n"
            f"{double_heure(maintenant_ist)}",
            silencieux=True,
        )
        etat["dernier_quotidien"] = aujourd_hui


def surveillance_terminee(config, etat):
    fin = date_fin(config)
    if fin and datetime.now(FUSEAU) > fin:
        if not etat.get("fin_annoncee"):
            envoyer("🏁 Match passé : la surveillance est terminée. "
                    "Tu peux désactiver le workflow sur GitHub (Actions → ⋯ → Disable workflow).")
            etat["fin_annoncee"] = True
        log("Date de fin dépassée : rien à faire.")
        return True
    return False


def mode_test(config):
    print("=== Test des sources ===")
    tout_ok = True
    lignes = []
    for source in config["sources"]:
        if not source.get("active", True):
            print(f"- {source['nom']} : désactivée")
            continue
        try:
            resultats = verifier_source(source)
            print(f"- {source['nom']} : OK, {len(resultats)} correspondance(s) actuellement")
            lignes.append(f"✅ {source['nom']}")
            for r in resultats[:3]:
                print(f"    · {r['titre'][:150]}")
        except Exception as e:
            tout_ok = False
            lignes.append(f"❌ {source['nom']} ({str(e)[:60]})")
            print(f"- {source['nom']} : ÉCHEC → {e}")
    print("\n=== Test traduction ===")
    exemple = traduire("Galatasaray - Barcelona maçının biletleri satışa çıktı")
    print(f"Traduction : {exemple or 'ÉCHEC (les titres resteront en turc)'}")
    lignes.append(f"✅ Traduction : « {exemple} »" if exemple
                  else "⚠️ Traduction indisponible (titres en turc)")
    print("\n=== Test Telegram ===")
    ok = envoyer(
        "🧪 Test réussi ! Le bot d'alerte Galatasaray – Barcelona est bien connecté.\n\n"
        + "\n".join(lignes)
        + "\n\nTu recevras un message ici dès qu'une annonce de billetterie apparaît."
    )
    print("Telegram : OK" if ok else "Telegram : ÉCHEC (vérifie le token et le chat ID)")
    return 0 if (ok and tout_ok) else 1


def mode_simulation():
    source = {"nom": "SIMULATION — galatasaray.org", "officielle": True}
    resultat = {
        "titre": "(EXEMPLE) BARCELONA MAÇI BİLETLERİ SATIŞA ÇIKTI",
        "lien": "https://www.galatasaray.org/haberler/futbol/biletler/321",
        "publie": datetime.now(FUSEAU),
    }
    ok = envoyer("⚠️ CECI EST UN EXERCICE ⚠️\n\n" + message_alerte(source, resultat))
    return 0 if ok else 1


# ---------------------------------------------------------------- point d'entrée

def main():
    parser = argparse.ArgumentParser(description="Alerte billets Galatasaray – Barcelona")
    parser.add_argument("--once", action="store_true", help="une seule vérification")
    parser.add_argument("--duree", type=float, default=0,
                        help="vérifier en boucle pendant N minutes puis s'arrêter")
    parser.add_argument("--test", action="store_true", help="tester sources + Telegram")
    parser.add_argument("--simulation", action="store_true", help="envoyer une fausse alerte")
    parser.add_argument("--trouver-chat-id", action="store_true", help="récupérer ton chat ID")
    parser.add_argument("--reset", action="store_true", help="effacer l'historique")
    args = parser.parse_args()

    config = charger_json(CHEMIN_CONFIG, None)
    if config is None:
        print("config.json introuvable ou invalide.")
        return 1
    global LANGUE_CIBLE
    LANGUE_CIBLE = config.get("langue_traduction", "fr")

    if args.trouver_chat_id:
        return trouver_chat_id()
    if args.test:
        return mode_test(config)
    if args.simulation:
        return mode_simulation()
    if args.reset:
        sauver_json(CHEMIN_ETAT, etat_initial())
        print("Historique effacé.")
        return 0

    etat = charger_json(CHEMIN_ETAT, etat_initial())
    for cle, valeur in etat_initial().items():
        etat.setdefault(cle, valeur)

    if surveillance_terminee(config, etat):
        sauver_json(CHEMIN_ETAT, etat)
        return 0

    if args.once:
        try:
            verifier(config, etat)
        finally:
            sauver_json(CHEMIN_ETAT, etat)
        return 0

    intervalle = max(30, int(config.get("intervalle_secondes", 90)))
    local = args.duree <= 0
    fin = time.time() + args.duree * 60 if not local else None
    log(f"Surveillance lancée (toutes les ~{intervalle} s"
        + (f", pendant {args.duree:g} min" if fin else "") + "). Ctrl+C pour arrêter.")
    try:
        while True:
            verifier(config, etat, local=local)
            sauver_json(CHEMIN_ETAT, etat)
            pause = intervalle * random.uniform(0.85, 1.15)
            if fin and time.time() + pause >= fin:
                break
            time.sleep(pause)
    except KeyboardInterrupt:
        log("Arrêt demandé.")
    finally:
        sauver_json(CHEMIN_ETAT, etat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
