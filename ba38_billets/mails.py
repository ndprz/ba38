# Textes et envoi des mails du module Billets spectacle
import html
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


def rendre_invitation(c, p, prefixe=""):
    """(sujet, corps HTML) personnalisés. Quill enregistre << >> en &lt;&lt; &gt;&gt;."""
    v = {k: html.escape(x) for k, x in variables(c, p).items()}
    brut = re.sub(r"&lt;&lt;(\w+)&gt;&gt;", r"<<\1>>", c["corps"] or "")
    corps = render_modele_email(brut, v)
    lien = url_inscription(p["token"]) if p["token"] else ""
    corps = re.sub(r'(?<!href=")(?<!href=\')<<lien_inscription>>',
                   f'<a href="{lien}">{lien}</a>', corps)
    corps = corps.replace("<<lien_inscription>>", lien)
    sujet = render_modele_email(c["sujet"] or f"Places offertes : {c['spectacle']}",
                                variables(c, p)).strip()
    return prefixe + sujet, corps


def _fiche(c):
    """Bloc récapitulatif HTML du spectacle."""
    lignes = [("Spectacle", c["spectacle"]), ("Lieu", c["lieu"]),
              ("Date", date_fr(c["date_spectacle"]) + (f" à {c['heure_spectacle'].replace(':', 'h')}" if c["heure_spectacle"] else "")),
              ("Offert par", c["donateur"])]
    return ("<table style='border-collapse:collapse'>" + "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#555'>{a}</td><td><b>{html.escape(b or '')}</b></td></tr>"
        for a, b in lignes if b) + "</table>")


def _cadre(prenom, contenu):
    return (f"<p>Bonjour {html.escape(prenom or '')},</p>{contenu}"
            "<p>Cordialement,<br>La Banque Alimentaire</p>")


def mail_confirmation_inscription(c, p):
    lien = url_inscription(p["token"])
    contenu = (
        f"<p>Votre inscription pour les places offertes est bien enregistrée.</p>{_fiche(c)}"
        f"<p>{c['nb_billets']} place(s) sont à attribuer. <b>Un tirage au sort sera effectué après la date limite "
        f"d'inscription, le {limite_fr(c['date_limite'])}</b> : vous recevrez alors un mail vous indiquant si vous "
        f"avez été retenu(e).</p>"
        f"<p>Vous pouvez vous désister (ou vous réinscrire) jusqu'à cette date avec votre lien personnel : "
        f"<a href=\"{lien}\">{lien}</a></p>")
    return f"Inscription enregistrée : {c['spectacle']}", _cadre(p["prenom"], contenu)


def mail_desistement(c, p):
    lien = url_inscription(p["token"])
    contenu = (f"<p>Votre désistement pour « {html.escape(c['spectacle'] or '')} » est bien pris en compte.</p>"
               f"<p>Vous pouvez vous réinscrire jusqu'au {limite_fr(c['date_limite'])} : "
               f"<a href=\"{lien}\">{lien}</a></p>")
    return f"Désistement enregistré : {c['spectacle']}", _cadre(p["prenom"], contenu)


def mail_resultat(c, p):
    """Mail de résultat du tirage (gagnant ou non retenu)."""
    if p["resultat"] == "gagnant":
        retrait = (f"<p><b>Retrait des billets :</b> {html.escape(c['mode_retrait'])}</p>"
                   if c["mode_retrait"] else "")
        if p["origine_place"] == "salarie":
            intro = ("<p>Les places n'ayant pas toutes trouvé preneur parmi les bénévoles inscrits, un tirage au sort "
                     "a été effectué parmi les salariés : <b>vous avez été tiré(e) au sort et une place vous est "
                     "attribuée</b> !</p>")
        else:
            intro = ("<p>Le tirage au sort parmi les inscrits a eu lieu : <b>félicitations, vous avez été "
                     "tiré(e) au sort et une place vous est attribuée</b> !</p>")
        return (f"Vous avez une place : {c['spectacle']}",
                _cadre(p["prenom"], intro + _fiche(c) + retrait +
                       "<p>Si vous ne pouvez finalement pas en profiter, merci de nous prévenir au plus vite "
                       "pour que la place soit proposée à quelqu'un d'autre.</p>"))
    contenu = (f"<p>Le tirage au sort pour « {html.escape(c['spectacle'] or '')} » a eu lieu et, malheureusement, "
               f"vous n'avez pas été retenu(e) cette fois-ci : il y avait plus d'inscrits que de places.</p>"
               "<p>Merci de votre participation, et à une prochaine occasion !</p>")
    return f"Résultat du tirage : {c['spectacle']}", _cadre(p["prenom"], contenu)


def envoyer(c, destinataire, sujet, corps, mode_test=False, user_email=None):
    """Envoi Mailjet d'un mail HTML ; lève l'exception en cas d'erreur.
    Retourne True si un message est parti (ou a été redirigé en mode test/DEV)."""
    return envoyer_mail(
        sujet=sujet, destinataires=[destinataire], texte=corps, is_html=True,
        sender_override=c["expediteur"] or EXPEDITEUR_DEFAUT,
        sender_name=c["expediteur_nom"], reply_to=c["reply_to"],
        mode_test=bool(mode_test), current_user_email=user_email)
