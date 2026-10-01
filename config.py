"""Friday Cloud ka minimal config -- sirf env vars, koi Windows cheez nahi."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CLOUD_TOKEN = os.environ.get("CLOUD_TOKEN", "").strip()

# ---- multi-provider auto-failover (see providers.py) ----
# order = preference: pehla healthy provider jeetta hai.
AI_PROVIDERS = os.environ.get(
    "AI_PROVIDERS", "vyceai,openrouter,apinex,xkiro,kiosapi,nvidia").strip()
AI_PROVIDER_TIMEOUT = float(os.environ.get("AI_PROVIDER_TIMEOUT", "20"))
AI_PROVIDER_COOLDOWN = float(os.environ.get("AI_PROVIDER_COOLDOWN", "120"))
# har provider: {NAME}_API_KEY (zaroori), {NAME}_BASE_URL / {NAME}_MODEL (optional)
# VYCEAI_API_KEY / OPENROUTER_API_KEY / APINEX_API_KEY / XKIRO_API_KEY / KIOSAPI_API_KEY

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "").strip()
NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL",
                                 "https://integrate.api.nvidia.com/v1").rstrip("/")
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "z-ai/glm-5.3").strip()
NVIDIA_MAX_TOKENS = int(os.environ.get("NVIDIA_MAX_TOKENS", "220"))
NVIDIA_TEMPERATURE = float(os.environ.get("NVIDIA_TEMPERATURE", "0.7"))

PLACEHOLDER_HINTS = ("", "YOUR_KEY", "YOUR_NVIDIA_API_KEY", "nvapi-REPLACE_ME",
                     "REPLACE_ME", "none", "null")
