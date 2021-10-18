"""
UPK_supervisor
Скрипт проверяет появление данных в указанной папке
Если скорость поступления данных ниже порога, то выполняется перезапуск службы
Также есть второй триггер, который перезапускает службу (без перезагрузки ИТО) с заданным интевалом

Перезагрузка ИТО
    если прибор не отвечает на тестовые команды или его IP:port недоступен,
    то производится перезагрузка с помощью управляемой розетки NetPing 2/PWR-220 v12/ETH

    Ограничения:
    - осуществляется только при перезапуске службы
    - ограничена частота перезагрузки
    - если перезагрузка не привела к результату, то интервал между попытками увеличивается в два раза
    - успешная перезагрузка возвращает интервал к первоначальному
    - ограничено общее количество неуспешных перезагрузок



ini-файл

;*********************************
[main]
;*********************************

; this file version, should be the same as program version
ini_file_version = 14.10.2021

; instrument description file, recommended instrument_description.json
; json-file with field 'IP_address' - contains ITO ip address, used to reboot it
; should be in %data_dir_path% folder
instrument_description_filename = instrument_description.json

; time period needs for ITO rebooting, recommended 40
ITO_rebooting_duration_sec = 40

; pause after stopping the service, recommended 10
win_service_restart_pause = 10

;*********************************
[netping]
;*********************************

; power control device (Netping) IP-address, if empty device wont be used
netping_relay_address = 10.0.0.56

; socket num (1 or 2) ITO is connected to
netping_relay_ito_socket_num = 2


;*********************************
; Data folder surveillance - release when new data comes slowly, restars service and reboot ITO if nedeeded
[trigger1]
;*********************************

; service name, recommended OAISKGN_UPK
service_name = OAISKGN_UPK

; data folder path
data_dir_path = c:\OAISKGN_UPK\data

; data files template, recommended *.txt
files_template = *.txt

; minimal data folder size speed when service work normal, recommended 8
; less speed means some problems with the service - it will be restarted
dir_size_speed_threshold_mb_per_h = 2

; how often data folder should be checked, recommended 60
dir_check_interval_sec = 3

; how many low speed triggers released before service restarts, recommended 5
num_of_triggers_before_action = 10

; how often ITO should be rebooted (0 - never, 1 - every service restart, 2 - every second service restart and so on), recommended 2
num_of_service_restarts_before_ito_reboot = 3

; how many unsuccessful reboots can be made, recommended 3
max_unsuccessful_reboots = 3



;*********************************
; Time trigger - release periodically, restars service doesn't reboot ITO
[trigger2]
;*********************************

; how often service restarts without any other conditions, recommended 3600
; 0 means never
win_service_restart_interval_sec = 3600

; ITO time correction when trigger2 released, recommended  1
; 0 - no correction
; 1 - UPK (local) UTC-time
; not released: 2 - OSM UTC-time. Based on log-file where is Ping Frame (opcode=9) from OSM.
ito_datetime_source = 1

"""

from datetime import datetime
import glob
import os
import time
import logging
import subprocess
import hyperion
import sys
import json
from pathlib import Path
import socket
import win32api
import configparser
from netpingrelay import NetpingRelay
from struct import pack, unpack

program_version = '14.10.2021'

# Глобальные переменные
cur_unsuccessful_reboots = 0
trigger1_enable = False
trigger2_enable = False

# константы, отвечающие за получение текущего времени ОСМ из лог-файла UPK_server
upk_server_log_files_template = "UPK_server_*.log"
upk_log_valid_age_days = 60  # how many days log file can be used for time sync, older files ignored because they may contain wrong time


def get_dir_size_bytes(template):
    total_size = 0
    for file_name in glob.glob(template):
        file_size = os.path.getsize(file_name)
        total_size += file_size
    return total_size


