import os
import errno
import time
import logging

from subprocess import Popen, PIPE

from calise import camera
from calise import screenBrightness
from calise.infos import __LowerName__


logger = logging.getLogger(".".join([__LowerName__, 'capture']))


def processList(valList):
    ''' Complete standard deviation list check

    Process a list until:
    - only 2 elements remain... (really bad measure, should be reported)
    - list returned after sdevListProcessor is the same as input one

    '''
    cv = ponderatedProcessList(valList)
    if len(cv) == 0:
        cv = valList
    while len(cv) > 2:
        nv = sdevListProcessor(cv)
        if len(cv) == len(nv):
            break
        else:
            cv = nv
    return cv


def sdevListProcessor(lista):
    ''' Standard deviation list check

    If some values are too different from the others, they get removed from
    output list

    NOTE: Best use of this function is executed from processList, since *every*
          discordant value gets removed in the end.

          eg: in a list X, Y, K, J are discordant from other values but X and Y
              are way more discordant than the other two.
              After a run of this function only X and Y are removed and only
              re-computing standard devaition leads to the removal of K and J.
    '''
    avg = sum(lista) / float(len(lista))
    dev = sDev(lista, avg)
    minimum = avg - dev
    maximum = avg + dev
    retList = []
    for idx in range(len(lista)):
        if not dev > 3 or not (lista[idx] > maximum or lista[idx] < minimum):
            retList.append(lista[idx])
    return retList


def ponderatedProcessList(lista):
    ''' remove white balance

    when disabling/re-enabling white balance, frames progrssively change from
    low brightness (balanced) to high and vice-versa.
    This function keeps only constant (sdev < 2) values at the end of capture
    list. Otherwise returns empty list.

    '''
    cont = []
    for i in range(len(lista)):
        if i != 0:
            ddev = abs(sDev(lista[-(i + 1):]) - sDev(lista[-(i):]))
        if i == 0:
            cont.append(lista[-(i+1)])
        elif ddev < 2:
            cont.append(lista[-(i+1)])
        elif ddev >= 2:
            if not len(cont) > 3:
                cont = []
            break
    return cont


# siple standard deviation function (used by sdevListProcessor)
def sDev(lista, average=None):
    if not average:
        average = sum(lista) / float(len(lista))
    dev = (sum([(x - average) ** 2 for x in lista]) / float(len(lista))) ** .5
    return dev


# default (and pretty simple) Error for camera class
class CameraError(Exception):

    def __init__(self, code, value):
        self.errno = code
        self.value = value

    def __str__(self):
        fmtstr = "[Errno %d] %s" % (self.errno, self.value)
        return fmtstr


