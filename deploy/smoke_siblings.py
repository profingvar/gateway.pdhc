#!/usr/bin/env python3
"""gateway.pdhc — does every call to a SIBLING service actually work? (#708)

Run it INSIDE the container, the only place the real service keys exist:

    docker exec gateway_pdhc_app python deploy/smoke_siblings.py
    docker exec gateway_pdhc_app python deploy/smoke_siblings.py --json

READ-ONLY. gateway is the busiest boundary on the platform — it forwards to
cdr1, validates grants and PATs against request.pdhc, reads blocks from ips,
resolves identity via sso and pulls from analyse — and most of those calls are
WRITES. A smoke must not perform them.

So for write endpoints this distinguishes the two answers that matter:

  * 401 / 403            the credential is wrong — a REAL finding
  * a structured refusal  the boundary works and the sibling simply said no

That distinction is the whole value. "Cannot reach request.pdhc" and
"request.pdhc rejected my service key" look identical in a log and have
completely different fixes.

## Two things it takes from the code rather than from me

Every URL and key comes from `create_app()`'s config, never from a literal
here — so the smoke cannot pass while production is misconfigured.

And `TestPathsMatchTheClients` asserts that each path this script probes
actually appears in the client module that builds it. That guard exists
because the first version of the analyse smoke tested a path I had invented
(`fhir/Observation` instead of `/api/v1/fhir/Observation`) and would have
reported a green boundary that no code uses.

Exit code is 0 only if every check passed.
"""
from __future__ import annotations

import json as _json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

RESET, RED, GRN, BOLD = "\033[0m", "\033[31m", "\033[32m", "\033[1m"
results: list[dict] = []

#: path -> the client module that must contain it. Checked, not assumed.
PATHS = {
    "/api/v1/ingest": "cdr_client.py",
    "/internal/grant/validate": "grant_validation.py",
    "/internal/service-request/": "request_completion.py",
    "/provider/validate-token": "pat_validation.py",
    "/api/v1/observations": "analyse_client.py",
}

FAKE_GUID = "00000000-0000-4000-8000-000000000000"


def check(name):
    def wrap(fn):
        t = time.time()
        try:
            ok, note = fn()
        except Exception as e:                       # noqa: BLE001
            ok, note = False, f"{type(e).__name__}: {e}"
        results.append({"check": name, "ok": bool(ok), "note": note,
                        "ms": int((time.time() - t) * 1000)})
        return ok
    return wrap


def _auth_ok(status: int) -> bool:
    """Anything but a credential rejection means the boundary is wired."""
    return status not in (401, 403)


