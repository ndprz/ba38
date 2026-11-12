# Textes et envoi des mails du module Billets spectacle
import html
import json
import os
import re
import sqlite3
from datetime import datetime

from ba38_utilitaires.core import envoyer_mail, render_modele_email, write_log

EXPEDITEUR_DEFAUT = "ba380@banquealimentaire.org"

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def maintenant():
    return datetime.now().isoformat(timespec="seconds")


def date_fr(iso, avec_jour=True):
    """'2026-12-05' → 'samedi 5 décembre 2026'."""
    try:
        d = datetime.strptime((iso or "")[:10], "%Y-%m-%d")
    except ValueError:
        return iso or ""
    s = f"{d.day}{'er' if d.day == 1 else ''} {MOIS[d.month - 1]} {d.year}"
    return f"{JOURS[d.weekday()]} {s}" if avec_jour else s


def limite_fr(iso):
    """'2026-11-20T23:59' → 'vendredi 20 novembre 2026 à 23h59'."""
    if not iso:
        return ""
    h = iso[11:16]
    return date_fr(iso) + (f" à {h.replace(':', 'h')}" if h else "")


def limite_depassee(c):
    lim = (c["date_limite"] or "")
    if not lim:
        return False
    if len(lim) == 10:
        lim += "T23:59"
    return datetime.now().isoformat(timespec="minutes") > lim[:16]


def url_inscription(token):
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    return f"{base}/billets/inscription/{token}"


def variables(c, p=None):
    """Variables <<...>> disponibles dans le message d'invitation."""
    v = {
        "spectacle": c["spectacle"] or "", "lieu": c["lieu"] or "", "donateur": c["donateur"] or "",
        "date_spectacle": date_fr(c["date_spectacle"]),
        "heure": (c["heure_spectacle"] or "").replace(":", "h"),
        "date_limite": limite_fr(c["date_limite"]),
        "nb_billets": str(c["nb_billets"] or ""),
        "mode_retrait": c["mode_retrait"] or "",
    }
    if p is not None:
        v["prenom"] = p["prenom"] or ""
        v["nom"] = p["nom"] or ""
    return v


VARIABLES_AIDE = ["prenom", "nom", "spectacle", "lieu", "donateur", "date_spectacle", "heure",
                  "date_limite", "nb_billets", "mode_retrait", "lien_inscription"]


def bouton_inscription(lien):
    """Bouton orange « INSCRIPTION » (table + styles en ligne : compatible clients mail)."""
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="margin:16px 0">'
        '<tr><td align="center" bgcolor="#f57c00" style="border-radius:6px">'
        f'<a href="{lien}" target="_blank" style="display:inline-block;padding:14px 36px;'
        'font-family:Arial,Helvetica,sans-serif;font-size:18px;font-weight:bold;color:#ffffff;'
        'text-decoration:none;letter-spacing:1px;border-radius:6px;background-color:#f57c00">'
        'INSCRIPTION</a></td></tr></table>')


def rendre_invitation(c, p, prefixe=""):
    """(sujet, corps HTML) personnalisés. Quill enregistre << >> en &lt;&lt; &gt;&gt;."""
    v = {k: html.escape(x) for k, x in variables(c, p).items()}
    brut = re.sub(r"&lt;&lt;(\w+)&gt;&gt;", r"<<\1>>", c["corps"] or "")
    corps = render_modele_email(brut, v)
    lien = url_inscription(p["token"]) if p["token"] else ""
    # Hors d'un href : bouton orange sur sa propre ligne (le <br> qui précède est retiré)
    corps = re.sub(r'<p>\s*<<lien_inscription>>\s*</p>', lambda m: bouton_inscription(lien), corps)
    corps = re.sub(r'(?<!href=")(?<!href=\')(?:<br\s*/?>\s*)?<<lien_inscription>>',
                   lambda m: "</p>" + bouton_inscription(lien) + "<p>", corps)
    corps = corps.replace("<<lien_inscription>>", lien)  # cas d'un lien posé dans un href
    sujet = render_modele_email(c["sujet"] or f"Places offertes : {c['spectacle']}",
                                variables(c, p)).strip()
    return prefixe + sujet, corps


