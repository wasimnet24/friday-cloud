"""Friday multi-provider auto-failover router.

Sab providers OpenAI-compatible hain (POST {base}/chat/completions, SSE stream).
Router providers ko AI_PROVIDERS order me try karta hai -- pehla healthy
provider jeetta hai. Timeout/error par turant agle par switch, user ko kuch
karne ki zaroorat nahi. NVIDIA aakhir me last-resort fallback hai.

Keys SIRF env vars se aati hain -- code/repo me kabhi hardcode nahi.
Har provider: {NAME}_API_KEY, {NAME}_BASE_URL, {NAME}_MODEL (base/model ke
liye tested defaults neeche hain, override kar sakte ho).

  AI_PROVIDERS=vyceai,openrouter,apinex,xkiro,kiosapi,nvidia
  AI_PROVIDER_TIMEOUT=20        # per-provider seconds (connect + first token)
  AI_PROVIDER_COOLDOWN=120      # fail hone par itne seconds tak skip karo
"""
import os
import time

import requests

# Browser jaisa User-Agent -- APInex ka Cloudflare default python UA ko
# 1010/403 deta hai (tested 2026-10-01).
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Tested defaults (2026-10-01). "model" wahi jo speed test me chala tha.
PROVIDER_DEFAULTS = {
    "vyceai": {
        "base_url": "https://vyceai.com/v1",
        "model": "deepseek-v4-flash",
        "headers": {"User-Agent": BROWSER_UA},  # Cloudflare 1010 bypass ke liye zaroori
        "note": "sabse tez warm (~1.1s), pehli cold hit slow ho sakti hai",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "liquid/lfm-2.5-2.6b:free",
        "headers": {
            "HTTP-Referer": "https://friday-cloud.onrender.com",
            "X-Title": "Friday AI",
        },
        "note": "~1.4s, 16 free models",
    },
    "apinex": {
        "base_url": "https://api.apinex.bond/v1",
        "model": "free/glm-5.3-flash",
        "headers": {"User-Agent": BROWSER_UA},  # Cloudflare bypass ke liye zaroori
        "note": "consistent ~1.6-2.7s, 30 req/min/IP limit",
    },
    "xkiro": {
        "base_url": "https://api.xkiro.com/v1",
        "model": "qwen/qwen3.7-flash:free",
        "headers": {},
        "note": "~4-5s",
    },
    "kiosapi": {
        "base_url": "https://kiosapi.com/v1",
        "model": "gpt-6-sol",
        "headers": {},
        "note": "nayi key ka intezar -- key aate hi active",
    },
    "agentrouter": {
        # PC-only candidate: iska WAF datacenter IPs ko block karta hai
        # ("unauthorized client detected"), isliye default order me NAHI hai.
        # Ghar ke PC se chal sakta hai -- AI_PROVIDERS me khud jodo.
        "base_url": "https://agentrouter.org/v1",
        "model": "deepseek-v4-flash",
        "headers": {},
        "note": "PC-only, default order se bahar",
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "z-ai/glm-5.3",
        "headers": {},
        "note": "last-resort fallback (legacy NVIDIA_API_KEY se bhi chalta hai)",
    },
}

# Default preference order = speed leaderboard (2026-10-01).
DEFAULT_ORDER = "vyceai,openrouter,apinex,xkiro,kiosapi,nvidia"

PLACEHOLDER_HINTS = ("", "YOUR_KEY", "nvapi-REPLACE_ME", "REPLACE_ME",
                     "xxx", "changeme", "none", "null")


def _valid_key(key):
    return bool(key) and key.strip() not in PLACEHOLDER_HINTS


class AllProvidersFailed(Exception):
    """Sab providers fail -- .errors me {provider: reason} dict hai."""
    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items())
                         or "koi provider configured nahi")


