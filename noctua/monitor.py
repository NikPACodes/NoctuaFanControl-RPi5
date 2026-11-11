# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Nikolay Petukhov (NikPACodes)
"""
Отображение состояния и настроек Noctua Fan Control.

Используется для:
- отображения status.json;
- live-мониторинга;
- отображения температур CPU и NVMe;
- отображения PWM и RPM;
- отображения запросов PWM от CPU и NVMe;
- отображения температурных кривых;
- отображения health/failsafe состояния;
- просмотра доступных данных CPU/HAT fan в режиме read-only.

Данные Noctua берутся из status.json, который формирует noctua_daemon.
"""
import os
import time
from pathlib import Path

from noctua.config import read_json, load_cfg
from noctua.paths import STATUS, BOOTCFG


# ------------------------------------------------------------
# Display helpers
# ------------------------------------------------------------
def _pct(pwm):
    """
    PWM 0..255 -> проценты
    """
    return float(pwm) * 100 / 255


def _system_model():
    """
    Модель Raspberry Pi
    """
    path = Path("/proc/device-tree/model")
    try:
        return path.read_text().replace("\x00", "").strip()
    except OSError:
        return "Unknown Raspberry Pi"


def _show_value(value, suffix=""):
    """
    Формат вывода значений
    """
    if value is None:
        return "—"
    return f"{value}{suffix}"


def _show_pwm(value):
    """
    Формат вывода PWM
    """
    if value is None:
        return "—"
    try:
        pwm = int(value)
        if not 0 <= pwm <= 255:
            return "—"
    except (TypeError, ValueError, OverflowError):
        return "—"
    return f"{pwm}/255 ({_pct(pwm):5.1f}%)"


def _show_level(temp, levels):
    """
    Положение температуры между LEVEL.
    """
    if temp is None:
        return "—"
    points = [ (float(temp), int(pwm)) for temp, pwm in levels ]

    if temp <= points[0][0]:
        return "LEVEL 0"

    for i in range(1, len(points)):
        if temp <= points[i][0]:
            return f"LEVEL {i-1}->{i}"
    return f"LEVEL {len(points)-1}"


# ------------------------------------------------------------
# Экранный вывод
# ------------------------------------------------------------
def _print_section(title):
    """
    Вывод заголовков
    """
    print("\n" + "─" * 76)
    print(title)
    print("─" * 76)


def _print_temperatures(state):
    """
    Вывод температуры CPU/NVMe
    """
    _print_section("Температуры")
    print(f"CPU temp:            {_show_value(state.get('cpu_temp_c'), '°C')}")
    print(f"NVMe сейчас:         {_show_value(state.get('nvme_temp_current_c'), '°C')}")
    print(f"NVMe средняя:        {_show_value(state.get('nvme_temp_avg_c'), '°C')}")
    print(f"NVMe samples:        {state.get('nvme_average_samples_used', '—')} / {state.get('nvme_average_samples_target', '—')}")


def _print_cpu_fan_data(data, standalone=False):
    """
    Вывод информации CPU/HAT fan (read-only)
    """
    if standalone:
        print("CPU/HAT fan (read-only)")
        print("═" * 76)
    else:
        _print_section("CPU/HAT fan (read-only)")

    print(f"Источник hwmon:      {data['source'] or '—'}")
    print(f"RPM:                 {_show_value(data['rpm'], ' rpm')}")
    print(f"PWM:                 {_show_pwm(data['pwm_raw'])}")
    print("\nfan_temp* из config.txt")
    print("─" * 76)
    if data["config_lines"]:
        for line in data["config_lines"]:
            print(line)
    else:
        print("явные настройки не найдены")


def _print_noctua(state, fan_name, age, reason=None):
    """
    Вывод информации Noctua
    """
    print("\n" + "═" * 76)
    print(fan_name)
    print("═" * 76)

    if reason:
        print(f"\nWARNING: {reason}\n")

    maintenance_code = state.get("maintenance")
    maintenance = {
        "OK": "OK",
        "OBSERVE": "RPM ниже baseline — наблюдение",
        "CHECK_FAN": "ПРОВЕРИТЬ ВЕНТИЛЯТОР / ФИЛЬТР / ПИТАНИЕ",
        "NO_BASELINE": "нет baseline — выполни calibrate",
        "NO_BASELINE_FOR_PWM": "для этого PWM нет baseline",
        "LOW_PWM": "PWM слишком низкий для диагностики",
        "DISABLED": "диагностика отключена",
    }.get(maintenance_code, maintenance_code or "—")

    ratio = state.get("rpm_ratio")
    try:
        ratio_text = "—" if ratio is None else f"{float(ratio) * 100:.1f}% baseline"
    except (TypeError, ValueError, OverflowError):
        ratio_text = "—"

    age_text = "—" if age is None else f"{age:.1f} s"
    alarms = state.get('alarms') or []
    print(f"Status age:          {age_text}")
    print(f"Mode:                {state.get('mode', '—')}")
    print(f"PWM сейчас:          {_show_pwm(state.get('applied_pwm'))}")
    print(f"PWM target:          {_show_pwm(state.get('target_pwm'))}")
    print(f"Control source:      {state.get('control_source', '—')}")
    print(f"RPM:                 {_show_value(state.get('rpm'), ' rpm')}")
    print(f"Expected RPM:        {_show_value(state.get('expected_rpm'), ' rpm')}")
    print(f"RPM health:          {ratio_text}")
    print(f"Maintenance:         {maintenance}")
    print(f"Alarms:              {', '.join(map(str, alarms)) if alarms else 'нет'}")


