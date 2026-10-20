import re

from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, HiddenField, SelectField, BooleanField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Optional, ValidationError
from flask import request
from ba38_utilitaires.core import get_db_connection  # ✅ connexion centralisée

# 📌 Formulaire de connexion
def _email_ou_identifiant_tablette(form, field):
    """Email, ou identifiant court sans @ d'un compte mode tablette (ex.
    "tab1" : la recherche du compte se limite alors à mode_tablette = 1)."""
    valeur = (field.data or "").strip()
    if "@" in valeur:
        Email(message="Adresse email invalide")(form, field)
    elif not re.fullmatch(r"[A-Za-z0-9._-]{2,30}", valeur):
        raise ValidationError("Email ou identifiant invalide")


class LoginForm(FlaskForm):
    email = StringField("Email (ou identifiant tablette)", validators=[DataRequired(), _email_ou_identifiant_tablette])
    password = PasswordField("Mot de passe", validators=[DataRequired()])
    activer_2fa = BooleanField("Activer la double authentification (2FA) sur mon compte")
    submit = SubmitField("Se connecter")

# 📌 Formulaire d'inscription
class RegistrationForm(FlaskForm):
    email = StringField('Adresse email', validators=[
        DataRequired(), 
        Email(message="Adresse email invalide"),
        Length(max=100)
    ])
    username = StringField('Nom d\'utilisateur', validators=[
        DataRequired(), 
        Length(min=2, max=100)
    ])
    password = PasswordField('Mot de passe', validators=[
        DataRequired(), 
        Length(min=6)
    ])
    confirm_password = PasswordField('Confirmation du mot de passe', validators=[
        DataRequired(), 
        EqualTo('password', message="Les mots de passe ne correspondent pas")
    ])
    submit = SubmitField("Créer le compte")

    def validate_email(self, email):
        """ Vérifie si l'email est conforme et déjà utilisé """
        user_id = request.view_args.get('user_id')
        email_str = email.data.strip().lower()

        if not email_str.startswith("ba380") or not email_str.endswith("@banquealimentaire.org"):
            raise ValidationError("L’adresse email doit commencer par 'ba380' et se terminer par '@banquealimentaire.org'.")

        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email_str,)).fetchone()
        conn.close()

        if user and (user_id is None or user['id'] != user_id):
            raise ValidationError("Cet email est déjà utilisé par un autre utilisateur.")




# 📌 Formulaire de demande de réinitialisation de mot de passe
class RequestResetForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Envoyer")

# 📌 Formulaire de réinitialisation du mot de passe
class ResetPasswordForm(FlaskForm):
    password = PasswordField("Nouveau mot de passe", validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField("Confirmer le mot de passe", validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField("Réinitialiser le mot de passe")

# 📌 Formulaire pour la protection CSRF
class CSRFForm(FlaskForm):
    csrf_token = HiddenField()
