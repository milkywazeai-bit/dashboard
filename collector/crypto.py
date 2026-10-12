"""대시보드 데이터 암호화.

GitHub Pages 는 저장소가 비공개여도 페이지 자체는 URL 만 알면 누구나 열 수 있다.
그래서 계좌 데이터는 비밀번호로 암호화해서 올리고, 브라우저가 같은 비밀번호로 푼다.

형식 (docs/data.enc.json):
    {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 310000,
     "salt": b64, "iv": b64, "ct": b64}      # ct = AES-256-GCM 암호문 + 16바이트 태그

브라우저 WebCrypto 의 PBKDF2 → AES-GCM 과 같은 형식이라 docs/index.html 이 그대로 푼다.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 310_000


def _key(password: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt(obj, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(_key(password, salt, ITERATIONS)).encrypt(iv, plain, None)
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITERATIONS,
            "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(envelope: dict, password: str):
    d = lambda k: base64.b64decode(envelope[k])  # noqa: E731
    key = _key(password, d("salt"), int(envelope.get("iter", ITERATIONS)))
    plain = AESGCM(key).decrypt(d("iv"), d("ct"), None)
    return json.loads(plain.decode("utf-8"))