def _fiche(c):
    """Bloc récapitulatif HTML du spectacle (variable <<fiche>>)."""
    lignes = [("Spectacle", c["spectacle"]), ("Lieu", c["lieu"]),
              ("Date", date_fr(c["date_spectacle"]) + (f" à {c['heure_spectacle'].replace(':', 'h')}" if c["heure_spectacle"] else "")),
              ("Offert par", c["donateur"])]
    return ("<table style='border-collapse:collapse'>" + "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#555'>{a}</td><td><b>{html.escape(b or '')}</b></td></tr>"
        for a, b in lignes if b) + "</table>")


# ------------------------------------------------------------
# Textes des autres mails (éditables dans la campagne ; valeurs par défaut ci-dessous).
# Les << >> sont écrits &lt;&lt; &gt;&gt; comme les enregistre l'éditeur Quill.
# ------------------------------------------------------------
_SIGN = "<p>Cordialement,<br>La Banque Alimentaire</p>"
TEXTES_MAILS = {
    "confirmation": {
        "libelle": "Confirmation d'inscription (envoyé au bénévole qui s'inscrit)",
        "sujet": "Inscription enregistrée : <<spectacle>>",
        "corps": (
            "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
            "<p>Votre inscription pour les places offertes est bien enregistrée.</p>&lt;&lt;fiche&gt;&gt;"
            "<p>&lt;&lt;nb_billets&gt;&gt; place(s) sont à attribuer. <b>Un tirage au sort sera effectué après la date "
            "limite d'inscription, le &lt;&lt;date_limite&gt;&gt;</b> : vous recevrez alors un mail vous indiquant si "
            "vous avez été retenu(e).</p>"
            "<p>Vous pouvez vous désister (ou vous réinscrire) jusqu'à cette date avec votre lien personnel : "
            "&lt;&lt;lien_inscription&gt;&gt;</p>" + _SIGN)},
    "desistement": {
        "libelle": "Désistement (envoyé au bénévole qui se désiste)",
        "sujet": "Désistement enregistré : <<spectacle>>",
        "corps": (
            "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
            "<p>Votre désistement pour « &lt;&lt;spectacle&gt;&gt; » est bien pris en compte.</p>"
            "<p>Vous pouvez vous réinscrire jusqu'au &lt;&lt;date_limite&gt;&gt; : &lt;&lt;lien_inscription&gt;&gt;</p>"
            + _SIGN)},
    "gagnant": {
        "libelle": "Résultat — gagnant tiré parmi les inscrits",
        "sujet": "Vous avez une place : <<spectacle>>",
        "corps": (
            "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
            "<p>Le tirage au sort parmi les inscrits a eu lieu : <b>félicitations, vous avez été tiré(e) au sort et une "
            "place vous est attribuée</b> !</p>&lt;&lt;fiche&gt;&gt;"
            "<p><b>Retrait des billets :</b> &lt;&lt;mode_retrait&gt;&gt;</p>"
            "<p>Si vous ne pouvez finalement pas en profiter, merci de nous prévenir au plus vite pour que la place "
            "soit proposée à quelqu'un d'autre.</p>" + _SIGN)},
    "gagnant_salarie": {
        "libelle": "Résultat — gagnant tiré parmi les salariés",
        "sujet": "Vous avez une place : <<spectacle>>",
        "corps": (
            "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
            "<p>Les places n'ayant pas toutes trouvé preneur parmi les bénévoles inscrits, un tirage au sort a été "
            "effectué parmi les salariés : <b>vous avez été tiré(e) au sort et une place vous est attribuée</b> !</p>"
            "&lt;&lt;fiche&gt;&gt;"
            "<p><b>Retrait des billets :</b> &lt;&lt;mode_retrait&gt;&gt;</p>"
            "<p>Si vous ne pouvez finalement pas en profiter, merci de nous prévenir au plus vite pour que la place "
            "soit proposée à quelqu'un d'autre.</p>" + _SIGN)},
    "non_retenu": {
        "libelle": "Résultat — inscrit non retenu",
        "sujet": "Résultat du tirage : <<spectacle>>",
        "corps": (
            "<p>Bonjour &lt;&lt;prenom&gt;&gt;,</p>"
            "<p>Le tirage au sort pour « &lt;&lt;spectacle&gt;&gt; » a eu lieu et, malheureusement, vous n'avez pas été "
            "retenu(e) cette fois-ci : il y avait plus d'inscrits que de places.</p>"
            "<p>Merci de votre participation, et à une prochaine occasion !</p>" + _SIGN)},
}


