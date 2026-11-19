# Reedus Lite wiring

The pins here are the ones in `firmware/src/config.h`. Wire it exactly like this and the firmware needs no changes.

## XIAO ESP32S3 pin map

The XIAO comes soldered to the reSpeaker XVF3800 board, and the board already uses six of its pins. We add three. On the back of the board, pads labelled **XIAO_D0 to XIAO_D3** sit in a row next to **5V, GND and 3.3V** pads. Solder our three wires to those pads.

| XIAO pin | GPIO | Used by | Connect to |
|---|---|---|---|
| **D0** | GPIO1 | **Face LED data (ours)** | SN74AHCT125 pin 2 (1A) |
| D1 | GPIO2 | nothing (pad only) | **Leave unconnected.** It may be the XVF3800 reset line (unconfirmed) |
| **D2** | GPIO3 | **Head servo signal (ours)** | SG90 orange wire, through the neck |
| **D3** | GPIO4 | **Battery sense (ours)** | Divider midpoint (see below), through the neck |
| D4 | GPIO5 | board: I2C SDA (XVF3800 0x2C, AIC3104 0x18) | already wired |
| D5 | GPIO6 | board: I2C SCL | already wired |
| D6 | GPIO43 | board: I2S data in (mics) | already wired |
| D7 | GPIO44 | board: I2S data out (speaker) | already wired |
| D8 | GPIO7 | board: I2S LRCK | already wired |
| D9 | GPIO8 | board: I2S BCLK | already wired |
| D10 | GPIO9 | board: MCLK net | already wired, don't use |

Why these three:
- All four free pads (D0 to D3) can do ADC (ADC1, which keeps working with Wi-Fi on) and any output.
- **D2 (GPIO3) is an ESP32-S3 strapping pin** (JTAG source select, read at reset). The servo signal is an input on the SG90 side, so it doesn't pull the pin either way at boot. That makes it safe there.
- The battery divider holds about 2 V on its pin constantly, so it goes on **D3**, which is not a strapping pin.
- D1 is avoided because one source says it's the XVF3800 reset line. If a scope later shows it isn't, it's a spare.

## Boards

The head circuit (the 74AHCT125 and its parts) goes on an **ElectroCookie half-size** (30 rows, with power rails), taped to the back of the face backer just below its cutout. The base circuit (divider, servo header, 470 µF, 5V/GND buses, speaker junction) goes on an **ElectroCookie mini** (17 rows, no rails), taped to the base floor plate on the robot's left of the servo. The full step-by-step soldering walkthrough with pictures is the "Reedus Lite Wiring" artifact; the same pictures are in `wiring/`.

Both boards are described component side up, **row 1 on the left, column A along the bottom**. Half-board rails as printed: top = − outer, + inner; bottom = − inner (next to A), + outer.

### Head board (half), hole by hole

| What | Holes |
|---|---|
| SN74AHCT125N (soldered in, no socket) | rows 10-16 across the channel, **notch toward row 30**: pin 1 = F16, pin 7 = F10, pin 8 = E10, pin 14 = E16 |
| GND jumpers | J10, J12, J13, J16 to the top − rail (pins 7, 5, 4, 1) |
| 5V jumpers | A11, A12, A14, A15, A16 to the bottom + rail, which is the OUTER bottom rail on this board (pins 9, 10, 12, 13, 14); insulated, hopping the − rail |
| 330 Ω | G14 to G19 |
| 100 nF | bottom + rail to bottom − rail, row 17 |
| 1000 µF | top + rail to top − rail, row 30, laid flat off the end |
| Rail links | not needed: the half board's bridge pads on the back join top and bottom rails (verify with a meter) |
| reSpeaker 5V / GND pads | top + / − rail, row 5 |
| reSpeaker XIAO_D0 | J15 |
| Matrix DIN / 5V / GND | J19 / top + rail row 28 / top − rail row 28 |
| Pass-throughs | reSpeaker XIAO_D2 at E21, XIAO_D3 at E23; the neck wire for each lands in A of the same row. (Speaker no longer uses this board.) |
| Neck 5V / GND | bottom + / − rail, row 3 |