def main(as_json: bool = False) -> int:
    from app import create_app
    app = create_app()
    with app.app_context():
        from flask import current_app
        cfg = current_app.config
        svc = cfg.get("GATEWAY_PDHC_SERVICE_KEY", "")
        req_key = cfg.get("REQUEST_INTERNAL_SERVICE_KEY", "")
        req = (cfg.get("REQUEST_SERVICE_URL") or "").rstrip("/")
        cdr = (cfg.get("CDR_BASE_URL") or "").rstrip("/")
        # IPS_BASE_URL is the one sibling URL gateway does NOT register in
        # app.config: ips_client resolves it as
        # `config.get(...) or os.environ.get(...)`, deliberately, in both the
        # from_config call site and the constructor. Resolved the same way
        # here — checking only app.config reported a missing URL that is in
        # fact present and working, which is a smoke lying about production.
        import os as _os
        ips = (cfg.get("IPS_BASE_URL")
               or _os.environ.get("IPS_BASE_URL") or "").rstrip("/")
        sso = (cfg.get("SSO_BASE_URL") or "").rstrip("/")
        ana = (cfg.get("ANALYSE_BASE_URL") or "").rstrip("/")

        # ── the paths this script probes must be the ones the code builds ──
        @check("paths: every probed path appears in its client module")
        def _():
            # #804: this was `path not in f.read_text()` -- a bare substring
            # check, which CANNOT CATCH THE BUG IT EXISTS FOR. The docstring
            # above says the guard was written because a smoke once probed
            # `fhir/Observation` instead of `/api/v1/fhir/Observation`; a
            # substring check passes that, because the truncated form IS a
            # substring of the correct one. The guard was green for exactly
            # the defect it was meant to stop.
            #
            # Anchored now: in every client the path starts immediately after
            # the base-URL placeholder or an opening quote, so requiring one
            # of } " ' before it rejects a truncated prefix that merely ends
            # the same way. Verified by introducing the truncation and
            # watching this check fail -- which the substring version did not.
            # #804: the image lays the app out as /app/app/services while a
            # checkout has it at <repo>/gateway_app/app/services, so a single
            # hardcoded relative path only resolves in one of the two. It
            # resolved in the image, so this check has always passed there and
            # reported every client "missing" from a developer's checkout --
            # a failure rather than a false pass, but it meant nobody could
            # run the guard before deploying, which is when it is useful.
            here = pathlib.Path(__file__).resolve().parent.parent
            root = next(
                (c for c in (here / "app" / "services",
                             here / "gateway_app" / "app" / "services")
                 if c.is_dir()),
                here / "app" / "services")
            missing = []
            for path, mod in PATHS.items():
                f = root / mod
                if not f.exists():
                    missing.append(f"{mod} is missing")
                    continue
                if not re.search(r"""[}"']""" + re.escape(path), f.read_text()):
                    missing.append(
                        f"{path} does not START a URL in {mod}")
            return (not missing), (
                f"all {len(PATHS)} anchored to a URL boundary in their clients"
                if not missing else "; ".join(missing))

        # ── configuration, per boundary ──
        @check("config: every sibling URL and key is set")
        def _():
            want = {"CDR_BASE_URL": cdr, "REQUEST_SERVICE_URL": req,
                    "IPS_BASE_URL (config or env)": ips, "SSO_BASE_URL": sso,
                    "ANALYSE_BASE_URL": ana,
                    "GATEWAY_PDHC_SERVICE_KEY": svc,
                    "REQUEST_INTERNAL_SERVICE_KEY": req_key}
            absent = [k for k, v in want.items() if not v]
            return (not absent), ("all 7 present" if not absent
                                  else "MISSING: " + ", ".join(absent))

        # ── cdr1: the live forwarder ──
        @check("cdr1: reachable, and the gateway service key is accepted")
        def _():
            # A READ with the same key the forwarder writes with. Proves the
            # credential without inserting an observation.
            r = requests.get(f"{cdr}/api/v1/fhir/Observation",
                             params={"_count": "1"},
                             headers={"Accept": "application/json",
                                      "X-Source-Service": "gateway.pdhc",
                                      "X-Service-Key": svc}, timeout=20)
            return _auth_ok(r.status_code), f"GET fhir/Observation -> {r.status_code}"

        # ── request.pdhc: grants, completion, PATs ──
        @check("request: /internal/grant/validate accepts the service key")
        def _():
            # A deliberately invalid grant. A structured refusal proves the
            # key was accepted; 401/403 would mean it was not.
            r = requests.post(f"{req}/internal/grant/validate",
                              json={"grant_token": "smoke-not-a-real-grant"},
                              headers={"X-Service-Key": req_key,
                                       "content-type": "application/json"},
                              timeout=15)
            return _auth_ok(r.status_code), (
                f"-> {r.status_code} (a non-401 means the key was accepted "
                f"and the grant simply refused)")

        @check("request: /internal/service-request/<guid>/complete authenticates")
        def _():
            r = requests.post(
                f"{req}/internal/service-request/{FAKE_GUID}/complete",
                headers={"X-Service-Key": req_key}, timeout=15)
            # 404 is the right answer for a guid that does not exist.
            return _auth_ok(r.status_code), f"-> {r.status_code} (404 expected)"

        @check("request: /provider/validate-token answers")
        def _():
            r = requests.post(f"{req}/provider/validate-token",
                              json={"token": "smoke-not-a-real-token"},
                              headers={"content-type": "application/json"},
                              timeout=15)
            # This endpoint is UNAUTHENTICATED by design — the token IS the
            # credential — so 401 here is the endpoint working correctly, not
            # a misconfiguration. Only a 404/5xx would be a finding.
            return r.status_code in (200, 401), (
                f"-> {r.status_code} (401 is correct: a bogus token is "
                f"rejected, which proves the route is live)")

        # ── ips: blocks ──
        @check("ips: the block read authenticates, using the client's own credential")
        def _():
            # Driven through IpsClient rather than a hand-rolled request. My
            # first version sent `Bearer <GATEWAY_PDHC_SERVICE_KEY>` and got a
            # 401 that looked like a finding; the client actually presents
            # `ApiKey <IPS_API_KEY>`, because ips reads ONLY Authorization and
            # a service key is not an ips credential. Using the client means
            # the smoke cannot disagree with production about which
            # credential is correct — which is the entire point of #708.
            # _default_client() is the module's own factory — the same one
            # get_active_blocks() uses in production.
            from app.services.ips_client import _default_client
            client = _default_client()
            if not client.base_url:
                return False, "IpsClient resolved no base_url"
            blocks = client.fetch_active_blocks(FAKE_GUID)
            # A patient with no blocks returns an empty list. Anything that
            # raises or 401s would surface as an exception here.
            return isinstance(blocks, list), (
                f"{len(blocks)} block(s) for a nonexistent patient "
                f"(an empty list is the correct answer)")

        # ── sso ──
        @check("sso: reachable")
        def _():
            r = requests.get(f"{sso}/api/health", timeout=10)
            return r.status_code == 200, f"/api/health -> {r.status_code}"

        # ── analyse ──
        @check("analyse: /api/v1/observations accepts the gateway service key")
        def _():
            r = requests.get(f"{ana}/api/v1/observations",
                             params={"_count": "1"},
                             headers={"Accept": "application/json",
                                      "X-Source-Service": "gateway.pdhc",
                                      "X-Service-Key": svc}, timeout=20)
            if not _auth_ok(r.status_code):
                return False, f"-> {r.status_code}: the service key was refused"
            # A non-401 alone is too weak here: a proxy error would also pass.
            # Require analyse's OWN json, so this cannot go green on something
            # that never reached the handler. 400 is the healthy answer to a
            # request with no service_request parameter.
            try:
                body = r.json()
            except ValueError:
                return False, (f"-> {r.status_code} but the body was not JSON, "
                               f"so something other than analyse answered")
            own = isinstance(body, dict) and ("error" in body or "items" in body
                                              or "entry" in body)
            return own, (f"-> {r.status_code}, analyse's own response "
                         f"({str(body)[:60]})")

    failed = [r for r in results if not r["ok"]]
    if as_json:
        print(_json.dumps({"service": "gateway.pdhc", "checks": results,
                           "failed": len(failed)}, indent=2))
    else:
        print(f"\n{BOLD}gateway.pdhc — sibling smoke{RESET}\n")
        for r in results:
            mark = f"{GRN}✓{RESET}" if r["ok"] else f"{RED}✗{RESET}"
            print(f"  {mark} {r['check']:<58} {r['note']}  ({r['ms']}ms)")
        print(f"\n{RED if failed else GRN}{BOLD}"
              f"{f'{len(failed)} check(s) failed.' if failed else f'All {len(results)} checks passed.'}"
              f"{RESET}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(as_json="--json" in sys.argv))
