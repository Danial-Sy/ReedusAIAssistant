
import time
from machine import Pin

BOOT_DELAY_S = 3

led = Pin("LED", Pin.OUT)

print("")
print("ReedOS / Reedus starting in %d s, Ctrl-C now for the REPL" % BOOT_DELAY_S)
for i in range(BOOT_DELAY_S, 0, -1):
    print("  %d..." % i)
    led.on(); time.sleep_ms(120)
    led.off(); time.sleep_ms(880)

print("starting link agent")
import reedus_link          # noqa: F401  -- never returns; serves until power-off
