# ☁️ Friday Cloud — PC off ho tab bhi mobile se AI chat

Ye chhota server 24/7 internet pe chalta hai. Phone hamesha **isi se** baat
karta hai:
- **PC on ho ya off** → AI chat, sawal-jawab, notes, web search: hamesha chalega ✅
- **PC on ho** → PC wale commands (notepad kholo, volume, shutdown…) bhi chalenge ✅
- **PC off ho** → PC commands pe Friday bolegi "PC offline hai" (jhootha "ho gaya" nahi)

## Chahiye kya

1. Free hosting account (neeche 3 options — **Fly.io sabse aasaan**)
2. Tumhara NVIDIA API key (wahi jo PC pe `.env` me hai)
3. Ek lamba random token (phone + PC + cloud teeno me same):

```bat
python -c "import secrets; print(secrets.token_hex(24))"
```

---

## Option A — Fly.io (recommended, ~10 min, git nahi chahiye)

1. [fly.io](https://fly.io) pe **sign up** karo (free tier me card nahi mangta).
2. PC pe **flyctl** install karo: [fly.io/docs/hands-on/install-flyctl](https://fly.io/docs/hands-on/install-flyctl)
   (Windows: PowerShell me `iwr https://fly.io/install.ps1 -useb | iex`)
3. Login: `fly auth login`
4. Friday ke `cloud/` folder me jao, phir:

```bat
cd cloud
fly launch --no-deploy
```
- App ka naam do, jaise `friday-wasim` → region `bom` (Mumbai, sabse paas) chunna.
- `fly secrets set CLOUD_TOKEN=<tumhara-token> NVIDIA_API_KEY=<tumhari-key> NVIDIA_MODEL=z-ai/glm-5.3`
- `fly deploy`

5. Address milega: `https://friday-wasim.fly.dev` — browser me kholo, token dalo, chat karo! 🎉

> Fly.io free tier me app soti nahi (24/7 chalti hai). Agar kabhi "suspended"
> dikhe to dashboard se resume kar dena.

## Option B — Render (git chahiye)

1. `cloud/` folder ko GitHub repo me dalo.
2. [render.com](https://render.com) → New → Web Service → repo chuno.
3. Runtime: **Docker**. Environment variables me dalo:
   `CLOUD_TOKEN`, `NVIDIA_API_KEY`, `NVIDIA_MODEL=z-ai/glm-5.3`.
4. Deploy → address milega `https://....onrender.com`.

⚠️ Render **free tier 15 min baad so jata hai** — pehla message ~30-50s me
jayega (uthne me time). Hamesha-fast chahiye to Fly.io (Option A) lo.

## Option C — Oracle Cloud (hamesha-on, thoda technical)

Oracle "Always Free" me lifetime free VM milta hai (card verification lagta hai).
VM pe Docker install karke:

```bash
docker build -t friday-cloud ./cloud
docker run -d -p 8000:8000 -e CLOUD_TOKEN=... -e NVIDIA_API_KEY=... friday-cloud
```

---

## Step 2 — PC ko cloud se jodo (taaki PC commands chalein)

PC ki Friday `.env` me ye 2 lines add karo:

```env
CLOUD_URL=https://friday-wasim.fly.dev
CLOUD_TOKEN=<wahi token jo cloud pe dala>
```

Phir `python run.py` restart karo. Console me dikhega:
`[Friday] PC-CLOUD link on -- phone se PC commands aayengi.`

Test: cloud mobile UI se bhejo `time kya ho raha hai` →
PC on hai to sahi time aayega; PC off karke bhejo to bolegi "PC offline hai".

## Step 3 — Phone pe lagao

Cloud ka address (`https://...`) phone browser me kholo → token dalo →
**"Add to Home Screen"** → ab Friday ka icon hamesha kaam karega,
PC on ho ya off. 📱

## Kharcha

- Fly.io free tier: **₹0** (personal use me enough hai)
- Render free: **₹0** (sota hai)
- NVIDIA API: free key (rate limits ke andar)

## Dikkat aaye to

| Problem | Fix |
|---|---|
| Cloud UI khul rahi, chat ka jawab nahi | `NVIDIA_API_KEY` secrets me sahi dala? `fly logs` me dekho |
| "galat token" | Phone me aur cloud secrets me token SAME hona chahiye |
| PC commands nahi chalte | PC pe `run.py` chal rahi? `.env` me CLOUD_URL/CLOUD_TOKEN? |
| PC offline dikh raha | PC on karo + `run.py` me "PC-CLOUD link on" dikhna chahiye |

## Privacy note

Cloud pe sirf chat text jata hai (NVIDIA API ko). PC ki files cloud pe nahi
aati — PC commands sirf tumhare token se encrypted connection pe chalte hain.
Token kisi se share mat karo.
