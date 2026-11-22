# Reedus Lite, standalone

This is Reedus Lite without the Xeon. The XIAO ESP32-S3 on the reSpeaker XVF3800 talks straight to a small **relay** in the cloud (Railway + Postgres), and the relay talks to Gemini (or Claude, GPT…), Inworld and the memory. Nothing runs at home except Lite and your router.

This is now the **primary** Reedus Lite. The run-box version in `ReedusLite/` is untouched and still works; both use the same hardware, so you flash whichever one you want.

```
 Reedus Lite (XIAO ESP32-S3 + XVF3800)            relay (Railway)                     the cloud
┌────────────────────────────────────┐  wss  ┌──────────────────────────────┐
│ face, head, battery, glance cards  │◄─────►│ endpointing, barge-in        │──► speech-to-text (Gemini)
│ "Reedus" wake word: mic up after it│ 16 kHz│ (openWakeWord: older fw only)│──► Reedus: persona + memory
│ speaker, ducking, music mixing     │  PCM  │ timers / alarms / lists      │    on Gemini / Claude / GPT…
│ ring tone, rings on its own        │ + JSON│ memory: bulletin + history   │──► Inworld (his voice)
│ bulletin copy in flash             │       │ credit, metering, pairing    │
└────────────────────────────────────┘       │ the website + its /app API   │◄── Postgres
                                             └──────────────────────────────┘
```

## What runs where

| | ESP32-S3 | relay | provider |
|---|---|---|---|
| Face, head, battery, glance screens, audio I/O | ✅ | | |
| Mic gate (nothing streams until "Reedus", or while a conversation is open) | ✅ | | |
| Timer countdowns, ringing (also when the link is down) | ✅ | decides | |
| Bulletin copy (offline memory) | ✅ | source of truth | |
| "Reedus" (the wake word) | ✅ microWakeWord | fallback for older firmware (openWakeWord "hey reedus") | |
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

Deploy once on Railway, then everything happens on the relay's website.

### 1. Deploy the relay on Railway

1. **New service** → *Deploy from GitHub repo* → this repo. In the service's **Settings**:
   - *Root Directory*: leave empty (the repo root; the relay imports `reedus_llm/`).
   - *Config-as-code* → *Railway config file*: `ReedusLiteS3/relay/railway.json`. It builds `ReedusLiteS3/relay/Dockerfile` and health-checks `/health`.
2. **Add Postgres**: *New* → *Database* → *PostgreSQL*. In the relay service's Variables, add `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`. The relay makes its tables on first start.
3. **Two variables** on the relay service:

   | Variable | |
   |---|---|
   | `RELAY_ADMIN_TOKEN` | any long random string. It's the **setup code**: the website asks for it when you set the relay up, and the owner can always sign in with it |
   | `TZ` | your timezone, e.g. `America/Chicago`, so "7 am" means your 7 am |

   API keys are added on the website later. (They can also be Railway variables: `GEMINI_API_KEY`, `INWORLD_API_KEY`, `STRIPE_SECRET_KEY`, …; a key saved on the website wins.)
4. *Settings* → *Networking* → **Generate Domain**, e.g. `reedus-relay.up.railway.app`. With a custom domain instead, also set `RELAY_PUBLIC_URL=https://<it>` so Stripe sends people back to the right place.

### 2. Set it up on the website

Open `https://<relay>/` on a phone or laptop (*Add to Home Screen* makes it an app).

