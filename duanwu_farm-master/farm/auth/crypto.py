# crypto — P-256 密钥、ECDSA 签名、JWK 编解码
import base64
import json
import os
import random
from urllib.parse import quote
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import utils


def to_url_safe_b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode("ascii")


def gen_nonce(length=16) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(random.choice(alphabet) for _ in range(length))


def generate_p256_key():
    return ec.generate_private_key(ec.SECP256R1())


def private_key_to_jwk_public(priv_key: ec.EllipticCurvePrivateKey) -> dict:
    pub = priv_key.public_key()
    nums = pub.public_numbers()
    x_b = nums.x.to_bytes(32, byteorder="big")
    y_b = nums.y.to_bytes(32, byteorder="big")
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": to_url_safe_b64(x_b),
        "y": to_url_safe_b64(y_b),
        "key_ops": ["verify"],
        "ext": True
    }


def ecdsa_sign_raw(private_key, data: bytes) -> bytes:
    sig_der = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(sig_der)
    r_bytes = r.to_bytes(32, byteorder="big")
    s_bytes = s.to_bytes(32, byteorder="big")
    return r_bytes + s_bytes


def js_encode_uri_component(s: str) -> str:
    return quote(s, safe="!~*'()")


def save_key(file_path: str, private_key):
    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode("utf-8")
    pub_jwk = private_key_to_jwk_public(private_key)
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump({"private_pem": priv_pem, "public_jwk": pub_jwk}, f, ensure_ascii=False, indent=2)


def load_key(file_path: str):
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    pem = d["private_pem"].encode("utf-8")
    return serialization.load_pem_private_key(pem, password=None)
