"""HTTP client for the loopback Local Companion service."""

import os

DEFAULT_BASE_URL = "http://127.0.0.1:8791"
PROTOCOL_VERSION = 1


class CompanionError(RuntimeError):
    pass


class CompanionUnavailable(CompanionError):
    pass


class CompanionUncertain(CompanionError):
    pass


def base_url():
    return (
        os.environ.get("ECHOMATE_PC_CONTROL_URL") or DEFAULT_BASE_URL
    ).rstrip("/")


def _request(method, path, *, payload=None, timeout=3):
    import requests

    try:
        response = requests.request(
            method,
            f"{base_url()}{path}",
            json=payload,
            timeout=(1.5, timeout),
        )
    except requests.ConnectionError as exc:
        raise CompanionUnavailable("local companion is offline") from exc
    except requests.Timeout as exc:
        raise CompanionUncertain("local companion response timed out") from exc
    except requests.RequestException as exc:
        raise CompanionError(type(exc).__name__) from exc

    if response.status_code == 404:
        return None

    try:
        data = response.json()
    except ValueError as exc:
        raise CompanionError("local companion returned invalid JSON") from exc

    if not response.ok or not data.get("ok"):
        message = str(data.get("error") or f"HTTP {response.status_code}")
        raise CompanionError(message[:200])
    return data


def health():
    data = _request("GET", "/health", timeout=2)
    if data is None:
        raise CompanionUnavailable("local companion health endpoint missing")
    return data


def submit_job(payload):
    try:
        data = _request("POST", "/jobs", payload=payload, timeout=4)
    except CompanionUncertain:
        raise
    except CompanionUnavailable:
        raise
    if data is None:
        raise CompanionError("local companion rejected job submission")
    return data


def get_job(job_id):
    return _request("GET", f"/jobs/{job_id}", timeout=3)


def cancel_job(job_id):
    return _request("POST", f"/jobs/{job_id}/cancel", payload={}, timeout=3)
