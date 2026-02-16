"""Deterministic OpenClaw gateway probes (no LLM calls).

This module is used by hive-daemon to enrich retained `meta/<node>/state`
with *actual* gateway health, cron state, and recent error summaries.

Design goals:
- Deterministic / read-only.
- No agent turns, no LLM calls.
- Avoid secrets in logs / published state.
- Keep payloads small (publish summaries, not full dumps).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hive_daemon.config import OcInstance


def _state_dir_for_profile(profile: str | None) -> Path:
    # OpenClaw convention: ~/.openclaw-<profile> else ~/.openclaw
    home = Path.home()
    return home / (f".openclaw-{profile}" if profile else ".openclaw")


def _config_path_for_profile(profile: str | None) -> Path:
    return _state_dir_for_profile(profile) / "openclaw.json"


def _read_gateway_token(profile: str | None) -> str | None:
    cfg_path = _config_path_for_profile(profile)
    try:
        data = json.loads(cfg_path.read_text())
    except Exception:
        return None
    return (((data.get("gateway") or {}).get("auth") or {}).get("token"))


def _sanitize_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Remove env vars that can hijack gateway selection across profiles."""
    env = dict(base or os.environ)
    for k in (
        "OPENCLAW_GATEWAY_PORT",
        "OPENCLAW_GATEWAY_TOKEN",
        "OPENCLAW_GATEWAY_URL",
        "OPENCLAW_LAUNCHD_LABEL",
    ):
        env.pop(k, None)
    # Self-signed TLS in local gateways.
    env.setdefault("NODE_TLS_REJECT_UNAUTHORIZED", "0")
    env.setdefault("NODE_NO_WARNINGS", "1")
    return env