class ProviderRouter:
    def __init__(self):
        self.timeout = float(os.environ.get("AI_PROVIDER_TIMEOUT", "20"))
        self.cooldown = float(os.environ.get("AI_PROVIDER_COOLDOWN", "120"))
        order = os.environ.get("AI_PROVIDERS", DEFAULT_ORDER)
        self.order = [p.strip().lower() for p in order.split(",") if p.strip()]
        self.providers = {}  # name -> dict(base_url, model, key, headers)
        for name in self.order:
            spec = PROVIDER_DEFAULTS.get(name)
            if not spec:
                continue  # unknown naam -- ignore, crash nahi
            prefix = name.upper()
            # nvidia legacy keys (NVIDIA_API_KEY) bhi accept karo
            key = os.environ.get(f"{prefix}_API_KEY", "").strip()
            if name == "nvidia" and not _valid_key(key):
                key = os.environ.get("NVIDIA_API_KEY", "").strip()
            if not _valid_key(key):
                continue  # key nahi = provider inactive
            base = os.environ.get(f"{prefix}_BASE_URL",
                                  spec["base_url"]).rstrip("/")
            if name == "nvidia" and f"{prefix}_BASE_URL" not in os.environ:
                base = os.environ.get("NVIDIA_BASE_URL",
                                      spec["base_url"]).rstrip("/")
            model = os.environ.get(f"{prefix}_MODEL", spec["model"]).strip()
            if name == "nvidia" and f"{prefix}_MODEL" not in os.environ:
                model = os.environ.get("NVIDIA_MODEL", spec["model"]).strip()
            self.providers[name] = {
                "base_url": base,
                "model": model,
                "key": key,
                "headers": dict(spec["headers"]),
            }
        # circuit breaker state: name -> [fail_count, last_fail_ts]
        self._state = {n: [0, 0.0] for n in self.providers}
        self._latency = {}  # name -> last success seconds
        self.last_provider = None

    @property
    def has_providers(self):
        return bool(self.providers)

    def status(self):
        """Diagnostics ke liye: har provider ka haal."""
        now = time.time()
        out = []
        for name in self.order:
            if name not in self.providers:
                out.append({"name": name, "configured": False})
                continue
            fails, last = self._state[name]
            cooling = (now - last) < self.cooldown if fails else False
            out.append({"name": name, "configured": True,
                        "model": self.providers[name]["model"],
                        "in_cooldown": cooling,
                        "recent_fails": fails,
                        "last_latency_s": self._latency.get(name)})
        return out

    def _healthy(self, name):
        fails, last = self._state[name]
        return not fails or (time.time() - last) >= self.cooldown

    def _post_one(self, name, messages, max_tokens, temperature):
        p = self.providers[name]
        url = f"{p['base_url']}/chat/completions"
        headers = {"Authorization": f"Bearer {p['key']}",
                   "Content-Type": "application/json"}
        headers.update(p["headers"])
        payload = {"model": p["model"], "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens,
                   "stream": True}
        t0 = time.time()
        resp = requests.post(url, headers=headers, json=payload,
                             stream=True, timeout=self.timeout)
        resp.raise_for_status()  # 4xx/5xx -> turant agla provider
        self._latency[name] = round(time.time() - t0, 2)
        return resp

    def post_stream(self, messages, max_tokens=220, temperature=0.7):
        """Pehla healthy provider try karo; fail par agla.

        Returns (provider_name, requests.Response). Sab fail par
        AllProvidersFailed raise hota hai.
        """
        if not self.providers:
            raise AllProvidersFailed({})
        errors = {}
        for name in self.order:
            if name not in self.providers:
                continue
            if not self._healthy(name):
                errors[name] = "cooldown me hai"
                continue
            try:
                resp = self._post_one(name, messages, max_tokens, temperature)
            except Exception as e:  # timeout / network / HTTP error
                self._state[name][0] += 1
                self._state[name][1] = time.time()
                errors[name] = _short_err(e)
                continue
            # success -- breaker reset
            self._state[name] = [0, 0.0]
            self.last_provider = name
            return name, resp
        raise AllProvidersFailed(errors)


def _short_err(e):
    msg = str(e).replace("\n", " ")
    return (msg[:120] + "...") if len(msg) > 120 else msg