def get_file_properties(fname):
    """
    Read all properties of the given file return them as a dictionary.
    https://stackoverflow.com/questions/580924/how-to-access-a-files-properties-on-windows
    """
    propNames = ('Comments', 'InternalName', 'ProductName',
        'CompanyName', 'LegalCopyright', 'ProductVersion',
        'FileDescription', 'LegalTrademarks', 'PrivateBuild',
        'FileVersion', 'OriginalFilename', 'SpecialBuild')

    props = {'FixedFileInfo': None, 'StringFileInfo': None, 'FileVersion': None}

    try:
        # backslash as parm returns dictionary of numeric info corresponding to VS_FIXEDFILEINFO struc
        fixedInfo = win32api.GetFileVersionInfo(fname, '\\')
        props['FixedFileInfo'] = fixedInfo
        props['FileVersion'] = "%d.%d.%d.%d" % (fixedInfo['FileVersionMS'] / 65536,
                fixedInfo['FileVersionMS'] % 65536, fixedInfo['FileVersionLS'] / 65536,
                fixedInfo['FileVersionLS'] % 65536)

        # \VarFileInfo\Translation returns list of available (language, codepage)
        # pairs that can be used to retreive string info. We are using only the first pair.
        lang, codepage = win32api.GetFileVersionInfo(fname, '\\VarFileInfo\\Translation')[0]

        # any other must be of the form \StringfileInfo\%04X%04X\parm_name, middle
        # two are language/codepage pair returned from above

        strInfo = {}
        for propName in propNames:
            strInfoPath = u'\\StringFileInfo\\%04X%04X\\%s' % (lang, codepage, propName)
            strInfo[propName] = win32api.GetFileVersionInfo(fname, strInfoPath)

        props['StringFileInfo'] = strInfo
    except:
        pass

    return props


def ito_check_connection():
    """
    Проверка связи с ИТО: пинг порта прибора, затем тестовой командой из API
    :return: True if ITO connected and answer commands
            reason(int), error description(str)
    reasons: 1 - no ping
             2 - no connection by ITO API
             3 - ITO doesn't answer to command #GetChannelDetectionSettingId
    """

    ret = True
    # проверка готовности прибора (должен отвечать порт, по которому идут команды)
    with socket.socket() as s:
        s.settimeout(1)
        try:
            s.connect((ito_ip, hyperion.COMMAND_PORT))  # подключаемся к порту команд
        except socket.error:
            ret = (1, f'command port is not active {ito_ip}:{hyperion.COMMAND_PORT}')
        else:
            # Hyperion command port test passed
            # let's check how it responds to command
            try:
                h1 = hyperion.Hyperion(ito_ip)
            except Exception as e:
                ret = (2, f'Some error during ITO init - exception: {e.__doc__}')
            else:
                # ito connected, sending a command
                try:
                    h1.get_channel_detection_setting(1)
                except Exception as e:
                    ret = (3, f'Exception during h1.get_channel_detection_setting(1): {e.__doc__}')

    return ret


def get_upk_osm_time_from_log(in_str):
    """
    Функция разбирает строчку лога, содержащую сообщение от ОСМ и возвращает показания часов компьютеров - УПК и ОСМ
    :param in_str: строчка из лог-файла
    :return: расхождение часов компьютеров - УПК и ОСМ
    """
    # 1.Распарсить входную строку на две даты/время
    # 2.Перевести время в datetime
    # 3.Вычислить расхождение

    osm_time = upk_time = None

    if 'opcode=9' not in in_str:
        raise ValueError('The string from log should contains "opcode=9"')

    # 1.Распарсить входную строку на две даты/время
    #  u'%(filename)s[LINE:%(lineno)d]# %(levelname)-8s [%(asctime)s]  %(message)s'
    #  protocol.py[LINE:1053]# DEBUG    [2021-08-31 14:51:27,887]  server < Frame(fin=True, opcode=9, data=b'31.08.2021 14:55:54', rsv1=False, rsv2=False, rsv3=False)

    try:
        # в строке лога есть левая часть - от УПК и правая - от ОСМ
        upk_str, osm_str = in_str.split('Frame', 1)

        # анализ части от УПК. интересуют вторые квадратные скобки
        upk_time_str = upk_str.split('[')[-1].split(',')[0]

        # анализ части от ОСМ. там перечислены параметры - вытащим 'data'
        osm_time_str = osm_str.split("data=b'")[-1].split("'")[0]
    except:
        raise ValueError(f'Wrong log string format - expected like protocol.py[LINE:1053]# DEBUG    [2021-08-31 14:51:27,887]  server < Frame(fin=True, opcode=9, data=b\'31.08.2021 14:55:54\', rsv1=False, rsv2=False, rsv3=False)"')

    # 2.Перевести время в datetime
    try:
        osm_time = datetime.strptime(osm_time_str, "%d.%m.%Y %H:%M:%S")
    except:
        raise ValueError(f'Wrong data format for OSM time - expected "%d.%m.%Y %H:%M:%S", got "{osm_time_str}"')

    try:
        upk_time = datetime.strptime(upk_time_str, logging.Formatter.default_time_format)
    except:
        raise ValueError(f'Wrong data format for UPK time - expected "%Y-%m-%d %H:%M:%S", got "{upk_time_str}"')

    return upk_time, osm_time


