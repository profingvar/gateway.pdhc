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


# ---------------------------------------------------------------------------
# #1025 — where the EPS address and key come from
# ---------------------------------------------------------------------------
#
# ips.pdhc is being renamed eps.pdhc (epic #1015, phase 3). EPS_BASE_URL /
# EPS_API_KEY are read FIRST; IPS_BASE_URL / IPS_API_KEY are the DEPRECATED
# fallback, kept until ips' per-caller counter shows no traffic to the old
# name. Every read goes through eps_env(), so the precedence lives here only.
# The Flask config keys (IPS_*, set by tests) keep their names. There is NO
# default: both names empty is unset, and the client fetches nothing, exactly
# as before (#833).

EPS_SETTING_SUFFIXES = ("BASE_URL", "API_KEY")


def eps_env(suffix: str) -> str:
    """``EPS_<suffix>`` if non-empty, else ``IPS_<suffix>``, else ``""``."""
    if suffix not in EPS_SETTING_SUFFIXES:
        raise KeyError(f"not an EPS setting: {suffix!r}")
    return os.environ.get(f"EPS_{suffix}", "") or os.environ.get(f"IPS_{suffix}", "")


def eps_setting(suffix: str, config=None) -> str:
    """``config["IPS_<suffix>"]`` if set, else the environment (``eps_env``).
    ``config`` defaults to ``current_app.config``."""
    if config is None:
        config = current_app.config
    return config.get(f"IPS_{suffix}") or eps_env(suffix)


DEFAULT_TTL_SECONDS = 30
DEFAULT_TIMEOUT = 4.0


class BlockStateUnknown(Exception):
    """eps could not tell us whether this patient has blocks (#1047).

    NOT the same answer as "no blocks". Until #1047 the client returned ``[]``
    for an unset address, a network error and every HTTP >= 400 alike, so a
    refusal from eps read as "this patient has blocked nothing", was cached
    for 30 s, and the observation filter passed every row. Measured in
    production 2026-11-22: eps answered 403 to gateway's key for a real
    patient, and — separately — every 200 parsed as empty because eps sends
    ``items`` and this client read ``blocks``. The control was dead both ways.

    Only two answers mean "no blocks": HTTP 200 carrying an empty list, and
    HTTP 404 (eps does not know the patient, so it holds no block for them).
    Everything else raises this, is never cached, and the caller decides
    (the observation read fails closed, see app/api/observations.py).

    ``reason`` is a short stable code for logs, audit and the response:
    ``not_configured``, ``network``, ``http_<status>``, ``unreadable_payload``,
    ``redacted_block``.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}{': ' + detail if detail else ''}")
        self.reason = reason
        self.detail = detail


#: The keys eps may carry the list under, in the order tried. eps answers
#: ``{"items": [...], "total": N}``; ``blocks`` is a temporary duplicate it
#: emits for older readers; ``entry`` is the legacy FHIR-ish spelling.
BLOCK_LIST_KEYS = ("items", "blocks", "entry")


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
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.token = token
        self.service_key = service_key
        self.api_key = api_key or eps_env("API_KEY") or None
        self.base_url = (base_url or eps_env("BASE_URL")).rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        # ips.pdhc reads ONLY the Authorization header (Bearer or ApiKey);
        # X-Service-Key is silently ignored (infra_ips_auth_header_scheme), so
        # the block fetch must present a registered ips ApiKey to work.
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        elif self.api_key:
            h["Authorization"] = f"ApiKey {self.api_key}"
        if self.service_key:
            h["X-Service-Key"] = self.service_key  # harmless; ignored by ips
        return h

    def fetch_active_blocks(self, patient_guid: str) -> list[Block]:
        """The patient's blocks as eps holds them.

        Returns a list — empty ONLY when eps said so (200 with an empty list)
        or does not know the patient (404). Raises ``BlockStateUnknown`` when
        the answer could not be had or could not be applied (#1047); it never
        turns "could not find out" into "no blocks".
        """
        if not patient_guid:
            return []
        if not self.base_url:
            raise BlockStateUnknown(
                "not_configured", "EPS_BASE_URL / IPS_BASE_URL is not set")
        url = f"{self.base_url}/api/v1/patients/{patient_guid}/blocks"
        try:
            r = requests.get(
                url, params={"active": "true"},
                headers=self._headers(), timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise BlockStateUnknown("network", type(e).__name__) from e
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            raise BlockStateUnknown(f"http_{r.status_code}")
        try:
            payload = r.json()
        except ValueError as e:
            raise BlockStateUnknown("unreadable_payload", "not JSON") from e
        raw = None
        if isinstance(payload, dict):
            for key in BLOCK_LIST_KEYS:
                if isinstance(payload.get(key), list):
                    raw = payload[key]
                    break
        if raw is None:
            raise BlockStateUnknown(
                "unreadable_payload",
                "none of " + "/".join(BLOCK_LIST_KEYS) + " is a list")
        blocks = []
        for b in raw:
            if not isinstance(b, dict):
                raise BlockStateUnknown("unreadable_payload", "a block is not an object")
            # eps redacts a block's source for a caller not entitled to see
            # it (`redacted: true`, `source_scope_id: null`). The filter
            # matches on that source, so a redacted ACTIVE block is one we
            # know exists and cannot apply: unknown, never "no block".
            if b.get("is_active") and (b.get("redacted")
                                       or not b.get("source_scope_id")):
                raise BlockStateUnknown(
                    "redacted_block", "an active block arrived without its source")
            blocks.append(Block.from_dict(b))
        return blocks


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
    # BlockStateUnknown propagates from here BEFORE the put below, so a
    # failure is never cached (#1047): the next read asks eps again rather
    # than replaying "unknown" — or, as before, a false "no blocks" — for 30 s.
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
        base_url=eps_setting("BASE_URL") or None,
        api_key=eps_setting("API_KEY") or None,
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
    unknown: dict | None = None,
) -> dict[str, list[Block]]:
    """Batch convenience — one IPS lookup per unique patient guid,
    returning ``{patient_guid: [Block...]}``. De-dupes and respects
    the cache. Used by /api/v1/observations after candidate selection.

    #1047: a patient whose block state could not be established is NOT in
    the returned dict (absent there has always meant "no blocks", which this
    patient is not). Pass a dict as ``unknown`` and it is filled with
    ``{patient_guid: reason}`` for those patients; without one the
    ``BlockStateUnknown`` propagates, so no caller can lose it by accident.
    """
    out: dict[str, list[Block]] = {}
    seen: set[str] = set()
    for pid in patient_guids:
        if not pid or pid in seen:
            continue
        seen.add(pid)
        try:
            out[pid] = get_active_blocks(pid, client=client)
        except BlockStateUnknown as e:
            if unknown is None:
                raise
            unknown[pid] = e.reason
    return out
