"""Licence tool (v1.1.0, preparation): not part of the exe.

    python tools/make_license.py new-key  <private-key-file>
        Creates the Ed25519 key pair ONCE. The private key file stays with you (password manager / USB stick);
        NEVER commit it. Prints the public key: paste it into licensing.PUBLIC_KEY.

    python tools/make_license.py issue <private-key-file> --licensee "Minta Kft." [--expires 2027-11-19]
                                       [--seats 3] [--machine <id> ...] [--feature transfer ...] [-o license.key]
        Issues a licence file. Copy it to %LOCALAPPDATA%\\CallCompanion\\license.key on the customer's PC.

    python tools/make_license.py machine-id
        Prints this PC's machine id (what --machine expects).

    python tools/make_license.py show <license.key>
        Shows the payload of a licence file and whether it verifies with licensing.PUBLIC_KEY.
"""
import argparse
import json
import os
import secrets
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import licensing as L  # noqa: E402


def _seed(path: str) -> bytes:
    seed = bytes.fromhex(Path(path).read_text(encoding="utf-8").strip())
    if len(seed) != 32:
        sys.exit("A privát kulcs fájl hibás (64 hex karakter kell).")
    return seed


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("new-key")
    k.add_argument("private_key_file")
    i = sub.add_parser("issue")
    i.add_argument("private_key_file")
    i.add_argument("--licensee", required=True)
    i.add_argument("--expires")
    i.add_argument("--seats", type=int)
    i.add_argument("--machine", action="append", default=[])
    i.add_argument("--feature", action="append", default=[])
    i.add_argument("-o", "--out", default="license.key")
    sub.add_parser("machine-id")
    s = sub.add_parser("show")
    s.add_argument("file")
    a = ap.parse_args()

    if a.cmd == "new-key":
        p = Path(a.private_key_file)
        if p.exists():
            sys.exit(f"{p} már létezik - nem írom felül.")
        seed = secrets.token_bytes(32)
        p.write_text(seed.hex(), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        print("Privát kulcs mentve:", p, "(ezt tartsd biztonságban, SOHA ne kerüljön a repóba)")
        print("Nyilvános kulcs (licensing.PUBLIC_KEY):", L.public_from_seed(seed).hex())
    elif a.cmd == "issue":
        seed = _seed(a.private_key_file)
        payload = {"licensee": a.licensee, "issued": date.today().isoformat(), "expires": a.expires or None}
        if a.expires:
            date.fromisoformat(a.expires)
        if a.seats:
            payload["seats"] = a.seats
        if a.machine:
            payload["machines"] = a.machine
        if a.feature:
            payload["features"] = a.feature
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        line = f"{L.PREFIX}.{L.b64e(body)}.{L.b64e(L.sign(seed, body))}"
        Path(a.out).write_text(line + "\n", encoding="utf-8")
        print("Licenc kiállítva:", a.out)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("Ellenőrzés a saját kulccsal:", L.parse(line, L.public_from_seed(seed).hex(), mid=(a.machine or [None])[0]))
    elif a.cmd == "machine-id":
        print(L.machine_id())
    elif a.cmd == "show":
        line = Path(a.file).read_text(encoding="utf-8-sig").strip()
        body = line.split(".")[1]
        print(json.dumps(json.loads(L._b64d(body)), ensure_ascii=False, indent=2))
        print(L.parse(line) if L.PUBLIC_KEY else "licensing.PUBLIC_KEY üres - nem ellenőrizhető")


if __name__ == "__main__":
    main()