def action_when_any_trigger_released(ito_reboot=False, reboot_by_netping=True):
    """
    Действия при срабатывании таймера, триггера
    Перезапуск службы, перезапуск ИТО, установка часов ИТО

    :param ito_reboot: boolean, ITO reboot permission
           reboot_by_netping: boolean, True (use Netping relay to reboot ITO reboot) or False(use ITO command #reboot)
    :return:
    """
    global cur_unsuccessful_reboots

    # stop the service
    try:
        logging.info(f'Stopping service {service_name}...')
        # stop the service
        args = ['sc', 'stop', service_name]
        result1 = subprocess.run(args)
        logging.info(f'Stop service {service_name} return code {result1.returncode}')

        logging.info(f"Pause for {win_service_restart_pause}sec")
        time.sleep(win_service_restart_pause)
    except Exception as e:
        logging.error(f'Exception during service stop: {e.__doc__}')

    # ITO check connection, reboot
    if ito_reboot and cur_unsuccessful_reboots < max_unsuccessful_reboots:

        # reboot by Netping
        if reboot_by_netping:
            logging.info('Reboot by Netping...')
            try:
                logging.info('Cheking Netping socket...')
                relay = NetpingRelay(netping_relay_address)

                relay_ok = relay.check_connection()
                if relay_ok == True:
                    logging.info('Netping socket connected.')
                    logging.info(f'Rebooting ITO by Netping socket...')
                    relay.reset_socket(netping_relay_ito_socket_num, 20)
                    relay.socket_on(netping_relay_ito_socket_num)
                    logging.info(f"Pause for {ITO_rebooting_duration_sec}sec")
                else:
                    logging.error(f'Netping socket error: {relay_ok}')
                    reboot_by_netping = False  # далее будет перезагрузка с помощью команды #reboot

            except Exception as e:
                logging.error(f'An exception happened: {e.__doc__}')

        if not reboot_by_netping:
            logging.info('Reboot by #reboot command...')
            logging.info('Check ITO connection...')
            ito_ok = ito_check_connection()
            if ito_ok == True:
                try:
                    logging.info('ITO reboot...')
                    h1 = hyperion.Hyperion(ito_ip)
                    h1.reboot()
                except Exception as e:
                    logging.error(f'An exception happened: {e.__doc__}')
            else:
                logging.info(f'No connection: {ito_ok}')

        # после перезагрузки нужно проверить связь с прибором
        logging.info('Check ITO connection...')
        ito_ok = ito_check_connection()
        if ito_ok:
            logging.info('Connection ok')
            cur_unsuccessful_reboots = 0
        else:
            logging.info(f'No connection: {ito_ok}')
            cur_unsuccessful_reboots += 1
            return False

    # ITO setting date/time, getting spectra
    logging.info('Check ITO connection...')
    ito_ok = ito_check_connection()
    if ito_ok == True:
        logging.info('Connection ok')

        # соединение с ИТО
        try:
            h1 = hyperion.Hyperion(ito_ip)

            # получение спектров
            try:
                logging.info('Getting spectra...')
                spectrum = h1.spectra

                logging.info('Saving spectra...')
                k = zip(spectrum.wavelengths, *spectrum.data)
                # ToDo для именования файла получать время из ИТО 
                spectrum_file_name = datetime.now().strftime(os.path.join(data_dir_path,'%Y%m%d%H%M%S_spectrum.txt'))
                with open(spectrum_file_name, 'w') as spectrum_file:
                    for i in k:
                        print('\t'.join(str(x) for x in i), file=spectrum_file)
            except Exception as e:
                logging.error(f'Some error during getting spectra - exception: {e.__doc__}')

            # установка времени прибора
            try:
                logging.info(f'Current ITO time {h1.instrument_utc_date_time.strftime("%d.%m.%Y %H:%M:%S")}')

                if ito_datetime_source > 0:
                    datetime_to_be_set = datetime.utcnow()

                    # 1 - UPK (local) UTC-time
                    if ito_datetime_source == 1:
                        datetime_to_be_set = datetime.utcnow()
                        logging.info(f'Setting ITO time based on UPK(UTC) {datetime_to_be_set.strftime("%d.%m.%Y %H:%M:%S")}')

                    # 2 - OSM UTC-time. Based on log-file where is Ping Frame (opcode=9) from OSM.
                    if ito_datetime_source == 2:
                        # Метод коррекции времени на основе Ping-сообщений от ОСМ. В строчке существующего
                        # лог-файла UPK_server есть информация о времени УПК и ОСМ на момент получения сообщения
                        # Используя эти два времени, находим сдвиг и применяем его к текущему времени УПК
                        # это будет ожидаемое текущее время ОСМ. Его и нужно установить на ИТО.

                        # Last log file should contain line with Ping Frame (opcode=9) from OSM.
                        #  "protocol.py[LINE:1053]# DEBUG    [2021-08-31 14:51:27,887]  server < Frame(fin=True, opcode=9, data=b'31.08.2021 14:55:54', rsv1=False, rsv2=False, rsv3=False)"
                        #
                        #  1. Get OSM time and UPK time from it to find time delay between those clocks
                        #  2. Apply the delay to current UPK time to get OSM time - this time should be used to ITO
                        logged_first_error = False
                        # iterate on log-files in time creation order (the newest is first)
                        for cur_log_file_name in sorted(glob.glob(os.path.join(data_dir_path, upk_server_log_files_template)),
                                                        key=os.path.getctime, reverse=True):
                            with open(cur_log_file_name) as f:
                                # line with 'opcode=9' sorted backward (from newest)
                                opcode9_lines = (line for line in reversed(list(f.readlines())) if 'opcode=9' in line)
                                for line in opcode9_lines:
                                    try:
                                        # parse UPK and OSM time from log line
                                        upk_time, osm_time = get_upk_osm_time_from_log(line)

                                        # check is time information fresh enough?
                                        if abs((datetime.now() - upk_time).days) < upk_log_valid_age_days:
                                            # OSM (UTC) time prediction
                                            osm_utcnow = datetime.utcnow() - (upk_time - osm_time)
                                            
                                            # this time will apply late
                                            datetime_to_be_set = osm_utcnow
                                            logging.info(f'Setting ITO time based on OSM {datetime_to_be_set.strftime("%d.%m.%Y %H:%M:%S")}')
                                            break

                                        elif not logged_first_error:
                                            logging.info(f"OSM time in log-file is not fresh enough, "
                                                         f"{os.path.basename(cur_log_file_name)}")
                                            logged_first_error = True

                                    except Exception as e:
                                        # log only once
                                        if not logged_first_error:
                                            logging.info(e)
                                            logged_first_error = True

                    h1.instrument_utc_date_time = datetime_to_be_set
                    logging.info(f'Current ITO time {h1.instrument_utc_date_time.strftime("%d.%m.%Y %H:%M:%S")}')
            except Exception as e:
                logging.error(f'Some error during ITO date/time setting - exception: {e.__doc__}')

            # получение температуры прибора
            try:
                # Returns the temperature of the instrument on the PCB, 8 byte (double)
                ito_board_temp = unpack('d', h1._execute_command("#GetBoardTemperature").content)[0]
                logging.info(f'ITO board temperature {ito_board_temp}')

            except Exception as e:
                logging.error(f'Some error during getting temperature - exception: {e.__doc__}')

        except Exception as e:
            logging.error(f'Some error during hyperion.Hyperion({ito_ip}) - exception: {e.__doc__}')
    else:
        logging.info(f'No connection: {ito_ok}')

    # start the service
    try:
        logging.info(f'Starting service {service_name}...')
        args = ['sc', 'start', service_name]
        result2 = subprocess.run(args)
        logging.info(f'Start service {service_name} return code {result2.returncode}')

    except Exception as e:
        logging.error(f'Exception during service start: {e.__doc__}')