def _print_interface(state):
    """
    Вывод интерфейса GPIO подключения Noctua
    """
    _print_section("Интерфейс")
    pwm_gpio = state.get('pwm_gpio')
    tach_gpio = state.get('tach_gpio')
    gpio_label = (f"GPIO{pwm_gpio} / GPIO{tach_gpio}"
                  if pwm_gpio is not None and tach_gpio is not None else "—")
    chip = state.get('gpiochip')
    chip_label = f"gpiochip{chip}" if chip is not None else "—"
    alarms = state.get('alarms') or []
    print(f"GPIO PWM/TACH:       {gpio_label}")
    print(f"PWM frequency:       {_show_value(state.get('pwm_frequency_hz'), ' Hz')}")
    print(f"GPIO chip:           {chip_label}")
    print(f"Alarms:              {', '.join(map(str, alarms)) if alarms else 'нет'}")


def _print_requests(state, cfg):
    """
    Вывод информации по запросам охлаждения Noctua
    """
    _print_section("Запросы охлаждения")
    print(f"CPU request:         {_show_pwm(state.get('pwm_cpu_requested'))}")
    print(f"NVMe request:        {_show_pwm(state.get('pwm_nvme_requested'))}")
    print(f"CPU level:           {_show_level(state.get('cpu_latched_temp_c'), cfg['control']['cpu_levels'])}")
    print(f"NVMe level:          {_show_level(state.get('nvme_latched_temp_c'), cfg['control']['nvme_levels'])}")


# ------------------------------------------------------------
# Температурная кривая
# ------------------------------------------------------------
def print_curve(title, levels):
    """
    Вывод уровней температурной кривой.
    """
    print(f"\n{title}")
    print("─" * 76)
    for index, (temp, pwm) in enumerate(levels):
        pwm = int(pwm)
        print(f"LEVEL {index:02d}: {float(temp):5.1f}°C -> PWM {pwm:3d}/255 ({_pct(pwm):5.1f}%)")


# ------------------------------------------------------------
# Status
# ------------------------------------------------------------
def status(clear=False, watch_mode=False):
    """Единый сбор данных; полный снимок или компактный live-экран."""
    if clear:
        print("\033[2J\033[H", end="")

    cfg = load_cfg()
    raw = read_json(STATUS)
    fan_name = cfg.get("fan", {}).get("name", "Unknown Fan")
    hostname = os.uname().nodename

    age = None
    reason = None
    state = {}

    print("═" * 76)
    print(f"Host: {hostname}")
    print(f"Board: {_system_model()}")
    print("═" * 76)

    # Не выдаём старые данные Noctua за показания в реальном времени.
    if raw:
        try:
            age = time.time() - float(raw["timestamp"])
            max_age = max(3.0, 3.0 * float(cfg["control"]["update_interval_s"]))
            if -2 <= age <= max_age:
                state = raw
            else:
                reason = "Noctua daemon: не отвечает (status.json не обновляется)."
        except (KeyError, TypeError, ValueError, OverflowError):
            reason = "Noctua daemon: ошибка данных status.json."
    else:
        reason = "Noctua daemon: нет данных (status.json отсутствует)."

    # Вывод температуры CPU/NVMe
    _print_temperatures(state)
    # Вывод информации CPU/HAT fan (read-only)
    _print_cpu_fan_data(get_cpu_fan_data())

    # Вывод информации Noctua
    _print_noctua(state, fan_name, age, reason)
    if not watch_mode:
        # Вывод интерфейса GPIO подключения Noctua
        _print_interface(state)
    # Вывод информации по запросам охлаждения Noctua
    _print_requests(state, cfg)

    if not watch_mode:
        # Вывод уровней температурной кривой
        print_curve("CPU levels (Noctua)", cfg["control"]["cpu_levels"])
        print_curve("NVMe levels (Noctua)", cfg["control"]["nvme_levels"])


