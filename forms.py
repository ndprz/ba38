from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, HiddenField, SelectField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Optional, ValidationError
from flask import request
from utils import get_db_connection  # ✅ connexion centralisée

# 📌 Formulaire de connexion
class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Mot de passe", validators=[DataRequired()])
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
    role = SelectField('Rôle', choices=[
        ('admin', 'Administrateur'),
        ('gestionnaire', 'Gestionnaire'),
        ('car', 'Car'),
        ('user', 'Utilisateur')
    ], validators=[DataRequired()])
    actif = SelectField('Actif', choices=[
        ('Oui', 'Oui'),
        ('Non', 'Non')
    ], validators=[DataRequired()])

    submit = SubmitField("Créer le compte")  # ✅ Ajout nécessaire

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
