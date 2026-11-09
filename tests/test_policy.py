"""
Тесты алгоритма управления Noctua NF-A4x20 5V PWM.

Проверяем:
- Температурные кривые CPU и NVMe.
- Приоритет охлаждения между двумя датчиками.
- Гистерезис и выдержку перед уменьшением PWM.
- Усреднение температуры NVMe.
- Режимы AUTO/MANUAL и аварийные пороги.
- Измерение RPM через TACH.
- Start-boost при запуске вентилятора с нуля.
- Предупреждение о снижении RPM относительно baseline.

Все температуры, импульсы и значения PWM моделируются.
Физическое поведение вентилятора этими тестами не подтверждается.
"""

import pytest
from conftest import daemon, reference_pwm

pytestmark = pytest.mark.policy

def test_01_interpolation_and_two_sensor_priority(project):
    """
    Интерполяция PWM и приоритет CPU/NVMe.

    Проверки:
    1. Проверяем PWM ниже, между и выше LEVEL.
    2. CPU требует большего PWM, чем NVMe.
    3. NVMe требует большего PWM, чем CPU.

    Ожидаемый результат:
    - Интерполяция возвращает правильный PWM.
    - Итоговый PWM равен максимуму двух запросов.
    - Указывается правильный источник управления.
    """
    ctl = project.controller()
    curve = ctl.cfg['control']['nvme_levels']

    for levels in (ctl.cfg['control']['cpu_levels'], curve):
        temperatures = [(levels[0][0] - 1),
                        (levels[-1][0] + 1)]
        temperatures += [temp for temp, _ in levels]
        temperatures += [((a[0] + b[0]) / 2) for a, b in zip(levels, levels[1:])]

        for temp in temperatures:
            assert daemon.interp_pwm(temp, levels) == reference_pwm(temp, levels)

    # Локальный сценарий: CPU требует больше PWM.
    ctl.cfg['control'].update(mode='auto',
                              min_running_pwm=0,
                              emergency_cpu_c=120,
                              emergency_nvme_c=120,
                              cpu_levels=[[20, 0], [60, 180]],
                              nvme_levels=[[20, 0], [60, 80]])
    assert ctl.choose_pwm(60, 20, 20)[:4] == (180, 180, 0, 'CPU')

    # Новый контроллер: предыдущие latch-значения не должны влиять на следующий независимый сценарий.
    ctl = project.controller()
    ctl.cfg['control'].update(mode='auto',
                              min_running_pwm=0,
                              emergency_cpu_c=120,
                              emergency_nvme_c=120,
                              cpu_levels=[[20, 0], [60, 80]],
                              nvme_levels=[[20, 0], [60, 180]])
    assert ctl.choose_pwm(20, 60, 60)[:4] == (180, 0, 180, 'NVME')


def test_02_hysteresis_three_degrees_and_dwell(project, monkeypatch):
    """
    Гистерезис и выдержка из текущего config.json.
    Проверяется логика кода, а не реальная температура CPU или NVMe.

    Проверки:
    1) Исходная температура управления 46°C.
    2) Повышение меньше заданного гистерезиса не принимается.
    3) Повышение на величину гистерезиса принимается.
    4) Снижение блокируется до завершения minimum_dwell_s.
    5) После завершения выдержки снижение разрешается.

    Ожидаемый результат:
    - Мелкие температурные скачки не меняют PWM.
    - Повышение на заданную величину гистерезиса принимается.
    - Снижение разрешено после выдержки.
    """
    ctl = project.controller()
    hys = float(ctl.cfg['control']['hysteresis_c'])
    dwell = float(ctl.cfg['control']['minimum_dwell_s'])
    base = 46.0
    ctl.last_policy_change_time = 100
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 100.5)

    if hys > 0:
        assert ctl.hysteresis_temp(base + hys / 2, base) == (base, False)

    assert ctl.hysteresis_temp(base + hys, base) == (base + hys, False)

    ctl.last_policy_change_time = 100
    if dwell > 0:
        monkeypatch.setattr(daemon.time,'monotonic',lambda: 100 + dwell / 2)
        assert ctl.hysteresis_temp(base - hys - 1, base) == (base, False)

    monkeypatch.setattr(daemon.time,'monotonic',lambda: 101 + dwell)
    assert ctl.hysteresis_temp(base - hys - 1, base) == (base - hys - 1, True)


def test_03_nvme_averaging_and_sensor_loss(project, monkeypatch):
    """
    Усреднение NVMe и потеря датчика.

    Проверки:
    1)  Выполняем N+1 измерений NVMe, где N = nvme_average_samples.
    Значения формируются повторением набора 49, 50, 52, 50, 49°C.
    2) Датчик перестаёт возвращать температуру.

    Ожидаемый результат:
    - Средняя температура соответствует среднему последних N измерений.
    - История содержит N выборок.
    - При потере датчика история очищается.
    - Устаревшее среднее не используется как текущее.
    """
    ctl = project.controller()
    n = int(ctl.cfg['control']['nvme_average_samples'])
    values = [[49, 50, 52, 50, 49][i % 5] for i in range(n + 1)]
    samples = iter(values)
    monkeypatch.setattr(daemon, 'cpu_temp_c', lambda: 50)
    monkeypatch.setattr(daemon, 'nvme_current_temp_c', lambda: (next(samples), [50]))
    expected_average = sum(values[-n:]) / n

    for _ in values:
        result = ctl.read_temperatures()
    assert result[2] == pytest.approx(expected_average)
    assert len(ctl.nvme_history) == n

    monkeypatch.setattr(daemon, 'nvme_current_temp_c', lambda: (None, []))
    assert ctl.read_temperatures()[2] is None
    assert len(ctl.nvme_history) == 0


