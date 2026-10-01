"""Friday's brain: NVIDIA OpenAI-compatible chat with STREAMING.

Latency design (1-2s first-token target):
  * stream=True -> we speak tokens as they arrive, no waiting for full reply
  * small fast model (nemotron-nano-8b) + low max_tokens (220)
  * plain `requests` with manual SSE parsing (no heavy SDK startup cost)

Action protocol (robust, with plain-chat fallback):
  The model may embed ONE action block anywhere in its reply:

      ```friday-action
      {"action": "open_app", "args": {"name": "notepad"}}
      ```

  Everything outside the block is the spoken reply. If no block is found,
  the whole reply is treated as plain chat. Unknown/broken action JSON is
  ignored safely (logged, never crashes).
"""
import json
import re
import time

import requests

import config
from memory.learn import LearnMemory

SYSTEM_PROMPT = """You are Friday, wasim's personal AI assistant. Female, warm, a little playful, \
like a close friend. You ALWAYS reply in Hindi written in Devanagari script (देवनागरी), \
mixed naturally with English tech words in Roman script (like YouTube, WhatsApp, phone). \
Write the way Indian friends text in Hindi -- natural, casual, never robotic. \
Keep voice replies SHORT: 1-3 sentences max.

IMPORTANT: Tum ab CLOUD server pe chal rahi ho (24/7 online), wasim ka Windows PC \
ghar pe hai aur kabhi OFFLINE ho sakta hai. PC wale actions (open_app, shutdown_pc, \
screenshot, waghaira) cloud se uske PC ko bheje jayenge -- agar PC offline hua to \
action ka result "offline" aayega. Aise me wasim ko saaf-saaf batao ki PC offline hai, \
jhootha "ho gaya" kabhi mat bolo. Normal baat-cheet, sawal-jawab, notes, web search \
hamesha kaam karte hain chahe PC on ho ya off.

You can control wasim's Windows PC by embedding ONE action block in your reply when needed:

```friday-action
{"action": "<name>", "args": {...}}
```

Available actions:
- open_app: {"name": "notepad"} - open an app/program
- close_app: {"name": "notepad"} - close an app
- type_text: {"text": "hello"} - type text at cursor
- press_key: {"key": "enter"} - press a key (enter, tab, esc, space, f5, ...)
- set_volume: {"level": 50} - 0..100
- mute: {"on": true}
- screenshot: {} - take a screenshot
- open_url: {"url": "https://..."}
- web_search: {"query": "..."} - search the web
- get_time: {} / get_date: {}
- take_note: {"text": "..."} - save a note
- read_clipboard: {}
- find_file: {"query": "resume"} - PC me file naam se dhoondo (Desktop/Documents/Downloads)
- lock_screen: {}
- shutdown_pc: {"confirm": true} - PC BAND karo. !! RULE: pehli baar "pc band kar do" pe action MAT chalao -- pehle puchho "pakka wasim? PC band kar dun?". Sirf jab wasim "haan"/"pakka"/"yes" kahe tab confirm:true ke saath chalao. confirm ke bina kabhi mat chalao.
- restart_pc: {"confirm": true} - PC restart karo. Wahi confirm RULE jaisa shutdown me.

Rules:
- Use an action ONLY when wasim asks you to DO something on the PC.
- After the action block, still write a short Hindi (Devanagari) spoken reply (e.g. "हो गया वसीम, नोटपैड खोल दिया।").
- For normal chit-chat/questions, output NO action block, just the reply.
- Never output more than one action block. Never invent actions.

Example:
wasim: notepad khol do
you:
```friday-action
{"action": "open_app", "args": {"name": "notepad"}}
```
हो गया वसीम, नोटपैड खोल दिया। अब बता क्या लिखना है?
"""