async def _run_openclaw_json(
    *,
    profile: str | None,
    args: list[str],
    timeout_s: float,
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Run `openclaw ...` and parse stdout as JSON.

    Returns: (ok, json_obj_or_none, err_str)
    """
    cmd = ["openclaw"]
    if profile:
        cmd.extend(["--profile", profile])
    cmd.extend(args)

    env = _sanitize_env()
    if extra_env:
        env.update(extra_env)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return False, None, "openclaw CLI not found"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, None, f"timeout after {timeout_s:.1f}s"

    out = (stdout or b"").decode(errors="replace").strip()
    err = (stderr or b"").decode(errors="replace").strip()

    if proc.returncode != 0:
        msg = (err or out or "unknown error").splitlines()[-1][:300]
        return False, None, msg

    try:
        return True, json.loads(out) if out else None, ""
    except Exception as exc:
        return False, None, f"stdout not json: {exc}"[:300]


def _summarize_cron_list(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return {}
    total = len(jobs)
    enabled = sum(1 for j in jobs if isinstance(j, dict) and j.get("enabled") is True)
    last_fail = None
    for j in jobs:
        if not isinstance(j, dict):
            continue
        st = (j.get("state") or {}) if isinstance(j.get("state"), dict) else {}
        if st.get("lastStatus") == "error":
            last_fail = {
                "id": j.get("id"),
                "name": j.get("name"),
                "lastRunAtMs": st.get("lastRunAtMs"),
                "consecutiveErrors": st.get("consecutiveErrors"),
            }
            break
    return {
        "jobsTotal": total,
        "jobsEnabled": enabled,
        "lastFailure": last_fail,
    }


def _classify_error(msg: str) -> str:
    s = (msg or "").lower()
    if any(x in s for x in ("429", "too many requests", "resource_exhausted", "rate limit", "cooldown")):
        return "cooldown"
    if any(x in s for x in ("context", "maximum context", "context length", "too long", "tokens")):
        return "context"
    if any(x in s for x in ("tls fingerprint mismatch", "unauthorized", "token_missing", "device token mismatch")):
        return "auth"
    return "other"


def _tail_lines(path: Path, max_bytes: int = 65536) -> list[str]:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        return data.decode(errors="replace").splitlines()
    except Exception:
        return []


def _scan_recent_session_errors(state_dir: Path, window_s: int = 3600) -> dict[str, Any]:
    """Best-effort scan of recent session jsonl logs for model/provider errors.

    We only scan session files updated within `window_s` and only tail each file.
    """
    cutoff = time.time() - window_s
    counts: dict[str, int] = {"cooldown": 0, "context": 0, "auth": 0, "other": 0}
    last: dict[str, Any] | None = None

    agents_dir = state_dir / "agents"
    if not agents_dir.exists():
        return {"window_s": window_s, "counts": counts, "last": None}

    # Limit work: only look at a few most-recently modified session logs.
    candidates: list[Path] = []
    for p in agents_dir.glob("*/sessions/*.jsonl"):
        try:
            if p.stat().st_mtime >= cutoff:
                candidates.append(p)
        except Exception:
            continue

    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = candidates[:25]

    for p in candidates:
        for line in reversed(_tail_lines(p)):
            if not line.strip().startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            # OpenClaw error lines often look like:
            # { ..., "stopReason":"error", "errorMessage":"..." }
            stop = obj.get("stopReason")
            err = obj.get("errorMessage") or obj.get("error")
            if stop == "error" and isinstance(err, str) and err.strip():
                kind = _classify_error(err)
                counts[kind] = counts.get(kind, 0) + 1
                if last is None:
                    last = {
                        "kind": kind,
                        "message": err.strip()[:200],
                        "file": str(p.name),
                        "ts": obj.get("timestamp") or int(p.stat().st_mtime),
                        "model": obj.get("model"),
                        "provider": obj.get("provider"),
                    }
                break

    return {"window_s": window_s, "counts": counts, "last": last}


def _summarize_sessions(state_dir: Path, active_window_s: int = 300) -> dict[str, Any]:
    """Summarize session activity from local sessions.json indices (no gateway calls)."""
    now_ms = int(time.time() * 1000)
    active_cutoff_ms = now_ms - (active_window_s * 1000)

    agents_dir = state_dir / "agents"
    if not agents_dir.exists():
        return {}

    total = 0
    active = 0
    by_kind: dict[str, int] = {}

    for agent_dir in agents_dir.iterdir():
        sessions_index = agent_dir / "sessions" / "sessions.json"
        if not sessions_index.exists():
            continue
        try:
            idx = json.loads(sessions_index.read_text())
        except Exception:
            continue

        # sessions.json is a dict keyed by sessionKey.
        for skey, meta in idx.items():
            if not isinstance(meta, dict):
                continue
            total += 1
            updated = meta.get("updatedAt")
            if isinstance(updated, int) and updated >= active_cutoff_ms:
                active += 1

            k = "other"
            if isinstance(skey, str):
                if ":cron:" in skey:
                    k = "cron"
                elif ":subagent:" in skey:
                    k = "subagent"
                elif ":whatsapp:" in skey:
                    k = "whatsapp"
                elif ":telegram:" in skey:
                    k = "telegram"
                elif ":mqtt:" in skey:
                    k = "mqtt"
            by_kind[k] = by_kind.get(k, 0) + 1

    return {
        "count": total,
        "active_window_s": active_window_s,
        "active": active,
        "by_kind": by_kind,
    }


@dataclass
class ProbeResult:
    ok: bool
    ts: int
    data: dict[str, Any]


async def probe_instance(inst: OcInstance, *, cron_timeout_s: float = 5.0) -> ProbeResult:
    """Probe a single configured OC instance deterministically."""
    ts = int(time.time())
    profile = inst.profile
    state_dir = _state_dir_for_profile(profile)

    # --- gateway RPC probe via cron status ---
    # Use cron status/list as a deterministic RPC probe (no agent turns).
    # IMPORTANT: do not pass tokens on argv; rely on profile config.
    gw: dict[str, Any] = {
        "rpcOk": False,
        "port": inst.port,
        "profile": profile,
        "error": None,
    }

    cron_status: dict[str, Any] = {}
    cron_list_summary: dict[str, Any] = {}

    ok1, st, err1 = await _run_openclaw_json(
        profile=profile,
        args=["cron", "status", "--json", "--timeout", "5000"],
        timeout_s=cron_timeout_s,
    )
    if ok1 and isinstance(st, dict):
        gw["rpcOk"] = True
        cron_status = {
            "enabled": st.get("enabled"),
            "jobs": st.get("jobs"),
            "nextWakeAtMs": st.get("nextWakeAtMs"),
        }

        ok2, lst, err2 = await _run_openclaw_json(
            profile=profile,
            args=["cron", "list", "--json", "--timeout", "5000"],
            timeout_s=cron_timeout_s,
        )
        if ok2:
            cron_list_summary = _summarize_cron_list(lst)
        else:
            cron_list_summary = {"error": err2}
    else:
        gw["error"] = err1 or "cron status failed"

    # --- deterministic local session + error summaries ---
    sessions_summary = _summarize_sessions(state_dir)
    errors_summary = _scan_recent_session_errors(state_dir)

    # --- provider usage snapshot (no gateway) ---
    usage_summary: dict[str, Any] = {}
    ok_u, u, err_u = await _run_openclaw_json(
        profile=profile,
        args=["status", "--usage", "--json"],
        timeout_s=5.0,
    )
    if ok_u and isinstance(u, dict):
        # Keep it small: only % usage windows.
        providers = []
        for p in (u.get("providers") or []):
            if not isinstance(p, dict):
                continue
            providers.append(
                {
                    "provider": p.get("provider"),
                    "plan": p.get("plan"),
                    "windows": p.get("windows"),
                }
            )
        usage_summary = {"providers": providers, "updatedAt": u.get("updatedAt")}
    else:
        usage_summary = {"error": err_u}

    data = {
        "gw": gw,
        "cron": {"status": cron_status, "jobs": cron_list_summary},
        "sessions": sessions_summary,
        "errors": errors_summary,
        "usage": usage_summary,
    }

    return ProbeResult(ok=True, ts=ts, data=data)
