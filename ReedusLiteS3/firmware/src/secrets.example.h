// copy to secrets.h (gitignored) and fill in.
#pragma once

#define WIFI_SSID "your-network-name"   // 2.4 GHz: the XIAO ESP32-S3 has no 5 GHz radio
#define WIFI_PASS "your-password"

// the relay on Railway: the service's public domain, without https:// (Settings → Networking)
#define RELAY_HOST "your-relay.up.railway.app"
#define RELAY_PORT 443
#define RELAY_TLS 1        // 0 only for a relay on your own laptop: ws://<laptop-ip>:8080

// this Lite's device token: `python -m reedus_relay device <account-id> "Desk Lite"` prints it
#define LITE_TOKEN "rld_..."
