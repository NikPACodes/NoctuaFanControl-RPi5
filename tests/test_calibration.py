"""
Тесты калибровки baseline RPM вентилятора Noctua.

Проверяем:
- Запрет калибровки без работающего daemon.
- Соответствие запрошенного и применённого PWM.
- Обработку отсутствующих показаний RPM.
- Сохранность предыдущего baseline при ошибках.
- Корректную запись новой калибровки.
- Восстановление предыдущего режима AUTO/MANUAL.

В тестах используются смоделированные значения RPM.
Физический вентилятор не запускается.
"""
import os
import time
import itertools

import pytest
from conftest import calibration

pytestmark = pytest.mark.calibration


def calibration_pwms(project):
    """
    PWM-точки для теста, только во временной конфигурации.
    """
    cfg = project.get()
    minimum = int(cfg['health']['min_pwm_for_check'])

    if minimum == 0:
        cfg['health']['min_pwm_for_check'] = minimum = 1
        project.put(cfg)

    pwms = sorted({int(pwm) for key in ('cpu_levels', 'nvme_levels')
                            for _, pwm in cfg['control'][key]
                            if minimum <= int(pwm) < 255})

    if not pwms:
        cfg['health']['min_pwm_for_check'] = 77
        cfg['control']['cpu_levels'] = [[30, 0], [40, 100]]
        project.put(cfg)
        pwms = [100]

    return pwms


def test_01_calibration_no_running_daemon_does_not_change_config(project):
    """
    Калибровка запрещена без работающего daemon.

    Проверка:
    Запускаем calibrate(), когда актуальный статус управляющего процесса отсутствует.

    Ожидаемый результат:
    - Калибровка завершается RuntimeError.
    - config.json не изменяется.
    - Исходное содержимое config.json, включая baseline_rpm, не изменяется.
    """
    calibration_pwms(project)
    previous = project.config_path.read_bytes()
    with pytest.raises(RuntimeError, match='Daemon не отвечает'):
        calibration.calibrate()
    assert project.config_path.read_bytes() == previous


def test_02_calibration_rejects_mismatched_pwm(project, monkeypatch):
    """
    Запрет калибровки при неправильном PWM.
    Защищает от некорректной калибровки при аварийном переопределении PWM.

    Проверки:
    - В конфигурации уже сохранён baseline.
    - Калибровка запрашивает очередной PWM.
    - Daemon сообщает фактически применённый PWM=255.

    Ожидаемый результат:
    - Несовпадение PWM обнаруживается.
    - Новые значения RPM не записываются в baseline.
    - Предыдущий baseline сохраняется.
    - Восстанавливается исходный режим AUTO или MANUAL.
    """
    calibration_pwms(project)
    cfg = project.get()
    cfg['health']['baseline_rpm'] = {'77': 1500}
    project.put(cfg)
    monkeypatch.setattr(calibration, 'current_state', lambda: {'timestamp': time.time(),
                                                                      'daemon_pid': os.getpid(),
                                                                      'rpm': 1600,
                                                                      'mode': 'manual',
                                                                      'applied_pwm': 255,})
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    monkeypatch.setattr(calibration.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='другое значение'):
        calibration.calibrate()
    assert project.get()['health']['baseline_rpm'] == {'77': 1500}
    assert project.get()['control']['mode'] == cfg['control']['mode']


def test_03_calibration_preserves_baseline_when_rpm_missing(project, monkeypatch):
    """
    Отсутствие RPM не уничтожает baseline.

    Проверки:
    1) Имеется ранее сохранённая калибровка.
    2) Daemon сообщает корректный применённый PWM.
    3) Но измеренные RPM постоянно равны нулю.

    Ожидаемый результат:
    - Калибровка обнаруживает недостаток валидных проб.
    - Новый baseline не сохраняется.
    - Предыдущие значения RPM остаются прежними.
    - Восстанавливается исходный режим AUTO или MANUAL.
    """
    calibration_pwms(project)
    cfg = project.get()
    cfg['health']['baseline_rpm'] = {'77': 1500}
    project.put(cfg)
    count = itertools.count(1)

    def no_rpm():
        c = project.get()
        return {'timestamp': next(count), 'daemon_pid': os.getpid(),
                'mode': c['control']['mode'],
                'applied_pwm': c['control']['manual_pwm'], 'rpm': 0}

    monkeypatch.setattr(calibration, 'current_state', no_rpm)
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    monkeypatch.setattr(calibration.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='четырёх'):
        calibration.calibrate()
    assert project.get()['health']['baseline_rpm'] == {'77': 1500}
    assert project.get()['control']['mode'] == cfg['control']['mode']


def test_04_calibration_success_saves_and_restores_original_mode(project, monkeypatch):
    """
    Успешная калибровка и восстановление исходного режима.
    Проверка алгоритма калибровки, а не достоверности реальной зависимости PWM/RPM.

    Проверка:
    Для каждого PWM создаём стабильное показание RPM: RPM = PWM * 20

    Калибровка последовательно обрабатывает уровни из температурных кривых и записывает baseline.

    Ожидаемый результат:
    - Все измеренные уровни сохранены.
    - PWM=255 исключён из длительной калибровки.
    - Восстановлен первоначальный режим AUTO или MANUAL.
    - Предыдущее значение manual_pwm не потеряно.
    """
    pwms = calibration_pwms(project)
    original = project.get()
    counter = itertools.count(1)

    def simulated_rpm():
        c = project.get()
        pwm = c['control']['manual_pwm']
        return {'timestamp': next(counter), 'daemon_pid': os.getpid(),
                'mode': c['control']['mode'],
                'applied_pwm': pwm, 'rpm': pwm * 20}

    monkeypatch.setattr(calibration, 'current_state', simulated_rpm)
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    monkeypatch.setattr(calibration.time, 'sleep', lambda _: None)
    calibration.calibrate()
    result = project.get()
    assert result['health']['baseline_rpm'] == { str(pwm): pwm * 20 for pwm in pwms }
    assert '255' not in result['health']['baseline_rpm']
    assert result['control']['mode'] == original['control']['mode']
    assert result['control']['manual_pwm'] == original['control']['manual_pwm']
