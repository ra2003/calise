#    Copyright (C)   2011-2012   Nicolo' Barbon
#
#    This file is part of Calise.
#
#    Calise is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    any later version.
#
#    Calise is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Calise.  If not, see <http://www.gnu.org/licenses/>.

import time
import datetime
import logging

from calise.system import computation
from calise.capture import imaging, processList
from calise.sun import getSun, get_daytime_mul, get_geo
from calise.infos import __LowerName__


caliseCompute = computation()


def forceTerm():
    import sys
    sys.exit(1)


class objects():

    def __init__(self, settings):
        self.logger = logging.getLogger(".".join([__LowerName__, 'objects']))
        self.arguments = settings
        self.oldies = []
        self.resetComers()
        self.wts = None  # weather timestamp
        self.gts = None  # geoip timestamp
        self.capture = imaging()
        self.capture.initializeCamera(self.arguments['cam'])
        self.stop = False

    def dumpValues(self, allv=False):
        if allv:
            return self.oldies
        else:
            return self.oldies[-1]

    def resetComers(self):
        self.newcomers = {
            "amb": None,  # ambient brightness
            "scr": None,  # screen brightness
            "pct": None,  # (corrected) brightness percentage
            "cbs": None,  # current backlight step
            "sbs": None,  # suggested backlight step
            "cts": None,  # capture timestamp (epoch)
            "css": None,  # sun state: either dawn, day, sunset or night
            "nss": None,  # seconds before next sun state
            "slp": None,  # thread sleeptime
        }

    # obtain timestamp
    def getCts(self):
        self.newcomers['cts'] = time.time()
        return self.newcomers['cts']

    def getAmb(self):
        ''' simple function to obtain ambient brightness

        NOTE: Capture module *can* raise "KeyboardInterrupt" if camera module
              returns EAGAIN for more than 15 seconds, in that case requests
              will continue until *success* or TERM flag is set (through
              self.stop).
        '''
        if not self.newcomers['cts']:
            self.getCts()
        while True:
            # camera initialization
            ci = 1
            while ci != 0:
                try:
                    self.capture.startCapture()
                    ci = 0
                except KeyboardInterrupt:
                    if ci == 1:
                        ci = -1
                        self.logger.error(
                            "Camera object is busy. Waiting until it's "
                            "available again (check every 10 seconds).")
                    # check if term flag (self.stop) is set, once per second for
                    # 10 seconds
                    for x in range(10):
                        if self.stop is True:
                            forceTerm()
                            break
                        elif self.stop is False:
                            time.sleep(1)
            # camera capture
            try:
                camValues = self.capture.getFrameBri(
                    self.arguments['capint'], self.arguments['capnum'])
            except KeyboardInterrupt:
                if self.stop is True:
                    forceTerm()
                    break
                else:
                    continue
            # camera uninitialization
            self.capture.stopCapture()
            camValues = processList(camValues)
            self.logger.debug(
                "Processed values: %s"
                % ', '.join(["%d" % x for x in camValues]))
            self.newcomers['amb'] = sum(camValues) / float(len(camValues))
            break
        return self.newcomers['amb']

    # simple function to obtain screen brightness (new or existing value)
    def getScr(self):
        if not self.newcomers['cts']:
            self.getCts()
        if self.arguments['screen'] is True:
            if self.arguments['scrmul'] is None:
                self.getScrMul()
            self.capture.getScreenBri()
            self.newcomers['scr'] = self.capture.scr
        else:
            self.newcomers['scr'] = 0
        return self.newcomers['scr']
    
    def getScrMul(self):
        self.arguments['scrmul'] = self.capture.getScreenMul()
        #print "newmul: %f" % self.arguments['scrmul']

    # obtains brightness percentage value, corrected if needed (by the amount
    # of brightness coming from the screen)
    def getPct(self):
        self.getAmb()
        self.getScr()
        self.getCbs()
        caliseCompute.percentage(
            self.newcomers['amb'],
            self.arguments['offset'], self.arguments['delta'],
            self.newcomers['scr'],
            self.arguments['scrmul'],
            self.adjustScale(self.newcomers['cbs']))
        self.logger.debug(
            "Correction amount (in /255): %4.1f" % caliseCompute.cor)
        self.newcomers['pct'] = caliseCompute.pct
        return self.newcomers['pct']

    # simple function to obtain current backlight step (new or existing value)
    def getCbs(self):
        caliseCompute.get_values('step', self.arguments['path'])
        self.newcomers['cbs'] = caliseCompute.bkstp
        self.arguments['bfile'] = caliseCompute.bfile
        return self.newcomers['cbs']

    # obtain suggested backlight step. This function need every value of
    # %newcomers% dictionary
    def getSbs(self):
        steps = self.arguments['steps']
        bkofs = self.arguments['bkofs']
        self.getPct()
        stp = int(self.newcomers['pct'] / (100.0 / steps) - .5 + bkofs)
        if self.arguments['invert']:
            stp = steps - 1 + bkofs - stp + bkofs
        # out-of-bounds control...
        if stp > steps - 1 + bkofs:
            stp = steps - 1 + bkofs
        elif stp < bkofs:
            stp = bkofs
        self.newcomers['sbs'] = stp
        return self.newcomers['sbs']

    # complementary to getPct function
    def adjustScale(self, cur):
        steps = self.arguments['steps']
        bkofs = self.arguments['bkofs']
        return (cur - bkofs + 1) * (1.00 / steps)

    def writeStep(self, increasing=None, standalone=False):
        ''' Backlight-step change writer

        Checks for read permission and if so writes current backlight step on
        sys brightness file (the one selected throug computation)

        '''
        if standalone:
            self.getCts()
        self.getSbs()
        bfile = self.arguments['bfile']
        if self.arguments['invert']:
            increasing = not increasing
        refer = int(self.newcomers['sbs']) - int(self.newcomers['cbs'])
        if abs(refer) > 0 and increasing is None:
            try:
                fp = open(bfile, 'w')
            except IOError as err:
                import errno
                if err.errno == errno.EACCES:
                    self.logger.error(
                        "IOError: [Errno %d] Permission denied: \'%s\'\n"
                        "Please set write permission for current user\n"
                        % (err.errno, bfile))
                    return 2
            else:
                with fp:
                    fp.write(str(self.newcomers['sbs']) + "\n")
                    self.newcomers['cbs'] = self.newcomers['sbs']
                    return 0
        else:
            return 1

    # get "weather" multiplier, updates only once per hour
    def getWtr(self, cur=None):
        if cur is None:
            cur = time.time()
        if self.wts is None or cur - self.wts > 3600:
            self.wts = time.time()
            mul = get_daytime_mul(
                self.arguments['latitude'], self.arguments['longitude'])
            self.daytime_mul = mul
            return 0
        return 1

    # get geoip informations, updates only once every 30 minutes
    def getGeo(self, cur=None):
        if cur is None:
            cur = time.time()
        if self.gts is None or cur - self.gts > 1800:
            self.gts = time.time()
            geo = get_geo()
            if geo:
                self.arguments['latitude'] = geo['lat']
                self.arguments['longitude'] = geo['lon']
                return 0
            else:
                return 2
        return 1

    def autoWrite(self):
        # assign increasing values (refer to writeStep for further info)
        if self.newcomers['css'] == "dawn":
            inc = True
        elif self.newcomers['css'] == "sunset":
            inc = False
        else:
            inc = None
        # logs writeStep execution
        r = self.writeStep(increasing=None)
        self.logger.debug("Function '%s' returned %d" % ('writeStep', r))
        return 0

    def executer(self, execute=True, ctime=None):
        ''' service "core"

        With the help of the getSun function (which use ephem module) in
        calise.sun module, discovers current time of the day and sets sleep
        time before a new capture and increasing/decreasing writeStep args
        accordingly

        '''
        if not self.newcomers['cts']:
            self.getCts()
        if ctime is None:
            cur_time = time.time()
        else:
            cur_time = ctime
        capture_time = self.arguments['capnum'] * self.arguments['capint']
        if self.arguments['geoip']:
            self.getGeo()

        if (
            not self.arguments.keys().count('latitude') or
            not self.arguments.keys().count('longitude') or
            self.arguments['latitude'] is None or
            self.arguments['longitude'] is None
        ):
            arbSlpVal = 90.0
            self.logger.warning(
                "Not able to geolocate, setting arbitrary sleeptime value: %d"
                % arbSlpVal)
            self.newcomers['css'] = None
            self.newcomers['nss'] = None
            self.newcomers['slp'] = arbSlpVal
            if execute:
                self.autoWrite()
            else:
                self.getSbs()
            return arbSlpVal - capture_time + cur_time

        sun = getSun(
            self.arguments['latitude'], self.arguments['longitude'], cur_time)
        daw = float(sun[0])
        sus = float(sun[1])
        # more or less the seconds that the sun needs to get from min to max
        # backlight step brightness
        daw_tw = int(sun[2])
        sus_tw = int(sun[3])
        # sleeptime between captures (for both dawn and sunset)
        daw_sl = daw_tw / 100.0
        sus_sl = sus_tw / 100.0

        # dawn
        if cur_time > daw and cur_time <= daw + daw_tw:
            self.newcomers['css'] = "dawn"
            self.newcomers['nss'] = daw + daw_tw - cur_time
            sleepTime = daw_sl * self.arguments['dusksm']
        # sunset
        elif cur_time >= sus - sus_tw and cur_time < sus:
            self.newcomers['css'] = "sunset"
            self.newcomers['nss'] = sus - cur_time
            sleepTime = sus_sl * self.arguments['dusksm']
        # night
        elif cur_time > sus or cur_time < daw:
            # if current time is before midnight, ask for next day dawn
            if cur_time > sus:
                tmp = getSun(
                    self.arguments['latitude'], self.arguments['longitude'],
                    cur_time + 86400)
                daw = float(tmp[0])
            if self.arguments['nightst'] == 0.0:
                sleepTime = daw - cur_time
            else:
                sleepTime = self.arguments['nightst']
            self.newcomers['css'] = "night"
            self.newcomers['nss'] = daw - cur_time
        # day
        else:
            self.newcomers['css'] = "day"
            self.newcomers['nss'] = sus - sus_tw - cur_time
            if self.arguments['weather']:
                self.getWtr(cur_time)
            else:
                self.daytime_mul = 0.6
            sleepTime = self.arguments['dayst'] * self.daytime_mul
            if sleepTime > self.newcomers['nss']:
                sleepTime = self.newcomers['nss']
        # *real* execute
        if execute:
            self.autoWrite()
        else:
            self.getSbs()
        # process output value
        self.newcomers['slp'] = sleepTime
        if sleepTime < capture_time + 1.0:
            return capture_time + 1.0 + cur_time
        else:
            return sleepTime - capture_time + cur_time

    def append_data(self):
        obj = self.newcomers
        self.oldies.append(obj)