def test_04_emergency_manual_and_missing_sensors(project):
    """
    MANUAL, аварийные температуры и отсутствие датчиков.

    Проверки:
    1. Включаем MANUAL с PWM=0.
    2. Повышаем CPU до emergency_cpu_c из конфигурации.
    3. Повышаем NVMe до emergency_nvme_c из конфигурации.
    4. Моделируем отсутствие обоих датчиков.
    5. Проверяем минимальный ненулевой PWM.

    Ожидаемый результат:
    - MANUAL PWM=0 принимается при обычных температурах.
    - Аварийный перегрев имеет приоритет над MANUAL.
    - При потере датчиков применяется sensor_failure_pwm из конфигурации.
    - Ненулевой PWM ниже min_running_pwm повышается до заданного минимума.
    """
    ctl = project.controller()
    control = ctl.cfg['control']
    control.update(mode='manual', manual_pwm=0)
    below_emergency = -1.0

    assert ctl.choose_pwm(below_emergency, below_emergency, below_emergency)[0] == 0
    assert ctl.choose_pwm(control['emergency_cpu_c'], below_emergency, below_emergency)[0] == 255
    assert ctl.choose_pwm(below_emergency, control['emergency_nvme_c'], below_emergency)[0] == 255
    assert ctl.choose_pwm(None, None, None)[0] == ctl.cfg['failsafe']['sensor_failure_pwm']

    control['manual_pwm'] = 1
    assert ctl.choose_pwm(below_emergency, below_emergency, below_emergency)[0] == max(1, control['min_running_pwm'])


def test_05_rpm_formula_and_tach_failsafe(project, monkeypatch):
    """
    Формула RPM и реакция на отсутствие TACH.

    Проверки:
    1) За одну секунду получено 187 импульсов.
    2) Число импульсов на оборот берётся из tach_pulses_per_rev.
    3) Затем моделируется длительное отсутствие RPM.

    Формула:
    RPM = pulses * 60 / (time_seconds * pulses_per_rev)

    Ожидаемый результат:
    - Расчёт RPM соответствует формуле с текущим pulses_per_rev.
    - При моделируемом длительном отсутствии TACH срабатывает fail-safe.
    - В тестовом сценарии target PWM повышается с 77 до 153.
    - Возвращается предупреждение TACH.
    """
    ctl = project.controller()
    ctl.last_rpm_time = 10
    ctl.pulses = 187
    ctl.applied_pwm = 153
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 11)
    ctl.update_rpm()

    # За 1 секунду: RPM = 187 * 60 / pulses_per_rev.
    ppr = ctl.cfg['gpio']['tach_pulses_per_rev']

    assert ctl.rpm_ema == pytest.approx(187 * 60 / ppr)

    # Гарантированно проверяем включённую TACH fail-safe.
    ctl.cfg['failsafe'].update(tach_fail_safe_enabled=True,
                               tach_zero_min_pwm=77,
                               tach_zero_seconds=5,
                               tach_failure_pwm=153)

    ctl.zero_since = 100
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 110)
    target, alarm = ctl.apply_tach_failsafe(77)
    assert target == 153
    assert 'TACH' in alarm


def test_06_start_boost_only_from_stopped_state(project, monkeypatch):
    """
    Start-boost только при запуске с нуля.

    Проверки:
    1. Вентилятор находится в состоянии PWM=0.
    2. Запрашиваем небольшой PWM=51.
    3. Проверяем кратковременное повышение до 255.
    4. Повышаем PWM с 51 до 77.

    Ожидаемый результат:
    - Переход 0→51 сначала устанавливает PWM=255.
    - Продолжительность boost соответствует kickstart_seconds из config.json.
    - Затем устанавливается PWM=51.
    - При переходе 51→77 boost не повторяется.
    """
    ctl = project.controller()
    ctl.applied_pwm = 0
    delays = []
    monkeypatch.setattr(daemon.time, 'sleep', delays.append)
    ctl.set_pwm(51)
    assert ctl.pwm.values[-2:] == [1.0, 51 / 255]
    assert delays == [ctl.cfg['control']['kickstart_seconds']]

    ctl.set_pwm(77)
    assert ctl.pwm.value == pytest.approx(77 / 255)
    assert delays == [ctl.cfg['control']['kickstart_seconds']]


def test_07_rpm_baseline_warning_requires_30_seconds(project, monkeypatch):
    """
    Предупреждение о снижении RPM относительно baseline.

    Проверки:
    1) Первоначально baseline отсутствует.
    2) Для PWM=115 задаём baseline=2000 RPM.
    3) Фактические RPM моделируем на уровне 1000.
    4) Проверяем состояние сразу и через 31 секунду.

    Ожидаемый результат:
    - Без baseline возвращается NO_BASELINE.
    - Сначала возвращается OBSERVE.
    - После выдержки возвращается CHECK_FAN.
    - Отношение RPM/baseline равно 0.5.

    Предупреждение означает необходимость проверки охлаждения.
    """
    ctl = project.controller()
    ctl.cfg['health'].update(enabled=True,
                             min_pwm_for_check=77,
                             min_rpm_ratio=0.75,
                             warn_after_seconds=30,
                             baseline_rpm={})

    assert ctl.health()[0] == 'NO_BASELINE'
    ctl.applied_pwm = 115
    ctl.rpm_ema = 1000
    ctl.cfg['health']['baseline_rpm'] = {'115': 2000}
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 100)
    assert ctl.health()[0] == 'OBSERVE'
    monkeypatch.setattr(daemon.time, 'monotonic', lambda: 131)
    result = ctl.health()
    assert result[0] == 'CHECK_FAN'
    assert result[1] == 2000
    assert result[2] == pytest.approx(0.5)
