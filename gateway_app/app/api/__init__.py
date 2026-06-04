from flask import Blueprint

api_bp = Blueprint('api', __name__)

from . import provider      # noqa: E402, F401
from . import observations  # noqa: E402, F401
