import datetime
import os
import time
import json
from influxdb_client import InfluxDBClient

import logging
from logging.handlers import RotatingFileHandler
import requests
import json
import base64

HOMEDIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = '/home/richard/influx.secret'
BA_FILE = os.path.join(HOMEDIR, 'ba.secret')

# The BlueAir API uses a fixed API key.
API_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJncmFudGVlIjoiYmx1ZWFpciIsImlhdCI6MTQ1MzEyNTYzMiwidmFsaWRpdHkiOi0xLCJqdGkiOiJkNmY3OGE0Yi1iMWNkLTRkZDgtOTA2Yi1kN2JkNzM0MTQ2NzQiLCJwZXJtaXNzaW9ucyI6WyJhbGwiXSwicXVvdGEiOi0xLCJyYXRlTGltaXQiOi0xfQ.CJsfWVzFKKDDA6rWdh-hjVVVE9S3d6Hu9BzXG9htWFw"  # noqa: E501

logfile = os.path.join(HOMEDIR, 'log.log')
logger = logging.getLogger('blueAir')
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [pid %(process)d] %(levelname)s %(message)s')
fh = RotatingFileHandler(logfile, maxBytes=1024*1024*5, delay=0, mode='a')
fh.setLevel(logging.INFO)
fh.setFormatter(formatter)
logger.addHandler(fh)

ba_info = json.load(open(BA_FILE, 'r'))
ba_user = ba_info['user']
ba_pw = ba_info['pw']
DEVICE_ID = ba_info['deviceID']

host = requests.get("https://api.blueair.io/v2/user/{username}/homehost/".format(username=ba_user), headers={"X-API-KEY-TOKEN": API_KEY}).text.replace('"', "")

auth_token = requests.get(
    "https://{host}/v2/user/{username}/login/".format(host=host, username=ba_user),
    headers={
        "X-API-KEY-TOKEN": API_KEY,
        "Authorization": "Basic " + base64.b64encode((ba_user + ":" + ba_pw).encode()).decode(),
    }).headers['X-AUTH-TOKEN']


headers = {"X-API-KEY-TOKEN": API_KEY, "X-AUTH-TOKEN": auth_token}

def main():
    logger.info('Starting BlueAir monitor')

    token = open(TOKEN_FILE,'r').read().strip()

    def openInflux():
        client = InfluxDBClient(url='https://127.0.0.1:8086', verify_ssl=False, org='orgname', token=token)
        query = client.query_api()
        return client, query

    retry_count = 0

    r = requests.get('https://{host}/v2/device/{deviceID}/attributes/'.format(host=host, deviceID=DEVICE_ID), headers=headers)
    if r.status_code != 200:
        logger.error('Got bad status code for initial speed')
        logger.error(r.status_code)
        logger.error(r.content)

    curr_speed = [item['currentValue'] for item in r.json() if item['name'] == 'fan_speed'][0]

    influx_client, influx = openInflux()

    def get_last_aqs():
        query_result = influx.query(org='orgname', query='''
    from(bucket: "aqm")
  |> range(start: -1m)
  |> filter(fn: (r) => r["_measurement"] == "PM25")
  |> filter(fn: (r) => r["_field"] == "air_quality_score")
  |> aggregateWindow(every: 1s, fn: last, createEmpty: false)
  |> yield(name: "last")
  |> limit(n:1, offset: 0)
  ''')
        last_aqs = query_result[0].records[-1].get_value()
        return last_aqs
    last_aqs = get_last_aqs()
    logger.info("Initial speed: %s, Initial AQS: %s", curr_speed, last_aqs)

    try:
        while True:
            if datetime.time(1, 0, 0) < datetime.datetime.now().time() < datetime.time(6, 0, 0):
                # It's between 1AM and 6 AM, I'm in bed and don't care what the air quality is like
                #   in the living room. 
                if curr_speed != 0:
                    curr_speed = 0
                    last_aqs = 49
                    setSpeed(0)
                time.sleep(60*30)

            try:
                res = get_last_aqs()
                retry_count = 0
            except Exception:
                logger.exception("Exception when reading from influxdb")
                if retry_count > 5:
                    logger.critical("Got 5 influx errors in a row!")
                    influx_client.close()
                    return
                retry_count += 1
                influx_client.close()
                influx_client, influx = openInflux()
                continue
            speed = calcNewSpeed(curr_speed, res, last_aqs)
            if speed != curr_speed:
                last_aqs = res
                curr_speed = speed
                setSpeed(speed)
            time.sleep(60)
    except Exception:
        logger.exception('Encountered exception')
        raise
    finally:
        influx_client.close()

def setSpeed(speed):
    url = 'https://{host}/v2/device/{deviceID}/attribute/fanspeed/'.format(host=host, deviceID=DEVICE_ID)
    logger.info('Setting speed to %s', speed)
    rb = {
        'scope':'device',
        'name': 'fan_speed',
        'uuid':DEVICE_ID,
        'currentValue': speed,
        'defaultValue': speed,
        }
    r = requests.post(url, headers=headers, json=rb)
    if r.status_code != 200:
        logger.error(r.text)



def calcNewSpeed(curr_speed, curr_aqs, last_aqs):
    aqs_speed_mapping = [50, 100, 150, 210] # index is speed
    for speed, aqs in enumerate(aqs_speed_mapping):
        if curr_aqs > aqs:
            continue
        break

    speed = curr_speed if abs(curr_aqs - last_aqs) < 30 else speed
    logger.info("Curr speed: %s  New Speed: %s  Curr AQS: %d  Last AQS: %d", curr_speed, speed, curr_aqs, last_aqs)
    return speed

if __name__ == '__main__':
    main()

