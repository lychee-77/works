# client — 请求签名与公钥注册
import os
import time
import json
import threading
import requests
from config import BASE_API, KEY_FILE, REGISTER_MARK_FILE, HEADERS_BASE
from farm.auth.session import ensure_access_token, invalidate_access_token
from farm.auth.crypto import (
    load_key, save_key, generate_p256_key, private_key_to_jwk_public,
    gen_nonce, ecdsa_sign_raw, js_encode_uri_component, to_url_safe_b64
)

# 农场通道与秘境线程会并发签名，串行化避免 nonce/时间戳撞车
_SIGN_LOCK = threading.RLock()


class SignClient:
    def __init__(self, *, quiet: bool = False):
        self._quiet = quiet
        self.token = ensure_access_token(quiet=quiet)
        self.base_api = BASE_API
        self.private_key = None
        self.pub_jwk = None
        self._load_or_create_key()

    def refresh_token(self) -> str:
        """强制重新登录并刷新 token。"""
        invalidate_access_token()
        self.token = ensure_access_token(force=True, quiet=self._quiet)
        return self.token

    def _load_or_create_key(self):
        """读取本地密钥，不存在则生成并保存（只生成一次）"""
        key = load_key(KEY_FILE)
        if key is None:
            print("本地密钥不存在，生成新P‑256密钥并保存...")
            key = generate_p256_key()
            save_key(KEY_FILE, key)
        elif not self._quiet:
            print("成功读取本地已保存密钥")
        self.private_key = key
        self.pub_jwk = private_key_to_jwk_public(self.private_key)

    def is_already_registered(self) -> bool:
        """简单标记文件，判断是否已完成公钥注册；账号重新登录后自动清除"""
        return os.path.exists(REGISTER_MARK_FILE)

    def register_signing_key(self, force: bool = False):
        """
        注册公钥到当前登录会话；
        标记文件存在且 force=False 则跳过注册；
        登录失效时会自动重新登录并重新注册
        """
        if not force and self.is_already_registered():
            print(f"检测标记文件，跳过公钥注册（如需强制注册删除 {REGISTER_MARK_FILE}）")
            return True

        print("开始执行会话公钥注册...")
        url = self.base_api + "/auth/register-signing-key"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        resp = requests.post(url, headers=headers, json={"publicKey": self.pub_jwk}, timeout=15)
        if resp.status_code == 401:
            self.refresh_token()
            headers["Authorization"] = f"Bearer {self.token}"
            resp = requests.post(url, headers=headers, json={"publicKey": self.pub_jwk}, timeout=15)
        if resp.status_code != 200:
            raise Exception(f"注册失败 status={resp.status_code}, body={resp.text}")

        mark_dir = os.path.dirname(REGISTER_MARK_FILE)
        if mark_dir:
            os.makedirs(mark_dir, exist_ok=True)
        with open(REGISTER_MARK_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time())}))
        print("公钥注册完成，已写入标记文件")
        return resp.json()

    def _build_sign_params(self, a: dict) -> str:
        """对齐 JS ym：Object.keys(a).sort() 后拼接 key=encodeURIComponent(value)"""
        if not a:
            return ""
        parts = []
        for key in sorted(a.keys()):
            u = a[key]
            if u is None:
                continue
            if isinstance(u, (dict, list)):
                # JSON.stringify(u) + encodeURIComponent
                val_str = json.dumps(u, ensure_ascii=False, separators=(",", ":"))
                parts.append(f"{key}={js_encode_uri_component(val_str)}")
            else:
                # JS: String(u)；bool 需对齐 JS 的 true/false
                if isinstance(u, bool):
                    val_str = "true" if u else "false"
                else:
                    val_str = str(u)
                parts.append(f"{key}={js_encode_uri_component(val_str)}")
        return "&".join(parts)

    def get_signed_headers(self, method: str, api_path: str, query: dict = None, body: dict = None, debug: bool = False):
        """
        对齐前端 ym(e, t, a, s, n)：
          METHOD + path(去尾斜杠) + params串 + timestamp + nonce
        a 参数：query 与 body 合并（同名以 query 为准）；GET 通常只有 query，POST 通常只有 body。
        """
        with _SIGN_LOCK:
            ts = int(time.time() * 1000)
            nonce = gen_nonce(16)

            r = method.upper()
            # path：去掉 ?query，再去尾部 /
            o = api_path.split("?")[0]
            o = o.rstrip("/") or "/"

            # ym 的 a：body + query（query 覆盖同名 key）
            a = {}
            if body:
                a.update(body)
            if query:
                a.update(query)
            i = self._build_sign_params(a)

            # 注意：path 与 params 之间没有额外的 & 或 ?
            raw_payload = f"{r}{o}{i}{ts}{nonce}"
            payload_bytes = raw_payload.encode("utf-8")

            sig_raw = ecdsa_sign_raw(self.private_key, payload_bytes)
            sig_b64 = to_url_safe_b64(sig_raw)

            if debug:
                print(f"sig_raw len={len(sig_raw)}")
                print("====真实payload====")
                print(raw_payload)
                print("====================")

            headers = dict(HEADERS_BASE)
            headers.update({
                "Authorization": f"Bearer {self.token}",
                "x-api-timestamp": str(ts),
                "x-api-nonce": nonce,
                "x-api-signature": sig_b64,
                "Content-Type": "application/json",
            })
            if debug:
                print("==== headers debug ====")
                for k, v in headers.items():
                    print(f"{k}: {repr(v)}")
            return headers