# ------------------------------------------------------------
# Watch
# ------------------------------------------------------------
def watch(period=1.0):
    """
    Live-мониторинг
    """
    period = float(period)
    if not 0.1 <= period <= 15.0:
        raise ValueError("Доступен период от 0.1 до 15 секунд")
    try:
        while True:
            status(clear=True, watch_mode=True)
            print("\nCtrl+C — выход")
            time.sleep(period)
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------
# Settings
# ------------------------------------------------------------
def settings():
    """
    Конфигурация
    """
    cfg = load_cfg()
    control = cfg["control"]
    health = cfg["health"]
    failsafe = cfg["failsafe"]
    fan_name = cfg.get("fan", {}).get("name", "Unknown Fan")
    baseline = health.get("baseline_rpm",{})

    print(f"Настройки {fan_name}")
    print("═" * 76)
    print(f"Mode:                {control['mode']}")
    print(f"Update interval:     {control['update_interval_s']} s")
    print(f"NVMe average samples:{control['nvme_average_samples']}")
    print(f"Hysteresis:          {control['hysteresis_c']}°C")
    print(f"Minimum dwell:       {control['minimum_dwell_s']} s")
    print(f"Min running PWM:     {control['min_running_pwm']}/255")
    print(f"Kickstart:           {control['kickstart_seconds']} s @ 255/255")
    print(f"Manual PWM:          {control['manual_pwm']}/255 ({_pct(control['manual_pwm']):.1f}%)")
    print(f"Emergency CPU:       {control['emergency_cpu_c']}°C")
    print(f"Emergency NVMe:      {control['emergency_nvme_c']}°C")
    print_curve("CPU levels", control["cpu_levels"],)
    print_curve("NVMe levels", control["nvme_levels"])

    # --------------------------------------------------------
    # Health
    # --------------------------------------------------------
    print("\nHealth")
    print("─" * 76)
    print(f"Enabled:             {health['enabled']}")
    print(f"Min PWM for check:   {health['min_pwm_for_check']}/255")
    print(f"Min RPM ratio:       {float(health['min_rpm_ratio']) * 100:.0f}%")
    print(f"Warning delay:       {health['warn_after_seconds']} s")

    # --------------------------------------------------------
    # Failsafe
    # --------------------------------------------------------
    print("\nFailsafe")
    print("─" * 76)
    print(f"Sensor failure PWM:  {failsafe['sensor_failure_pwm']}/255")
    print(f"TACH enabled:        {failsafe['tach_fail_safe_enabled']}")
    print(f"TACH zero timeout:   {failsafe['tach_zero_seconds']} s")
    print(f"TACH check from PWM: {failsafe['tach_zero_min_pwm']}/255")
    print(f"TACH failure PWM:    {failsafe['tach_failure_pwm']}/255")

    # --------------------------------------------------------
    # Baseline
    # --------------------------------------------------------
    print("\nBaseline RPM")
    print("─" * 76)
    if not baseline:
        print("нет — запусти: sudo python3 fanctl.py calibrate")
    else:
        rows = sorted((int(pwm), int(rpm)) for pwm, rpm in baseline.items())
        print(f"{'PWM':>7}  {'Duty':>7}  {'RPM':>6}")
        print("─" * 26)
        for pwm, rpm in rows:
            print(f"{pwm:3d}/255  {_pct(pwm):6.1f}%  {rpm:6d}")


# ------------------------------------------------------------
# CPU/HAT fan
# ------------------------------------------------------------
def get_cpu_fan_data():
    """
    Сбор фактически доступной информации CPU/HAT fan (read-only)

    Показываем только реально найденные hwmon/config.txt данные.
    """
    rpm = None
    pwm_raw = None
    source = None

    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            has_rpm = (hw / "fan1_input").exists()
            has_pwm = (hw / "pwm1").exists()
            if not (has_rpm or has_pwm):
                continue

            if has_rpm:
                rpm = int((hw / "fan1_input").read_text().strip())

            if has_pwm:
                pwm_raw = int((hw / "pwm1").read_text().strip())

            try:
                source = (hw / "name").read_text().strip()
            except OSError:
                source = hw.name
            break
        except (OSError, ValueError):
            continue

    config_lines = []
    if BOOTCFG.exists():
        try:
            for raw in BOOTCFG.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "fan_temp" in line:
                    config_lines.append(line)
        except OSError:
            pass

    return {
        'rpm': rpm,
        'pwm_raw': pwm_raw,
        'source': source,
        'config_lines': config_lines,
    }


def cpu_fan_info():
    """
    Вывод информации CPU/HAT fan для fanctl cpu.
    """
    _print_cpu_fan_data(get_cpu_fan_data(), standalone=True)