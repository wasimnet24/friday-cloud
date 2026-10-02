"""Friday's brain: multi-provider auto-failover chat with STREAMING.

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

import config
import providers
from memory.learn import LearnMemory

SYSTEM_PROMPT = """You are Friday, wasim's personal AI assistant. Female, warm, a little playful, \
like a close friend. You ALWAYS reply in Hindi written in Devanagari script (देवनागरी), \
mixed naturally with English tech words in Roman script (like YouTube, WhatsApp, phone). \
Write the way Indian friends text in Hindi -- natural, casual, never robotic.

!! REPLY STYLE (sabse important): MAX 2 chhoti lines. Seedhi baat, no bakwaas. \
Bina wajah sawal mat puchho, lecture mat do. Jaise dost text karte hain.

!! IMAANDARI (kabhi mat todo):
- Action ka RESULT aane se pehle "ho gaya / khol diya / kar diya" MAT BOLO.
- Tum pehle action block bhejti ho, phir system action chala ke result deta hai. \
Tumhara likha "ho gaya" sirf tab sach hai jab result me success likha ho \
("khol diya", "kar diya", "mil gayi"). Result me "nahi", "dikkat", "koshish", \
"offline" aaye to saaf-saaf batao ki kaam NAHI hua aur kyun.
- Andaza mat lagao. Pata nahi to "pata nahi" bolo.

IMPORTANT: Tum ab CLOUD server pe chal rahi ho (24/7 online), wasim ka Windows PC \
ghar pe hai aur kabhi OFFLINE ho sakta hai. PC wale actions (open_app, shutdown_pc, \
screenshot, waghaira) cloud se uske PC ko bheje jayenge -- agar PC offline hua to \
action ka result "offline" aayega. Aise me wasim ko saaf-saaf batao ki PC offline hai, \
jhootha "ho gaya" kabhi mat bolo. Normal baat-cheet, sawal-jawab, notes, web search \
hamesha kaam karte hain chahe PC on ho ya off.

You can control wasim's Windows PC by embedding action blocks in your reply:

```friday-action
{"action": "<name>", "args": {...}}
```

Available actions:
- open_app: {"name": "notepad"} - open an app/program (whatsapp, spotify bhi chalta hai)
- close_app: {"name": "notepad"} - close an app
- open_folder: {"path": "D:\\j\\personal\\friday"} - EXACT folder path ko Explorer me kholo. \
Agar wasim ne poora path diya (D:\\..., C:\\...) to HAMESHA ye use karo, find_file nahi.
- close_folders: {} - saare khule folder windows band karo
- find_file: {"query": "resume"} - file YA folder naam se dhoondo (Desktop/Documents/Downloads/D:/C:)
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
- lock_screen: {}
- shutdown_pc: {"confirm": true} - PC BAND karo. !! RULE: pehli baar "pc band kar do" pe action MAT chalao -- pehle puchho "pakka wasim? PC band kar dun?". Sirf jab wasim "haan"/"pakka"/"yes" kahe tab confirm:true ke saath chalao. confirm ke bina kabhi mat chalao.
- restart_pc: {"confirm": true} - PC restart karo. Wahi confirm RULE jaisa shutdown me.

Rules:
- Use an action ONLY when wasim asks you to DO something on the PC.
- MULTI-STEP: agar wasim kahe "pehle X phir Y" (jaise "folders band karke shutdown kar do"), \
to 3 tak action blocks ek ke baad ek de sakti ho -- system unhe order me chalega. \
Example: close_folders phir shutdown_pc (confirm ke saath, agar wasim ne pehle hi haan kaha ho).
- After the action blocks, still write a SHORT Hindi (Devanagari) spoken reply (MAX 2 lines).
- For normal chit-chat/questions, output NO action block, just the reply.
- Never output more than 3 action blocks. Never invent actions.

