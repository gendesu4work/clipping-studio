"""Seller's tool: make license keys for Creator Studio. Never ship this folder's secrets to buyers.

  python tools/make_license.py init
      One time. Creates your private signing key in %USERPROFILE%\\CreatorStudio-Licensing and puts the
      matching public key into license.py. BACK UP that folder: without the signing key you can't make
      keys that existing copies of the app accept.

  python tools/make_license.py make <MACHINE-CODE> --customer "Name or email" [--days 30 | --expires 2027-12-31]
      Makes a key for one PC. No --days/--expires = never expires. Every sale is logged in sales.csv.

  python tools/make_license.py list
      Shows every key you've made.
"""
from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import license  # noqa: E402

VAULT = os.path.join(os.path.expanduser("~"), "CreatorStudio-Licensing")
SIGNING_KEY = os.path.join(VAULT, "signing_key.pem")
SALES = os.path.join(VAULT, "sales.csv")
LICENSE_PY = os.path.join(ROOT, "license.py")


def load_signing_key():
    from cryptography.hazmat.primitives import serialization
    if not os.path.exists(SIGNING_KEY):
        sys.exit("No signing key yet. Run:  python tools/make_license.py init")
    with open(SIGNING_KEY, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def public_b64(private) -> str:
    from cryptography.hazmat.primitives import serialization
    raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def cmd_init(_):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    os.makedirs(VAULT, exist_ok=True)
    if os.path.exists(SIGNING_KEY):
        print(f"You already have a signing key ({SIGNING_KEY}). Keeping it.")
        private = load_signing_key()
    else:
        private = Ed25519PrivateKey.generate()
        with open(SIGNING_KEY, "wb") as f:
            f.write(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        print(f"Created your signing key: {SIGNING_KEY}")
        print("BACK UP that folder (e.g. a USB stick or password manager) and never share it.")
    src = open(LICENSE_PY, encoding="utf-8").read()
    new = re.sub(r'^PUBLIC_KEY = "[^"]*"', f'PUBLIC_KEY = "{public_b64(private)}"', src, count=1, flags=re.M)
    if new != src:
        with open(LICENSE_PY, "w", encoding="utf-8") as f:
            f.write(new)
        print("Put your public key into license.py.")
    else:
        print("license.py already has your public key.")


def _sales():
    if not os.path.exists(SALES):
        return []
    with open(SALES, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cmd_make(a):
    private = load_signing_key()
    if public_b64(private) != license.PUBLIC_KEY:
        sys.exit("license.py doesn't have your public key. Run:  python tools/make_license.py init")
    try:
        machine = license.parse_machine_code(a.machine_code)
    except Exception:
        sys.exit("That machine code doesn't look right. Ask the buyer to copy it again from the activation screen.")
    expires = None
    if a.days:
        expires = dt.date.today() + dt.timedelta(days=a.days)
    elif a.expires:
        expires = dt.date.fromisoformat(a.expires)
    sales = _sales()
    customer = max([int(s["number"]) for s in sales] or [1000]) + 1
    payload = license.pack_payload(machine, customer, expires)
    key = license.format_key(payload, private.sign(payload))
    new = not os.path.exists(SALES)
    with open(SALES, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["number", "customer", "machine_code", "expires", "made_on"])
        w.writerow([customer, a.customer, a.machine_code.strip().upper(), expires or "never", dt.date.today()])
    print(f"License #{customer} for {a.customer}" + (f", expires {expires}" if expires else ", never expires"))
    print()
    print(key)
    print()
    print("Send the key above to the buyer. It only works on the PC with that machine code.")


def cmd_list(_):
    sales = _sales()
    if not sales:
        print("No keys made yet."); return
    for s in sales:
        print(f"#{s['number']}  {s['customer']:<30} {s['machine_code']:<20} expires {s['expires']:<11} made {s['made_on']}")


def main():
    p = argparse.ArgumentParser(description="Make Creator Studio license keys.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    m = sub.add_parser("make")
    m.add_argument("machine_code")
    m.add_argument("--customer", required=True, help="who bought it (name or email), for your records")
    g = m.add_mutually_exclusive_group()
    g.add_argument("--days", type=int, help="key stops working after this many days")
    g.add_argument("--expires", help="key stops working after this date, YYYY-MM-DD")
    m.set_defaults(fn=cmd_make)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