ACTION_BLOCK_RE = re.compile(
    r"```(?:friday-action)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)


def extract_action(text):
    """Return (action_dict_or_None, spoken_text). Safe: never raises."""
    match = ACTION_BLOCK_RE.search(text or "")
    if not match:
        return None, (text or "").strip()
    try:
        action = json.loads(match.group(1))
        if isinstance(action, dict) and "action" in action:
            spoken = (text[:match.start()] + text[match.end():]).strip()
            return action, spoken
    except (json.JSONDecodeError, ValueError):
        pass
    return None, (text or "").strip()


class FridayBrain:
    def __init__(self):
        self.history = []  # list of {"role":..., "content":...}
        self.learn = LearnMemory()  # self-learning: corrections -> rules
        self.offline = not config.NVIDIA_API_KEY or \
            config.NVIDIA_API_KEY in config.PLACEHOLDER_HINTS

    def _system_prompt(self):
        """Base prompt + learned rules (so Friday never repeats a mistake)."""
        prompt = SYSTEM_PROMPT
        rules = self.learn.get_rules()
        if rules:
            prompt += ("\n\nLearned rules about wasim -- inhe HAMESHA follow karo, "
                       "ye sabse upar hain:\n" +
                       "\n".join(f"- {r}" for r in rules[-8:]))
        return prompt

    # -- low-level streaming -------------------------------------------------
    def _post_stream(self, messages):
        url = f"{config.NVIDIA_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.NVIDIA_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": config.NVIDIA_MODEL,
            "messages": messages,
            "temperature": config.NVIDIA_TEMPERATURE,
            "max_tokens": config.NVIDIA_MAX_TOKENS,
            "stream": True,
        }
        # stream=True on requests: first token latency ~1-2s on fast models
        return requests.post(url, headers=headers, json=payload,
                             stream=True, timeout=60)

    def chat_stream(self, user_text):
        """Generator yielding ("token", chunk) live, then ("done", full_text).

        In offline mode (no API key) yields a canned Hinglish reply instead.
        """
        self.history.append({"role": "user", "content": user_text})
        self.history = self.history[-10:]  # keep context small = fast

        if self.offline:
            reply = ("haan wasim, sun rahi hun! (offline mode hun abhi -- "
                     ".env me NVIDIA_API_KEY daal do, phir full AI chat chalega.)")
            yield ("token", reply)
            yield ("done", reply)
            self.history.append({"role": "assistant", "content": reply})
            return

        messages = [{"role": "system", "content": self._system_prompt()}] + self.history
        try:
            resp = self._post_stream(messages)
            resp.raise_for_status()
        except Exception as e:  # network/auth failure -> graceful Hindi error
            msg = str(e)
            if "410" in msg or "404" in msg:
                # model retired/removed by NVIDIA -- not our bug, tell clearly
                reply = ("arey wasim, lagta hai NVIDIA ne ye AI model band kar "
                         "diya hai (410 Gone). .env me NVIDIA_MODEL badal do, "
                         "jaise z-ai/glm-5.3, phir restart karo.")
            else:
                reply = (f"arey wasim, AI se connect nahi ho paya ({e}). "
                         f"thodi der me try karte hain, ya bolo to PC wala kaam kar dun.")
            yield ("token", reply)
            yield ("done", reply)
            self.history.append({"role": "assistant", "content": reply})
            return

        full = []
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"]
            except (KeyError, IndexError, json.JSONDecodeError, ValueError):
                continue
            # Nemotron reasoning models may stream reasoning_content; skip it
            # for voice latency -- we only speak final content.
            token = delta.get("content") or ""
            if token:
                full.append(token)
                yield ("token", token)
        reply = "".join(full).strip() or "hmm, kuch samajh nahi aaya wasim, phir se bolo?"
        yield ("done", reply)
        self.history.append({"role": "assistant", "content": reply})

    # -- high-level: chat + run at most one PC action ------------------------
    def handle_user_text(self, user_text, action_runner=None):
        """Returns dict(reply=spoken_text, action=action_or_None, action_result=...).

        Streams tokens via on_token callback if given (for live avatar status).
        """
        tokens = []
        full_text = ""
        for kind, payload in self.chat_stream(user_text):
            if kind == "token":
                tokens.append(payload)
            elif kind == "done":
                full_text = payload
        action, spoken = extract_action(full_text)
        result = None
        if action and action_runner:
            try:
                result = action_runner(action.get("action"),
                                       action.get("args") or {})
            except Exception as e:
                result = f"action me dikkat aayi: {e}"
        return {"reply": spoken or full_text, "action": action,
                "action_result": result,
                "raw": full_text}
