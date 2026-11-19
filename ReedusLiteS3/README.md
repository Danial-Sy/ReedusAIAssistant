# Reedus Lite, standalone

This is Reedus Lite without the Xeon. The XIAO ESP32-S3 on the reSpeaker XVF3800 talks straight to a small **relay** in the cloud (Railway + Postgres), and the relay talks to Gemini (or Claude, GPT…), Inworld and the memory. Nothing runs at home except Lite and your router.

This is now the **primary** Reedus Lite. The run-box version in `ReedusLite/` is untouched and still works; both use the same hardware, so you flash whichever one you want.

```
 Reedus Lite (XIAO ESP32-S3 + XVF3800)            relay (Railway)                     the cloud
┌────────────────────────────────────┐  wss  ┌──────────────────────────────┐
│ face, head, battery, glance cards  │◄─────►│ wake word ("hey reedus")     │──► speech-to-text (Gemini)
│ speech gate: mic up only on a voice│ 16 kHz│ endpointing, barge-in        │──► Reedus: persona + memory
│ speaker, ducking, music mixing     │  PCM  │ timers / alarms / lists      │    on Gemini / Claude / GPT…
│ ring tone, rings on its own        │ + JSON│ memory: bulletin + history   │──► Inworld (his voice)
│ bulletin copy in flash             │       │ funding pool, metering       │
└────────────────────────────────────┘       │ /app API for ReedOS          │◄── Postgres
                                             └──────────────────────────────┘
```

## What runs where

