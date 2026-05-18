from flask import Blueprint

web_bp = Blueprint('web', __name__)

from . import views  # noqa: E402, F401
from . import docs   # noqa: E402, F401
from .auth import auth_bp  # noqa: E402, F401
