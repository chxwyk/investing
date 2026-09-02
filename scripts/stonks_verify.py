#!/usr/bin/env python3
"""Read-only verification for the Stonks adapters.  Run this in production.

Why this script exists: an adapter stays disabled until its factory is verified
on chain against a bytecode digest obtained independently, and the sandbox this
repository is developed in cannot reach any of the hosts involved.  Production
can.  So the check ships as a script the operator runs where the network works,
and it prints exactly what needs to go into configuration.

It performs **only reads**:

* ``GET https://api.robinhood.com/rhj/assets`` — the documented stock-token
  endpoint.  Counts chain-4663 deployments and prints examples so the parser
  can be checked against a real response.
* ``eth_chainId`` and ``eth_blockNumber`` against the configured RPC — proves
  the node is reachable and is actually Robinhood Chain.
* ``eth_getCode`` for any factory address passed on the command line — prints
  the sha256 of the runtime bytecode, which is the value the adapter compares
  against.

It signs nothing, sends nothing, writes nothing, and touches no database.  It
does not enable anything either: printing a digest is not the same as trusting
it, and the operator still has to compare what this prints against the
launchpad's own repository, its documentation, or the Robinhood Chain explorer
before putting it into configuration.

    python scripts/stonks_verify.py
    python scripts/stonks_verify.py 0x8660A7F019C7943b0b0A91B8E39AFf3b6DB6Ae62
"""

from __future__ import annotations

import json
import sys
import urllib.request
from hashlib import sha256

ASSETS_URL = "https://api.robinhood.com/rhj/assets"
DEFAULT_RPC = "https://rpc.mainnet.chain.robinhood.com"
ROBINHOOD_CHAIN_ID = 4663
TIMEOUT = 20


def _get(url: str) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _rpc(url: str, method: str, params: list[object]) -> object:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        body = json.loads(response.read().decode("utf-8"))
    if "error" in body:
        raise RuntimeError(str(body["error"]))
    return body.get("result")


def check_assets() -> None:
    print("== Robinhood stock-token registry ==")
    try:
        payload = _get(ASSETS_URL)
    except Exception as exc:  # noqa: BLE001 - a diagnostic reports, never raises
        print(f"  UNREACHABLE: {type(exc).__name__}: {exc}")
        return
    rows = payload if isinstance(payload, list) else (payload or {}).get("results", [])
    if not isinstance(rows, list):
        keys = sorted(payload)[:12] if isinstance(payload, dict) else "n/a"
        print(f"  UNEXPECTED SHAPE: top level is {type(payload).__name__}, keys {keys}")
        return
    deployments = 0
    samples: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("tokenSymbol") or row.get("symbol") or "?"
        for deployment in row.get("deployments") or []:
            if not isinstance(deployment, dict):
                continue
            try:
                chain = int(deployment.get("chainId"))
            except (TypeError, ValueError):
                continue
            if chain != ROBINHOOD_CHAIN_ID:
                continue
            deployments += 1
            if len(samples) < 3:
                samples.append(
                    f"{symbol} -> {deployment.get('contractAddress')} [{row.get('status')}]"
                )
    print(f"  rows: {len(rows)}   chain-{ROBINHOOD_CHAIN_ID} deployments: {deployments}")
    for sample in samples:
        print(f"    {sample}")
    if not deployments:
        print("  NO chain-4663 deployments found — the parser needs the real field names.")
        first = next((row for row in rows if isinstance(row, dict)), None)
        if first:
            print(f"  first row keys: {sorted(first)[:14]}")


def check_rpc(url: str) -> None:
    print(f"== Robinhood Chain RPC ({url}) ==")
    try:
        chain_id = _rpc(url, "eth_chainId", [])
        block = _rpc(url, "eth_blockNumber", [])
    except Exception as exc:  # noqa: BLE001
        print(f"  UNREACHABLE: {type(exc).__name__}: {exc}")
        return
    resolved = int(str(chain_id), 16) if isinstance(chain_id, str) else chain_id
    ok = "OK" if resolved == ROBINHOOD_CHAIN_ID else "WRONG CHAIN"
    print(f"  chainId: {resolved} ({ok})   head block: {int(str(block), 16)}")


def check_factory(url: str, address: str) -> None:
    print(f"== factory {address} ==")
    try:
        code = _rpc(url, "eth_getCode", [address, "latest"])
    except Exception as exc:  # noqa: BLE001
        print(f"  UNREACHABLE: {type(exc).__name__}: {exc}")
        return
    raw = str(code or "").lower().removeprefix("0x").strip()
    if not raw or raw == "0":
        print("  NO CODE AT THIS ADDRESS — do not configure it.")
        return
    digest = sha256(raw.encode("ascii")).hexdigest()
    print(f"  runtime bytecode: {len(raw) // 2} bytes")
    print(f"  sha256: {digest}")
    print("  Compare this against the launchpad's own repository, its docs, or the")
    print("  Robinhood Chain explorer BEFORE putting it in configuration. If the")
    print("  address is a proxy, verify the implementation it points at as well.")


def main(argv: list[str]) -> int:
    rpc_url = DEFAULT_RPC
    addresses = [item for item in argv[1:] if item.lower().startswith("0x")]
    for item in argv[1:]:
        if item.startswith("https://"):
            rpc_url = item
    print("Stonks read-only verification. Nothing is signed, sent or stored.\n")
    check_assets()
    print()
    check_rpc(rpc_url)
    for address in addresses:
        print()
        check_factory(rpc_url, address)
    if not addresses:
        print("\nNo factory address given. Pass one to get its bytecode digest, e.g.")
        print("  python scripts/stonks_verify.py 0x8660A7F019C7943b0b0A91B8E39AFf3b6DB6Ae62")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
