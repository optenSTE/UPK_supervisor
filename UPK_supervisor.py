"""
UPK_supervisor
Скрипт проверяет появление данных в указанной папке
Если скорость поступления данных ниже порога, то выполняется перезапуск службы
Также есть второй триггер, который перезапускает службу (без перезагрузки ИТО) с заданным интевалом
ini-файл
;*********************************
[main]
;*********************************
; instrument description file, default instrument_description.json
; json-file with field 'IP_address' - contains ITO ip address, used to reboot it
; should be in %data_dir_path% folder
instrument_description_filename = instrument_description.json
; time period needs for ITO rebooting, default 40
ITO_rebooting_duration_sec = 40
; pause after stopping the service, default 10
win_service_restart_pause = 10
;*********************************
[trigger1]
;*********************************
; service name, default OAISKGN_UPK
service_name = OAISKGN_UPK
; data folder path, default
data_dir_path = C:\OAISKGN_UPK\data
; data files template, default *.txt
files_template = *.txt
; minimal data folder size speed when service work normal, default 8
; less speed means some problems with the service - it will be restarted
dir_size_speed_threshold_mb_per_h = 8
; how often data folder should be checked, default 60
dir_check_interval_sec = 60
; how many low speed triggers released before service restarts, default 5
num_of_triggers_before_action = 5
; how often ITO should be rebooted (0 - never, 1 - every service restart, 2 - every second service restart and so on), default 2
num_of_service_restarts_before_ito_reboot = 0
;*********************************
[trigger2]
;*********************************
; how often service restarts without any other conditions, default 3600
; 0 means never
win_service_restart_interval_sec = 3600
"""

import datetime
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

program_version = '14012021'

# Глобальные переменные
files_template = '*.txt'  # шаблон имени файла для подсчета размера папки
instrument_description_filename = 'instrument_description.json'  # имя файла с описанием оборудования
ITO_rebooting_duration_sec = 40  # время перезагрузки прибора
win_service_restart_pause = 10  # пауза при перезапуске службы


def get_dir_size_bytes(template):
    total_size = 0
    for file_name in glob.glob(template):
        file_size = os.path.getsize(file_name)
        total_size += file_size
    return total_size


def getFileProperties(fname):
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
            ## print str_info
            strInfo[propName] = win32api.GetFileVersionInfo(fname, strInfoPath)

        props['StringFileInfo'] = strInfo
    except:
        pass

    return props


def action_when_trigger_released(ITO_reboot=False):

    try:
        logging.info(f'Stopping service {service_name}...')
        # stop the service
        args = ['sc', 'stop', service_name]
        result1 = subprocess.run(args)
        logging.info(f'Stop service {service_name} return code {result1.returncode}')

        logging.info(f"Pause for {win_service_restart_pause}sec")
        time.sleep(win_service_restart_pause)
    except Exception as e:
        logging.error(f'An exception happened: {e.__doc__}')

    # проверка готовности прибора (должен отвечать порт, по которому идут команды)
    with socket.socket() as s:
        s.settimeout(1)
        instrument_address = (instrument_ip, hyperion.COMMAND_PORT)
        try:
            s.connect(instrument_address)  # подключаемся к порту команд
        except socket.error:
            logging.error(f'command port is not active {instrument_ip}:{hyperion.COMMAND_PORT}')
        else:
            logging.info(f"Hyperion command port test passed {instrument_ip}:{hyperion.COMMAND_PORT}")

            if ITO_reboot:

                try:
                    h1 = hyperion.Hyperion(instrument_ip)
                except Exception as e:
                    logging.debug(f'Some error during ITO init - exception: {e.__doc__}')
                else:

                    try:
                        logging.info(f'Current ITO time {h1.instrument_utc_date_time.strftime("%d.%m.%Y %H:%M:%S")}')

                        utcnow = datetime.datetime.utcnow()
                        logging.info(f'Setting ITO time to UPK-UTC {utcnow.strftime("%d.%m.%Y %H:%M:%S")}')
                        h1.instrument_utc_date_time = utcnow

                        logging.info(f'Current ITO time {h1.instrument_utc_date_time.strftime("%d.%m.%Y %H:%M:%S")}')

                        logging.info(f"Pause for {win_service_restart_pause}sec")
                        time.sleep(win_service_restart_pause)

                    except Exception as e:
                        logging.debug(f'Some error during h1.instrument_utc_date_time - exception: {e.__doc__}')

                    try:
                        logging.info(f'Rebooting ITO...')
                        h1.reboot()

                        logging.info(f"Pause for {ITO_rebooting_duration_sec}sec")
                        time.sleep(ITO_rebooting_duration_sec)
                        logging.info(f"Instrument {h1.instrument_name}, ip {instrument_ip} rebooted")

                    except Exception as e:
                        logging.error(f'An exception happened: {e.__doc__}')

    try:
        # start the service
        logging.info(f'Starting service {service_name}...')
        args = ['sc', 'start', service_name]
        result2 = subprocess.run(args)
        logging.info(f'Start service {service_name} return code {result2.returncode}')

    except Exception as e:
        logging.error(f'An exception happened: {e.__doc__}')


