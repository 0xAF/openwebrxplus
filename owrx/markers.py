from owrx.config.core import CoreConfig
from owrx.config import Config
from owrx.version import openwebrx_version
from owrx.map import Map, Location
from owrx.aprs import getSymbolData
from owrx.eibi import EIBI
from owrx.repeaters import Repeaters
from json import JSONEncoder
from datetime import datetime, timedelta, timezone

import urllib
import threading
import logging
import json
import re
import os
import time

logger = logging.getLogger(__name__)


class MyJSONEncoder(JSONEncoder):
    def default(self, obj):
        return obj.toJSON()


class MarkerLocation(Location):
    def __init__(self, attrs):
        self.attrs = attrs
        # Making sure older cached files load
        self.attrs["type"] = "latlon"

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
            logger.info("Stopping marker database thread.")
            self.event.set()
            self.thread.join()

    # This is the actual thread function
    def _refreshThread(self):
        logger.info("Starting marker database thread...")

        # No markers yet
        self.markers   = {}
        self.rxmarkers = {}
        self.txmarkers = {}
        self.remarkers = {}

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

        # Load current schedule from the EIBI database
        self.txmarkers = self.loadCurrentTransmitters()

        # Load repeaters from the Repeaters database
        self.remarkers = self.loadRepeaters()

        # Update map with markers
        logger.info("Updating map...")
        self.updateMap(self.markers)
        self.updateMap(self.rxmarkers)
        self.updateMap(self.txmarkers)
        self.updateMap(self.remarkers)

        #
        # Main Loop
        #

        while not self.event.is_set():
            # Wait for the head of the next hour
            self.event.wait((60 - datetime.utcnow().minute) * 60)
            if self.event.is_set():
                break

            # Load new transmitters schedule from the EIBI
            logger.info("Refreshing transmitters schedule..")
            tx = self.loadCurrentTransmitters()

            # Check if we need to exit
            if self.event.is_set():
                break

            # Remove station markers that have no transmissions
            map  = Map.getSharedInstance()
            notx = [x for x in self.txmarkers.keys() if x not in tx]
            for key in notx:
                map.removeLocation(key)
                del self.txmarkers[key]

            # Create a timestamp far into the future, for permanent markers
            permanent = datetime.now(timezone.utc) + timedelta(weeks=500)

            # Update station markers that have transmissions
            for key in tx.keys():
                r = tx[key]
                map.updateLocation(r.getId(), r, r.getMode(), timestamp=permanent)
                self.txmarkers[key] = r

            # Done with the schedule
            notx = None
            tx   = None

            # Check if we need to exit
            if self.event.is_set():
                break

            # Update cached receivers data
            if time.time() - ts >= self.refreshPeriod:
                logger.info("Refreshing receivers database...")
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
                        map.updateLocation(r.getId(), r, r.getMode(), timestamp=permanent)
                        self.rxmarkers[key] = r
                    # Done updating receivers
                    norx = None
                    rx   = None

        # Done with the thread
        logger.info("Stopped marker database thread.")
        self.thread = None

    # Save markers to a given file
    def saveMarkers(self, file: str, markers):
        logger.info("Saving {0} markers to '{1}'...".format(len(markers), file))
        try:
            with open(file, "w") as f:
                json.dump(markers, f, cls=MyJSONEncoder, indent=2)
                f.close()
        except Exception as e:
            logger.error("saveMarkers() exception: {0}".format(e))

    # Load markers from a given file
    def loadMarkers(self, file: str):
        logger.info("Loading markers from '{0}'...".format(file))
        # Load markers list from JSON file
        try:
            with open(file, "r") as f:
                db = json.load(f)
                f.close()
        except Exception as e:
            logger.error("loadMarkers() exception: {0}".format(e))
            return

        # Process markers list
        result = {}
        for key in db.keys():
            attrs = db[key]
            result[key] = MarkerLocation(attrs)

        # Done
        logger.info("Loaded {0} markers from '{1}'.".format(len(result), file))
        return result

    # Update given markers on the map
    def updateMap(self, markers):
        # Must have valid markers to update
        if markers is not None:
            # Create a timestamp far into the future, for permanent markers
            permanent = datetime.now(timezone.utc) + timedelta(weeks=500)
            for r in markers.values():
                Map.getSharedInstance().updateLocation(r.getId(), r, r.getMode(), timestamp=permanent)

    # Scrape online databases, updating cache file
    def updateCache(self):
        # Scrape websites for data
        file  = self._getCachedMarkersFile()
        cache = {}
        logger.info("Scraping KiwiSDR website...")
        cache.update(self.scrapeKiwiSDR())
        logger.info("Scraping WebSDR website...")
        cache.update(self.scrapeWebSDR())
        logger.info("Scraping OpenWebRX website...")
        cache.update(self.scrapeOWRX())

        # Save parsed data into a file, if there is anything to save
        if cache:
            self.saveMarkers(file, cache)

        # Done
        return cache

    #
    # Following functions scrape data from websites and internal databases
    #

    def loadRepeaters(self, rangeKm: int = 200):
        # No result yet
        result = {}
        # Refresh / load repeaters database, as needed
        Repeaters.getSharedInstance().refresh()
        # Load repeater sites from repeaters database
        for entry in Repeaters.getSharedInstance().getAllInRange(rangeKm):
            rl = MarkerLocation({
                "type"    : "latlon",
                "mode"    : "Repeaters",
                "id"      : entry["name"],
                "lat"     : entry["lat"],
                "lon"     : entry["lon"],
                "freq"    : entry["freq"],
                "mmode"   : entry["mode"],
                "status"  : entry["status"],
                "updated" : entry["updated"],
                "comment" : entry["comment"]
            })
            result[rl.getId()] = rl
        # Done
        return result

    def loadCurrentTransmitters(self):
        #url = "https://www.short-wave.info/index.php?txsite="
        url = "https://www.google.com/search?q="
        result = {}

        # Refresh / load EIBI database, as needed
        EIBI.getSharedInstance().refresh()

        # Load transmitter sites from EIBI database
        for entry in EIBI.getSharedInstance().currentTransmitters().values():
            # Extract target regions and languages, removing duplicates
            schedule = entry["schedule"]
            langs   = {}
            targets = {}
            comment = ""
            langstr = ""
