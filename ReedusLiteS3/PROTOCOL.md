# Standalone Reedus Lite protocol (v3)

The standalone S3 firmware (`firmware/src/relay.cpp`) talks to the relay (`relay/reedus_relay/session.py`) over one WebSocket at `wss://<relay>/lite`. Version 3 is the run box's version 2 (`ReedusLite/PROTOCOL.md`) with the changes below. Everything not listed here (audio format, face and glance messages, music lane framing) is unchanged.

## What's different from v2

| | v2 (run box) | v3 (relay) |
|---|---|---|
| Transport | `ws://` on the LAN, found by UDP beacon | `wss://` to a fixed host (`RELAY_HOST`), certificate pinned by `tools/fetch_ca.py` |
| Who it is | shared `LITE_TOKEN` | a per-device token from the relay (`rld_…`); revoking one Lite doesn't touch the others |
| Mic | streams all the time | streams only after the **wake word** (or, with the relay doing the wake word, while the **speech gate** is open), or while the relay has said `listen` |
| Wake word | run box | the S3 hears "Reedus" itself (microWakeWord) and says `wake`; the relay runs openWakeWord only for a Lite that can't |
| Ringing | run box plays the sound | the S3 makes the ring tone, and rings on its own if the relay doesn't |
| Memory | run box only | the relay pushes the bulletin; the S3 keeps a copy in flash |

## Device to relay

| type | fields | when |
|---|---|---|
| `hello` | `proto: 3, device, fw, token, rate, frame_ms, caps, bulletin_rev` | first message. `caps` includes `gate`, `local_ring`, `bulletin` (and `music24` on the 48 kHz build, `wake` when the wake word model loaded). Bad token closes with 4001 |
| `wake` | `score` | the S3 heard "Reedus" (`score`: the sliding-window average probability, 0–1). Only after `welcome` said `wake: "device"`. A `gate` open and `WAKE_PREROLL_MS` of pre-roll follow right behind it, then live audio |
| `gate` | `open` | the gate opened (the wake word; or, with the relay doing the wake word, a voice or a loud beam) or closed (`GATE_HOLD_MS` after the voice stopped). Mic frames only flow between an open and a close, plus the pre-roll sent right after the open |
| `bulletin_ack` | `rev` | the bulletin was saved to flash |
| `doa`, `battery`, `music` | as v2 | |
| `status` | v2 fields + `gate`, `wake` (`device` or `relay`) | every 10 s |

Mic frames are unchanged: 16 kHz mono int16, 20 ms (640 bytes) per binary message.

## Relay to device

| type | fields | effect |
|---|---|---|
| `welcome` | `proto: 3, volume, brightness, account, device, wake` | reply to `hello`. `wake: "device"`: the relay trusts the S3's wake word, so the S3 keeps the mic to itself until it hears the name. `"relay"` (or missing, from an older relay): the S3 uses its speech gate and the relay listens for the name |
| `listen` | `open` | `true`: a conversation is open, stream every mic frame (the relay is listening for the end of a sentence or a follow-up). `false`: back to the gate |
| `bulletin` | `rev, text` | Reedus's notes about this account. Saved to NVS, acknowledged with `bulletin_ack` |
| `state`, `wake`, `emotion`, `expr`, `flush`, `volume`, `brightness`, `look`, `clock`, `timers`, `glance`, `music` | as v2 | |

Speech comes down exactly as in v2 (kind byte `1`, then 16 kHz int16), paced to stay at most `RELAY_LEAD_S` (0.35 s) ahead of the device's playhead, so a `flush` silences him within one frame.

## The wake word

A Lite whose wake word model loaded (`firmware/src/wake.cpp`) puts `wake` in its `caps`. With `RELAY_WAKE=device` (the default) the relay answers `wake: "device"`, and then:

1. Nothing goes up while idle. The S3 runs microWakeWord on its own copy of the mic: a 40-channel spectrogram slice every 10 ms, the model every 30 ms, a detection when the average of the last 5 probabilities passes the cutoff (0.90, from `wakeword/models/reedus.json`), then 2 s of refractory.
2. On "Reedus" the S3 perks up the face, turns the head, ducks music and hushes any speech that's playing, then sends `wake`, `gate` open, the last `WAKE_PREROLL_MS` (250 ms, which covers the detector's delay so a sentence said in one breath keeps its first word), and live audio.
3. The relay treats `wake` like its own detection: `wake` and `listen: true` back, or a barge-in while he's thinking or speaking. A `wake` while it's already listening is ignored. The S3 doesn't replay the face for the relay's echo of its own wake.
4. When the relay closes `listen`, the gate closes and the S3 goes back to listening on its own.