if __name__ == "__main__":
    # default ini-values
    data_dir_path = r'C:\OAISKGN_UPK\data'
    dir_size_speed_threshold_mb_per_h = 8  # минимальная скорость прироста размера папки, при которой не будет перезапускаться служба
    service_name = "OAISKGN_UPK"  # имя службы для перезапуска
    dir_check_interval_sec = 60  # интервал проверки
    num_of_triggers_before_action = 5  # количество срабатываний триггера до перезапуска службы
    win_service_restart_interval_sec = 3600  # интервал безусловной перезагрузки службы
    num_of_service_restarts_before_ito_reboot = 0  # количество перезапусков службы до перезагрузки прибора

    try:
        filename, file_extension = os.path.splitext(sys.argv[0])
        ini_file_name = f"{filename}.ini"
        if not Path(ini_file_name).is_file():
            raise FileExistsError(f'no file {ini_file_name}')

        config = configparser.ConfigParser()

        config.read(ini_file_name)

        instrument_description_filename = config['main']['instrument_description_filename']
        ITO_rebooting_duration_sec = float(config['main']['ITO_rebooting_duration_sec'])
        win_service_restart_pause = float(config['main']['win_service_restart_pause'])

        data_dir_path = config['trigger1']['data_dir_path']
        instrument_description_filename = data_dir_path + '\\' + instrument_description_filename
        files_template = config['trigger1']['files_template']
        dir_size_speed_threshold_mb_per_h = float(config['trigger1']['dir_size_speed_threshold_mb_per_h'])
        service_name = config['trigger1']['service_name']
        dir_check_interval_sec = float(config['trigger1']['dir_check_interval_sec'])
        num_of_triggers_before_action = int(config['trigger1']['num_of_triggers_before_action'])
        num_of_service_restarts_before_ito_reboot = int(config['trigger1']['num_of_service_restarts_before_ito_reboot'])

        win_service_restart_interval_sec = float(config['trigger2']['win_service_restart_interval_sec'])

    except Exception as e:
        print(f'Error during ini-file reading: {str(e)}')
        sys.exit(0)

    # check if folder exist, create if not
    try:
        os.makedirs(data_dir_path)
    except FileExistsError:
        # already exist
        pass
    except Exception as e:
        print(f"Can't create folder {data_dir_path}: {str(e)}")
        sys.exit(0)

    log_file_name = datetime.datetime.now().strftime(f'{data_dir_path}\\UPK_supervisor_%Y%m%d%H%M%S.log')
    logging.basicConfig(format=u'%(filename)s[LINE:%(lineno)d]# %(levelname)-8s [%(asctime)s]  %(message)s',
                        level=logging.DEBUG, filename=log_file_name)

    logging.info(u'Program starts v.' + program_version)
    logging.info(f'EXE-file {sys.argv[0]}')
    logging.info(getFileProperties(sys.argv[0]))

    # сохранить ini в логе
    logging.info(f'INI-file {ini_file_name}')
    with open(ini_file_name, 'r') as f:
        for line in f.readlines():
            if len(line) > 1:
                logging.info('\t' + line.strip())

    last_dir_check_time = datetime.datetime.now().timestamp()
    last_unconditional_reboot_time = datetime.datetime.now().timestamp()
    cur_time = 0
    cur_dir_size = 0
    last_dir_size = 0
    cur_num_of_triggers = 0

    num_of_service_restarts = 0

    logging.info(f'Looking for instrument description file {instrument_description_filename}...')
    instrument_ip = None
    while not instrument_ip:
        # если есть задание на диске, то загрузим его и начнем работать до получения нового задания
        if Path(instrument_description_filename).is_file():
            try:
                with open(instrument_description_filename, 'r') as f:
                    instrument_description = json.load(f)
            except Exception as e:
                logging.debug(f'Some error during instrument description file reading {instrument_description_filename}; exception: {e.__doc__}')
            else:
                logging.info('Loaded instrument description ' + json.dumps(instrument_description))

            instrument_ip = instrument_description['IP_address']
        else:
            logging.info(f'No file {instrument_description_filename}, pause for {dir_check_interval_sec} sec..')
            time.sleep(dir_check_interval_sec)

    # print("cur_time\tlast_check_time\ttime_diff_sec\tdir_size_diff_byte\tcur_speed_mb_per_hour")
    while True:

        # безуслованя перезагрузка по таймеру
        if win_service_restart_interval_sec > 0:
            cur_time = datetime.datetime.now().timestamp()
            if (cur_time - last_unconditional_reboot_time) >= win_service_restart_interval_sec > 0:

                last_unconditional_reboot_time = cur_time

                logging.info('Trigger2 released')
                action_when_trigger_released()

                cur_num_of_triggers = 0
                num_of_service_restarts = 0

        # перезагрузка по скорости наполнения папки с данными
        try:
            cur_time = datetime.datetime.now().timestamp()
            cur_dir_size = get_dir_size_bytes(data_dir_path + '\\' + files_template)
            if (cur_time - last_dir_check_time) >= dir_check_interval_sec:

                time_diff_sec = cur_time - last_dir_check_time
                dir_size_diff_byte = cur_dir_size - last_dir_size

                if time_diff_sec <= 0:
                    raise NameError('time_diff_sec should be more than zero')

                cur_speed_mb_per_h = 3600 / (1024 * 1024) * dir_size_diff_byte / time_diff_sec

                # print(cur_time, last_dir_check_time, time_diff_sec, dir_size_diff_byte, cur_speed_mb_per_h, sep='\t')

                logging.info('Speed, [Mb/h]\t%.3f' % cur_speed_mb_per_h)
                if 0.0 <= cur_speed_mb_per_h < dir_size_speed_threshold_mb_per_h:
                    # print('Speed %.1fMb/h is too slow' % cur_speed_mb_per_h)

                    cur_num_of_triggers += 1
                    if cur_num_of_triggers >= num_of_triggers_before_action:
                        cur_num_of_triggers = 0

                        ITO_reboot_now = False
                        if num_of_service_restarts >= num_of_service_restarts_before_ito_reboot > 0:
                            num_of_service_restarts = 0
                            ITO_reboot_now = True

                        logging.info('Trigger1 released')
                        action_when_trigger_released(ITO_reboot_now)
                        num_of_service_restarts += 1

            sleeping_time = dir_check_interval_sec - (cur_time - last_dir_check_time)
            if sleeping_time < 0 or sleeping_time > dir_check_interval_sec:
                sleeping_time = dir_check_interval_sec
            time.sleep(sleeping_time)

        except Exception as e:
            logging.error(f'An exception happened: {e.__doc__}')

        finally:
            last_dir_check_time = cur_time
            last_dir_size = cur_dir_size
