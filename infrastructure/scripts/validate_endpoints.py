#!/usr/bin/env python3
"""Validate all 32 experiment endpoints by sending a test POST request.

Reads endpoints.txt (format: {name}={url}), sends a POST with a small tier
payload to each endpoint, and prints a pass/fail summary.
"""

import argparse
import json
import sys
from pathlib import Path

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession

# Timeout per request in seconds
REQUEST_TIMEOUT = 30
REGION = "us-east-2"

# SigV4 credentials for IAM-authenticated API Gateway requests
_botocore_session = BotocoreSession()
_botocore_credentials = _botocore_session.get_credentials()

# Test payload — small tier is the lightest-weight invocation for all archetypes
TEST_PAYLOAD = {"payload_tier": "small"}


def load_endpoints(path: Path) -> list[tuple[str, str]]:
    """Read endpoints.txt and return a list of (name, url) tuples."""
    endpoints = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            print(f"WARNING: skipping malformed line {lineno}: {line}", file=sys.stderr)
            continue
        name, url = line.split("=", 1)
        endpoints.append((name.strip(), url.strip()))
    return endpoints


def validate_endpoint(name: str, url: str) -> tuple[bool, str]:
    """Send a test POST to the endpoint and return (passed, detail).

    Lambda endpoints (execute-api URLs) use SigV4 signing.
    Fargate endpoints (ALB URLs) use plain HTTP.
    """
    try:
        is_lambda = "execute-api" in url
        if is_lambda:
            data = json.dumps(TEST_PAYLOAD)
            headers = {"Content-Type": "application/json"}
            aws_req = AWSRequest(method="POST", url=url, data=data, headers=headers)
            SigV4Auth(_botocore_credentials, "execute-api", REGION).add_auth(aws_req)
            resp = requests.post(
                url,
                headers=dict(aws_req.headers),
                data=data,
                timeout=REQUEST_TIMEOUT,
            )
        else:
            resp = requests.post(
                url,
                json=TEST_PAYLOAD,
                timeout=REQUEST_TIMEOUT,
            )
        if resp.status_code == 200:
            return True, "HTTP 200"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.ConnectionError as exc:
        return False, f"Connection error: {exc}"
    except requests.Timeout:
        return False, f"Timeout after {REQUEST_TIMEOUT}s"
    except requests.RequestException as exc:
        return False, f"Request error: {exc}"


def main():
    parser = argparse.ArgumentParser(
        description="Validate all experiment endpoints in endpoints.txt"
    )
    parser.add_argument(
        "--input",
        default="endpoints.txt",
        help="Path to endpoints.txt (default: endpoints.txt)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: {input_path} not found.", file=sys.stderr)
        sys.exit(1)

    endpoints = load_endpoints(input_path)
    if not endpoints:
        print("ERROR: No endpoints found in file.", file=sys.stderr)
        sys.exit(1)

    total = len(endpoints)
    passed = []
    failed = []

    print(f"Validating {total} endpoints...\n")

    for name, url in endpoints:
        ok, detail = validate_endpoint(name, url)
        if ok:
            passed.append(name)
            print(f"  PASS  {name}")
        else:
            failed.append((name, detail))
            print(f"  FAIL  {name} — {detail}")

    # Summary
    print(f"\n{'='*60}")
    print(f"Results: {len(passed)}/{total} passed")

    if failed:
        print(f"\nFailed endpoints ({len(failed)}):")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
    else:
        print("\nAll endpoints healthy!")

    # Exit with non-zero if any failures
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