### Base board (mini), hole by hole

| What | Holes |
|---|---|
| Divider | 100k C2 to C6, 100k D6 to D10, 100 nF B6 to B10 (row 2 = BAT+, row 6 = middle, row 10 = GND) |
| GND bridge | E10 to H10 |
| 470 µF | − in F10, + in F11 (standing) |
| Servo leads | 3 male dupont jumpers, cut 8 cm from the pin end, soldered into J10 GND (brown), J11 5V (red), J12 signal (orange); the male pins plug into the SG90's plug, taped together |
| In | BAT+ A2 (UPS battery + terminal, big round point on the left), UPS− pad A10, rocker black lead (switched 5V) H11 |
| Neck | 5V I11, GND I10, servo signal H12, battery sense A6 |
| Speaker | not on this board: the lead is extended (~40-45 cm) and plugs straight into the reSpeaker SPK socket |

Row 10 F-J is the base GND bus and row 11 F-J the 5V bus.

## Head (turns with the servo)

### LED matrix (BTF-LIGHTING WS2812B 16x16, GRB)

```
5V bus ──┬──────────────── matrix 5V (red)
         │   ┌───────────┐
         ├──►│14 VCC     │
D0 ─────────►│2  1A   1Y 3├──[330R]──► matrix DIN (green)
GND ─────┬──►│7  GND      │
         ├──►│1  1OE      │   (OE low = gate on)
         │   └───────────┘
         └──────────────── matrix GND (white)
```

- SN74AHCT125N: pin 14 to 5V, pin 7 to GND, **pin 1 (1OE) to GND**, pin 2 from D0, pin 3 through 330 Ω to DIN.
- Put a 100 nF ceramic across the head board's bottom rails at row 17, next to pin 14.
- Unused gates: no input may float. Tie pins 4 and 5 (2OE, 2A, on the top side, next to the − rail) to **GND**. Tie pins 9, 10, 12, 13 (3A, 3OE, 4A, 4OE, on the bottom side) to **5V** on the outer bottom rail. Leave the outputs (6, 8, 11) unconnected. Every pin then reaches its nearest rail without crossing the chip. The tables above give the hole-by-hole layout.
- Put a 1000 µF / 10 V electrolytic across the head board's top rails at row 30, stripe (−) on the − rail, laid flat off the end. It sits next to where the matrix power leaves the board (row 28).
- Power the matrix from the 5V bus, not from the board's 3.3V pad. The firmware caps brightness at 18% and the frame current at 1.2 A (`BRIGHTNESS_CAP`, `LED_POWER_MA`).
- Feed DIN at the panel's **input** end (the arrow on the strip points away from it).

### Speaker

Seeed 4 Ω 5 W speaker on the board's speaker connector (marked SPK). It sits in the base: its lead is cut, extended ~30 cm per wire (soldered, heat-shrunk, joints staggered) and runs up the neck with the bundle, plug end first.

## Base

### Servo (SG90)

| SG90 wire | Connect to |
|---|---|
| brown | GND (base bus) |
| red | 5V (base bus, straight from the UPS side, **not** from the board) |
| orange | D2, through the neck |

Put a 470 µF electrolytic across the servo's 5V and GND near the servo. A starting SG90 pulls a spike that can otherwise reset the XIAO. The firmware stops pulsing the servo half a second after each move (`HEAD_DETACH_MS`), so it doesn't buzz into the mics while it's still.

### Battery sense

```
cell + (UPS module B+) ──[100k]──┬──[100k]── GND
                                 ├──[100nF]── GND
                                 └────────── D3 (through the neck)
```

- Build the divider on the base board (the ElectroCookie mini), next to the cells. Only its midpoint goes up the neck.
- At 4.2 V the cell gives 2.1 V at D3, well inside the ADC range.
- Once it's running, compare the `info` reading against a multimeter on the cells. Put the ratio in `BATT_CAL`.

### Power