| | ESP32-S3 | relay | provider |
|---|---|---|---|
| Face, head, battery, glance screens, audio I/O | ✅ | | |
| Speech gate (stream only while someone's talking) | ✅ | | |
| Timer countdowns, ringing (also when the link is down) | ✅ | decides | |
| Bulletin copy (offline memory) | ✅ | source of truth | |
| "Hey Reedus" | phase 2 | ✅ phase 1 | |
| End of sentence, barge-in, follow-ups without the name | | ✅ | |
| Timers, alarms, reminders, lists (the run box's `assist` code) | | ✅ | |
| Persona (the robot's own `persona.py`), conversation history | | ✅ | |
| Memory: bulletin + searchable history | | ✅ Postgres | |
| Funding pool, cost per turn, ledger | | ✅ | |
| Speech-to-text | | asks | Gemini (or OpenAI) |
| Thinking | | asks | Gemini / Claude / GPT / OpenRouter… |
| Web search | | asks | the brain's own (Google, Claude, OpenAI) |
| Voice | | asks | Inworld |

The relay imports the brain code from `reedus_llm/reedus_companion`: persona, the multi-provider LLM layer, Inworld, timers and emotion tagging. The robot and Lite are still one character, and switching LLMs works the same way on both.

## Setup

Four steps. Only the first happens on Railway's website; the rest is in ReedOS.

### 1. Deploy the relay on Railway

1. **New service** → *Deploy from GitHub repo* → this repo. In the service's **Settings**:
   - *Root Directory*: leave empty (the repo root; the relay imports `reedus_llm/`).
   - *Config-as-code* → *Railway config file*: `ReedusLiteS3/relay/railway.json`. It points the build at `ReedusLiteS3/relay/Dockerfile` and the health check at `/health`.
2. **Add Postgres**: *New* → *Database* → *PostgreSQL*. In the relay service's Variables, add `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`. The relay creates its tables on first start.
3. **Two variables** on the relay service. Everything else, API keys included, is set from ReedOS later.

   | Variable | |
   |---|---|
   | `RELAY_ADMIN_TOKEN` | any long random string. It makes accounts and can always reach Setup |
   | `TZ` | your timezone, e.g. `America/Chicago`, so "7 am" means your 7 am |

   Keys can still be Railway variables if you prefer (`GEMINI_API_KEY`, `INWORLD_API_KEY`, `ANTHROPIC_API_KEY`, `STRIPE_SECRET_KEY`, …); a key saved in ReedOS overrides the variable.
4. **The wake word model**: copy `hey_reedus.onnx` from the run box (`$REEDUS_ROOT/wakeword/`) into `ReedusLiteS3/relay/models/` and commit it. Without it Lite won't wake, and both the log and ReedOS's Setup say so. For a first test you can set `RELAY_WAKE=gate`, which makes any speech near Lite count as the wake word.
5. *Settings* → *Networking* → **Generate Domain**. That's your relay, e.g. `reedus-relay.up.railway.app`. `https://<that>/health` should say `reedus relay ok`. Stripe sends people back to this address; if you use a custom domain instead, set `RELAY_PUBLIC_URL=https://<it>`.

### 2. Make your account

```bash
pip install websockets
```

```bash
python ReedusLiteS3/tools/admin.py wss://<relay> <RELAY_ADMIN_TOKEN> account "Danial"
```

It prints your **app token** (shown once). The first account on a relay is its **owner**: it can set the relay's keys and add Lites from ReedOS. Accounts made later only use the relay.

### 3. Set everything up in ReedOS

Open `https://<relay>/` on your phone or laptop (add it to the home screen if you like) and paste the app token. **Setup** at the top of Standalone Lite is a checklist; tap a step and it walks you through it:

| Step | What it asks for |
|---|---|
| **A brain** | Pick Gemini, Claude, GPT, OpenRouter, Grok, DeepSeek, Groq or Mistral. It shows where to get that key, step by step, and that the account needs money on it. Paste the key; the relay tests it with one tiny request and only saves it if it works. Your first brain key also becomes what Reedus thinks with |
| **His voice** | The Inworld key, same way |
| **A Lite** | Name it, and you get its `secrets.h` with the device token and your relay's address already filled in. The token is shown once |
| **"Hey Reedus"** | Whether the relay has the wake word model (step 1.4) |
| **One-tap top-ups** (optional) | A Stripe key. It explains test vs live mode and suggests a restricted key that can only make Checkout pages |
| **Money in the pool** | Top up from the Pool card |

Keys are stored in the relay's Postgres, are only ever shown as `sk-a…good`, and only the owner can change them.

### 4. Flash the S3

1. Paste the `secrets.h` from *A Lite* into `ReedusLiteS3/firmware/src/secrets.h` and fill in your Wi-Fi (2.4 GHz).
2. Pin the relay's certificate so the S3 checks it's really your relay:
   ```bash
   python ReedusLiteS3/tools/fetch_ca.py <relay host>
   ```
3. Flash it (keep the XIAO's antenna plugged in):
   ```bash
   cd ReedusLiteS3/firmware && pio run -t upload && pio device monitor
   ```
4. The serial monitor's `help` lists everything. `info` shows the relay link and whether the mic is streaming, `gate` prints the speech gate opening and closing (tune `GATE_RMS` with it), `bulletin` shows what Reedus remembers.

Flash the XVF3800's I2S master firmware first, exactly as for the run-box Lite (`ReedusLite/WIRING.md`).

**Before flashing**, you can talk to the relay from the laptop: record "Hey Reedus, what's the weather like?" as a 16 kHz mono WAV and run `python ReedusLiteS3/tools/sim.py wss://<relay> <device token> hello.wav`. It prints every event and the reply latency, and saves his answer to `reply.wav`.

## ReedOS, with the Xeon off

Standalone Lite (Settings → Standalone Lite) is the whole Lite, from any phone:

- **Setup**: the checklist above.
- **Pool**: the balance, $5 / $10 / $20 / $50 and **Top up**, which opens Stripe Checkout (card, Apple Pay, Google Pay: one tap on a phone). You come back to ReedOS with the money already in the pool. Tick **Save my card and top up automatically** once, and after that the pool refills itself: when it drops under $2 (or $1 / $5) it adds your amount from that card. The card shows as `Visa •••• 4242` with an on/off switch, the two amounts, and *Remove card*. The recent charges below break each turn's cost down (voice, thinking, speech-to-text).
- **Brain**: the provider and model for every Lite on the account. Only providers with a key are listed.
- **Your Lites**: online or not, what each is doing, battery, and volume and face brightness that apply live.
- **Memory**: Reedus's notes about you, editable (saved notes go straight to each Lite), the recent conversations, and *Forget everything*.
- **Talk to him**: type to Reedus with the same brain and memory. The regular Chat tab also goes through the relay whenever the Xeon isn't reachable.

On the run box's ReedOS (or the desktop app), the same page asks for the relay's address as well as the token. The top-bar pill reads **Xeon off** when the robot side is simulated but Lite is live on the relay. *Try the demo* shows the whole page, Setup and top-ups included, without a relay.

## Money

Every cloud cost (speech-to-text, the LLM, web searches, Inworld) is metered per turn and taken from the account's pool, and `ledger` lists each charge. When the pool hits zero, Lite shows `TOP UP IN REEDOS` and says he's out of credit instead of spending more. Top-ups go through Stripe Checkout: the relay starts the payment, asks Stripe whether it was paid when you come back, and credits the pool exactly once. It re-checks open payments every 30 s, so closing the tab before you come back still credits it. Stripe's fee (about 2.9% + 30¢) is the relay owner's; the full amount goes into the pool.

**Auto top-up** charges the saved card off-session (you agreed to that on Stripe's page when you saved it), right after the charge that took the pool under your line. Guard rails: at most one automatic charge every 10 minutes and 3 a day (`RELAY_AUTO_GAP_S`, `RELAY_AUTO_PER_DAY`), each with its own Stripe idempotency key so a network retry can't charge twice. If the card is declined, has expired, or the bank wants 3-D Secure, auto top-up switches itself off and the Pool card says why. The card itself stays with Stripe; the relay keeps only its brand, last four digits and expiry. The owner can still add money by hand with `tools/admin.py … topup <account> 20`. At list prices, one exchange (his ~3,500-token prompt on Gemini Flash plus a ~150-character spoken reply on Inworld TTS-2) costs a bit under a cent, more than half of it the voice. On Claude Sonnet it's a bit over a cent. These are estimates; the ledger shows the real numbers. Railway's own bill (relay + Postgres) is separate, on your Railway plan.

## Status

Verified on this laptop:
- `relay/tests/test_relay.py`: 121 checks, end to end over real WebSockets with the cloud services faked. Covers wake → listen → think → speak → follow-up, paced speech, the timer fast path, barge-in, the pool, memory (history, recall, bulletin folding and push), the app API and admin, the relay serving ReedOS (no files outside the app folder), Setup (owner-only, keys tested before they're saved, never sent back whole), top-ups against a fake Stripe (credited once, a foreign return address refused, an abandoned tab credited by the reconciler), and auto top-up (card saved through Checkout, refill only under the line, the 10-minute gap and daily cap, unique idempotency keys, a decline and a 3-D Secure request switching it off with the reason, Remove card detaching it).
- ReedOS's Standalone Lite page against a real local relay (fake LLM, fake Lite connected), in the browser: sign-in (a wrong token and a revoked saved token are both refused clearly), pool and ledger, switching to Claude, volume reaching the connected Lite live, editing notes (pushed to the Lite), chat (the Chat tab included), phone width, and the demo. Then Setup and top-ups against a real local relay and a fake Stripe whose checkout page pays and redirects back: bad keys refused with the reason, good keys saved, the first Claude key switching the brain, a $20 top-up landing in the pool, cancelling, and adding a Lite with its secrets.h. And auto top-up end to end: a top-up with "save my card" (card shown as Visa •••• 4242, auto on), raising the threshold to $5, spending in chat until the pool fell under it, exactly one off-session $10 charge refilling it, and Remove card detaching it at Stripe.
- A real local relay with no API keys, driven by `tools/sim.py`: it fails politely (confused face, back to idle, the missing key named in the log).
- The firmware **compiles** for `seeed_xiao_esp32s3` (RAM 26.7%, flash 28.8%), both with a pinned certificate and without.

Not verified yet:
- Anything on the real S3: TLS memory with a live audio stream, the gate threshold, the ring tone volume.
- Real Gemini / Inworld / Stripe calls through the relay (the key tests and Checkout are written against their documented APIs, but only fakes have answered), and the relay's Postgres code path. Tests use SQLite; the SQL is written for both, but only SQLite has run.
- A Railway deploy.

## Roadmap

1. **Bring-up (next):** deploy, run `sim.py`, flash, tune `GATE_RMS`, measure reply latency.
2. **Wake word on the S3** (microWakeWord): nothing leaves Lite until you say the name, and the relay stops running openWakeWord.
3. ~~**ReedOS standalone mode.**~~ Done: Settings → Standalone Lite, and the relay serves the app.
4. ~~**One-tap funding and easy setup.**~~ Done: Stripe Checkout top-ups, auto top-up from a saved card, and the Setup checklist in ReedOS.
5. **Offline mode:** if the relay is unreachable, the S3 calls Gemini and Inworld directly with the cached bulletin.
6. **Glance cards from the relay** (weather, next class, now playing), **Spotify control**, per-account timezones.

## Files

| | |
|---|---|
| `firmware/` | PlatformIO project. The hardware modules are forked from `ReedusLite/firmware` (same board); `relay.cpp` (link + speech gate), `store.cpp` (bulletin in NVS) and local ringing in `glance.cpp` are new |
| `relay/reedus_relay/` | `server.py` (sockets, app API), `session.py` (one Lite's conversation loop), `brain.py`, `speech.py`, `wake.py`, `memory.py`, `accounts.py`, `meter.py`, `assist_store.py`, `db.py` |
| `relay/Dockerfile`, `relay/railway.json` | the Railway build (context = repo root) |
| `relay/tests/test_relay.py` | the end-to-end test: `python ReedusLiteS3/relay/tests/test_relay.py` |
| `tools/admin.py`, `tools/sim.py`, `tools/fetch_ca.py` | accounts and devices; a fake Lite; certificate pinning |
| `ReedOS/reedos-app/src/js/relay.js`, `js/standalone.js`, `js/setup.js` | the app's relay client, the Standalone Lite page, and its Setup checklist |
| `relay/reedus_relay/vault.py`, `funding.py` | keys set from ReedOS (tested, owner-only), and Stripe Checkout top-ups |
| `PROTOCOL.md` | the wire protocol (v3) and the app API |
