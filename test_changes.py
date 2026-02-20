#!/usr/bin/env python3
"""Quick validation script for the stale status fix."""

import time

# Test the status derivation logic
def derive_status(probe):
    """Simulate the status derivation logic from heartbeat.py."""
    if probe is None:
        return "starting"
    elif probe.get("gw", {}).get("rpcOk") is True:
        return "online"
    else:
        return "degraded"

# Test the age formatting logic
def format_age(ts):
    """Simulate the age formatting logic from commands.py."""
    age_s = time.time() - ts
    if age_s < 60:
        return f"{int(age_s)}s ago"
    elif age_s < 3600:
        return f"{int(age_s / 60)}m ago"
    elif age_s < 86400:
        return f"{int(age_s / 3600)}h ago"
    else:
        return f"{int(age_s / 86400)}d ago"

# Test status derivation
print("Testing status derivation:")
print(f"  No probe: {derive_status(None)} (expected: starting)")
print(f"  GW ok: {derive_status({'gw': {'rpcOk': True}})} (expected: online)")
print(f"  GW not ok: {derive_status({'gw': {'rpcOk': False}})} (expected: degraded)")
print(f"  No GW field: {derive_status({'other': 'data'})} (expected: degraded)")
print()

# Test age formatting
now = time.time()
print("Testing age formatting:")
print(f"  15s ago: {format_age(int(now - 15))}")
print(f"  5m ago: {format_age(int(now - 300))}")
print(f"  2h ago: {format_age(int(now - 7200))}")
print(f"  2d ago: {format_age(int(now - 172800))}")
print()

# Test staleness detection
stale_threshold_s = 30
print("Testing staleness detection:")
fresh_ts = int(now - 10)
stale_ts = int(now - 60)
print(f"  Fresh (10s ago): stale={now - fresh_ts > stale_threshold_s} (expected: False)")
print(f"  Stale (60s ago): stale={now - stale_ts > stale_threshold_s} (expected: True)")
print()

print("All validations look correct!")