if __name__ == "__main__":

    # ini-file reading
    config = None
    ini_file_name = ""
    try:
        filename, file_extension = os.path.splitext(sys.argv[0])
        ini_file_name = f"{filename}.ini"
        if not Path(ini_file_name).is_file():
            raise FileExistsError(f'no file {ini_file_name}')

        config = configparser.ConfigParser()

        config.read(ini_file_name)
    except Exception as e:
        print(f'Fatal error during ini-file reading, cant read file{ini_file_name}\n{str(e)}')
        sys.exit(0)

    # Main settings
    try:

        ini_file_version = config['main']['ini_file_version']
        instrument_description_filename = config['main']['instrument_description_filename']
        ITO_rebooting_duration_sec = float(config['main']['ITO_rebooting_duration_sec'])
        win_service_restart_pause = float(config['main']['win_service_restart_pause'])

    except Exception as e:
        print(f'Fatal error during ini-file reading [main] section: {str(e)}')
        sys.exit(0)

    # NetPing settings
    netping_relay_address = ''
    netping_relay_ito_socket_num = 0
    reboot_by_netping = True
    try:
        netping_relay_address = config['netping']['netping_relay_address']  # '10.0.0.56'  # адрес управляемой розетки
        netping_relay_ito_socket_num = int(config['netping']['netping_relay_ito_socket_num'])  # номер розетки, в которую воткнут ИТО
    except KeyError as e:
        print(f'Error during ini-file reading [netping] section, continue without netping: {str(e)}')
        reboot_by_netping = False

    # Trigger1 settings
    data_dir_path = r'C:\OAISKGN_UPK\data'
    dir_size_speed_threshold_mb_per_h = 8  # минимальная скорость прироста размера папки, при которой не будет перезапускаться служба
    service_name = "OAISKGN_UPK"  # имя службы для перезапуска
    dir_check_interval_sec = 60  # интервал проверки
    num_of_triggers_before_action = 5  # количество срабатываний триггера до перезапуска службы
    num_of_service_restarts_before_ito_reboot = 0  # количество перезапусков службы до перезагрузки прибора
    max_unsuccessful_reboots = 3  # максимальное число перезапусков ИТО (неудачных подряд)
    try:
        data_dir_path = config['trigger1']['data_dir_path']
        instrument_description_filename = data_dir_path + '\\' + instrument_description_filename
        files_template = config['trigger1']['files_template']
        dir_size_speed_threshold_mb_per_h = float(config['trigger1']['dir_size_speed_threshold_mb_per_h'])
        service_name = config['trigger1']['service_name']
        dir_check_interval_sec = float(config['trigger1']['dir_check_interval_sec'])
        num_of_triggers_before_action = int(config['trigger1']['num_of_triggers_before_action'])
        num_of_service_restarts_before_ito_reboot = int(config['trigger1']['num_of_service_restarts_before_ito_reboot'])
        max_unsuccessful_reboots = int(config['trigger1']['max_unsuccessful_reboots'])

    except Exception as e:
        print(f'Fatal error during ini-file reading [trigger1] section: {str(e)}')
        sys.exit(0)
    else:
        trigger1_enable = True

    # Trigger2 settings
    win_service_restart_interval_sec = 0
    try:
        win_service_restart_interval_sec = float(config['trigger2']['win_service_restart_interval_sec'])
        ito_datetime_source = int(config['trigger2']['ito_datetime_source'])
    except Exception as e:
        print(f'Error during ini-file reading [trigger2] section, continue without trigger2: {str(e)}')
        win_service_restart_interval_sec = 0
    else:
        trigger2_enable = True

    # check if data folder exist, create if not - for next log file
    try:
        os.makedirs(data_dir_path)
    except FileExistsError:
        # already exist
        pass
    except Exception as e:
        print(f"Can't create folder {data_dir_path}: {str(e)}")
        sys.exit(0)

    log_file_name = datetime.now().strftime(f'{data_dir_path}\\UPK_supervisor_%Y%m%d%H%M%S.log')
    logging.basicConfig(format=u'%(filename)s[LINE:%(lineno)d]# %(levelname)-8s [%(asctime)s]  %(message)s',
                        level=logging.DEBUG, filename=log_file_name)

    logging.info(u'Program starts v.' + program_version)
    logging.info(f'EXE-file {sys.argv[0]}')
    logging.info(get_file_properties(sys.argv[0]))

    # сохранить ini в логе
    logging.info(f'INI-file {ini_file_name}')
    with open(ini_file_name, 'r') as f:
        for line in f.readlines():
            if len(line) > 1:
                logging.info('\t' + line.strip())

    last_dir_check_time = datetime.now().timestamp()
    last_unconditional_reboot_time = datetime.now().timestamp()
    cur_dir_size = 0
    last_dir_size = 0
    cur_num_of_triggers = 0

    num_of_service_restarts = 0

    logging.info(f'Looking for instrument description file {instrument_description_filename}...')
    ito_ip = None
    while not ito_ip:
        # если есть задание на диске, то загрузим его и начнем работать до получения нового задания
        if Path(instrument_description_filename).is_file():
            try:
                with open(instrument_description_filename, 'r') as f:
                    instrument_description = json.load(f)
            except Exception as e:
                logging.debug(f'Some error during instrument description file reading {instrument_description_filename}; exception: {e.__doc__}')
            else:
                logging.info('Loaded instrument description ' + json.dumps(instrument_description))

            ito_ip = instrument_description['IP_address']
        else:
            logging.info(f'No file {instrument_description_filename}, pause for {dir_check_interval_sec} sec..')
            time.sleep(dir_check_interval_sec)

    while True:
        time.sleep(1)

        # Trigger2 - release periodically
        try:
            if trigger2_enable and (datetime.now().timestamp() - last_unconditional_reboot_time) >= win_service_restart_interval_sec > 0:

                last_unconditional_reboot_time = datetime.now().timestamp()

                logging.info('Trigger2 released')
                action_when_any_trigger_released(False)

                cur_num_of_triggers = 0
                num_of_service_restarts = 0
        except Exception as e:
            logging.error(f'Trigger2 exception: {e.__doc__}')

        # Trigger1 - data folder surveillance
        try:
            # is the moment to check data folder?
            if (datetime.now().timestamp() - last_dir_check_time) >= dir_check_interval_sec:
                cur_dir_size = get_dir_size_bytes(data_dir_path + '\\' + files_template)

                time_diff_sec = datetime.now().timestamp() - last_dir_check_time
                dir_size_diff_byte = cur_dir_size - last_dir_size

                last_dir_check_time = datetime.now().timestamp()
                last_dir_size = cur_dir_size

                if time_diff_sec <= 0:
                    raise NameError('time_diff_sec should be more than zero')

                cur_speed_mb_per_h = 3600 / (1024 * 1024) * dir_size_diff_byte / time_diff_sec
                logging.info('Speed, [Mb/h]\t%.3f' % cur_speed_mb_per_h)

                if 0.0 <= cur_speed_mb_per_h < dir_size_speed_threshold_mb_per_h:
                    if cur_num_of_triggers >= num_of_triggers_before_action:
                        cur_num_of_triggers = 0

                        ITO_reboot_now = False
                        if num_of_service_restarts >= num_of_service_restarts_before_ito_reboot > 0:
                            num_of_service_restarts = 0
                            ITO_reboot_now = True

                        logging.info(f'Trigger1 released, reboot_ITO={ITO_reboot_now}')
                        action_when_any_trigger_released(ito_reboot=ITO_reboot_now, reboot_by_netping=reboot_by_netping)
                        num_of_service_restarts += 1
                    else:
                        cur_num_of_triggers += 1
                else:
                    # при успешном триггере сбрасывем счетчик перезапуска ИТО
                    num_of_service_restarts = 0

        except Exception as e:
            logging.error(f'Trigger1 exception: {e.__doc__}')
