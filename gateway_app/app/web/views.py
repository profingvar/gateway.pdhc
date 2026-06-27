import logging
from datetime import datetime, timezone, timedelta
from flask import render_template, request as flask_request, flash, redirect, url_for, jsonify, current_app
from . import web_bp
from .auth import require_login, require_analyst, require_admin
from ..models import AuditLog, ServiceRequestStatus
from ..models.guid_resolution_cache import GuidResolutionCache
from ..services.sso_service import get_access_blob, is_admin
from ..extensions import db

logger = logging.getLogger(__name__)


@web_bp.route('/')
@require_login
def dashboard():
    return render_template('dashboard.html')


@web_bp.route('/requests')
@require_analyst
def requests_list():
    """List tracked service requests with completion/expiry status."""
    from ..services.request_completion import RequestCompletionService

    status_filter = flask_request.args.get('status', '').strip() or None
    page = flask_request.args.get('page', 1, type=int)

    # Check for newly expired requests on each page load
    RequestCompletionService.check_expirations()

    pagination = RequestCompletionService.get_all(
        status_filter=status_filter, page=page,
    )

    return render_template(
        'requests_list.html',
        requests=pagination.items,
        pagination=pagination,
        status_filter=status_filter or '',
    )


@web_bp.route('/pats')
@require_admin
def pat_activity():
    """PAT validation/rejection activity grouped by provider org."""
    actor_filter = flask_request.args.get('actor_guid', '').strip()

    query = AuditLog.query.filter(
        AuditLog.event_type.in_(['pat.validated', 'pat.rejected']),
        AuditLog.actor_guid.isnot(None),
    ).order_by(AuditLog.created_at.desc())

    if actor_filter:
        query = query.filter(AuditLog.actor_guid == actor_filter)

    events = query.all()

    # Aggregate per provider org (ordered by last_seen desc)
    stats = {}
    order = []
    for e in events:
        key = e.actor_guid
        if key not in stats:
            stats[key] = {'validated': 0, 'rejected': 0, 'last_seen': None, 'last_ip': None}
            order.append(key)
        s = stats[key]
        if e.event_type == 'pat.validated':
            s['validated'] += 1
        else:
            s['rejected'] += 1
        if s['last_seen'] is None:
            s['last_seen'] = e.created_at
            s['last_ip'] = e.ip_address

    providers = [{'actor_guid': k, **stats[k]} for k in order]

    total_validated = sum(p['validated'] for p in providers)
    total_rejected = sum(p['rejected'] for p in providers)

    return render_template(
        'pats.html',
        providers=providers,
        actor_filter=actor_filter,
        total_validated=total_validated,
        total_rejected=total_rejected,
    )


@web_bp.route('/audit')
@require_admin
def audit_log():
    """Paginated audit log with event type and actor filters."""
    event_filter = flask_request.args.get('event_type', '').strip()
    actor_filter = flask_request.args.get('actor_guid', '').strip()
    page = flask_request.args.get('page', 1, type=int)
    per_page = 50

    known_events = [
        'pat.validated', 'pat.rejected',
        'report.received', 'report.rejected',
        'feed.accessed', 'bundle.downloaded',
        'bundle.pushed', 'bundle.push_failed',
        'receipt.acknowledged',
    ]

    query = AuditLog.query.order_by(AuditLog.created_at.desc())
    if event_filter:
        query = query.filter(AuditLog.event_type == event_filter)
    if actor_filter:
        query = query.filter(AuditLog.actor_guid == actor_filter)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        'audit_log.html',
        entries=pagination.items,
        pagination=pagination,
        event_filter=event_filter,
        actor_filter=actor_filter,
        known_events=known_events,
    )


@web_bp.route('/grants')
@require_admin
def grant_status():
    """Grant token validity status for tracked service requests."""
    view_filter = flask_request.args.get('view', 'active').strip()
    page = flask_request.args.get('page', 1, type=int)
    per_page = 50

    now = datetime.now(timezone.utc)
    soon = now + timedelta(hours=24)
    # SQLite returns naive datetimes; strip tz for template comparisons
    now_naive = now.replace(tzinfo=None)
    soon_naive = soon.replace(tzinfo=None)

    query = ServiceRequestStatus.query.order_by(ServiceRequestStatus.grant_expires_at.asc())

    if view_filter == 'active':
        query = query.filter(ServiceRequestStatus.status == 'active')
    elif view_filter == 'expiring':
        query = query.filter(
            ServiceRequestStatus.status == 'active',
            ServiceRequestStatus.grant_expires_at.isnot(None),
            ServiceRequestStatus.grant_expires_at <= soon,
        )
    elif view_filter == 'expired':
        query = query.filter(ServiceRequestStatus.status.in_(['expired', 'partial']))

    total_active = ServiceRequestStatus.query.filter(
        ServiceRequestStatus.status == 'active'
    ).count()
    total_expiring = ServiceRequestStatus.query.filter(
        ServiceRequestStatus.status == 'active',
        ServiceRequestStatus.grant_expires_at.isnot(None),
        ServiceRequestStatus.grant_expires_at <= soon,
    ).count()
    total_expired = ServiceRequestStatus.query.filter(
        ServiceRequestStatus.status.in_(['expired', 'partial'])
    ).count()

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return render_template(
        'grant_status.html',
        grants=pagination.items,
        pagination=pagination,
        view_filter=view_filter,
        now=now_naive,
        soon=soon_naive,
        total_active=total_active,
        total_expiring=total_expiring,
        total_expired=total_expired,
    )


# ---------------------------------------------------------------------------
# Admin: Cache management
# ---------------------------------------------------------------------------

