# shared hardware layer: Pico W + 2x BTS7960 + 2x 37mm 12V 76RPM encoder motors (100:1)
# lives on the Pico; test_both_motors.py and pid_drive.py import it.

from machine import Pin, PWM
import time

# bench-verified constants
COUNTS_PER_REV = 4400      # 11 PPR x 4 (quadrature) x 100:1 gearbox
RATED_RPM      = 76        # no-load output-shaft RPM at 12 V
PWM_FREQ       = 20000     # Hz, above audible range

# LEFT wheel  -> PWM slice 5, encoder GP14/15
M_L_RPWM  = 10
M_L_LPWM  = 11
M_L_ENC_A = 14
M_L_ENC_B = 15
# RIGHT wheel -> PWM slice 6, encoder GP16/17
M_R_RPWM  = 12
M_R_LPWM  = 13
M_R_ENC_A = 16
M_R_ENC_B = 17
# R_EN / L_EN on both drivers are tied to 5 V (always enabled), no GPIO needed

class Motor:
    def __init__(self, rpwm_pin, lpwm_pin, freq=PWM_FREQ, invert=False):
        self.rpwm = PWM(Pin(rpwm_pin)); self.rpwm.freq(freq)
        self.lpwm = PWM(Pin(lpwm_pin)); self.lpwm.freq(freq)
        self.invert = invert
        self.stop()

    def drive(self, duty):
        # duty in [-1.0, +1.0]; positive = forward for this wheel
        if self.invert:
            duty = -duty
        if duty > 1.0:
            duty = 1.0
        elif duty < -1.0:
            duty = -1.0
        if duty >= 0:
            self.lpwm.duty_u16(0)
            self.rpwm.duty_u16(int(duty * 65535))
        else:
            self.rpwm.duty_u16(0)
            self.lpwm.duty_u16(int(-duty * 65535))

    def stop(self):
        self.rpwm.duty_u16(0)
        self.lpwm.duty_u16(0)

_QTABLE = (0,  1, -1,  0,
          -1,  0,  0,  1,
           1,  0,  0, -1,
           0, -1,  1,  0)

class Encoder:
    def __init__(self, pin_a, pin_b, sign=1):
        self.a = Pin(pin_a, Pin.IN, Pin.PULL_UP)
        self.b = Pin(pin_b, Pin.IN, Pin.PULL_UP)
        self.sign = sign              # flip to -1 if "forward" reads negative
        self.count = 0
        self._state = (self.a.value() << 1) | self.b.value()
        self.a.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=self._cb)
        self.b.irq(trigger=Pin.IRQ_RISING | Pin.IRQ_FALLING, handler=self._cb)

    def _cb(self, pin):
        new = (self.a.value() << 1) | self.b.value()
        self.count += self.sign * _QTABLE[(self._state << 2) | new]
        self._state = new

    def reset(self):
        self.count = 0

def counts_to_rpm(delta_counts, dt_s):
    if dt_s <= 0:
        return 0.0
    return (delta_counts / COUNTS_PER_REV) / dt_s * 60.0

M_L_INVERT = True      # verified on the bench: both flipped = forward
M_R_INVERT = False

ENC_L_SIGN = -1
ENC_R_SIGN = 1


def make_drive():
    """Both motors and encoders, correctly oriented. Use this everywhere."""
    left = Motor(M_L_RPWM, M_L_LPWM, invert=M_L_INVERT)
    right = Motor(M_R_RPWM, M_R_LPWM, invert=M_R_INVERT)
    enc_l = Encoder(M_L_ENC_A, M_L_ENC_B, sign=ENC_L_SIGN)
    enc_r = Encoder(M_R_ENC_A, M_R_ENC_B, sign=ENC_R_SIGN)
    return left, right, enc_l, enc_r
