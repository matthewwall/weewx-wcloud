# $Id: wcloud.py 1797 2019-03-07 23:35:27Z tkeffer $
# Copyright 2014 Matthew Wall

"""
This is a weewx extension that uploads data to WeatherCloud.

http://weather.weathercloud.com

Based on weathercloud API documentation v0.5 as of 15oct2014.

The preferred upload frequency (post_interval) is one record every 10 minutes.

These are the possible states for sensor values:
- sensor exists and returns valid value
- sensor exists and returns invalid value that passes StdQC
- sensor exists and returns None (e.g., windDir when windSpeed is zero)
- sensor exists but is not working
- sensor does not exist

Regarding None/NULL values, the folks at weathercloud say the following:

"In order to fix this issue, WeeWX should send our error code (-32768) instead
of omitting that variable in the frame. This way we know that the device is
able to measure the variable but that it's not currently working."

Minimal Configuration:

[StdRESTful]
    [[WeatherCloud]]
        id = WEATHERCLOUD_ID
        key = WEATHERCLOUD_KEY
"""

import re
import sys
import syslog
import time

try:
    # Python 3
    import queue
except ImportError:
    # Python 2
    import Queue as queue

try:
    # Python 3
    from urllib.parse import urlencode
except ImportError:
    # Python 2
    from urllib import urlencode

try:
    # Python 3
    MAXSIZE = sys.maxsize
except AttributeError:
    # Python 2
    MAXSIZE = sys.maxint

import weewx
import weewx.restx
import weewx.units
import weewx.wxformulas
from weeutil.weeutil import to_bool

VERSION = "0.12"

if weewx.__version__ < "3":
    raise weewx.UnsupportedFeature("weewx 3 is required, found %s" %
                                   weewx.__version__)

def logmsg(level, msg):
    syslog.syslog(level, 'restx: WeatherCloud: %s' % msg)

def logdbg(msg):
    logmsg(syslog.LOG_DEBUG, msg)

def loginf(msg):
    logmsg(syslog.LOG_INFO, msg)

def logerr(msg):
    logmsg(syslog.LOG_ERR, msg)

# weewx uses a status of 1 to indicate failure, wcloud uses 0
def _invert(x):
    if x is None:
        return None
    if x == 0:
        return 1
    return 0

# utility to convert to METRICWX windspeed
def _convert_windspeed(v, from_unit_system):
    if from_unit_system is None:
        return None
    if from_unit_system != weewx.METRICWX:
        (from_unit, _) = weewx.units.getStandardUnitType(
            from_unit_system, 'windSpeed')
        from_t = (v, from_unit, 'group_speed')
        v = weewx.units.convert(from_t, 'meter_per_second')[0]
    return v

# FIXME: this formula is suspect
def _calc_thw(heatindex_C, windspeed_mps):
    if heatindex_C is None or windspeed_mps is None:
        return None
    windspeed_mph = 2.25 * windspeed_mps
    heatindex_F = 32 + heatindex_C * 9 / 5
    thw_F = heatindex_F - (1.072 * windspeed_mph)
    thw_C = (thw_F - 32) * 5 / 9
    return thw_C

def _get_windavg(dbm, ts, interval=600):
    sts = ts - interval
    val = dbm.getSql("SELECT AVG(windSpeed) FROM %s "
                     "WHERE dateTime>? AND dateTime<=?" % dbm.table_name,
                     (sts, ts))
    return val[0] if val is not None else None

# weathercloud wants "10-min maximum gust of wind".  some hardware reports
# a wind gust, others do not, so try to deal with both.
def _get_windhi(dbm, ts, interval=600):
    sts = ts - interval
    val = dbm.getSql("""SELECT
 MAX(CASE WHEN windSpeed >= windGust THEN windSpeed ELSE windGust END)
 FROM %s
 WHERE dateTime>? AND dateTime<=?""" % dbm.table_name, (sts, ts))
    return val[0] if val is not None else None

def _get_winddiravg(dbm, ts, interval=600):
    sts = ts - interval
    val = dbm.getSql("SELECT AVG(windDir) FROM %s "
                     "WHERE dateTime>? AND dateTime<=?" %
                     dbm.table_name, (sts, ts))
    return val[0] if val is not None else None

