from flask import Flask
from .extensions import db, migrate
from config import Config


def create_app(config_class=Config):
    app = Flask(
        __name__,
        static_folder='../static',
        template_folder='../templates',
    )
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)

    # Enable pgvector extension on first connection
    with app.app_context():
        _enable_pgvector(app)

    from .api import api_bp
    app.register_blueprint(api_bp, url_prefix='/api/v1')

    from .web import web_bp
    app.register_blueprint(web_bp)

    from .web.auth import auth_bp
    app.register_blueprint(auth_bp, url_prefix='/auth')

    from .errors import register_error_handlers
    register_error_handlers(app)

    # Inject current user info into all templates
    @app.context_processor
    def inject_user():
        from .services.sso_service import get_access_blob, map_role, is_admin, has_analysis_access
        blob = get_access_blob()
        return {
            'current_user': blob,
            'current_role': map_role(blob) if blob else None,
            'is_admin': is_admin(blob) if blob else False,
            'is_analyst': has_analysis_access(blob) if blob else False,
        }

    @app.route('/api/v1/health')
    def health():
        from flask import jsonify
        from sqlalchemy import text
        db_ok = False
        try:
            db.session.execute(text('SELECT 1'))
            db_ok = True
        except Exception:
            pass
        status = 'ok' if db_ok else 'degraded'
        code = 200 if db_ok else 503
        resp = jsonify({
            'status': status,
            'service': 'gateway.pdhc',
            'database': 'connected' if db_ok else 'unavailable',
        })
        # Ticket #70 / CLAUDE.md §10: let www.pdhc.se/services.html read the
        # JSON body cross-origin so it can drive real status/DB dots. Specific
        # origin + Vary: Origin (not "*") keeps future Allow-Credentials
        # spec-compliant. /api/v1/health is not HMAC-gated (services.html needs
        # anonymous read).
        resp.headers['Access-Control-Allow-Origin'] = 'https://www.pdhc.se'
        resp.headers['Access-Control-Allow-Methods'] = 'GET'
        resp.headers['Vary'] = 'Origin'
        resp.headers['Cache-Control'] = 'no-store'
        return resp, code

    with app.app_context():
        _bootstrap_admin(app)

    
    _register_metadata(app)
    _register_stockholm_filter(app)

    return app


def _enable_pgvector(app):
    """Enable pgvector extension if using PostgreSQL."""
    if app.config.get('TESTING'):
        return
    try:
        from sqlalchemy import text
        db.session.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _bootstrap_admin(app):
    """Create bootstrap SU on first run if table exists."""
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    if not inspector.has_table('audit_log'):
        return

    su_key = app.config.get('BOOTSTRAP_SU_API_KEY')
    if not su_key:
        return

    # Bootstrap logic will be expanded in Phase 2
    app.logger.info('Gateway bootstrap complete.')


def _register_stockholm_filter(app):
    """Register `local` Jinja filter rendering datetimes as Europe/Stockholm."""
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        return
    from datetime import datetime, timezone
    _STO = ZoneInfo("Europe/Stockholm")

    def _local(value, fmt="%Y-%m-%d %H:%M"):
        if value is None or value == "":
            return ""
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return value
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(_STO).strftime(fmt)

    app.jinja_env.filters["local"] = _local


# ── FHIR CapabilityStatement ─────────────────────────────────────────
def _register_metadata(app):
    from flask import jsonify
    from datetime import datetime, timezone

    @app.route("/metadata")
    @app.route("/api/v1/metadata")
    def metadata():
        return jsonify({
            "resourceType": "CapabilityStatement",
            "status": "active",
            "date": datetime.now(timezone.utc).isoformat(),
            "kind": "instance",
            "software": {"name": "gateway.pdhc", "version": "1.0.0"},
            "fhirVersion": "5.0.0",
            "format": ["json"],
            "rest": [{
                "mode": "server",
                "resource": [
                    {
                        "type": "Observation",
                        "profile": "http://hl7.org/fhir/StructureDefinition/Observation",
                        "interaction": [
                            {"code": "read"},
                            {"code": "search-type"},
                            {"code": "create"},
                        ],
                        "searchParam": [
                            {"name": "organization", "type": "reference", "documentation": "Filter observations by requesting organization GUID"},
                        ],
                    },
                    {
                        "type": "Bundle",
                        "profile": "http://hl7.org/fhir/StructureDefinition/Bundle",
                        "interaction": [{"code": "read"}],
                        "documentation": "Searchset bundles of Observations returned by GET /api/v1/observations",
                    },
                    {
                        "type": "ServiceRequest",
                        "interaction": [
                            {"code": "read"},
                            {"code": "search-type"},
                        ],
                        "documentation": "Provider feed lists ServiceRequests via GET /api/v1/provider/feed",
                    },
                    {
                        "type": "QuestionnaireResponse",
                        "interaction": [{"code": "create"}],
                        "documentation": "Accepted via provider report endpoint POST /api/v1/provider/report/{sr_guid}",
                    },
                ],
                "operation": [
                    {
                        "name": "provider-report",
                        "definition": "POST /api/v1/provider/report/{service_request_guid}",
                        "documentation": "Submit observation report from a provider. Requires Provider Access Token.",
                    },
                    {
                        "name": "provider-feed",
                        "definition": "GET /api/v1/provider/feed",
                        "documentation": "Poll for pending ServiceRequests. Requires Provider Access Token with read scope.",
                    },
                    {
                        "name": "provider-download",
                        "definition": "GET /api/v1/provider/download/{service_request_guid}",
                        "documentation": "Download FHIR Bundle + grant_token for a ServiceRequest.",
                    },
                    {
                        "name": "provider-ack",
                        "definition": "POST /api/v1/provider/receipt/{receipt_token}/ack",
                        "documentation": "Acknowledge a delivery receipt.",
                    },
                    {
                        "name": "observations-search",
                        "definition": "GET /api/v1/observations?organization={org_guid}",
                        "documentation": "Search observations by requesting organisation. Requires SSO Bearer token with analysis phase access. Returns FHIR R5 searchset Bundle with full metadata (basedOn, performer, referenceRange, contract/plan extensions).",
                    },
                    {
                        "name": "vectors-by-patient",
                        "definition": "GET /api/v1/vectors/by-patient/{patient_guid}",
                        "documentation": "Query observation vectors by patient (experimental).",
                    },
                    {
                        "name": "vectors-by-careplan",
                        "definition": "GET /api/v1/vectors/by-careplan/{careplan_guid}",
                        "documentation": "Query observation vectors by careplan (experimental).",
                    },
                    {
                        "name": "vectors-similar",
                        "definition": "POST /api/v1/vectors/similar",
                        "documentation": "Find semantically similar observation vectors (experimental).",
                    },
                    {
                        "name": "vectors-resolve",
                        "definition": "POST /api/v1/vectors/resolve/{service_request_guid}",
                        "documentation": "Trigger GUID chain resolution and vector construction for a ServiceRequest.",
                    },
                ],
                "security": {
                    "service": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/restful-security-service", "code": "OAuth"}]}],
                    "description": "Provider endpoints require Provider Access Tokens (PAT) issued by request.pdhc. Analysis endpoints require SSO Bearer tokens validated via sso.pdhc /api/auth/me/service.",
                },
            }],
        })