Example (multi-step):
wasim: saare folders band karke pc band kar do... haan pakka
you:
```friday-action
{"action": "close_folders", "args": {}}
```
```friday-action
{"action": "shutdown_pc", "args": {"confirm": true}}
```
ho gaya wasim, folders band, PC band ho raha hai.
"""

ACTION_BLOCK_RE = re.compile(
    r"```(?:friday-action)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)


def extract_action(text):
    """Return (action_dict_or_None, spoken_text). Safe: never raises.

    Backward-compat wrapper -- pehla action block leta hai.
    Multi-step ke liye extract_actions use karo.
    """
    actions, spoken = extract_actions(text)
    return (actions[0] if actions else None), spoken


def extract_actions(text):
    """Return (list_of_action_dicts, spoken_text). Safe: never raises.

    wasim agar "pehle X phir Y" kahe to model 3 tak action blocks de sakta
    hai -- sab extract honge, order me chalenge. Max 3 (safety).
    """
    text = text or ""
    found, spans = [], []
    for match in ACTION_BLOCK_RE.finditer(text):
        if len(found) >= 3:
            break
        try:
            action = json.loads(match.group(1))
            if isinstance(action, dict) and "action" in action:
                found.append(action)
                spans.append(match.span())
        except (json.JSONDecodeError, ValueError):
            continue
    parts, last = [], 0
    for s, e in spans:
        parts.append(text[last:s])
        last = e
    parts.append(text[last:])
    return found, "".join(parts).strip()


class FridayBrain:
    def __init__(self):
        self.history = []  # list of {"role":..., "content":...}
        self.learn = LearnMemory()  # self-learning: corrections -> rules
        # multi-provider router: offline sirf jab koi provider key nahi
        self.router = providers.ProviderRouter()
        self.offline = not self.router.has_providers
        self.last_provider = None

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
        """Router se pehla healthy provider; fail par auto next.

        Returns (provider_name, response). Sab fail par
        providers.AllProvidersFailed raise hota hai.
        """
        name, resp = self.router.post_stream(
            messages,
            max_tokens=config.NVIDIA_MAX_TOKENS,
            temperature=config.NVIDIA_TEMPERATURE,
        )
        self.last_provider = name
        return resp

    def chat_stream(self, user_text):
        """Generator yielding ("token", chunk) live, then ("done", full_text).

        In offline mode (no API key) yields a canned Hinglish reply instead.
        """
        self.history.append({"role": "user", "content": user_text})
        self.history = self.history[-10:]  # keep context small = fast

        if self.offline:
            reply = ("haan wasim, sun rahi hun! (offline mode hun abhi -- "
                     "Render Environment me kisi provider ki API key daal do "
                     "(VYCEAI_API_KEY, OPENROUTER_API_KEY...), "
                     "phir full AI chat chalega.)")
            yield ("token", reply)
            yield ("done", reply)
            self.history.append({"role": "assistant", "content": reply})
            return

        messages = [{"role": "system", "content": self._system_prompt()}] + self.history
        try:
            resp = self._post_stream(messages)
            resp.raise_for_status()
        except providers.AllProvidersFailed as e:  # sab providers fail
            detail = str(e)[:200]
            reply = ("arey wasim, saare AI providers fail ho gaye. "
                     f"({detail}) thodi der me try karte hain.")
            yield ("token", reply)
            yield ("done", reply)
            self.history.append({"role": "assistant", "content": reply})
            return
        except Exception as e:  # network/auth failure -> graceful Hindi error
            msg = str(e)
            if "410" in msg or "404" in msg:
                # model retired/removed by provider -- not our bug, tell clearly
                reply = ("arey wasim, lagta hai provider ne ye AI model band kar "
                         "diya hai. Render env me model badal do, phir redeploy karo.")
            else:
                reply = (f"arey wasim, AI se connect nahi ho paya ({e}). "
                         f"thodi der me try karte hain.")
            yield ("token", reply)
            yield ("done", reply)
            self.history.append({"role": "assistant", "content": reply})
            return

        full = []
        # NOTE: decode_unicode=True mat use karo -- kuch providers charset header
        # nahi bhejte aur requests ISO-8859-1 guess karke Devanagari bigaad deta
        # hai (mojibake). Hamesha explicit UTF-8 decode karo.
        for raw in resp.iter_lines():
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
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

    # -- high-level: chat + run actions (multi-step supported, max 3) ------
    def handle_user_text(self, user_text, action_runner=None):
        """Returns dict(reply=spoken_text, actions=[{action,result}...]).

        Backward compat: "action"/"action_result" = pehle action ka.
        Actions order me chalte hain; koi ek fail ho to aage wale phir bhi
        chalte hain (har result alag record hota hai).
        """
        tokens = []
        full_text = ""
        for kind, payload in self.chat_stream(user_text):
            if kind == "token":
                tokens.append(payload)
            elif kind == "done":
                full_text = payload
        actions, spoken = extract_actions(full_text)
        ran = []
        if action_runner:
            for action in actions:
                try:
                    res = action_runner(action.get("action"),
                                        action.get("args") or {})
                except Exception as e:
                    res = f"action me dikkat aayi: {e}"
                ran.append({"action": action, "result": res})
                # shutdown/restart ne confirm manga to aage mat badho
                if isinstance(res, str) and res.startswith("CONFIRM_NEEDED"):
                    break
        first = ran[0] if ran else None
        return {"reply": spoken or full_text,
                "actions": ran,
                "action": first["action"] if first else None,
                "action_result": first["result"] if first else None,
                "raw": full_text}
