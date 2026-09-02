from flask import Blueprint

sms_bp = Blueprint("sms", __name__)

from ba38_sms import routes
