"""Client for ips.pdhc — fetch active spärr (PatientBlock) entries.

Ticket #206 / spärr Phase 3 (gateway half). Mirrors the dashboard-side
client in dashboard.pdhc/app/services/ips_client.py — same TTL cache,
same filter semantics — but groups requests per-patient at the point of
use (gateway returns observations spanning many patients in one bundle).

Performance target (ticket acceptance criteria): the IPS call + filter
must add < 50 ms per query at hit, and the cache hit-rate should be
> 90 % for typical workloads. The 30 s TTL is sized around that —
legal-confirmed 2026-06-04 as the acceptable staleness window.

Webhook-driven invalidation (IPS Renov 6 / #202) is wired through
``invalidate(patient_guid)``; until #202 ships the cache is bounded by
TTL alone.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Iterable

import requests
from flask import current_app


DEFAULT_TTL_SECONDS = 30
DEFAULT_TIMEOUT = 4.0


@dataclass(frozen=True)
class Block:
    guid: str
    patient_guid: str
    source_scope_type: str
    source_scope_id: str
    is_active: bool
    lift_kind: str | None
    lift_concept_guids: list | None
    lift_from_date: str | None
    lift_until_date: str | None

    @classmethod
    def from_dict(cls, d: dict) -> "Block":
        return cls(
            guid=str(d.get("guid")),
            patient_guid=str(d.get("patient_guid")),
            source_scope_type=d.get("source_scope_type") or "clinic",
            source_scope_id=str(d.get("source_scope_id")),
            is_active=bool(d.get("is_active")),
            lift_kind=d.get("lift_kind"),
            lift_concept_guids=d.get("lift_concept_guids"),
            lift_from_date=d.get("lift_from_date"),
            lift_until_date=d.get("lift_until_date"),
        )


class IpsClient:
    def __init__(
        self,
        token: str | None = None,
        service_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.token = token
        self.service_key = service_key
        self.base_url = (
            base_url or os.environ.get("IPS_BASE_URL", "")
        ).rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.service_key:
            h["X-Service-Key"] = self.service_key
        return h

    def fetch_active_blocks(self, patient_guid: str) -> list[Block]:
        if not self.base_url or not patient_guid:
            return []
        url = f"{self.base_url}/api/v1/patients/{patient_guid}/blocks"
        try:
            r = requests.get(
                url, params={"active": "true"},
                headers=self._headers(), timeout=self.timeout,
            )
        except requests.RequestException:
            current_app.logger.warning(
                "ips block fetch failed (network) for %s",
                patient_guid[:12],
            )
            return []
        if r.status_code == 404:
            return []
        if r.status_code >= 400:
            current_app.logger.warning(
                "ips block fetch %s -> %s", patient_guid[:12], r.status_code
            )
            return []
        payload = r.json() or {}
        raw = payload.get("blocks") or payload.get("entry") or []
        return [Block.from_dict(b) for b in raw if isinstance(b, dict)]


class _BlockCache:
    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[str, tuple[float, list[Block]]] = {}
        # Hit/miss counters — exposed via stats() so the perf gate in
        # the ticket ("cache hit-rate > 90% on typical workloads") can
        # be verified against a real workload.
        self.hits = 0
        self.misses = 0

    def get(self, patient_guid: str) -> list[Block] | None:
        with self._lock:
            entry = self._data.get(patient_guid)
            if not entry or time.monotonic() >= entry[0]:
                self.misses += 1
                return None
            self.hits += 1
            return entry[1]

    def put(self, patient_guid: str, blocks: list[Block]) -> None:
        with self._lock:
            self._data[patient_guid] = (time.monotonic() + self.ttl, blocks)

    def invalidate(self, patient_guid: str | None = None) -> None:
        with self._lock:
            if patient_guid is None:
                self._data.clear()
            else:
                self._data.pop(patient_guid, None)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            hit_rate = (self.hits / total) if total else 0.0
            return {
                "hits": self.hits, "misses": self.misses,
                "hit_rate": hit_rate, "size": len(self._data),
            }


_cache = _BlockCache()


def invalidate(patient_guid: str | None = None) -> None:
    """Webhook entry point — drop cache for a patient (or everyone).

    IPS Renov 6 / #202 webhook subscriber will call this with the
    patient guid embedded in the block.created / block.lifted event.
    """
    _cache.invalidate(patient_guid)


def cache_stats() -> dict:
    return _cache.stats()


def get_active_blocks(
    patient_guid: str,
    *,
    client: IpsClient | None = None,
    use_cache: bool = True,
) -> list[Block]:
    if not patient_guid:
        return []
    if use_cache:
        cached = _cache.get(patient_guid)
        if cached is not None:
            return cached
    client = client or _default_client()
    blocks = [b for b in client.fetch_active_blocks(patient_guid) if b.is_active]
    if use_cache:
        _cache.put(patient_guid, blocks)
    return blocks


def _default_client() -> IpsClient:
    """Gateway is a service — it talks to ips with its service key, not
    the caller's bearer (callers are dashboards / federated consumers
    whose SSO blob would have its own org scope, which we deliberately
    don't want filtering the block list — the block list is patient-
    scoped, not caller-scoped)."""
    return IpsClient(
        service_key=current_app.config.get("GATEWAY_PDHC_SERVICE_KEY") or None,
        base_url=current_app.config.get("IPS_BASE_URL") or None,
    )


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------


def blocked_clinic_ids(blocks: Iterable[Block]) -> set[str]:
    """Clinic-scope source ids for active blocks. v1 ignores caregiver-
    scope blocks (#204)."""
    return {
        b.source_scope_id
        for b in blocks
        if b.is_active and b.source_scope_type == "clinic"
    }


def filter_blocked_observations(
    rows: list,
    blocks_by_patient: dict,
) -> list:
    """Drop InboundObservation rows whose provider_org matches an active
    block for that row's patient. Honours indispensable-care lifts via
    the mechanical filter (lift_concept_guids + date range).

    ``rows``: iterable of InboundObservation-shaped objects (need
    ``patient_guid``, ``provider_org_guid``, ``concept_guid``, and a
    timestamp attribute; we accept either ``recorded_at`` from the
    raw FHIR doc or fall back to ``received_at``).

    ``blocks_by_patient``: ``{patient_guid: [Block, ...]}`` — caller
    is expected to batch the IPS calls per unique patient_guid in the
    result set.
    """
    out = []
    for r in rows:
        pid = getattr(r, "patient_guid", None)
        blocks = blocks_by_patient.get(pid) or []
        if not blocks:
            out.append(r)
            continue
        blocked = blocked_clinic_ids(blocks)
        provider = getattr(r, "provider_org_guid", None)
        if provider not in blocked:
            out.append(r)
            continue
        # Active block hits this provider; check lifts.
        if _row_passes_any_lift(r, blocks, provider):
            out.append(r)
    return out


def _row_passes_any_lift(row, blocks: list[Block], provider: str) -> bool:
    concept = str(getattr(row, "concept_guid", "") or "")
    observed_iso = _observed_iso(row)
    for b in blocks:
        if b.source_scope_id != provider or b.source_scope_type != "clinic":
            continue
        if b.lift_kind != "indispensable_care" or not b.lift_concept_guids:
            continue
        allowed = {str(g) for g in (b.lift_concept_guids or [])}
        if concept not in allowed:
            continue
        if b.lift_from_date and observed_iso and observed_iso < b.lift_from_date:
            continue
        if b.lift_until_date and observed_iso and observed_iso > b.lift_until_date:
            continue
        return True
    return False


def _observed_iso(row) -> str | None:
    """Pick the best timestamp for the mechanical-filter date range."""
    raw = getattr(row, "fhir_observation_json", None) or {}
    rec = raw.get("recorded_at") if isinstance(raw, dict) else None
    if rec:
        return str(rec)
    received = getattr(row, "received_at", None)
    if received is not None:
        try:
            return received.isoformat()
        except AttributeError:
            return str(received)
    return None


def fetch_blocks_for_patients(
    patient_guids: Iterable[str],
    *,
    client: IpsClient | None = None,
) -> dict[str, list[Block]]:
    """Batch convenience — one IPS lookup per unique patient guid,
    returning ``{patient_guid: [Block...]}``. De-dupes and respects
    the cache. Used by /api/v1/observations after candidate selection.
    """
    out: dict[str, list[Block]] = {}
    seen: set[str] = set()
    for pid in patient_guids:
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out[pid] = get_active_blocks(pid, client=client)
    return out