1. **Create the owner account**: your name and the setup code. That browser stays signed in.
2. **Home → Getting set up** lists what's left, and each step opens the right place:

   | Step | What it needs |
   |---|---|
   | **AI key** | any one of Gemini, Claude, GPT, OpenRouter, Grok, DeepSeek, Groq, Mistral. *Settings → Service keys* shows where to get each, tests the key with one tiny request and only saves it if it works |
   | **Hearing** | a Gemini or OpenAI key turns speech into text (Claude and the others can't take audio). One Gemini key covers thinking and hearing |
   | **Voice** | an Inworld key |
   | **Pair your Lite** | the 6-digit code on Lite's face (step 3) |
   | **Add credit** | the owner can add credit by hand (*Credit*), or anyone can pay by card once there's a Stripe key |
   | **Card payments** (optional) | a Stripe key. Start with a test key: nothing real is charged |

3. **AI model** (*Settings*): any provider that has a key, any model. Pick from the list, *Show every model my key can use*, or type any model id.

**More people:** *Settings → People → Invite someone* makes a one-time link (7 days). Each person gets their own Lites, memory and credit. **Another device:** *Settings → Sign in another device* shows a 6-digit code to type on the other device's sign-in page.

### 3. Flash Lite and pair it

1. Copy `firmware/src/secrets.example.h` to `firmware/src/secrets.h` and fill in your Wi-Fi (2.4 GHz) and `RELAY_HOST`. Leave `LITE_TOKEN` alone: pairing fills it in. One build works for every Lite on the relay.
2. Pin the relay's certificate: `python ReedusLiteS3/tools/fetch_ca.py <relay host>`
3. Flash the XVF3800's I2S master firmware first, exactly as for the run-box Lite (`ReedusLite/WIRING.md`), then the S3 (keep the antenna plugged in):
   ```bash
   cd ReedusLiteS3/firmware && pio run -t upload && pio device monitor
   ```
4. Once it's online, Lite's face flashes **PAIR** and then holds six digits in two rows: top row first, then the bottom (the serial monitor prints them too). Tap **Pair a Lite** on the website and type them. Lite gets its own token, keeps it in flash, and reconnects as itself.

Serial commands worth knowing: `info` (link, pairing, wake word), `unpair` (forget the token and show a new code), `wifi <network> <password>` (join another network without reflashing), `help` for the rest.

**Before flashing**, you can talk to the relay from the laptop with `tools/sim.py` (it needs a device token: `python ReedusLiteS3/tools/admin.py wss://<relay> <RELAY_ADMIN_TOKEN> device <account-id> "Sim"`).

### The wake word

Nothing to set up: the S3 hears "Reedus" itself (`firmware/src/wake.cpp`). Only a Lite on older firmware needs the relay's openWakeWord model (`hey_reedus.onnx` in `relay/models/`; `RELAY_WAKE=model` forces it for everyone, `RELAY_WAKE=gate` makes any speech count, for a first test).

## The website

`ReedusLiteS3/web`, served by the relay at `/`. Four tabs:

- **Home**: what's left to set up, your Lites (online or not, what each is doing, battery, volume and brightness live, rename, remove) and **Pair a Lite**.
- **Chat**: type to Reedus, same brain and memory as talking to Lite.
- **Credit**: what's left, **Pay** by card (Stripe Checkout: card, Apple Pay, Google Pay) with *Save my card and refill automatically*, auto refill settings, the owner's *Add credit by hand*, and recent activity (each turn's cost).
- **Settings**: the AI model, *Service keys* and *People* (owner), what Reedus remembers (editable, pushed to each Lite), *Sign in another device*, theme, sign out.

## Listening, speed, and what the app controls

- **End of a sentence**: Silero VAD (`relay/models/silero_vad.onnx`) decides what's speech, so a fan or a TV doesn't hold the turn open; anything 15 dB quieter than the person talking to Lite counts as background. The sentence ends 0.6 s after they stop (`RELAY_END_SILENCE_S`). `RELAY_VAD=energy` goes back to loudness only.
- **Speed**: transcription starts at the first 0.3 s pause, so it's usually done when the sentence ends; it uses `gemini-3.5-transcribe` (falls back to Flash). The brain thinks at the lowest level the model allows and only gets web search when the sentence sounds like it needs it (`RELAY_SEARCH=auto|always|off`). Weather at home comes from Open-Meteo, no search. Every turn logs `heard in … ms … voice started … ms after you stopped`.
- **Voice**: Settings → Voice lists Inworld's voices, plays a sample, and sets the speed.
- **Idle screen**: Settings → Idle screen turns on the weather card for a place you type.
- **Controls** (Home → a Lite): expressions, state previews, the head dial, a 16x16 drawing pad with animations (up to 32 frames), and **Update** (owner): the latest CI build (`.github/workflows/lite-firmware.yml` publishes it) or a `firmware.bin` you upload. The first firmware with updates in it (0.3.0) goes on over USB once.

## Money

Every cloud cost (speech-to-text, the LLM, web searches, Inworld) is metered per turn and taken from the account's pool, and `ledger` lists each charge. When the pool hits zero, Lite shows `TOP UP IN REEDOS` and says he's out of credit instead of spending more. Top-ups go through Stripe Checkout: the relay starts the payment, asks Stripe whether it was paid when you come back, and credits the pool exactly once. It re-checks open payments every 30 s, so closing the tab before you come back still credits it. Stripe's fee (about 2.9% + 30¢) is the relay owner's; the full amount goes into the pool.

**Auto top-up** charges the saved card off-session (you agreed to that on Stripe's page when you saved it), right after the charge that took the pool under your line. Guard rails: at most one automatic charge every 10 minutes and 3 a day (`RELAY_AUTO_GAP_S`, `RELAY_AUTO_PER_DAY`), each with its own Stripe idempotency key so a network retry can't charge twice. If the card is declined, has expired, or the bank wants 3-D Secure, auto top-up switches itself off and the Pool card says why. The card itself stays with Stripe; the relay keeps only its brand, last four digits and expiry. The owner can still add money by hand with `tools/admin.py … topup <account> 20`. At list prices, one exchange (his ~3,500-token prompt on Gemini Flash plus a ~150-character spoken reply on Inworld TTS-2) costs a bit under a cent, more than half of it the voice. On Claude Sonnet it's a bit over a cent. These are estimates; the ledger shows the real numbers. Railway's own bill (relay + Postgres) is separate, on your Railway plan.

## Status

Verified on this laptop:
- `relay/tests/test_relay.py`: 175 checks, end to end over real WebSockets with the cloud services faked. Covers wake → listen → think → speak → follow-up, the S3's own wake word, paced speech, the timer fast path, barge-in, credit, memory (history, recall, bulletin folding and push), the website socket and admin, the relay serving the website (no files outside its folder), signing in without tokens (setup refused once claimed, the owner's setup code, one-time invites, one-time 6-digit device codes, sign-out killing that browser's token, owner-only actions), pairing (a code on the face, a wrong code refused, the Lite handed its token and reconnecting, rename, remove sending it back to a code, older firmware still refused), any model id, the fallback when a picked provider loses its key, who does the hearing (Gemini, else OpenAI), the setup checklist, keys tested before they're saved, top-ups and auto top-up against a fake Stripe.
- The website against a real local relay in the browser, desktop and phone width, dark and light: setting the relay up (a wrong setup code refused), the checklist, pairing a pretend Lite by its code (it got its token, reconnected, showed online with its battery), switching to Claude with a typed-in model id, adding credit by hand, an invite link making a second account (who sees only their own two steps), sign-out, the owner signing back in with the setup code, and a 6-digit code signing a browser in.
- A real local relay with no API keys, driven by `tools/sim.py`: it fails politely (confused face, back to idle, the missing key named in the log).
- The firmware **compiles** for `seeed_xiao_esp32s3` with the wake word (RAM 20.1%, flash 34.9%; without it 19.6% / 28.7%: TFLite Micro with ESP-NN plus the 62 KB model is ~210 KB of flash).
- The wake word's feature path, built on the laptop from the same sources (`firmware/tools/check_wake_features.py`, 361 clips: held-out Kokoro voices, sound-alikes, espeak, 5 min of LibriSpeech): the spectrogram is bit-identical to pymicro_features (what microWakeWord trains on), the int8 inputs are never more than one step from exact quantization, and the device's detector wakes on the same clips as `wakeword/mww_eval.py`'s path (358–360 of 361; the rest sit right at the cutoff).

Not verified yet:
- Anything on the real S3: TLS memory with a live audio stream, the gate threshold, the ring tone volume.
- The wake word on the real S3: inference time and heap (`wake` prints both), which I2S slot hears it best (`WAKE_CHANNEL`), whether 250 ms of pre-roll keeps the first word without the end of the name, and recall on your voice through the XVF3800 (`wakeword/README.md`).
- The firmware's pairing on a real S3 (it compiles; the flow was checked against the relay with a pretend Lite).
- Real Gemini / Inworld / Stripe calls through the relay (the key tests and Checkout are written against their documented APIs, but only fakes have answered), and the relay's Postgres code path. Tests use SQLite; the SQL is written for both, but only SQLite has run.
- The new website, pairing and sign-in codes on the Railway deploy. The relay itself is deployed there and starts on Postgres, but its Postgres queries have only run in that startup, not under use.

## Roadmap

1. **Bring-up (next):** deploy, run `sim.py`, flash, tune `GATE_RMS`, measure reply latency.
2. **Wake word on the S3** (microWakeWord): in the firmware. Nothing leaves Lite until you say "Reedus", and the relay runs openWakeWord only for older firmware. Left: check it on the bench (`wake`) and on your own voice.
3. ~~**The website.**~~ Done: the relay serves it; set-up, pairing by code, invites, credit, any model.
4. ~~**One-tap funding and easy setup.**~~ Done: Stripe Checkout top-ups, auto top-up from a saved card.
5. **Wi-Fi without a cable:** a setup hotspot on the S3 so Wi-Fi doesn't have to be in `secrets.h` or typed over serial.
6. **Offline mode:** if the relay is unreachable, the S3 calls Gemini and Inworld directly with the cached bulletin.
7. **Glance cards from the relay** (weather, next class, now playing), **Spotify control**, per-account timezones.

## Files

| | |
|---|---|
| `firmware/` | PlatformIO project. The hardware modules are forked from `ReedusLite/firmware` (same board); `relay.cpp` (link + mic gate), `wake.cpp` (the wake word, on TFLite Micro from `nickjgniklu/ESP_TF`), `lib/microfrontend` (the spectrogram, same code microWakeWord trains with), `store.cpp` (bulletin in NVS) and local ringing in `glance.cpp` are new |
| `firmware/tools/` | `embed_wake_model.py` (builds the model into the firmware on every `pio run`), `check_wake_features.py` (the laptop check above) |
| `relay/reedus_relay/` | `server.py` (sockets, app API), `session.py` (one Lite's conversation loop), `brain.py`, `speech.py`, `wake.py`, `memory.py`, `accounts.py`, `meter.py`, `assist_store.py`, `db.py` |
| `relay/Dockerfile`, `relay/railway.json` | the Railway build (context = repo root) |
| `relay/tests/test_relay.py` | the end-to-end test: `python ReedusLiteS3/relay/tests/test_relay.py` |
| `tools/admin.py`, `tools/sim.py`, `tools/fetch_ca.py` | accounts and devices; a fake Lite; certificate pinning |
| `web/` | the website the relay serves: `index.html`, `app.js` (relay client and every page), `app.css` |
| `relay/reedus_relay/vault.py`, `funding.py` | keys set from the website (tested, owner-only), and Stripe Checkout top-ups |
| `PROTOCOL.md` | the wire protocol (v3) and the app API |
| `wakeword/` | the on-device "Reedus" wake word: the trained model, and the pipeline that makes it (see its README) |