class imaging():
    ''' Image/Brightness capture class

    Manages image captures and 0 < 255 brightness value obtaining for both
    camera and screen.

    Capture from camera: imaging.getFrameBri() > imaging.amb
    Capture from screen: imaging.getScreenBri() > imaging.scr

    NOTE: Although screen needn't, camera needs to be initialized before
          capturing.
    '''

    def __init__(self):
        self.cameraObj = None   # v4l2 camera object
        self.camPaths = None    # available cameras
        self.camPath = None     # camera path (eg /dev/video)
        self.amb = None         # ambient brightness 0 < 255
        self.scr = None         # screen brightness 0 < 255
        self.ctrls = {}         # controls (all queried) dictionary
        self.stop = None        # readFrame loop control flag
        self.logger = logging.getLogger(".".join([__LowerName__, 'capture']))
        self.deviceStatus = None
        self.authorizer = None

    # defines the camera to be used, path has to be a valid device path like
    # '/dev/video', if no path is given, first cam of camera.camPaths is taken
    def initializeCamera(self, path=None):
        camPaths = camera.listDevices()
        if not camPaths:
            raise CameraError(2, "No available cameras found.")
        if camPaths.count(str(path)) > 0:
            camPath = path
        else:
            logger.warning(
                "given camera ('%s') not among valid v4l2 cameras, using "
                "first available camera ('%s') instead" % camPaths[0])
            camPath = camPaths[0]
        self.camPaths = camPaths
        self.camPath = camPath
        self.cameraObj = camera.Device()
        self.cameraObj.setName(self.camPath)

    def startCapture(self):
        if self.deviceStatus is True:
            return
        self.cameraObj.openPath()
        self.adjustCtrls()
        self.cameraObj.initialize()
        self.cameraObj.startCapture()
        self.deviceStatus = True

    def stopCapture(self):
        if self.deviceStatus is False:
            return
        self.cameraObj.stopCapture()
        self.cameraObj.uninitialize()
        self.restoreCtrls()
        self.cameraObj.closePath()
        self.deviceStatus = False

    def freeCameraObj(self):
        ''' Frees cameraObj (to re-inizialize or on TERMINATE)

        NOTE: This has to be executed after a capture session has been stopped
              or even before starting a capture. Launching with the camera
              active will make the program not able to catch the camera back.
        '''
        del self.cameraObj
        self.cameraObj = None

    def getFrameBriSimple(self):
        expiryTimer = time.time()
        val = None
        while val is None:
            try:
                val = self.cameraObj.readFrame()
            except camera.Error as err:
                if time.time() - expiryTimer > 30:
                    self.stopCapture()
                    self.freeCameraObj()
                    logger.error(
                        "Unable to get a frame from the camera: "
                        "device is continuously returning "
                        "V4L2.EAGAIN (Try Again)")
                    logger.critical(
                        "30 seconds anti-lock"
                        "timer expired, closing.")
                    os.kill(os.getpid(), 9)  # TODO: Fix that issue
                elif errno.EAGAIN == err[0]:
                    time.sleep(1.0 / 30.0)  # 1/30 is arbitrary
                else:
                    raise
        self.amb = val
        return val

    def getFrameBri(self, interval=None, captures=1, loop=False, keep=True):
        ''' Get brightness from a camera frame

        Inside the C module camera takes a 160x120 picture and computes its
        brightness. If camera is not ready yet, a CameraError.EAGAIN is
        raised.

        NOTE: C module has 1 frame buffered (should change this behavior) so
              it's needed to have 2 captures to get *real* frame.

        NOTE: self.stop is a parameter that will stop the function and:

               - return all value obtained from start as list if started with
               integer (or no 'captures' parameter)
               - simply stop execution if started with 'None' as capture
               parameter

               (The second case will need either to be threaded or to be
               controlled by a different thread.)
        '''
        if loop is True:
            self.stop = False
        if keep is True:
            retList = []
            addList = []
        else:
            retList = None
            addList = None
        x = 0
        while x < captures + 1:
            startTime = time.time()
            val = self.getFrameBriSimple()
            # if not first *discarded* frame, set values
            if x != 0:
                if retList is not None:
                    if len(retList) < captures:
                        retList.append(int(val))
                    elif len(retList) == captures:
                        addList.append(int(val))
                # if not last step in schedule sleep
                if x < captures:
                    sleeptime = interval - time.time() + startTime
                    if sleeptime > 0:
                        time.sleep(sleeptime)
                # last step, after last capture in schedule
                if x == captures and retList is not None:
                    if len(addList) == 0:
                        logger.debug(
                            "Raw values: %s" %
                            (', '.join(["%d" % k for k in retList])))
                    # if capture precision is too low, keep capturing
                    if len(processList(retList + addList)) < 3:
                        # log that the program it's going to do additional
                        # captures only one time
                        if len(addList) == 0:
                            logger.info(
                                "Capture precision is too low, requesting "
                                "additional captures")
                        sleeptime = interval - time.time() + startTime
                        if sleeptime > 0:
                            time.sleep(sleeptime)
                        x -= 1
            # after having *discarded* first frame, set 'x' according to
            # 'self.stop' value
            if self.stop is False:
                x = -1
            elif self.stop is None:
                x += 1
            # if 'self.stop' cought, break loop cycle (even if not started with
            # 'loop' parameter)
            if self.stop is True:
                self.stop = None
                break
        if addList is not None and len(addList) > 0:
            logger.debug(
                "Additional values: %s" %
                (', '.join(["%d" % k for k in addList])))
            retList += addList
        return retList


    def adjustCtrls(self):
        ''' Disable controls that modify image brightness during execution:

        (12) V4L2_CID_AUTO_WHITE_BALANCE
        (18) V4L2_CID_AUTOGAIN
        (28) V4L2_CID_BACKLIGHT_COMPENSATION

        A dictionary containing all controls above data is created so that
        then it's possible to restore values to old ones.

        TODO: Remember controls setting from calibration (through profile) and
              set them to now to calibration ones
        '''
        for x in (12, 18, 28):
            idx = str(x)
            try:
                tmp = self.cameraObj.queryCtrl(x)
                self.ctrls[idx] = {
                    'id': tmp[0],
                    'name': tmp[1],
                    'min': tmp[2],
                    'max': tmp[3],
                    'step': tmp[4],
                    'default': tmp[5],
                    'old': tmp[6],
                    'new': None,
                }
                # for controls 12, 18 and 28 *min* means disable control
                if x in (12, 18, 28):
                    cw = self.ctrls[idx]['min']
                    if self.ctrls[idx]['old'] != cw:
                        self.cameraObj.setCtrl(x, cw)
                        logger.debug(
                            "\'v4l2-%s\' set from %s to %s" % (
                                self.ctrls[idx]['name'],
                                self.ctrls[idx]['old'], cw))
                    self.ctrls[idx]['new'] = cw
            except camera.Error as err:
                # errno 22 means control is not available
                if err[0] != 22:
                    raise

    # Restore previously modified controls to original values
    def restoreCtrls(self):
        for x in [int(k) for k in self.ctrls.keys()]:
            idx = str(x)
            # raise if somehow 'new' has not been initialized
            if self.ctrls[idx]['new'] is None:
                raise CameraError(
                    5,
                    "Control not initialized for \'%s\' (%d)"
                    % (self.ctrls[idx]['name'], x))
            if self.ctrls[idx]['new'] != self.ctrls[idx]['old']:
                self.cameraObj.setCtrl(x, self.ctrls[idx]['old'])
                logger.debug(
                    "\'v4l2-%s\' restored to %s from %s" % (
                        self.ctrls[idx]['name'],
                        self.ctrls[idx]['old'],
                        self.ctrls[idx]['new']))

    # obtains %scr (screen brightness in /255)
    def getScreenBri(self):
        self.scr = 0
        display = os.getenv('DISPLAY')
        if not display and os.getuid() == 0:
            if self.authorizer is None:
                self.authorizer = secessionist()
                self.authorizer.getActiveSeat()
            if self.authorizer.seat:
                self.authorizer.getActiveSession()
            if self.authorizer.session:
                self.authorizer.getActiveDisplay()
            if self.authorizer.display:
                activeUser = self.authorizer.getActiveUser()
                xauthority = getXauthority(activeUser)
                if xauthority != os.getenv('XAUTHORITY'):
                    os.environ['XAUTHORITY'] = xauthority
                    logger.debug("X11 authority set to %s" % xauthority)
                    display = self.authorizer.display
        if display:
            scr = screenBrightness.getDisplayBrightness(display)
            if scr:
                self.scr = scr
        logger.debug("Screen capture returned %s" % self.scr)
        return self.scr


