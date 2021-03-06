#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
"""

import os
import urllib2
import subprocess
import time
import itertools
import tempfile

try:
    import jsonrpclib
except ImportError:
    pass

__version__ = "0.1.1"
__author__ = "Xiaocong He"


def param_to_property(**props):
    class Wrapper(object):

        def __init__(self, func):
            self.func = func
            self.kwargs = {}

        def __getattribute__(self, attr):
            try:
                return super(Wrapper, self).__getattribute__(attr)
            except AttributeError:
                for prop_name, prop_values in props.items():
                    if attr in prop_values and prop_name not in self.kwargs:
                        self.kwargs[prop_name] = attr
                        return self
                raise

        def __call__(self, *args, **kwargs):
            kwargs.update(self.kwargs)
            self.kwargs = {}
            return self.func(*args, **kwargs)
    return Wrapper


class _SelectorBuilder(object):

    """The class is to build parameters for UiSelector passed to Android device.
    """
    __fields = {
        "text": (0x01L, None),  # MASK_TEXT,
        "textContains": (0x02L, None),  # MASK_TEXTCONTAINS,
        "textMatches": (0x04L, None),  # MASK_TEXTMATCHES,
        "textStartsWith": (0x08L, None),  # MASK_TEXTSTARTSWITH,
        "className": (0x10L, None),  # MASK_CLASSNAME
        "classNameMatches": (0x20L, None),  # MASK_CLASSNAMEMATCHES
        "description": (0x40L, None),  # MASK_DESCRIPTION
        "descriptionContains": (0x80L, None),  # MASK_DESCRIPTIONCONTAINS
        "descriptionMatches": (0x0100L, None),  # MASK_DESCRIPTIONMATCHES
        "descriptionStartsWith": (0x0200L, None),  # MASK_DESCRIPTIONSTARTSWITH
        "checkable": (0x0400L, False),  # MASK_CHECKABLE
        "checked": (0x0800L, False),  # MASK_CHECKED
        "clickable": (0x1000L, False),  # MASK_CLICKABLE
        "longClickable": (0x2000L, False),  # MASK_LONGCLICKABLE,
        "scrollable": (0x4000L, False),  # MASK_SCROLLABLE,
        "enabled": (0x8000L, False),  # MASK_ENABLED,
        "focusable": (0x010000L, False),  # MASK_FOCUSABLE,
        "focused": (0x020000L, False),  # MASK_FOCUSED,
        "selected": (0x040000L, False),  # MASK_SELECTED,
        "packageName": (0x080000L, None),  # MASK_PACKAGENAME,
        "packageNameMatches": (0x100000L, None),  # MASK_PACKAGENAMEMATCHES,
        "resourceId": (0x200000L, None),  # MASK_RESOURCEID,
        "resourceIdMatches": (0x400000L, None),  # MASK_RESOURCEIDMATCHES,
        "index": (0x800000L, 0),  # MASK_INDEX,
        "instance": (0x01000000L, 0),  # MASK_INSTANCE,
        "fromParent": (0x02000000L, None),  # MASK_FROMPARENT,
        "childSelector": (0x04000000L, None)  # MASK_CHILDSELECTOR
    }
    __mask = "mask"

    def __init__(self, **kwargs):
        self._dict = {k: v[1] for k, v in self.__fields.items()}
        self._dict[self.__mask] = 0

        for k, v in kwargs.items():
            if k in self.__fields:
                self[k] = v

    def __getitem__(self, k):
        return self._dict[k]

    def __setitem__(self, k, v):
        if k in self.__fields:
            self._dict[k] = v  # call the method in superclass
            self._dict[self.__mask] = self[self.__mask] | self.__fields[k][0]
        else:
            raise ReferenceError("%s is not allowed." % k)

    def __delitem__(self, k):
        if k in self.__fields:
            self[k] = self.__fields[k][1]
            self[self.__mask] = self[self.__mask] & ~self.__fields[k][0]

    def build(self):
        d = self._dict.copy()
        for k, v in d.items():
            # if isinstance(v, SelectorBuilder):
            # TODO workaround.
            # something wrong in the module loader, likely SelectorBuilder was
            # loaded as another type...
            if k in ["childSelector", "fromParent"] and v is not None:
                d[k] = v.build()
        return d

    def keys(self):
        return self.__fields.keys()

SelectorBuilder = _SelectorBuilder


def rect(top=0, left=0, bottom=100, right=100):
    return {"top": top, "left": left, "bottom": bottom, "right": right}


def point(x=0, y=0):
    return {"x": x, "y": y}


_adb_cmd = None


def get_adb():
    global _adb_cmd
    if _adb_cmd is None:
        if "ANDROID_HOME" in os.environ:
            _adb_cmd = os.environ["ANDROID_HOME"] + "/platform-tools/adb"
            if not os.path.exists(_adb_cmd):
                raise EnvironmentError(
                    "Adb not found in $ANDROID_HOME path: %s." % os.environ["ANDROID_HOME"])
        else:
            import distutils
            _adb_cmd = distutils.spawn.find_executable("adb")
            if _adb_cmd is not None:
                _adb_cmd = os.path.realpath(cmd)
            else:
                raise EnvironmentError("$ANDROID_HOME environment not set.")
    return _adb_cmd


def adb_cmd(*args):
    return subprocess.Popen(["%s %s" % (get_adb(), " ".join(args))], shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def adb_devices():
    '''check if device is attached.'''
    out = adb_cmd("devices").communicate()[0]
    match = "List of devices attached"
    index = out.find(match)
    if index < 0:
        raise EnvironmentError("adb is not working.")
    return dict([s.split() for s in out[index + len(match):].strip().splitlines()])


def adb_forward(local_port, device_port):
    adb_cmd("forward", "tcp:%d" % local_port, "tcp:%d" % device_port).wait()


class _AutomatorServer(object):

    """start and quit rpc server on device.
    """
    __jar_files = {
        "bundle.jar": 'https://github.com/xiaocong/android-uiautomator-jsonrpcserver/blob/release/dist/bundle.jar?raw=true',
        "uiautomator-stub.jar": "https://github.com/xiaocong/android-uiautomator-jsonrpcserver/blob/release/dist/uiautomator-stub.jar?raw=true"
    }

    def __init__(self):
        self.__automator_process = None
        self.__local_port = 9008
        self.__device_port = 9008

    def __get__(self, instance, owner):
        return self

    def __download_and_push(self):
        lib_path = os.path.join(tempfile.gettempdir(), "libs")
        if not os.path.exists(lib_path):
            os.mkdir(lib_path)
        for jar in self.__jar_files:
            jarfile = os.path.join(lib_path, jar)
            if not os.path.exists(jarfile):  # not exist, then download it
                u = urllib2.urlopen(self.__jar_files[jar])
                with open(jarfile, 'w') as f:
                    f.write(u.read())
            # push to device
            adb_cmd("push", jarfile, "/data/local/tmp/").wait()
        return self.__jar_files.keys()

    def __adb_forward(self, local_port, device_port):
        adb_cmd("forward", "tcp:%d" %
                local_port, "tcp:%d" % device_port).wait()

    @property
    def jsonrpc(self):
        if not self.alive:  # start server if not
            self.start()
        return jsonrpclib.Server(self.rpc_uri)

    def start(self, local_port=9008, device_port=9008): #TODO add customized local remote port.
        self.__local_port = local_port
        self.__device_port = device_port
        devices = adb_devices()
        if len(devices) is 0:
            raise EnvironmentError("Device not attached.")
        elif len(devices) > 1 and "ANDROID_SERIAL" not in os.environ:
            raise EnvironmentError(
                "Multiple devices attaches but $ANDROID_SERIAL environment not set.")

        files = self.__download_and_push()
        cmd = ["shell", "uiautomator", "runtest"] + \
            files + ["-c", "com.github.uiautomatorstub.Stub"]
        self.__automator_process = adb_cmd(*cmd)
        adb_forward(local_port, 9008)  # TODO device_port, currently only 9008
        while not self.__can_ping():
            time.sleep(0.1)

    def __can_ping(self):
        try:
            return jsonrpclib.Server(self.rpc_uri).ping() == "pong" # not use self.jsonrpc here to avoid recursive invoke
        except:
            return False

    @property
    def alive(self):
        '''Check if the rpc server is alive.'''
        return self.__can_ping()

    def stop(self):
        '''Stop the rpc server.'''
        if self.__automator_process is not None and self.__automator_process.poll() is None:
            try:
                urllib2.urlopen(self.stop_uri)
                self.__automator_process.wait()
            except:
                self.__automator_process.kill()
            finally:
                self.__automator_process = None
        out = adb_cmd("shell", "ps", "-C", "uiautomator").communicate()[
            0].strip().splitlines()
        index = out[0].split().index("PID")
        for line in out[1:]:
            adb_cmd("shell", "kill", "-9", line.split()[index]).wait()

    @property
    def stop_uri(self):
        return "http://localhost:%d/stop" % self.__local_port

    @property
    def rpc_uri(self):
        return "http://localhost:%d/jsonrpc/device" % self.__local_port


class _AutomatorDevice(object):

    '''uiautomator wrapper of android device'''
    server = _AutomatorServer()

    _orientation = (  # device orientation
        (0, "natural", "n", 0),
        (1, "left", "l", 90),
        (2, "upsidedown", "u", 180),
        (3, "right", "r", 270)
    )

    def __init__(self):
        pass

    def __call__(self, **kwargs):
        return _AutomatorDeviceObject(self.server.jsonrpc, **kwargs)

    def ping(self):
        '''ping the device, by default it returns "pong".'''
        return self.server.jsonrpc.ping()

    @property
    def info(self):
        '''Get the device info.'''
        return self.server.jsonrpc.deviceInfo()

    def click(self, x, y):
        '''click at arbitrary coordinates.'''
        self.server.jsonrpc.click(x, y)

    def swipe(self, sx, sy, ex, ey, steps=100):
        return self.server.jsonrpc.swipe(sx, sy, ex, ey, steps)

    def drag(self, sx, sy, ex, ey, steps=100):
        '''Swipe from one point to another point.'''
        return self.server.jsonrpc.drag(sx, sy, ex, ey, steps)

    def dump(self, filename):
        '''dump device window and pull to local file.'''
        device_file = self.server.jsonrpc.dumpWindowHierarchy(True, "dump.xml")
        if device_file is None or len(device_file) is 0:
            return None
        p = adb_cmd("pull", device_file, filename)
        p.wait()
        adb_cmd("shell", "rm", device_file)
        return filename if p.returncode is 0 else None

    def screenshot(self, filename, scale=1.0, quality=100):
        '''take screenshot.'''
        device_file = self.server.jsonrpc.takeScreenshot(
            "screenshot.png", scale, quality)
        if device_file is None or len(device_file) is 0:
            return None
        p = adb_cmd("pull", device_file, filename)
        p.wait()
        adb_cmd("shell", "rm", device_file)
        return filename if p.returncode is 0 else None

    def freeze_rotation(self, freeze=True):
        '''freeze or unfreeze the device rotation in current status.'''
        self.server.jsonrpc.freezeRotation(freeze)

    @property
    def orientation(self):
        '''
        orienting the devie to left/right or natural.
        left/l:       rotation=90 , displayRotation=1
        right/r:      rotation=270, displayRotation=3
        natural/n:    rotation=0  , displayRotation=0
        upsidedown/u: rotation=180, displayRotation=2
        '''
        return self._orientation[self.info["displayRotation"]][1]

    @orientation.setter
    def orientation(self, value):
        '''setter of orientation property.'''
        for values in self._orientation:
            if value in values:
                # can not set upside-down until api level 18.
                self.server.jsonrpc.setOrientation(values[1])
                break
        else:
            raise ValueError("Invalid orientation.")

    @property
    def last_traversed_text(self):
        '''get last traversed text. used in webview for highlighted text.'''
        return self.server.jsonrpc.getLastTraversedText()

    def clear_traversed_text(self):
        '''clear the last traversed text.'''
        self.server.jsonrpc.clearLastTraversedText()

    @property
    def open(self):
        '''
        Open notification or quick settings.
        Usage:
        d.open.notification()
        d.open.quick_settings()
        '''
        obj = self

        class Target(object):

            def notification(self):
                return obj.server.jsonrpc.openNotification()

            def quick_settings(self):
                return obj.server.jsonrpc.openQuickSettings()
        return Target()

    def watcher_triggered(self, name):
        '''check if the registered watcher was triggered.'''
        return self.server.jsonrpc.hasWatcherTriggered(name)

    @property
    def press(self):
        '''
        press key via name or key code. Supported key name includes:
        home, back, left, right, up, down, center, menu, search, enter,
        delete(or del), recent(recent apps), voulmn_up, volumn_down,
        volumn_mute, camera, power.
        Usage:
        d.press.back()  # press back key
        d.press.menu()  # press home key
        d.press(89)     # press keycode
        '''
        obj = self
        @param_to_property(key=["home", "back", "left", "right", "up", "down", "center", "menu", "search", "enter", "delete", "del", "recent", "voulmn_up", "volumn_down", "volumn_mute", "camera", "power"])
        def _press(key, meta=None):
            if isinstance(key, int):
                return obj.server.jsonrpc.pressKeyCode(key, meta) if meta else self.server.jsonrpc.pressKeyCode(key)
            else:
                return obj.server.jsonrpc.pressKey(str(key))
        return _press

    def wakeup(self):
        '''turn on screen in case of screen off.'''
        self.server.jsonrpc.wakeUp()

    def sleep(self):
        '''turn off screen in case of screen on.'''
        self.server.jsonrpc.sleep()

    @property
    def screen(self):
        '''
        Turn on/off screen.
        Usage:
        d.screen.on()
        d.screen.off()
        '''
        obj = self

        @param_to_property(action=["on", "off"])
        def _screen(action):
            return obj.wakeup() if action is "on" else obj.sleep()
        return _screen

    @property
    def wait(self):
        '''
        Waits for the current application to idle or window update event occurs.
        Usage:
        d.wait.idle(timeout=1000)
        d.wait.update(timeout=1000, package_name="com.android.settings")
        '''
        obj = self

        @param_to_property(action=["idle", "update"])
        def _wait(action, timeout=1000, package_name=None):
            if action is "idle":
                return obj.server.jsonrpc.waitForIdle(timeout)
            elif action is "update":
                return obj.server.jsonrpc.waitForWindowUpdate(package_name, timeout)
        return _wait


class _AutomatorDeviceObject(object):

    '''Represent a UiObject, on which user can perform actions, such as click, set text
    '''

    __alias = {'description': "contentDescription", "class": "className"}

    def __init__(self, jsonrpc, **kwargs):
        self.jsonrpc = jsonrpc
        self.__selector = SelectorBuilder(**kwargs)
        self.__actions = []

    @property
    def selector(self):
        return self.__selector.build()

    def child_selector(self, **kwargs):
        '''set chileSelector.'''
        self.__selector["childSelector"] = SelectorBuilder(**kwargs)
        return self

    def from_parent(self, **kwargs):
        '''set fromParent selector.'''
        self.__selector["fromParent"] = SelectorBuilder(**kwargs)
        return self

    def exist(self):
        '''check if the object exists in current window.'''
        return self.jsonrpc.exist(self.selector)

    def __getattribute__(self, attr):
        '''alias of fields in info property.'''
        try:
            return super(_AutomatorDeviceObject, self).__getattribute__(attr)
        except AttributeError:
            info = self.info
            if attr in info:
                return info[attr]
            elif attr in self.__alias:
                return info[self.__alias[attr]]
            else:
                raise

    @property
    def info(self):
        '''ui object info.'''
        return self.jsonrpc.objInfo(self.selector)

    def set_text(self, text):
        '''set the text field.'''
        if text in [None, ""]:
            self.jsonrpc.clearTextField(self.selector)  # TODO no return
        else:
            return self.jsonrpc.setText(self.selector, text)

    def clear_text(self):
        '''clear text. alias for set_text(None).'''
        self.set_text(None)

    @property
    def click(self):
        '''
        click on the ui object.
        Usage:
        d(text="Clock").click()  # click on the center of the ui object
        d(text="OK").click.wait(timeout=3000) # click and wait for the new window update
        d(text="John").click.topleft() # click on the topleft of the ui object
        d(text="John").click.bottomright() # click on the bottomright of the ui object
        '''
        obj = self
        @param_to_property(action=["tl", "topleft", "br", "bottomright", "wait"])
        def _click(action=None, timeout=3000):
            if action is None:
                return obj.jsonrpc.click(obj.selector)
            elif action in ["tl", "topleft", "br", "bottomright"]:
                return obj.jsonrpc.click(obj.selector, action)
            else:
                return obj.jsonrpc.clickAndWaitForNewWindow(obj.selector, timeout)
        return _click

    @property
    def long_click(self):
        '''
        Perform a long click action on the object.
        Usage:
        d(text="Image").long_click()  # long click on the center of the ui object
        d(text="Image").long_click.topleft()  # long click on the topleft of the ui object
        d(text="Image").long_click.bottomright()  # long click on the topleft of the ui object
        '''
        obj = self

        @param_to_property(corner=["tl", "topleft", "br", "bottomright"])
        def _long_click(corner=None):
            if corner is None:
                return obj.jsonrpc.longClick(obj.selector)
            else:
                return obj.jsonrpc.longClick(obj.selector, corner)
        return _long_click

    @property
    def drag(self):
        '''
        Drag the ui object to other point or ui object.
        Usage:
        d(text="Clock").drag.to(x=100, y=100)  # drag to point (x,y)
        d(text="Clock").drag.to(text="Remove") # drag to another object
        '''
        obj = self

        class Drag(object):

            def to(self, *args, **kwargs):
                if len(args) >= 2 or "x" in kwargs or "y" in kwargs:
                    drag_to = lambda x, y, steps=100: obj.jsonrpc.dragTo(
                        obj.selector, x, y, steps)
                else:
                    drag_to = lambda steps=100, **kwargs: obj.jsonrpc.dragTo(
                        obj.selector, SelectorBuilder(**kwargs).build(), steps)
                return drag_to(*args, **kwargs)
        return Drag()

    def gesture(self, start1, start2, *args, **kwargs):
        '''
        perform two point gesture.
        Usage:
        d().gesture(startPoint1, startPoint2).to(endPoint1, endPoint2, steps)
        d().gesture(startPoint1, startPoint2, endPoint1, endPoint2, steps)
        '''
        obj = self

        class Gesture(object):

            def to(self, end1, end2, steps=100):
                return obj.jsonrpc.gesture(obj.selector,
                                           start1, start2,
                                           end1, end2, steps)
        if len(args) == 0:
            return Gesture()
        elif 3 >= len(args) >= 2:
            f = lambda end1, end2, steps=100: obj.jsonrpc.gesture(
                obj.selector, start1, start2, end1, end2, steps)
            return f(*args, **kwargs)
        else:
            raise SyntaxError("Invalid parameters.")

    @property
    def pinch(self):
        '''
        Perform two point gesture from edge to center(in) or center to edge(out).
        Usages:
        d().pinch.In(percent=100, steps=10)
        d().pinch.Out(percent=100, steps=100)
        '''
        obj = self

        @param_to_property(in_or_out=["In", "Out"])
        def _pinch(in_or_out="Out", percent=100, steps=50):
            if in_or_out in ["Out", "out"]:
                return obj.jsonrpc.pinchOut(obj.selector, percent, steps)
            elif in_or_out in ["In", "in"]:
                return obj.jsonrpc.pinchIn(obj.selector, percent, steps)
        return _pinch

    @property
    def swipe(self):
        '''
        Perform swipe action.
        Usages:
        d().swipe.right()
        d().swipe.left(steps=10)
        d().swipe.up(steps=10)
        d().swipe.down()
        d().swipe("right", steps=20)
        '''
        obj = self

        @param_to_property(direction=["up", "down", "right", "left"])
        def _swipe(direction="left", steps=10):
            return obj.jsonrpc.swipe(obj.selector, direction, steps)
        return _swipe

    @property
    def fling(self):
        '''
        Perform fling action.
        Usage:
        d().fling()  # default vertically, forward
        d().fling.horiz.forward()
        d().fling.vert.backward()
        d().fling.toBeginning(max_swipes=100) # vertically
        d().fling.horiz.toEnd()
        '''
        obj = self

        @param_to_property(
            dimention=["vert", "vertically", "vertical",
                       "horiz", "horizental", "horizentally"],
            action=["forward", "backward", "toBeginning", "toEnd"])
        def _fling(dimention="vert", action="forward", max_swipes=1000):
            vertical = dimention in ["vert", "vertically", "vertical"]
            if action is "forward":
                return obj.jsonrpc.flingForward(obj.selector, vertical)
            elif action is "backward":
                return obj.jsonrpc.flingBackward(obj.selector, vertical)
            elif action is "toBeginning":
                return obj.jsonrpc.flingToBeginning(obj.selector, vertical, max_swipes)
            elif action is "toEnd":
                return obj.jsonrpc.flingToEnd(obj.selector, vertical, max_swipes)

        return _fling

    @property
    def scroll(self):
        '''
        Perfrom scroll action.
        Usage:
        d().scroll(steps=50) # default vertically and forward
        d().scroll.horiz.forward(steps=100)
        d().scroll.vert.backward(steps=100)
        d().scroll.horiz.toBeginning(steps=100, max_swipes=100)
        d().scroll.vert.toEnd(steps=100)
        d().scroll.horiz.to(text="Clock")
        '''
        obj = self

        def __scroll(vertical, forward, steps=100):
            return obj.jsonrpc.scrollForward(obj.selector, vertical, steps) if forward else obj.jsonrpc.scrollBackward(obj.selector, vertical, steps)

        def __scroll_to_beginning(vertical, steps=100, max_swipes=1000):
            return obj.jsonrpc.scrollToBeginning(obj.selector, vertical, max_swipes, steps)

        def __scroll_to_end(vertical, steps=100, max_swipes=1000):
            return obj.jsonrpc.scrollToEnd(obj.selector, vertical, max_swipes, steps)

        def __scroll_to(vertical, **kwargs):
            return obj.jsonrpc.scrollTo(obj.selector, SelectorBuilder(**kwargs).build(), vertical)

        @param_to_property(
            dimention=["vert", "vertically", "vertical",
                       "horiz", "horizental", "horizentally"],
            action=["forward", "backward", "toBeginning", "toEnd", "to"])
        def _scroll(dimention="vert", action="forward", **kwargs):
            vertical = dimention in ["vert", "vertically", "vertical"]
            if action in ["forward", "backward"]:
                return __scroll(vertical, action is "forward", **kwargs)
            elif action is "toBeginning":
                return __scroll_to_beginning(vertical, **kwargs)
            elif action is "toEnd":
                return __scroll_to_end(vertical, **kwargs)
            elif action is "to":
                return __scroll_to(vertical, **kwargs)
        return _scroll

    @property
    def wait(self):
        '''
        Wait until the ui object gone or exist.
        Usage:
        d(text="Clock").wait.gone()  # wait until it's gone.
        d(text="Settings").wait.exist() # wait until it appears.
        '''
        obj = self

        @param_to_property(action=["exist", "gone"])
        def _wait(action, timeout=3000):
            if action is "exist":
                return obj.jsonrpc.waitForExists(obj.selector, timeout)
            elif action is "gone":
                return obj.jsonrpc.waitUntilGone(obj.selector, timeout)
        return _wait

device = _AutomatorDevice()
