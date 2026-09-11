# slow single-motor spin test
from machine import Pin, PWM
import time

RPWM = PWM(Pin(10))
LPWM = PWM(Pin(11))
RPWM.freq(20000)
LPWM.freq(20000)

def stop():
    RPWM.duty_u16(0)
    LPWM.duty_u16(0)

def forward(duty_pct):
    LPWM.duty_u16(0)
    RPWM.duty_u16(int(duty_pct / 100 * 65535))

def reverse(duty_pct):
    RPWM.duty_u16(0)
    LPWM.duty_u16(int(duty_pct / 100 * 65535))

try:
    print("Ramping forward to 25%...")
    for d in range(0, 26):
        forward(d)
        time.sleep(0.05)
    print("Holding forward at 25% for 2 s")
    time.sleep(2)

    print("Ramping down...")
    for d in range(25, -1, -1):
        forward(d)
        time.sleep(0.05)
    stop()
    time.sleep(1)

    print("Ramping reverse to 25%...")
    for d in range(0, 26):
        reverse(d)
        time.sleep(0.05)
    print("Holding reverse at 25% for 2 s")
    time.sleep(2)

    print("Ramping down...")
    for d in range(25, -1, -1):
        reverse(d)
        time.sleep(0.05)
    stop()
    print("Motor test done.")
finally:
    stop()