`RELAY_WAKE=model` (openWakeWord for everyone) and `gate` answer `wake: "relay"`, and so does any relay from before this; the S3 then uses its speech gate, exactly as before. A Lite without the cap gets openWakeWord whatever `RELAY_WAKE` says (unless it's `gate`).

## Ringing

The relay's assistant (the same `reedus_companion.assist` code the run box uses) decides when something rings and sends it in `timers.ring`. Whenever `ring` is non-empty, the S3 beeps every 1.6 s unless a conversation is open (you're saying "stop" or "snooze"). The wake word hushes it on the relay side.

If a countdown on the device hits zero, or an armed alarm comes due, and no ring arrives within 2.5 s (or the link is down), the S3 rings anyway for up to `LOCAL_RING_MAX_MS`. The next `timers` message from the relay replaces whatever the device decided.

## Plain HTTP

| path | |
|---|---|
| `/health` | `reedus relay ok` (Railway's health check) |
| `/relay.json` | `{"relay": true, "name", "app": "/app"}`: tells ReedOS it was served by a relay |
| `/`, `/js/…`, `/css/…`, `/assets/…` | the ReedOS app (`ReedOS/reedos-app/src`, copied to `/app/ui` in the image) |

## The app socket (`/app`)

ReedOS (and `tools/admin.py`) send `{"type":"auth","token":…}` first. An account token (`rla_…`) signs in as that account; `RELAY_ADMIN_TOKEN` signs in as admin. `auth.ok` says `owner: true` for the relay's first account (and admin): owners can set the relay's keys and add Lites. Then each request is `{"id", "action", …}` and each reply echoes `id`.

| action | fields | reply |
|---|---|---|
| `status` | | account, balance, `low`, devices (online, state, battery), current LLM, providers this relay has keys for |
| `llm.set` | `provider, model` | switches every Lite on the account from its next sentence |
| `memory.get` | `limit?` | `bulletin, rev, recent` turns |
| `memory.set` | `bulletin` | replaces the notes, pushes them to the devices |
| `memory.forget` | | deletes the notes and all history |
| `device.set` | `device, volume?, brightness?` | saved and pushed live |
| `ledger` | `limit?` | top-ups and spend, newest first |
| `chat` | `text` | type to Reedus: same brain and memory, no voice |
| `topup.start` | `usd` ($5–$200), `return_to?`, `save_card?`, `auto? {below, amount}` | a Stripe Checkout `url` to send the browser to. `return_to` is honoured only for this relay or a private-network ReedOS. `save_card` keeps the card (under a Stripe Customer for the account) for auto top-up |
| `topup.confirm` | `session` | asks Stripe; credits the pool once (`credited`, `already`, or `status: open/expired`). With `save_card`, also `card: {brand, last4, exp}`, and auto top-up switches on |
| `billing.get` | | `card` (brand, last four, expiry, never the card itself), `auto {enabled, below, amount}`, `auto_failed` (why it switched itself off), `limits {gap_min, per_day}`. Also inside `status` |
| `billing.set` | `enabled, below?, amount?` | auto top-up on/off and its numbers (below $0.50–$50, amount $5–$200). On needs a saved card |
| `billing.forget_card` | | detaches the card at Stripe, forgets it here, auto top-up off |
| `setup.status` | | the checklist (`items`: brain, voice, lite, wake, payments, pool), `payments`, `stripe_mode`, `amounts`. Owners also get `brains` and `services`: every provider/service with its get-a-key steps, billing note and key status (`set`, `hint`, `source`: `app` or `railway`) |
| owner `setup.key` | `name, value, force?` | tests the key (one tiny real request) and saves it only if it works, unless `force` |
| owner `setup.test` / `setup.forget` | `name` | test the saved key / remove it (falls back to the Railway variable) |
| owner `device.create` | `name, host?` | a new Lite: its token (shown once) and a ready `secrets_h` |
| admin `account.create` | `name, balance` | the account and its app token (shown once) |
| admin `device.create` | `account, name` | the device and its device token (shown once) |
| admin `topup` | `account, usd` | the new balance |
| admin `accounts` | | every account and balance |