```
2x P28A (parallel) ── LX-2BUPS UPS ── UPS+ ── rocker ── 5V bus ──┬── board 5V pad (head)
                                                                    ├── matrix + 74AHCT125 (head)
                                                                    └── servo (base)
```

- Every GND is common: UPS (UPS− pad), board GND pad, matrix, 74AHCT125, servo, divider.
- **Flashing over USB:** the board's 5V pad and its USB-C share a rail. Turn the rocker **off** before plugging the XIAO into a computer, or the UPS and the PC back-feed each other.

## Neck: 4 wires plus the speaker lead

| Wire | From | To |
|---|---|---|
| 5V | base board I11 | head board bottom + rail (outer), row 3 |
| GND | base board I10 | head board bottom − rail (inner), row 3 |
| servo signal | head board A21 (XIAO D2) | base board H12 (SG90 orange) |
| battery sense | base board A6 (divider midpoint) | head board A23 (XIAO D3) |
| speaker lead | speaker (base) | reSpeaker SPK socket (head), not on either board |

All 22 AWG, 30 cm (speaker lead ~40-45 cm total). The plug end of the speaker lead fits the 7 mm holes, so thread it first. Solder the head ends on the bench; thread through head floor, hub and collar slot; then solder the base ends. The head only turns ±90°, so a loose ~5 cm loop in the base is enough and no slip ring is needed.

## Before the first power-up

1. **Flash the XVF3800's I2S master firmware.** The firmware expects the XVF3800 to clock the I2S bus at 48 kHz, which is the setup the ESPHome integration uses. Download `respeaker_xvf3800_i2s_master_*_48k.bin` from `xmos_firmwares/i2s` in [respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY). Plug into the **XMOS** USB-C port (the one nearer the 3.5 mm jack), then run:
   ```bash
   dfu-util -R -e -a 1 -D respeaker_xvf3800_i2s_master_v1.0.9_48k.bin
   ```
   Don't cut power mid-flash. To use Seeed's other I2S firmware instead (XIAO as master at 16 kHz), build with `XVF_I2S_MASTER_48K 0`.
2. Run the `test` command and check the matrix orientation: red at top-left, green at top-right, as seen from the front. If they're wrong, set `MATRIX_ROTATE` / `MATRIX_FLIP_X`.
3. `servo 30` should turn the face to **its** left. If it goes right, set `SERVO_DIR -1`. Then adjust `SERVO_CENTER_US` until `servo 0` looks dead ahead.
4. `doa`: stand in front and talk. Put the reported `raw` angle in `DOA_FRONT_DEG`. Step to its left; `rel` must go positive, otherwise set `DOA_SIGN -1`.

## Sources

- Seeed board pinout photo (the XIAO_D0 to D3, MCLK and power pads): [respeaker_xvf3800_introduction](https://wiki.seeedstudio.com/respeaker_xvf3800_introduction/)
- Bus pins and the XVF-as-master setup ("Do not change the pins. They are fixed by the board layout."): [formatBCE Respeaker-XVF3800-ESPHome-integration](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration)
- Seeed's I2S example pins (8/7/44/43): [respeaker_xvf3800_xiao_i2s](https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_i2s/)
- XVF3800 I2C commands (DoA = resid 20, cmd 18): [respeaker_xvf_3800_i2c_list](https://wiki.seeedstudio.com/respeaker_xvf_3800_i2c_list/)
- XIAO ESP32S3 D-pin to GPIO table: [xiao_esp32s3_getting_started](https://wiki.seeedstudio.com/xiao_esp32s3_getting_started/)
- ESP32-S3 strapping pins (GPIO0, 3, 45, 46) and ADC1 channels: [ESP-IDF GPIO reference](https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/gpio.html)
- MCLK on GPIO9: [tutoduino XVF3800 guide](https://tutoduino.fr/en/respeaker-xvf3800/)
- D1 as a possible reset line (a sister board, so unconfirmed here): [keryx notes](https://github.com/Flopsstuff/keryx/blob/main/docs/respeaker-flex-xvf3800.md)
