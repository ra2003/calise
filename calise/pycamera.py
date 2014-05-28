#    Copyright (C)   2011-2013   Nicolo' Barbon
#
#    This file is part of Calise.
#
#    Calise is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published 
#    by the Free Software Foundation, either version 3 of the License,
#    or any later version.
#
#    Calise is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Calise.  If not, see <http://www.gnu.org/licenses/>.


import time
import errno
import logging

from calise import camera
from calise.infos import __LowerName__


logger = logging.getLogger(".".join([__LowerName__, 'camera']))


# default (and pretty simple) Error for camera class
class CameraError(Exception):

    def __init__(self, code, value):
        self.errno = code
        self.value = value

    def __str__(self):
        fmtstr = "[Errno %d] %s" % (self.errno, self.value)
        return fmtstr


class CameraProcess():
    """ Camera functions:
    
    initializeCamera -> camera initialization
    start_capture -> capture session initialization
    stop_capture -> capture session finalization
    freemem -> memory cleaner
    set_ctrls -> camera options switcher
    reset_ctrls -> camera options restorer
    get_frame_brightness -> get /255 brightness from camera
    
    NOTE: after the camera is initialized and a capture session is
          started, the function "get_frame_brightness" can be executed
          as many consecutive times as needed (one at time). When you
          are done with "get_frame_brightness" executions, finalize the
          capture session and clear the memory.
    """

    def __init__(self):
        self.device = None           # v4l2 camera object
        self.devpaths = None         # available v4l2 cameras
        self.devpath = None          # camera path (eg /dev/video)
        self.ctrls = {}              # camera controls (all queried) dictionary
        self.device_status = None    # camera power on/off flag

    def dev_init(self, path=None):
        """ Camera device inizialization
        
        Define the camera to be used for this session's CameraObj. Path
        has to be a valid device path like '/dev/video', if no path is
        given, first device from the list generated by camera.devpaths
        is chosen.
        
        """
        devpaths = camera.listDevices()
        if not devpaths:
            raise CameraError(2, "No available cameras found.")
        if devpaths.count(str(path)) > 0:
            devpath = path
        else:
            logger.warning(
                "given camera ('%s') not among valid v4l2 cameras, using "
                "first available camera ('%s') instead" % devpaths[0])
            devpath = devpaths[0]
        self.devpaths = devpaths
        self.devpath = devpath
        self.device = camera.Device()
        self.device.set_name(self.devpath)

    def start_capture(self):
        """ Start a capture session
        
        Activate CameraObj (from initialization) camera allowing it to
        actually return frames.
        
        """
        if self.device_status is True:
            return
        self.device.open_path()
        self.set_ctrls()
        try:
            self.device.initialize()
        except camera.Error as err:
            if err[0] == errno.EBUSY:
                logger.error(err[1].rstrip('\n'))
                self.reset_ctrls()
                self.device.close_path()
                #raise KeyboardInterrupt --COMMENTED OUT--
                raise err
        self.device.start_capture()
        self.device_status = True

    def stop_capture(self):
        """ Stop a capture session
        
        Deactivate CameraObj camera. To capture more frames after this
        point a new capture session has to be launched.
        
        """
        if self.device_status is False:
            return
        self.device.stop_capture()
        self.device.uninitialize()
        self.reset_ctrls()
        self.device.close_path()
        self.device_status = False

    def freemem(self):
        """ Free device object (to re-inizialize or on TERMINATE)

        NOTE: This has to be executed after a capture session has been
              stopped or even before starting a capture. Launching with
              the camera active will make the program not able to catch
              the camera back.
        """
        del self.device
        self.device = None
    
    def set_ctrls(self):
        """ Disable controls that modify image brightness:

        (12) V4L2_CID_AUTO_WHITE_BALANCE
        (18) V4L2_CID_AUTOGAIN
        (28) V4L2_CID_BACKLIGHT_COMPENSATION

        A dictionary containing all controls above data is created so
        that then it's possible to restore values to old ones.
        
        """
        for x in (12, 18, 28):
            idx = str(x)
            try:
                tmp = self.device.query_ctrl(x)
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
                        self.device.set_ctrl(x, cw)
                        logger.debug(
                            "\'v4l2-%s\' set from %s to %s" %
                            (self.ctrls[idx]['name'],
                             self.ctrls[idx]['old'],
                             cw
                            ))
                    self.ctrls[idx]['new'] = cw
            except camera.Error as err:
                # EINVAL means control is not available (errorcode 22)
                if err[0] != errno.EINVAL:
                    raise

    def reset_ctrls(self):
        """ Restore any modified camera control
        
        NOTE: Does't reset to "factory default" but to the values found
              by 'set_ctrls' function before they were changed, this
              means that this function HAS to be executed only after
              'set_ctrls'.
        """
        for x in [int(k) for k in self.ctrls.keys()]:
            idx = str(x)
            # raise if somehow 'new' has not been initialized
            if self.ctrls[idx]['new'] is None:
                raise CameraError(
                    5, "Control not initialized for \'%s\' (%d)"
                    % (self.ctrls[idx]['name'], x))
            if self.ctrls[idx]['new'] != self.ctrls[idx]['old']:
                self.device.set_ctrl(x, self.ctrls[idx]['old'])
                logger.debug(
                    "\'v4l2-%s\' restored to %s from %s" %
                    (self.ctrls[idx]['name'],
                     self.ctrls[idx]['old'],
                     self.ctrls[idx]['new']
                    ))

    def get_frame_brightness(self):
        """ Get brightness from a camera frame

        Inside the C module camera takes a 160x120 picture and computes
        its brightness. If camera is not ready yet, a CameraError.EAGAIN
        is raised.

        NOTE: Since camera capture (in camera C-module) has been set
              with flag "O_NONBLOCK", until at least 1 buffer is free
              on the camera, asking for a capture will raise V4L2.EAGAIN
              error.
              A loop cycle is set to ask until valid value is returned
              but there's also an error exception to avoid buffer
              lock-ups (default timeout 5 seconds).

        NOTE: (for higher level apps) C-module has 1 frame buffered so
              it's needed to have 2 captures to get the *real* frame.
        """
        timeout = time.time()
        frame_brightness = None
        while frame_brightness is None:
            try:
                frame_brightness = self.device.read_frame()
            except camera.Error as err:
                if time.time() - timeout > 5:
                    if errno.EAGAIN == err[0]:
                        self.stop_capture()
                        logger.error(
                            "Unable to get a frame from the camera: "
                            "device is continuously returning "
                            "V4L2.EAGAIN (Try Again). 5 seconds anti-lock "
                            "timer expired, discarding capture session.")
                    raise err
                elif errno.EAGAIN == err[0]:
                    time.sleep(1.0 / 30.0)  # 1/30 sec is arbitrary
                else:
                    raise err
        return frame_brightness