def textes_campagne(c):
    """{clé: {libelle, sujet, corps}} : textes par défaut surchargés par ceux enregistrés dans la campagne."""
    try:
        perso = json.loads(c["textes_mails"] or "{}")
    except (KeyError, IndexError, ValueError, TypeError):
        perso = {}
    res = {}
    for cle, d in TEXTES_MAILS.items():
        p = perso.get(cle) or {}
        res[cle] = {"libelle": d["libelle"], "sujet": (p.get("sujet") or "").strip() or d["sujet"],
                    "corps": p.get("corps") if (p.get("corps") or "").strip() else d["corps"]}
    return res


def rendre_texte(c, p, cle):
    """(sujet, corps) d'un des mails éditables, variables remplacées (valeurs échappées)."""
    t = textes_campagne(c)[cle]
    v = {k: html.escape(x) for k, x in variables(c, p).items()}
    lien = url_inscription(p["token"]) if p["token"] else ""
    v["lien_inscription"] = f'<a href="{lien}">{lien}</a>' if lien else ""
    v["fiche"] = _fiche(c)
    brut = re.sub(r"&lt;&lt;(\w+)&gt;&gt;", r"<<\1>>", t["corps"])
    corps = render_modele_email(brut, v)
    sujet = render_modele_email(re.sub(r"&lt;&lt;(\w+)&gt;&gt;", r"<<\1>>", t["sujet"]), variables(c, p)).strip()
    return sujet, corps


def mail_confirmation_inscription(c, p):
    return rendre_texte(c, p, "confirmation")


def mail_desistement(c, p):
    return rendre_texte(c, p, "desistement")


def mail_resultat(c, p):
    """Mail de résultat du tirage (gagnant inscrit / gagnant salarié / non retenu)."""
    if p["resultat"] == "gagnant":
        return rendre_texte(c, p, "gagnant_salarie" if p["origine_place"] == "salarie" else "gagnant")
    return rendre_texte(c, p, "non_retenu")


def envoyer(c, destinataire, sujet, corps, mode_test=False, user_email=None):
    """Envoi Mailjet d'un mail HTML ; lève l'exception en cas d'erreur.
    En mode test le destinataire devient l'utilisateur mais l'expéditeur reste celui de la
    campagne (validé chez Mailjet) : une adresse d'utilisateur non validée est rejetée en silence.
    Vérifie aussi le statut renvoyé par Mailjet (un rejet ne lève pas d'exception HTTP)."""
    res = envoyer_mail(
        sujet=sujet, destinataires=[destinataire], texte=corps, is_html=True,
        sender_override=EXPEDITEUR_DEFAUT,
        sender_name=c["expediteur_nom"], reply_to=c["reply_to"],
        mode_test=bool(mode_test), current_user_email=user_email, garder_expediteur=True)
    for m in (res or {}).get("Messages", []):
        if m.get("Status") != "success":
            raise RuntimeError("Mailjet a refusé le message : " + "; ".join(
                e.get("ErrorMessage", "") for e in m.get("Errors", [])) [:300])
    return True
