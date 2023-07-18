from owrx.config.core import CoreConfig
from owrx.map import Map, Location
from owrx.aprs import getSymbolData
from json import JSONEncoder
from owrx.eibi import EIBI_Locations, EIBI
from datetime import datetime

import urllib
import threading
import logging
import json
import re
import os
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class MyJSONEncoder(JSONEncoder):
    def default(self, obj):
        return obj.toJSON()


class MarkerLocation(Location):
    def __init__(self, attrs):
        self.attrs = attrs

    def getId(self):
        return self.attrs["id"]

    def getMode(self):
        return self.attrs["mode"]

    def __dict__(self):
        return self.attrs

    def toJSON(self):
        return self.attrs


class Markers(object):
    sharedInstance = None
    creationLock = threading.Lock()

    @staticmethod
    def getSharedInstance():
        with Markers.creationLock:
            if Markers.sharedInstance is None:
                Markers.sharedInstance = Markers()
        return Markers.sharedInstance

    @staticmethod
    def start():
        Markers.getSharedInstance().startThread()

    @staticmethod
    def stop():
        Markers.getSharedInstance().stopThread()

    @staticmethod
    def _getCachedMarkersFile():
        coreConfig = CoreConfig()
        return "{data_directory}/markers.json".format(data_directory=coreConfig.get_data_directory())

    def __init__(self):
        self.refreshPeriod = 60*60*24
        self.event = threading.Event()
        self.fmarkers = {}
        self.wmarkers = {}
        self.smarkers = {}
        self.thread = None
        # Known database files
        self.fileList = [
            "markers.json",
            "/etc/openwebrx/markers.json",
        ]
        # Find additional marker files in the markers.d folder
        try:
            markersDir = "/etc/openwebrx/markers.d"
            self.fileList += [ markersDir + "/" + file
                for file in os.listdir(markersDir) if file.endswith(".json")
            ]
        except Exception:
            pass

    # Start the main thread
    def startThread(self):
        if self.thread is None:
            self.event.clear()
            self.thread = threading.Thread(target=self._refreshThread)
            self.thread.start()

    # Stop the main thread
    def stopThread(self):
        if self.thread is not None:
            self.event.set()
            self.thread.join()

    # This is the actual thread function
    def _refreshThread(self):
        logger.debug("Starting marker database thread...")

        # No markers yet
        self.markers   = {}
        self.rxmarkers = {}
        self.txmarkers = {}

        # Load miscellaneous markers from local files
        for file in self.fileList:
            if os.path.isfile(file):
                self.markers.update(self.loadMarkers(file))

        # This file contains cached receivers database
        file = self._getCachedMarkersFile()
        ts   = os.path.getmtime(file) if os.path.isfile(file) else 0

        # If cached receivers database stale, update it
        if time.time() - ts >= self.refreshPeriod:
            self.rxmarkers = self.updateCache()
            ts = os.path.getmtime(file) if os.path.isfile(file) else 0

        # If receivers database update did not run or failed, use cache
        if not self.rxmarkers:
            self.rxmarkers = self.loadMarkers(file)

        # Load new transmitters schedule from the EIBI
        time.sleep(30)
        self.txmarkers = self.loadEIBI()

        # Update map with markers
        logger.debug("Updating map...")
        self.updateMap(self.markers)
        self.updateMap(self.rxmarkers)
        self.updateMap(self.txmarkers)

        #
        # Main Loop
        #

        while not self.event.is_set():
            # Wait for the head of the next hour
            self.event.wait((60 - datetime.utcnow().minute) * 60)
            if self.event.is_set():
                break

            # Load new transmitters schedule from the EIBI
            logger.debug("Refreshing transmitters schedule..")
            tx = self.loadEIBI()

            # Check if we need to exit
            if self.event.is_set():
                break

            # Remove station markers that have no transmissions
            map  = Map.getSharedInstance()
            notx = [x for x in self.txmarkers.keys() if x not in tx]
            for key in notx:
                map.removeLocation(key)
                del self.txmarkers[key]

            # Update station markers that have transmissions
            for key in tx.keys():
                r = tx[key]
                map.updateLocation(r.getId(), r, r.getMode(), permanent=True)
                self.txmarkers[key] = r

            # Done with the schedule
            notx = None
            tx   = None

            # Check if we need to exit
            if self.event.is_set():
                break

            # Update cached receivers data
            if time.time() - ts >= self.refreshPeriod:
                logger.debug("Refreshing receivers database...")
                rx = self.updateCache()
                ts = os.path.getmtime(file)
                if rx:
                    # Remove receiver markers that no longer exist
                    norx = [x for x in self.rxmarkers.keys() if x not in rx]
                    for key in norx:
                        map.removeLocation(key)
                        del self.rxmarkers[key]
                    # Update receiver markers that are online
                    for key in rx.keys():
                        r = rx[key]
                        map.updateLocation(r.getId(), r, r.getMode(), permanent=True)
                        self.rxmarkers[key] = r
                    # Done updating receivers
                    norx = None
                    rx   = None

        # Done with the thread
        logger.debug("Stopped marker database thread.")
        self.thread = None

    # Load markers from a given file
    def loadMarkers(self, file: str):
        logger.debug("Loading markers from '{0}'...".format(file))
        # Load markers list from JSON file
        try:
            with open(file, "r") as f:
                db = json.load(f)
                f.close()
        except Exception as e:
            logger.debug("loadMarkers() exception: {0}".format(e))
            return

        # Process markers list
        result = {}
        for key in db.keys():
            attrs = db[key]
            result[key] = MarkerLocation(attrs)

        # Done
        logger.debug("Loaded {0} markers from '{1}'.".format(len(result), file))
        return result

    # Update given markers on the map
    def updateMap(self, markers):
        for r in markers.values():
            Map.getSharedInstance().updateLocation(r.getId(), r, r.getMode(), permanent=True)

    # Scrape online databases, updating cache file
    def updateCache(self):
        # Scrape websites for data
        file  = self._getCachedMarkersFile()
        cache = {}
        logger.debug("Scraping KiwiSDR website...")
        cache.update(self.scrapeKiwiSDR())
        logger.debug("Scraping WebSDR website...")
        cache.update(self.scrapeWebSDR())
        logger.debug("Scraping OpenWebRX website...")
        cache.update(self.scrapeOWRX())
        # Save parsed data into a file, if there is anything to save
        if len(cache) > 0:
            logger.debug("Saving {0} markers to '{1}'...".format(len(cache), file))
            try:
                with open(file, "w") as f:
                    json.dump(cache, f, cls=MyJSONEncoder, indent=2)
                    f.close()
            except Exception as e:
                logger.debug("updateCache() exception: {0}".format(e))
        # Done
        return cache

    #
    # Following functions scrape data from websites and internal databases
    #

    def loadEIBI(self):
        logger.debug("Loading EIBI transmitter locations...")
        #url = "https://www.short-wave.info/index.php?txsite="
        url = "https://www.google.com/search?q="
        result = {}
        # Load transmitter sites from EIBI database
        for entry in EIBI.getSharedInstance().currentTransmitters().values():
            rl = MarkerLocation({
                "type"    : "feature",
                "mode"    : "Stations",
                "comment" : "Transmitter",
                "id"      : entry["name"],
                "lat"     : entry["lat"],
                "lon"     : entry["lon"],
                "url"     : url + urllib.parse.quote_plus(entry["name"]),
                "schedule": entry["schedule"]
            })
            result[rl.getId()] = rl
        # Done
        logger.debug("Loaded {0} EIBI transmitter locations.".format(len(result)))
        return result

    def scrapeOWRX(self, url: str = "https://www.receiverbook.de/map"):
        patternJson = re.compile(r"^\s*var\s+receivers\s+=\s+(\[.*\]);\s*$")
        result = {}
        try:
            data = None
            for line in urllib.request.urlopen(url).readlines():
                # Convert read bytes to a string
                line = line.decode('utf-8')
                # When we encounter a URL...
                m = patternJson.match(line)
                if m:
                    data = json.loads(m.group(1))
                    break
            if data is not None:
                for entry in data:
                    lat = entry["location"]["coordinates"][1]
                    lon = entry["location"]["coordinates"][0]
                    for r in entry["receivers"]:
                        if "version" in r:
                            dev = r["type"] + " " + r["version"]
                        else:
                            dev = r["type"]
                        rl = MarkerLocation({
                            "type"    : "feature",
                            "mode"    : r["type"],
                            "id"      : re.sub(r"^.*://(.*?)(/.*)?$", r"\1", r["url"]),
                            "lat"     : lat,
                            "lon"     : lon,
                            "comment" : r["label"],
                            "url"     : r["url"],
                            "device"  : dev
                        })
                        result[rl.getId()] = rl
                        # Offset colocated receivers by ~500m
                        lon = lon + 0.0005

        except Exception as e:
            logger.debug("scrapeOWRX() exception: {0}".format(e))

        # Done
        return result

    def scrapeWebSDR(self, url: str = "http://websdr.ewi.utwente.nl/~~websdrlistk?v=1&fmt=2&chseq=0"):
        result = {}
        try:
            data = urllib.request.urlopen(url).read().decode('utf-8')
            data = json.loads(re.sub("^\s*//.*", "", data, flags=re.MULTILINE))

            for entry in data:
                if "lat" in entry and "lon" in entry and "url" in entry:
                    # Save accumulated attributes, use hostname as key
                    lat = entry["lat"]
                    lon = entry["lon"]
                    rl  = MarkerLocation({
                        "type"    : "feature",
                        "mode"    : "WebSDR",
                        "id"      : re.sub(r"^.*://(.*?)(/.*)?$", r"\1", entry["url"]),
                        "lat"     : lat,
                        "lon"     : lon,
                        "comment" : entry["desc"],
                        "url"     : entry["url"],
                        "users"   : int(entry["users"]),
                        "device"  : "WebSDR"
                    })
                    result[rl.getId()] = rl

        except Exception as e:
            logger.debug("scrapeWebSDR() exception: {0}".format(e))

        # Done
        return result

    def scrapeKiwiSDR(self, url: str = "http://kiwisdr.com/public/"):
        result = {}
        try:
            patternAttr = re.compile(r".*<!--\s+(\S+)=(.*)\s+-->.*")
            patternUrl  = re.compile(r".*<a\s+href=['\"](\S+?)['\"].*>.*</a>.*")
            patternGps  = re.compile(r"\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\)")
            entry = {}

            for line in urllib.request.urlopen(url).readlines():
                # Convert read bytes to a string
                line = line.decode('utf-8')
                # When we encounter a URL...
                m = patternUrl.match(line)
                if m is not None:
                    # Add URL attribute
                    entry["url"] = m.group(1)
                    # Must have "gps" attribut with latitude / longitude
                    if "gps" in entry and "url" in entry:
                        m = patternGps.match(entry["gps"])
                        if m is not None:
                            # Save accumulated attributes, use hostname as key
                            lat = float(m.group(1))
                            lon = float(m.group(2))
                            rl = MarkerLocation({
                                "type"    : "feature",
                                "mode"    : "KiwiSDR",
                                "id"      : re.sub(r"^.*://(.*?)(/.*)?$", r"\1", entry["url"]),
                                "lat"     : lat,
                                "lon"     : lon,
                                "comment" : entry["name"],
                                "url"     : entry["url"],
                                "users"   : int(entry["users"]),
                                "maxusers": int(entry["users_max"]),
                                "loc"     : entry["loc"],
                                "altitude": int(entry["asl"]),
                                "antenna" : entry["antenna"],
                                "device"  : re.sub("_v", " ", entry["sw_version"])
                            })
                            result[rl.getId()] = rl
                    # Clear current entry
                    entry = {}
                else:
                    # Save all parsed attributes in the current entry
                    m = patternAttr.match(line)
                    if m is not None:
                        # Save attribute in the current entry
                        entry[m.group(1).lower()] = m.group(2)

        except Exception as e:
            logger.debug("scrapeKiwiSDR() exception: {0}".format(e))

        # Done
        return result