class WeatherCloud(weewx.restx.StdRESTbase):
    def __init__(self, engine, config_dict):
        """This service recognizes standard restful options plus the following:

        id: WeatherCloud identifier

        key: WeatherCloud key
        """
        super(WeatherCloud, self).__init__(engine, config_dict)
        loginf("service version is %s" % VERSION)
        site_dict = weewx.restx.get_site_dict(config_dict, 'WeatherCloud', 'id', 'key')
        if site_dict is None:
            return
        site_dict['manager_dict'] = weewx.manager.get_manager_dict(
            config_dict['DataBindings'], config_dict['Databases'], 'wx_binding')

        self.archive_queue = queue.Queue()
        self.archive_thread = WeatherCloudThread(self.archive_queue, **site_dict)
        self.archive_thread.start()
        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        loginf("Data will be uploaded for id=%s" % site_dict['id'])

    def new_archive_record(self, event):
        self.archive_queue.put(event.record)

class WeatherCloudThread(weewx.restx.RESTThread):

    _SERVER_URL = 'http://api.weathercloud.net/v01/set'

    # this data map supports the default database schema
    # FIXME: design a config option to override this map
    #             wcloud_name   weewx_name      format  multiplier
    _DATA_MAP = {'temp':       ('outTemp',      '%.0f', 10.0), # C * 10
                 'hum':        ('outHumidity',  '%.0f', 1.0),  # percent
                 'wdir':       ('windDir',      '%.0f', 1.0),  # degree
                 'wspd':       ('windSpeed',    '%.0f', 10.0), # m/s * 10
                 'bar':        ('barometer',    '%.0f', 10.0), # hPa * 10
                 'rain':       ('dayRain',      '%.0f', 10.0), # mm * 10
                 'rainrate':   ('rainRate',     '%.0f', 10.0), # mm/hr * 10
                 'tempin':     ('inTemp',       '%.0f', 10.0), # C * 10
                 'humin':      ('inHumidity',   '%.0f', 1.0),  # percent
                 'uvi':        ('UV',           '%.0f', 10.0), # index * 10
                 'solarrad':   ('radiation',    '%.0f', 10.0), # W/m^2 * 10
                 'et':         ('ET',           '%.0f', 10.0), # mm * 10
                 'chill':      ('windchill',    '%.0f', 10.0), # C * 10
                 'heat':       ('heatindex',    '%.0f', 10.0), # C * 10
                 'dew':        ('dewpoint',     '%.0f', 10.0), # C * 10
                 'battery':    ('consBatteryVoltage', '%.0f', 100.0), # V * 100
                 'temp01':     ('extraTemp1',   '%.0f', 10.0), # C * 10
                 'temp02':     ('extraTemp2',   '%.0f', 10.0), # C * 10
                 'temp03':     ('extraTemp3',   '%.0f', 10.0), # C * 10
                 'temp04':     ('leafTemp1',    '%.0f', 10.0), # C * 10
                 'temp05':     ('leafTemp2',    '%.0f', 10.0), # C * 10
                 'temp06':     ('soilTemp1',    '%.0f', 10.0), # C * 10
                 'temp07':     ('soilTemp2',    '%.0f', 10.0), # C * 10
                 'temp08':     ('soilTemp3',    '%.0f', 10.0), # C * 10
                 'temp09':     ('soilTemp4',    '%.0f', 10.0), # C * 10
                 'temp10':     ('heatingTemp4', '%.0f', 10.0), # C * 10
                 'leafwet01':  ('leafWet1',     '%.0f', 1.0),  # [0,15]
                 'leafwet02':  ('leafWet2',     '%.0f', 1.0),  # [0,15]
                 'hum01':      ('extraHumid1',  '%.0f', 1.0),  # percent
                 'hum02':      ('extraHumid2',  '%.0f', 1.0),  # percent
                 'soilmoist01': ('soilMoist1',  '%.0f', 1.0),  # Cb [0,200]
                 'soilmoist02': ('soilMoist2',  '%.0f', 1.0),  # Cb [0,200]
                 'soilmoist03': ('soilMoist3',  '%.0f', 1.0),  # Cb [0,200]
                 'soilmoist04': ('soilMoist4',  '%.0f', 1.0),  # Cb [0,200]

                 # these are calculated by this extension
#                 'thw':        ('thw',          '%.0f', 10.0), # C * 10
                 'wspdhi':     ('windhi',       '%.0f', 10.0), # m/s * 10
                 'wspdavg':    ('windavg',      '%.0f', 10.0), # m/s * 10
                 'wdiravg':    ('winddiravg',   '%.0f', 1.0),  # degree
                 'heatin':     ('inheatindex',  '%.0f', 10.0), # C * 10
                 'dewin':      ('indewpoint',   '%.0f', 10.0), # C * 10
                 'battery01':  ('bat01',        '%.0f', 1.0),  # 0 or 1
                 'battery02':  ('bat02',        '%.0f', 1.0),  # 0 or 1
                 'battery03':  ('bat03',        '%.0f', 1.0),  # 0 or 1
                 'battery04':  ('bat04',        '%.0f', 1.0),  # 0 or 1
                 'battery05':  ('bat05',        '%.0f', 1.0),  # 0 or 1

                 # these are in the wcloud api but are not yet implemented
#                 'tempagroXX':   ('??',       '%.0f', 10.0), # C * 10
#                 'wspdXX':       ('??',       '%.0f', 10.0), # m/s * 10
#                 'wspdavgXX':    ('??',       '%.0f', 10.0), # m/s * 10
#                 'wspdhiXX':     ('??',       '%.0f', 10.0), # m/s * 10
#                 'wdirXX':       ('??',       '%.0f', 1.0), # degree
#                 'wdiravgXX':    ('??',       '%.0f', 1.0), # degree
#                 'bartrend':     ('??',       '%.0f', 1.0), # -60,-20,0,20,60
#                 'forecast':     ('??',       '%.0f', 1.0),
#                 'forecasticon': ('??',       '%.0f', 1.0),
                 }

    def __init__(self, queue, id, key, manager_dict,
                 server_url=_SERVER_URL, skip_upload=False,
                 post_interval=600, max_backlog=MAXSIZE, stale=None,
                 log_success=True, log_failure=True,
                 timeout=60, max_tries=3, retry_wait=5):
        super(WeatherCloudThread, self).__init__(queue,
                                               protocol_name='WeatherCloud',
                                               manager_dict=manager_dict,
                                               post_interval=post_interval,
                                               max_backlog=max_backlog,
                                               stale=stale,
                                               log_success=log_success,
                                               log_failure=log_failure,
                                               max_tries=max_tries,
                                               timeout=timeout,
                                               retry_wait=retry_wait)
        self.id = id
        self.key = key
        self.server_url = server_url
        self.skip_upload = to_bool(skip_upload)

    # calculate derived quantities and other values needed by wcloud
    def get_record(self, record, dbm):
        rec = super(WeatherCloudThread, self).get_record(record, dbm)

        # put everything into units required by weathercloud
        rec = weewx.units.to_METRICWX(rec)

        # calculate additional quantities
        rec['windavg'] = _get_windavg(dbm, record['dateTime'])
        rec['windhi'] = _get_windhi(dbm, record['dateTime'])
        rec['winddiravg'] = _get_winddiravg(dbm, record['dateTime'])

        # ensure wind direction is in [0,359]
        if rec.get('windDir') is not None and rec['windDir'] > 359:
            rec['windDir'] -= 360
        if rec.get('winddiravg') is not None and rec['winddiravg'] > 359:
            rec['winddiravg'] -= 360

        # these observations are non-standard, so do unit conversions directly
        rec['windavg'] = _convert_windspeed(rec.get('windavg'), record['usUnits'])
        rec['windhi'] = _convert_windspeed(rec.get('windhi'), record['usUnits'])

        if 'inTemp' in rec and 'inHumidity' in rec:
            rec['inheatindex'] = weewx.wxformulas.heatindexC(
                rec['inTemp'], rec['inHumidity'])
            rec['indewpoint'] = weewx.wxformulas.dewpointC(
                rec['inTemp'], rec['inHumidity'])
