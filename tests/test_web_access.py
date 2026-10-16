"""Cloudflare Access JWT verification: synthetic RSA keys, fake JWKS, no network."""

from __future__ import annotations

import base64
import hashlib
import json
import random
import subprocess
import sys

import pytest

from pocketsync.web.access import (
    AccessConfig,
    AccessConfigError,
    AccessVerifier,
    VerificationError,
    verify_rs256,
)

TEAM = "example-team.cloudflareaccess.com"
AUD = "synthetic-aud-tag-0123456789"
NOW = 1_800_000_000
_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


# --- synthetic test RSA keys: generated at test time, never committed -----------------------


def _is_prime(n: int, rng: random.Random) -> bool:
    if n < 4:
        return n in (2, 3)
    if n % 2 == 0:
        return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(20):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        cand = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(cand, rng) and (cand - 1) % 65537:
            return cand


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int(value: int) -> str:
    return b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


class Keypair:
    """A 1024-bit RSA key from a fixed seed (insecure on purpose: test material only)."""

    def __init__(self, kid: str, seed: int) -> None:
        rng = random.Random(seed)  # nosec B311 - synthetic test key
        p, q = _prime(512, rng), _prime(512, rng)
        self.kid, self.n, self.e = kid, p * q, 65537
        self.d = pow(self.e, -1, (p - 1) * (q - 1))

    def jwk(self) -> dict[str, str]:
        return {
            "kid": self.kid,
            "kty": "RSA",
            "alg": "RS256",
            "use": "sig",
            "n": _int(self.n),
            "e": _int(self.e),
        }

    def sign(self, data: bytes) -> bytes:
        k = (self.n.bit_length() + 7) // 8
        t = _DIGEST_INFO + hashlib.sha256(data).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")


_KEYS: dict[str, Keypair] = {}


def keypair(kid: str = "kid-1", seed: int | None = None) -> Keypair:
    key = f"{kid}/{seed}"
    if key not in _KEYS:
        _KEYS[key] = Keypair(kid, seed if seed is not None else sum(map(ord, kid)))
    return _KEYS[key]


def jwks(*pairs: Keypair) -> dict:
    return {"keys": [p.jwk() for p in (pairs or (keypair(),))]}


def claims(**changes) -> dict:
    base = {
        "aud": [AUD],
        "iss": f"https://{TEAM}",
        "sub": "user-sub-1",
        "email": "alice@example.com",
        "iat": NOW - 10,
        "nbf": NOW - 10,
        "exp": NOW + 600,
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v is not None}


def token(body=None, *, pair=None, header=None) -> str:
    pair = pair or keypair()
    head = header if header is not None else {"alg": "RS256", "kid": pair.kid, "typ": "JWT"}
    signing = f"{b64(json.dumps(head).encode())}.{b64(json.dumps(body or claims()).encode())}"
    return f"{signing}.{b64(pair.sign(signing.encode()))}"


def verifier(fetches=None, keyset=None, **kw) -> AccessVerifier:
    def fetch():
        if fetches is not None:
            fetches.append(1)
        return keyset if keyset is not None else jwks()

    return AccessVerifier(TEAM, AUD, fetch_jwks=fetch, clock=lambda: float(NOW), **kw)


def reason(v: AccessVerifier, tok: str) -> str:
    with pytest.raises(VerificationError) as ei:
        v.verify(tok)
    return ei.value.reason


# --- verification ---------------------------------------------------------------------------


def test_valid_token_yields_sso_identity():
    ident = verifier().verify(token())
    assert (ident.kind, ident.email, ident.subject) == ("sso", "alice@example.com", "user-sub-1")
    assert ident.identity == "alice@example.com"


def test_valid_service_token_yields_service_identity():
    ident = verifier().verify(token(claims(email=None, common_name="ci.abc.access", sub="")))
    assert (ident.kind, ident.identity) == ("service", "ci.abc.access")


def test_audience_may_be_a_single_string():
    assert verifier().verify(token(claims(aud=AUD))).kind == "sso"


def test_forged_signature_is_rejected():
    forger = keypair("kid-1", seed=999)  # same kid, different key
    assert forger.n != keypair().n
    assert reason(verifier(), token(pair=forger)) == "bad_signature"


def test_tampered_payload_is_rejected():
    head, _, sig = token().split(".")
    evil = b64(json.dumps(claims(email="root@example.com")).encode())
    assert reason(verifier(), f"{head}.{evil}.{sig}") == "bad_signature"


def test_expired_token_is_rejected():
    assert reason(verifier(), token(claims(exp=NOW - 1))) == "expired"
    assert reason(verifier(), token(claims(exp=NOW))) == "expired"


def test_not_yet_valid_token_is_rejected():
    assert reason(verifier(), token(claims(nbf=NOW + 60))) == "not_yet_valid"


def test_wrong_audience_is_rejected():
    assert reason(verifier(), token(claims(aud=["someone-else"]))) == "bad_audience"


def test_wrong_issuer_is_rejected():
    assert reason(verifier(), token(claims(iss="https://evil.example.com"))) == "bad_issuer"


@pytest.mark.parametrize(
    "header",
    [{"alg": "none", "kid": "kid-1"}, {"alg": "HS256", "kid": "kid-1"}, {"alg": "RS256"}],
)
def test_algorithm_confusion_and_missing_kid_are_malformed(header):
    assert reason(verifier(), token(header=header)) == "malformed"