def _cache_stats():
    """Return cache statistics grouped by source_type."""
    entries = GuidResolutionCache.query.all()
    buckets = {}
    for e in entries:
        b = buckets.setdefault(e.source_type, {
            'source_type': e.source_type,
            'total': 0, 'expired': 0, 'fresh': 0,
            'oldest': None, 'newest': None,
        })
        b['total'] += 1
        if e.is_expired():
            b['expired'] += 1
        else:
            b['fresh'] += 1
        if e.fetched_at:
            if b['oldest'] is None or e.fetched_at < b['oldest']:
                b['oldest'] = e.fetched_at
            if b['newest'] is None or e.fetched_at > b['newest']:
                b['newest'] = e.fetched_at
    return sorted(buckets.values(), key=lambda b: b['source_type'])


@web_bp.route('/admin/cache')
@require_admin
def cache_management():
    """Cache statistics and flush controls."""
    stats = _cache_stats()
    total = sum(s['total'] for s in stats)
    total_expired = sum(s['expired'] for s in stats)

    # Recent cache entries for inspection
    recent = GuidResolutionCache.query.order_by(
        GuidResolutionCache.fetched_at.desc()
    ).limit(25).all()

    return render_template(
        'cache_management.html',
        stats=stats,
        total=total,
        total_expired=total_expired,
        recent=recent,
    )


@web_bp.route('/admin/cache/flush', methods=['POST'])
@require_admin
def cache_flush():
    """Flush stale or all cache entries."""
    mode = flask_request.form.get('mode', 'stale')
    source_type = flask_request.form.get('source_type', '').strip() or None

    query = GuidResolutionCache.query
    if source_type:
        query = query.filter(GuidResolutionCache.source_type == source_type)

    if mode == 'all':
        count = query.count()
        query.delete(synchronize_session=False)
        db.session.commit()
        flash(f'Flushed all {count} cache entries'
              + (f' of type "{source_type}"' if source_type else '') + '.', 'success')
    else:
        # Flush only expired entries
        all_entries = query.all()
        flushed = 0
        for entry in all_entries:
            if entry.is_expired():
                db.session.delete(entry)
                flushed += 1
        db.session.commit()
        flash(f'Flushed {flushed} stale entries'
              + (f' of type "{source_type}"' if source_type else '') + '.', 'success')

    return redirect(url_for('web.cache_management'))


# ---------------------------------------------------------------------------
# Admin: Health report — upstream connectivity + recent errors
# ---------------------------------------------------------------------------

def _probe_service(name, url, headers=None, timeout=5):
    """Probe a service URL and return status dict."""
    import requests as http_requests
    result = {'name': name, 'url': url, 'status': 'unknown', 'latency_ms': None, 'error': None}
    try:
        start = datetime.now(timezone.utc)
        resp = http_requests.get(url, headers=headers or {}, timeout=timeout)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        result['latency_ms'] = round(elapsed, 1)
        result['http_status'] = resp.status_code
        if resp.status_code == 200:
            result['status'] = 'ok'
            try:
                result['detail'] = resp.json()
            except Exception:
                result['detail'] = {}
        else:
            result['status'] = 'degraded'
            result['error'] = f'HTTP {resp.status_code}'
    except http_requests.ConnectionError:
        result['status'] = 'unreachable'
        result['error'] = 'Connection refused'
    except http_requests.Timeout:
        result['status'] = 'timeout'
        result['error'] = f'Timeout after {timeout}s'
    except Exception as e:
        result['status'] = 'error'
        result['error'] = str(e)[:200]
    return result


@web_bp.route('/admin/health-report')
@require_admin
def health_report():
    """Cross-service health report and recent error log."""
    # Probe upstream services
    req_url = current_app.config.get('REQUEST_SERVICE_URL', '')
    req_key = current_app.config.get('REQUEST_INTERNAL_SERVICE_KEY', '')
    contract_url = current_app.config.get('CONTRACT_SERVICE_URL', '')
    sso_url = current_app.config.get('SSO_BASE_URL', '')

    probes = []

    # Gateway's own DB
    from sqlalchemy import text
    gw_db = {'name': 'Gateway DB', 'url': 'local', 'status': 'unknown', 'latency_ms': None, 'error': None}
    try:
        start = datetime.now(timezone.utc)
        db.session.execute(text('SELECT 1'))
        elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        gw_db['latency_ms'] = round(elapsed, 1)
        gw_db['status'] = 'ok'
    except Exception as e:
        gw_db['status'] = 'error'
        gw_db['error'] = str(e)[:200]
    probes.append(gw_db)

    # request.pdhc health
    if req_url:
        # The base is like http://127.0.0.1:9060/api/v1 — health is at /api/health
        health_url = req_url.replace('/api/v1', '') + '/api/health'
        probes.append(_probe_service('request.pdhc', health_url))

    # contract.pdhc health
    if contract_url:
        probes.append(_probe_service('contract.pdhc', f'{contract_url}/health'))

    # SSO health
    if sso_url:
        probes.append(_probe_service('sso.pdhc', f'{sso_url}/api/health'))

    # Count ok / degraded
    ok_count = sum(1 for p in probes if p['status'] == 'ok')
    total_probes = len(probes)

    # Recent error events from audit log (last 24h)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    error_events = AuditLog.query.filter(
        AuditLog.event_type.in_([
            'pat.rejected', 'report.rejected',
            'bundle.push_failed',
        ]),
        AuditLog.created_at >= cutoff,
    ).order_by(AuditLog.created_at.desc()).limit(50).all()

    # Error summary by type
    error_summary = {}
    for evt in error_events:
        error_summary[evt.event_type] = error_summary.get(evt.event_type, 0) + 1

    return render_template(
        'health_report.html',
        probes=probes,
        ok_count=ok_count,
        total_probes=total_probes,
        error_events=error_events,
        error_summary=error_summary,
    )