class secessionist():
    ''' ConsoleKit DBus query to get (eventual) Active X11 session

    When the program is executed as root, outside user session, the
    enviroment variable DISPLAY is not set and so, to get it it's needed
    to know: active seat, active user and (of course) if user has a X11
    display open and active.

    NOTE: Right now, the only way I found to know that is to query DBUS
          interface "org.freedesktop.ConsoleKit" *but* doing that natively
          failed due to a dbus-module SEGFAULT, as workaround I used
          subprocessed dbus-send calls.
    '''

    def __init__(self):
        self.bus = None
        self.busObject = 'org.freedesktop.ConsoleKit'
        self.busPath = '/org/freedesktop/ConsoleKit'
        self.seat = None
        self.session = None
        self.display = None
        self.xauthority = None

    def getActiveSeat(self):
        self.seat = None
        cliArg = [
            'dbus-send', '--system', '--print-reply', '--type=method_call',
            '--dest=%s' % self.busObject, "%s/Manager" % self.busPath,
            '%s.Manager.GetSeats' % self.busObject,]
        p = Popen(cliArg, stdout=PIPE, stderr=PIPE)
        r = p.communicate()
        for line in r[0].splitlines():
            if line.count(self.busPath):
                self.seat = line.split('\"')[1]
        return self.seat

    def getActiveSession(self):
        self.session = None
        cliArg = [
            'dbus-send', '--system', '--print-reply', '--type=method_call',
            '--dest=%s' % self.busObject, self.seat,
            '%s.Seat.GetActiveSession' % self.busObject,]
        p = Popen(cliArg, stdout=PIPE, stderr=PIPE)
        r = p.communicate()
        for line in r[0].splitlines():
            if line.count(self.busPath):
                self.session = line.split('\"')[1]
        return self.session

    def getActiveDisplay(self):
        self.display = None
        cliArg = [
            'dbus-send', '--system', '--print-reply', '--type=method_call',
            '--dest=%s' % self.busObject, self.session,
            '%s.Session.GetX11Display' % self.busObject,]
        p = Popen(cliArg, stdout=PIPE, stderr=PIPE)
        r = p.communicate()
        for line in r[0].splitlines():
            if line.count('string'):
                self.display = line.split('\"')[1]
        return self.display

    def getActiveUser(self):
        self.display = None
        cliArg = [
            'dbus-send', '--system', '--print-reply', '--type=method_call',
            '--dest=%s' % self.busObject, self.session,
            '%s.Session.GetUnixUser' % self.busObject,]
        p = Popen(cliArg, stdout=PIPE, stderr=PIPE)
        r = p.communicate()
        for line in r[0].splitlines():
            if line.count('uint32'):
                self.display = line.split()[1]
        return self.display


def getUsernameFromUid(uid):
    retVal = None
    with open(os.path.join('/etc', 'passwd'), 'r') as fp:
        passwd = fp.readlines()
    for line in passwd:
        rec = line.split(':')
        if rec[2] == str(uid):
            retVal = rec[0]
            break
    return retVal


def getXauthority(usr):
    retVal = None
    username = getUsernameFromUid(usr)
    xauthPath = os.path.join('/home', username, '.Xauthority')
    if os.path.isfile(xauthPath):
        retVal = xauthPath
    return retVal