#        if 'heatindex' in rec and 'windSpeed' in rec:
#            rec['thw'] = _calc_thw(rec['heatindex'], rec['windSpeed'])
        if 'txBatteryStatus' in record:
            rec['bat01'] = _invert(record['txBatteryStatus'])
        if 'windBatteryStatus' in record:
            rec['bat02'] = _invert(record['windBatteryStatus'])
        if 'rainBatteryStatus' in record:
            rec['bat03'] = _invert(record['rainBatteryStatus'])
        if 'outTempBatteryStatus' in record:
            rec['bat04'] = _invert(record['outTempBatteryStatus'])
        if 'inTempBatteryStatus' in record:
            rec['bat05'] = _invert(record['inTempBatteryStatus'])
        return rec

    def format_url(self, record):
        # put data into expected structure and format
        time_tt = time.gmtime(record['dateTime'])
        values = {
            'ver': str(weewx.__version__),
            'type': 251,  # identifier assigned to weewx by weathercloud
            'wid': self.id,
            'key': self.key,
            'time': time.strftime("%H%M", time_tt),  # assumes leading zeros
            'date': time.strftime("%Y%m%d", time_tt)
            }
        for key in self._DATA_MAP:
            rkey = self._DATA_MAP[key][0]
            if rkey in record and record[rkey] is not None:
                v = record[rkey] * self._DATA_MAP[key][2]
                values[key] = self._DATA_MAP[key][1] % v
        url = self.server_url + '?' + urlencode(values)
        if weewx.debug >= 2:
            logdbg('url: %s' % re.sub(r"key=[^\&]*", "key=XXX", url))
        return url
