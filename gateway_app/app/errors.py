import logging
from flask import jsonify, request

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, message, code='ERROR', status_code=400, details=None):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or []

    def to_dict(self):
        resp = {'code': self.code, 'message': self.message}
        if self.details:
            resp['details'] = self.details
        return resp


def register_error_handlers(app):
    @app.errorhandler(APIError)
    def handle_api_error(error):
        if error.status_code >= 500:
            logger.error('APIError %s: %s', error.code, error.message)
        return jsonify(error.to_dict()), error.status_code

    @app.errorhandler(400)
    def bad_request(error):
        if request.path.startswith('/api/'):
            return jsonify({'code': 'BAD_REQUEST', 'message': 'Bad request'}), 400
        return 'Bad request', 400

    @app.errorhandler(401)
    def unauthorized(error):
        if request.path.startswith('/api/'):
            return jsonify({'code': 'UNAUTHORIZED', 'message': 'Authentication required'}), 401
        return 'Unauthorized', 401

    @app.errorhandler(403)
    def forbidden(error):
        if request.path.startswith('/api/'):
            return jsonify({'code': 'FORBIDDEN', 'message': 'Access denied'}), 403
        return 'Forbidden', 403

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith('/api/'):
            return jsonify({'code': 'NOT_FOUND', 'message': 'Resource not found'}), 404
        return render_404()

    @app.errorhandler(405)
    def method_not_allowed(error):
        if request.path.startswith('/api/'):
            return jsonify({'code': 'METHOD_NOT_ALLOWED', 'message': 'Method not allowed'}), 405
        return 'Method not allowed', 405

    @app.errorhandler(409)
    def conflict(error):
        if request.path.startswith('/api/'):
            return jsonify({'code': 'CONFLICT', 'message': 'Conflict'}), 409
        return 'Conflict', 409

    @app.errorhandler(422)
    def unprocessable(error):
        if request.path.startswith('/api/'):
            return jsonify({'code': 'VALIDATION_ERROR', 'message': 'Validation failed'}), 422
        return 'Unprocessable entity', 422

    @app.errorhandler(500)
    def internal_error(error):
        logger.exception('Internal server error')
        if request.path.startswith('/api/'):
            return jsonify({'code': 'INTERNAL_ERROR', 'message': 'Internal server error'}), 500
        return 'Internal server error', 500


def render_404():
    try:
        from flask import render_template
        return render_template('404.html'), 404
    except Exception:
        return 'Not found', 404