#            for row in schedule:
#                lang   = row["lang"]
#                target = row["tgt"]
#                if target and target not in targets:
#                    targets[target] = True
#                    comment += (", " if comment else " to ") + target
#                if lang and lang not in langs:
#                    langs[lang] = True
#                    langstr += (", " if langstr else "") + re.sub(r"(:|\s*\().*$", "", lang)

            # Compose comment
            comment = ("Transmitting" + comment) if comment else "Transmitter"
            comment = (comment + " (" + langstr + ")") if langstr else comment

            rl = MarkerLocation({
                "type"    : "latlon",
                "mode"    : "Stations",
                "comment" : comment,
                "id"      : entry["name"],
                "lat"     : entry["lat"],
                "lon"     : entry["lon"],
                "ttl"     : entry["ttl"] * 1000,
                "url"     : url + urllib.parse.quote_plus(entry["name"]),
                "schedule": schedule
            })
            result[rl.getId()] = rl

        # Done
        logger.info("Loaded {0} transmitters from EIBI.".format(len(result)))
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
                            "type"    : "latlon",
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
            logger.error("scrapeOWRX() exception: {0}".format(e))

        # Done
        return result

    def scrapeWebSDR(self, url: str = "http://websdr.ewi.utwente.nl/~~websdrlistk?v=1&fmt=2&chseq=0"):
        result = {}
        try:
            data = urllib.request.urlopen(url).read().decode('utf-8')
            data = json.loads(re.sub(r"^\s*//.*", "", data, flags=re.MULTILINE))

            for entry in data:
                if "lat" in entry and "lon" in entry and "url" in entry:
                    # Save accumulated attributes, use hostname as key
                    lat = entry["lat"]
                    lon = entry["lon"]
                    rl  = MarkerLocation({
                        "type"    : "latlon",
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
            logger.error("scrapeWebSDR() exception: {0}".format(e))

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
                                "type"    : "latlon",
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
            logger.error("scrapeKiwiSDR() exception: {0}".format(e))

        # Done
        return result

