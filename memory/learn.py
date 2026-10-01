"""Friday ki LEARNING memory -- persistent, file-backed.

data/memory.json structure:
{
  "profile": {"name": "wasim"},
  "preferences": {...},
  "corrections": [{"ts":..., "rule":"...", "context":"..."}],
  "interruption_scores": {"morning": 1.0, "nudge": 1.0, "evening": 1.0}
}

The CORRECTION LOG is the heart of self-learning: when wasim says
"Friday, ye galat kiya, aise karo", the rule is saved here and brain.py
injects it into every future system prompt -- so the same mistake is not
repeated. No retraining, no code changes.
"""
import datetime
import json
import re
from pathlib import Path

import config

MEMORY_PATH = config.DATA_DIR / "memory.json"

# Hinglish + English patterns for "you did it wrong, do it like this"
CORRECTION_PATTERNS = [
    r"\bgalat\b", r"\bghalat\b",
    r"aise nahi", r"aisa nahi", r"aise mat",
    r"\bmat kar\w*\b", r"dobara mat",
    r"\byaad rakh\w*\b",
    r"\bwrong\b", r"don'?t do that", r"\bmistake\b",
]

# "stop interrupting me" -- feeds the adaptive proactive engine.
# Note: "chup chaap" (quietly) is excluded -- it's not a dismissal.
DISMISSAL_PATTERNS = [
    r"\bchup\b(?! chaap)", r"baad me", r"\bruko\b",
    r"rehen de", r"rehne de", r"jaane de",
    r"\bnot now\b", r"shut up",
]

DEFAULT_DATA = {
    "profile": {"name": "wasim"},
    "preferences": {},
    "corrections": [],
    "interruption_scores": {"morning": 1.0, "nudge": 1.0, "evening": 1.0},
}


class LearnMemory:
    def __init__(self, path=None):
        self.path = Path(path) if path else MEMORY_PATH
        self.data = self._load()

    def _load(self):
        data = json.loads(json.dumps(DEFAULT_DATA))  # deep copy of defaults
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                for key, val in loaded.items():
                    if isinstance(val, dict) and isinstance(data.get(key), dict):
                        data[key].update(val)
                    else:
                        data[key] = val
            except (json.JSONDecodeError, OSError):
                pass
        return data

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:
            print(f"[Friday][WARN] memory save nahi hui: {e}")

    # -- correction detection + log -------------------------------------------
    @staticmethod
    def detect_correction(text):
        t = (text or "").lower()
        return any(re.search(p, t) for p in CORRECTION_PATTERNS)

    @staticmethod
    def detect_dismissal(text):
        t = (text or "").lower()
        return any(re.search(p, t) for p in DISMISSAL_PATTERNS)

    @staticmethod
    def clean_rule(text):
        rule = re.sub(r"^(hey\s+)?friday[,\s]*", "", (text or "").strip(),
                      flags=re.IGNORECASE)
        return rule[:200]

    def add_correction(self, text, context=""):
        """Save a correction rule. Returns the cleaned rule ("" if empty)."""
        rule = self.clean_rule(text)
        if not rule:
            return ""
        if any(c.get("rule") == rule for c in self.data["corrections"]):
            return rule  # already learned
        self.data["corrections"].append({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "rule": rule,
            "context": (context or "")[:120],
        })
        self.data["corrections"] = self.data["corrections"][-50:]
        self.save()
        return rule

    def get_rules(self):
        return [c["rule"] for c in self.data.get("corrections", [])
                if c.get("rule")]

    # -- preferences ------------------------------------------------------------
    def set_pref(self, key, value):
        self.data["preferences"][key] = value
        self.save()

    def get_pref(self, key, default=None):
        return self.data["preferences"].get(key, default)

    def profile_name(self):
        return self.data.get("profile", {}).get("name", "wasim")

    # -- adaptive interruption scores -------------------------------------------
    def slot_score(self, slot):
        return float(self.data.get("interruption_scores", {}).get(slot, 1.0))

    def record_interruption(self, slot, annoyed=True):
        """Dismissal ('chup'/'baad me') halves the slot's score (min 0.1)."""
        scores = self.data.setdefault("interruption_scores", {})
        cur = float(scores.get(slot, 1.0))
        scores[slot] = round(max(0.1, cur * 0.5) if annoyed
                             else min(1.0, cur + 0.2), 2)
        self.save()
        return scores[slot]

    def reward_slot(self, slot, amount=0.05):
        """Small recovery when a proactive speak goes fine."""
        scores = self.data.setdefault("interruption_scores", {})
        scores[slot] = round(min(1.0, float(scores.get(slot, 1.0)) + amount), 2)
        self.save()
        return scores[slot]
