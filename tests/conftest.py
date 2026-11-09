"""
Изоляция pytest: тесты не обращаются к настоящим GPIO Raspberry Pi.
"""
from __future__ import annotations

import copy
import json
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

class FakePWM:
    """
    Запоминает назначение вывода и все выставленные значения duty.
    """
    def __init__(self, pin, *, initial_value=0, frequency=100, **kwargs):
        # Имитируем метаданные аппаратного PWM для status.json
        self.pin = pin
        self.frequency = frequency
        self.chip = Path("pwmchip0")
        self.channel = self.chip / "pwm2"
        self.values = []
        self._value = None
        self.value = initial_value
        self.closed = False

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, duty):
        self._value = duty
        self.values.append(duty)

    def close(self):
        self.closed = True


class FakeTACH:
    def __init__(self, pin, **kwargs):
        self.pin = pin
        self.when_activated = None
        self.closed = False

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self, chip=0, **kwargs):
        self.chip = chip
        self.closed = False

    def close(self):
        self.closed = True


# Имитируем gpiozero до импорта daemon. Подмена существует только в pytest-процессе.
gpiozero = types.ModuleType('gpiozero')
gpiozero.PWMOutputDevice = FakePWM
gpiozero.DigitalInputDevice = FakeTACH
pins = types.ModuleType('gpiozero.pins')
lgpio = types.ModuleType('gpiozero.pins.lgpio')
lgpio.LGPIOFactory = FakeFactory
sys.modules.update({'gpiozero': gpiozero, 'gpiozero.pins': pins,
                    'gpiozero.pins.lgpio': lgpio})


import noctua_daemon as daemon  # noqa: E402
from noctua import config, calibration, monitor  # noqa: E402

@pytest.fixture
def project(tmp_path, monkeypatch):
    """
    Изолированная копия config/status и безопасный контроллер без __init__.
    """
    original = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    config_path = tmp_path / 'config.json'
    status_path = tmp_path / 'status.json'
    config_path.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')

    monkeypatch.setattr(config, 'CONFIG', config_path)
    monkeypatch.setattr(daemon, 'CONFIG', config_path)
    monkeypatch.setattr(daemon, 'STATUS', status_path)
    monkeypatch.setattr(calibration, 'STATUS', status_path)
    monkeypatch.setattr(monitor, 'STATUS', status_path)

    class Context:
        def __init__(self):
            self.original = original
            self.config_path = config_path
            self.status_path = status_path
            self.tmp_path = tmp_path

        def get(self):
            return json.loads(config_path.read_text(encoding='utf-8'))

        def put(self, new_config):
            config_path.write_text(json.dumps(new_config, ensure_ascii=False), encoding='utf-8')

        def controller(self):
            """
            Тест без запроса gpiochip и открытия GPIO.
            """
            instance = daemon.Controller.__new__(daemon.Controller)
            instance.cfg = copy.deepcopy(self.get())
            instance.applied_pwm = 0
            instance.cpu_latched_temp = None
            instance.nvme_latched_temp = None
            instance.last_policy_change_time = time.monotonic()
            instance.nvme_history = deque(maxlen=int(instance.cfg['control']['nvme_average_samples']))
            instance.zero_since = None
            instance.low_rpm_since = None
            instance.rpm_ema = 0.0
            instance.last_rpm_time = time.monotonic()
            instance.lock = threading.Lock()
            instance.pulses = 0
            instance.pwm = FakePWM(int(instance.cfg['gpio']['pwm_gpio']),
                                   initial_value=0,
                                   frequency=int(instance.cfg['gpio']['pwm_frequency_hz']))
            return instance

    return Context()


def reference_pwm(temperature, levels):
    """
    Независимая линейная интерполяция для ожидаемого PWM.

    Использует текущие LEVEL из config.json, но НЕ вызывает daemon.interp_pwm().
    """
    if temperature <= levels[0][0]:
        return levels[0][1]
    if temperature >= levels[-1][0]:
        return levels[-1][1]

    for left, right in zip(levels, levels[1:]):
        t0, pwm0 = left
        t1, pwm1 = right
        if t0 <= temperature <= t1:
            return round(pwm0 + (temperature - t0) * (pwm1 - pwm0) / (t1 - t0))

    raise AssertionError("Нет интервала для температуры")