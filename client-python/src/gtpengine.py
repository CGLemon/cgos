"""
Copyright (C) 2009 Christian Nentwich and contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import logging
import logging.handlers
import subprocess
import sys
import time
import threading
import queue
from common import Colour


class EngineConnectorError(Exception):
    def __init__(self, msg):
        self._msg = msg

    def __str__(self):
        return repr(self._msg)


class GTPTools(object):
    """
    Static utilities for converting GTP colours and coordinates to canonical representations.
    """

    __LEGAL_COORDINATES = "abcdefghjklmnopqrstuvwxyz"

    @staticmethod
    def convertConstantToColour(constant):
        """Convert a constant from the Colour class to a GTP colour"""
        return {Colour.WHITE: "W", Colour.BLACK: "B"}[constant]

    @staticmethod
    def convertXYToCoordinate(self, xy):
        """Convert an (x,y) tuple to a GTP board coordinate."""
        (x, y) = xy
        return GTPTools.__LEGAL_COORDINATES[x - 1]

    @staticmethod
    def convertColourToConstant(colourstr):
        """Convert a GTP coordinate to a constant from the Colour class"""
        try:
            colourstr = colourstr.lower()
            const = {
                "w": Colour.WHITE,
                "white": Colour.WHITE,
                "b": Colour.BLACK,
                "black": Colour.BLACK,
            }[colourstr]
            return const
        except KeyError:
            raise EngineConnectorError("Invalid GTP player colour: '" + colourstr + "'")

    @staticmethod
    def convertCoordinateToXY(coordstr):
        """Convert a GTP board coordinate to an (x,y) tuple"""
        try:
            column = GTPTools.__LEGAL_COORDINATES.index(coordstr[0].lower()) + 1
            row = int(coordstr[1:])
            return (column, row)
        except ValueError as IndexError:
            raise EngineConnectorError("Invalid coordinate: '" + coordstr + "'")

class Query:
    def __init__(self, commandString):
        self.commandString = commandString
        self.result = None
        self.response = list()

    def __str__(self):
        out = str()
        for line in self.response:
            out += ("{}\n".format(line))
        # Remove the GTP token '=' or '?'.
        out = out[1:]
        return out.strip()


class EngineConnector(object):
    """
    EngineConnector is the interface to a GTP engine, whether that engine is creating
    moves or just observing by receiving play commands.

    The "notify" methods are used to send commands to the engine and check responses.
    It is safe to give time handling commands to the engine - the connector will only
    issue them if the engine supports it. The "notifyCGOS" methods call cgos GTP extensions.

    Call connect() to launch the engine before using it. When done, the engine is given
    a "quit" command and if it fails to respond in time, it is killed using the OS.
    """

    MANDATORY_PLAYING_COMMANDS = [
        "boardsize",
        "clear_board",
        "komi",
        "play",
        "genmove",
        "quit",
    ]
    """ Mandatory commands for an engine that can play a game. """

    MANDATORY_OBSERVE_COMMANDS = ["boardsize", "clear_board", "komi", "play", "quit"]
    """ Mandatory commands for an engine that can observe a game (like GoGUI). """

    def __init__(
        self, programCommandLine, name, logger="EngineConnector", logfile="engine.log"
    ):
        self._programCommandLine = programCommandLine
        self._name = name
        self._subprocess = None
        self._supportedCommands = []

        self.logger = logging.getLogger(logger)
        self.logger.setLevel(logging.DEBUG)

        self._queryQueue = queue.Queue()
        self._waitingQueue = queue.Queue()
        self._finishedQueue = queue.Queue()

        self._running = True
        self._sendQueryThread = threading.Thread(target=self._sendQueryLoop, daemon=True)
        self._handleGtpThread = threading.Thread(target=self._handleGtpLoop, daemon=True)
        self._threadLock = threading.Lock()

        for t in [self._sendQueryThread, self._handleGtpThread]:
            t.start()

        if len(self.logger.handlers) == 0:
            self.handler = logging.FileHandler(logfile)
            self.handler.setLevel(logging.DEBUG)

            self.formatter = logging.Formatter(
                "%(asctime)s - %(levelname)s: %(message)s"
            )
            self.handler.setFormatter(self.formatter)

            self.logger.addHandler(self.handler)

            # Log info output to console
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(logging.INFO)

            formatter = logging.Formatter("%(asctime)s: %(message)s")
            handler.setFormatter(formatter)

            self.logger.addHandler(handler)

    def __del__(self):
        self.shutdown()

    def connect(self, mandatoryCommands=MANDATORY_PLAYING_COMMANDS):
        """
        Launch the GTP engine as a sub-process. Will throw an EngineConnectorError if
        this fails. This will also use GTP list_commands to check the capabilities of the
        engine.
        """
        self.logger.info(
            "Starting GTP engine, command line: " + self._programCommandLine
        )
        if sys.platform == "win32":
            args = self._programCommandLine
        else:
            args = self._programCommandLine.split()
        self._subprocess = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, shell=False, text=True
        )
        time.sleep(1)
        self._findSupportedCommands(mandatoryCommands)

    def getName(self):
        return self._name

    def shutdown(self):
        """
        Shut down the GTP engine using the 'quit' command, or kill it if it does not
        support that or takes too long.
        """
        if self._subprocess is not None and self._subprocess.poll() is None:
            self.logger.info(
                "Shutting down GTP engine, command line: " + self._programCommandLine
            )
            self._sendNoResponseCommand("quit")

            for i in range(5):
                time.sleep(0.5)
                if self._subprocess.poll() is None:
                    self.logger.info("Sending terminate")
                    self._subprocess.terminate()
                    break

            self._subprocess = None

            self.__running = False
            for t in [self._sendQueryThread, self._handleGtpThread]:
                t.join()

    def _sendQueryLoop(self):
        """
        Keep sending the GTP command to engine.
        """
        while self._running:
            try:
                query = self._queryQueue.get(block=True, timeout=0.1)
            except queue.Empty:
                continue

            with self._threadLock:
                commandString = query.commandString

                try:
                    self._subprocess.stdin.write(commandString + "\n")
                    self._subprocess.stdin.flush()
                    self._waitingQueue.put(query)
                except OSError as e:
                    return

    def _handleGtpLoop(self):
        """
        Keep receiving the GTP reponse from engine.
        """
        handlingQuery = None
        while self._running:
            if handlingQuery is None:
                try:
                    query = self._waitingQueue.get(block=True, timeout=0.1)
                    handlingQuery = query
                except queue.Empty:
                    continue

            with self._threadLock:
                try:
                    line = self._subprocess.stdout.readline().strip()
                except OSError as e:
                    return

            if not line:
                self._finishedQueue.put(handlingQuery)
                handlingQuery = None
                continue

            if line.split()[0] in ["=", "?"] and \
                   handlingQuery.result is not None:
                handlingQuery.result = line.split()[0]

            handlingQuery.response.append(line)

    def pushQuery(self, commandString):
        """
        Push the GTP command into queue.
        """
        query = Query(commandString)
        self._queryQueue.put(query)

    def tryGetLastResponse(self, block):
        """
        Get GTP reponse from queue. Will get None object if there is no
        reponse in the queue and 'block' is False.
        """
        try:
            query = self._finishedQueue.get(block=block, timeout=9999)
        except queue.Empty:
            if block:
                raise EngineConnectorError("Can not receive the response")
            return None, None
        return query.result, str(query)

    def hasTimeControl(self):
        """
        Return true if the engine supports time management.
        """
        return (
            "time_left" in self._supportedCommands
            and "time_settings" in self._supportedCommands
        )

    def notifyBoardSize(self, size):
        self._sendNoResponseCommand("boardsize " + str(size))

    def notifyKomi(self, komi):
        self._sendNoResponseCommand("komi " + str(komi))

    def notifyClearBoard(self):
        self._sendNoResponseCommand("clear_board")

    def notifyTimeSettings(self, totalTimeMSec):
        if self.hasTimeControl():
            self._sendNoResponseCommand(
                "time_settings " + str(int(totalTimeMSec / 1000)) + " 0 0"
            )

    def notifyTimeLeft(self, gtpColour, timemsec):
        """
        Notify the engine using the 'time_left' command, *if* the engine supports
        it.
        """
        if self.hasTimeControl():
            self._sendNoResponseCommand(
                "time_left " + gtpColour + " " + str(int(timemsec / 1000)) + " 0"
            )

    def notifyPlay(self, gtpColour, gtpCoord):
        """
        Notify the engine using the 'play' command. The colour is a GTP move
        colour and coord a GTP coordinate.
        """
        self._sendNoResponseCommand("play " + gtpColour + " " + gtpCoord)

    def requestGenMove(self, gtpColour):
        """
        Request move generation from the engine for a particular colour. The colour
        is in GTP format and the result will be a GTP coordinate (including 'pass' or
        'resign')
        """
        result = self._sendListResponseCommand("genmove " + gtpColour)
        if len(result) == 0:
            raise EngineConnectorError("Received invalid response to genmove")
        return result[0]

    def notifyCGOSOpponentName(self, name):
        """
        Send cgos-opponent_name to engine.
        """
        if "cgos-opponent_name" in self._supportedCommands:
            self._sendNoResponseCommand("cgos-opponent_name " + name)

    def notifyCGOSOpponentRating(self, rating):
        """
        Send cgos-opponent_rating to engine.
        """
        if "cgos-opponent_rating" in self._supportedCommands:
            self._sendNoResponseCommand("cgos-opponent_rating " + rating)

    def notifyCGOSGameover(self, result):
        """
        Sent cgos-game_over to engine. The calling function must format this
        into B+x.y, B+Resign, B+Time or B+Forfeit to make it SGF compliant.
        """
        if "cgos-gameover" in self._supportedCommands:
            self._sendNoResponseCommand("cgos-gameover " + result)

    def _findSupportedCommands(self, mandatoryCommands):
        """
        Fill the _supportedCommands list with GTP commands. All commands in
        'mandatoryCommands' must be present or an exception is thrown.
        """
        self._supportedCommands = self._sendListResponseCommand("list_commands")

        for cmd in mandatoryCommands:
            if not (cmd in self._supportedCommands):
                raise EngineConnectorError(
                    "Mandatory GTP command not implemented: " + cmd
                )

    def _sendNoResponseCommand(self, commandString):
        """
        Send a GTP command to the engine. The command must be one that requires only a
        success / failure response and no output.

        If the engine returns an error, EngineConnectorError is raised.
        """
        self._sendSyncRawGTPCommand(commandString)

    def _sendListResponseCommand(self, commandString):
        """
        Send a GTP command to the engine. The command must be one that requires only a
        line-separated list as a response. The lines are returned as an array, with
        whitespace stripped.

        If the engine returns an error, EngineConnectorError is raised.
        """
        return self._sendSyncRawGTPCommand(commandString)

    def _sendSyncRawGTPCommand(self, commandString):
        """
        Send a GTP command to the engine and return everything up to the next blank
        line as the response. If the response is malformed, EngineConnectorError is raised.

        Don't call this even within this class. Use the other send methods for specific
        command types (list response, etc.)
        """

        if self._subprocess.poll() is not None:
            raise EngineConnectorError("Cannot send GTP command. Engine has terminated")

        self.logger.debug("Sending: " + commandString)
        self.pushQuery(commandString)

        result, response = self.tryGetLastResponse(block=True)
        if result == "?":
            raise EngineConnectorError("GTP command rejected: " + response)

        self.logger.debug("Response: " + str(response))
        return response