@pytest.mark.parametrize("bad", ["", "a.b", "a.b.c.d", "!!.!!.!!", "x.y.z"])
def test_malformed_tokens_are_rejected(bad):
    assert reason(verifier(), bad) == "malformed"


def test_missing_exp_is_malformed():
    assert reason(verifier(), token(claims(exp=None))) == "malformed"


def test_reason_never_carries_token_material():
    tok = token(claims(aud=["someone-else"]))
    with pytest.raises(VerificationError) as ei:
        verifier().verify(tok)
    assert tok.split(".")[1] not in str(ei.value)


# --- JWKS cache: refetch window, rotation, unreachable --------------------------------------


def test_unknown_kid_refetches_once_per_window_then_refuses():
    fetches: list = []
    v = verifier(fetches)
    v.verify(token())
    assert len(fetches) == 1
    other = keypair("kid-2")
    assert reason(v, token(pair=other)) == "unknown_kid"
    assert reason(v, token(pair=other)) == "unknown_kid"
    assert len(fetches) == 2  # one forced refetch inside the window, not one per request


def test_unknown_kid_refetches_again_after_window():
    now = {"t": float(NOW)}
    fetches: list = []

    def fetch():
        fetches.append(1)
        return jwks()

    v = AccessVerifier(TEAM, AUD, fetch_jwks=fetch, clock=lambda: now["t"], refetch_window=60)
    v.verify(token())
    other = keypair("kid-2")
    assert reason(v, token(pair=other)) == "unknown_kid"
    now["t"] += 61
    assert reason(v, token(pair=other)) == "unknown_kid"
    assert len(fetches) == 3


def test_key_rotation_is_picked_up_on_kid_miss():
    box = {"value": jwks()}
    v = AccessVerifier(TEAM, AUD, fetch_jwks=lambda: box["value"], clock=lambda: float(NOW))
    v.verify(token())
    box["value"] = jwks(keypair("kid-2"))
    assert v.verify(token(pair=keypair("kid-2"))).kind == "sso"


def test_jwks_unreachable_is_unavailable_and_negative_cached():
    calls: list = []

    def boom():
        calls.append(1)
        raise OSError("network down")

    v = AccessVerifier(TEAM, AUD, fetch_jwks=boom, clock=lambda: float(NOW))
    assert reason(v, token()) == "unavailable"
    assert reason(v, token()) == "unavailable"
    assert len(calls) == 1  # negative cache: no fetch per request


def test_jwks_recovers_after_window():
    now = {"t": float(NOW)}
    up = {"ok": False}

    def fetch():
        if not up["ok"]:
            raise OSError("down")
        return jwks()

    v = AccessVerifier(TEAM, AUD, fetch_jwks=fetch, clock=lambda: now["t"], refetch_window=60)
    assert reason(v, token()) == "unavailable"
    up["ok"] = True
    now["t"] += 61
    assert v.verify(token()).kind == "sso"


def test_jwks_entries_that_are_not_rs256_signing_keys_are_ignored():
    jwk = keypair().jwk()
    keyset = {"keys": [{**jwk, "use": "enc"}, {**jwk, "kty": "EC"}, {**jwk, "alg": "RS512"}]}
    assert reason(verifier(keyset=keyset), token()) in ("unknown_kid", "unavailable")


def test_team_domain_scheme_is_dropped_and_jwks_is_https():
    v = AccessVerifier(f"HTTP://{TEAM}/", AUD, fetch_jwks=jwks, clock=lambda: float(NOW))
    assert v.team_domain == TEAM
    assert v.jwks_url == f"https://{TEAM}/cdn-cgi/access/certs"


def test_verify_rs256_roundtrip():
    pair = keypair()
    sig = pair.sign(b"hello")
    assert verify_rs256(pair.n, pair.e, b"hello", sig)
    assert not verify_rs256(pair.n, pair.e, b"hellO", sig)
    assert not verify_rs256(pair.n, pair.e, b"hello", sig[:-1])
    assert not verify_rs256(pair.n, pair.e, b"hello", b"\x00" * len(sig))


# --- all-or-nothing configuration -----------------------------------------------------------

ENV = {"POCKETSYNC_ACCESS_TEAM_DOMAIN": TEAM, "POCKETSYNC_ACCESS_AUD": AUD}


def test_config_both_set():
    cfg = AccessConfig.from_env(ENV)
    assert (cfg.team_domain, cfg.audience) == (TEAM, AUD)


def test_config_both_unset_is_off():
    assert AccessConfig.from_env({}) is None
    assert AccessConfig.from_env({k: " " for k in ENV}) is None


@pytest.mark.parametrize("missing", sorted(ENV))
def test_config_partial_is_refused(missing):
    with pytest.raises(AccessConfigError) as ei:
        AccessConfig.from_env({k: v for k, v in ENV.items() if k != missing})
    assert "together" in str(ei.value)


def test_verifier_requires_domain_and_audience():
    with pytest.raises(AccessConfigError):
        AccessVerifier("", AUD)


def test_access_module_is_stdlib_only():
    code = (
        "import sys\n"
        "for m in ('cryptography', 'jwt', 'culture_rules'):\n    sys.modules[m] = None\n"
        "import pocketsync.web.access\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
