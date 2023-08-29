from csdr.module import PickleModule
from math import sqrt, atan2, pi, floor, acos, cos
from owrx.map import LatLngLocation, IncrementalUpdate, TTLUpdate, Location, Map
from owrx.metrics import Metrics, CounterMetric
from datetime import timedelta
from enum import Enum
import time

FEET_PER_METER = 3.28084


class AirplaneLocation(IncrementalUpdate, TTLUpdate, LatLngLocation):
    mapKeys = [
        "lat",
        "lon",
        "altitude",
        "heading",
        "groundtrack",
        "groundspeed",
        "verticalspeed",
        "identification",
        "TAS",
        "IAS",
        "heading",
    ]
    ttl = 30

    def __init__(self, icao, message):
        self.history = []
        self.timestamp = time.time()
        self.props = message
        self.icao = icao
        if "lat" in message and "lon" in message:
            super().__init__(message["lat"], message["lon"])
        else:
            self.lat = None
            self.lon = None

    def update(self, previousLocation: Location):
        history = previousLocation.history
        now = time.time()
        history = [p for p in history if now - p["timestamp"] < self.ttl]
        history += [{
            "timestamp": self.timestamp,
            "props": self.props,
        }]
        self.history = history

        merged = {}
        for p in self.history:
            merged.update(p["props"])

        self.props = merged
        if "lat" in merged:
            self.lat = merged["lat"]
        if "lon" in merged:
            self.lon = merged["lon"]

    def __dict__(self):
        dict = super().__dict__()
        dict.update(self.props)
        dict["icao"] = self.icao
        return dict

    def getTTL(self) -> timedelta:
        return timedelta(seconds=self.ttl)


class CprRecordType(Enum):
    AIR = ("air", 360)
    GROUND = ("ground", 90)

    def __new__(cls, *args, **kwargs):
        name, baseAngle = args
        obj = object.__new__(cls)
        obj._value_ = name
        obj.baseAngle = baseAngle
        return obj


class CprCache:
    def __init__(self):
        self.airRecords = {}
        self.groundRecords = {}

    def __getRecords(self, cprType: CprRecordType):
        if cprType is CprRecordType.AIR:
            return self.airRecords
        elif cprType is CprRecordType.GROUND:
            return self.groundRecords

    def getRecentData(self, icao: str, cprType: CprRecordType):
        records = self.__getRecords(cprType)
        if icao not in records:
            return []
        now = time.time()
        filtered = [r for r in records[icao] if now - r["timestamp"] < 10]
        records_sorted = sorted(filtered, key=lambda r: r["timestamp"])
        records[icao] = records_sorted
        return [r["data"] for r in records_sorted]

    def addRecord(self, icao: str, data: any, cprType: CprRecordType):
        records = self.__getRecords(cprType)
        if icao not in records:
            records[icao] = []
        records[icao].append({"timestamp": time.time(), "data": data})


class ModeSParser(PickleModule):
    def __init__(self):
        self.cprCache = CprCache()
        name = "dump1090.decodes.adsb"
        self.metrics = Metrics.getSharedInstance().getMetric(name)
        if self.metrics is None:
            self.metrics = CounterMetric()
            Metrics.getSharedInstance().addMetric(name, self.metrics)
        super().__init__()

    def process(self, input):
        format = (input[0] & 0b11111000) >> 3
        message = {
            "mode": "ADSB",
            "format": format
        }
        if format == 17:
            message["capability"] = input[0] & 0b111
            message["icao"] = icao = input[1:4].hex()
            type = (input[4] & 0b11111000) >> 3
            message["adsb_type"] = type

            if type in [1, 2, 3, 4]:
                # identification message
                id = [
                    (input[5] & 0b11111100) >> 2,
                    ((input[5] & 0b00000011) << 4) | ((input[6] & 0b11110000) >> 4),
                    ((input[6] & 0b00001111) << 2) | ((input[7] & 0b11000000) >> 6),
                    input[7] & 0b00111111,
                    (input[8] & 0b11111100) >> 2,
                    ((input[8] & 0b00000011) << 4) | ((input[9] & 0b11110000) >> 4),
                    ((input[9] & 0b00001111) << 2) | ((input[10] & 0b11000000) >> 6),
                    input[10] & 0b00111111
                ]

                message["identification"] = bytes(b + (0x40 if b < 27 else 0) for b in id).decode("ascii").strip()

            elif type in [5, 6, 7, 8]:
                # surface position
                # there's no altitude data in this message type, but the type implies the aircraft is on ground
                message["altitude"] = "ground"

                movement = ((input[4] & 0b00000111) << 4) | ((input[5] & 0b11110000) >> 4)
                if movement == 1:
                    message["groundspeed"] = 0
                elif 2 <= movement < 9:
                    message["groundspeed"] = (movement - 1) * .0125
                elif 9 <= movement < 13:
                    message["groundspeed"] = 1 + (movement - 8) * .25
                elif 13 <= movement < 39:
                    message["groundspeed"] = 2 + (movement - 12) * .5
                elif 39 <= movement < 94:
                    message["groundspeed"] = 15 + (movement - 38)  # * 1
                elif 94 <= movement < 109:
                    message["groundspeed"] = 70 + (movement - 108) * 2
                elif 109 <= movement < 124:
                    message["groundspeeed"] = 100 + (movement - 123) * 5

                if (input[5] & 0b00001000) >> 3:
                    track = ((input[5] & 0b00000111) << 3) | ((input[6] & 0b11110000) >> 4)
                    message["groundtrack"] = (360 * track) / 128

                cpr = self.__getCprData(icao, input, CprRecordType.GROUND)
                if cpr is not None:
                    lat, lon = cpr
                    message["lat"] = lat
                    message["lon"] = lon

            elif type in [9, 10, 11, 12, 13, 14, 15, 16, 17, 18]:
                # airborne position (w/ baro  altitude)

                cpr = self.__getCprData(icao, input, CprRecordType.AIR)
                if cpr is not None:
                    lat, lon = cpr
                    message["lat"] = lat
                    message["lon"] = lon

                q = (input[5] & 0b1)
                altitude = ((input[5] & 0b11111110) << 3) | ((input[6] & 0b11110000) >> 4)
                if q:
                    message["altitude"] = altitude * 25 - 1000
                elif altitude > 0:
                    message["altitude"] = self._grayDecode(altitude) * 100

            elif type == 19:
                # airborne velocity
                subtype = input[4] & 0b111
                if subtype in [1, 2]:
                    # velocity is reported in an east/west and a north/south component
                    # vew = velocity east / west
                    vew = ((input[5] & 0b00000011) << 8) | input[6]
                    # vns = velocity north / south
                    vns = ((input[7] & 0b01111111) << 3) | ((input[8] & 0b1110000000) >> 5)

                    # 0 means no data
                    if vew != 0 and vns != 0:
                        # dew = direction east/west (0 = to east, 1 = to west)
                        dew = (input[5] & 0b00000100) >> 2
                        # dns = direction north/south (0 = to north, 1 = to south)
                        dns = (input[7] & 0b10000000) >> 7

                        vx = vew - 1
                        if dew:
                            vx *= -1
                        vy = vns - 1
                        if dns:
                            vy *= -1
                        # supersonic
                        if subtype == 2:
                            vx *= 4
                            vy *= 4
                        message["groundspeed"] = sqrt(vx ** 2 + vy ** 2)
                        message["groundtrack"] = (atan2(vx, vy) * 360 / (2 * pi)) % 360

                    # vertical rate
                    vr = ((input[8] & 0b00000111) << 6) | ((input[9] & 0b11111100) >> 2)
                    if vr != 0:
                        # vertical speed sign (1 = negative)
                        svr = ((input[8] & 0b00001000) >> 3)
                        # vertical speed
                        vs = 64 * (vr - 1)
                        if svr:
                            vs *= -1
                        message["verticalspeed"] = vs

                elif subtype in [3, 4]:
                    sh = (input[5] & 0b00000100) >> 2
                    if sh:
                        hdg = ((input[5] & 0b00000011) << 8) | input[6]
                        message["heading"] = hdg * 360 / 1024
                    airspeed = ((input[7] & 0b01111111) << 3) | ((input[8] & 0b11100000) >> 5)
                    if airspeed != 0:
                        airspeed -= 1
                        # supersonic
                        if subtype == 4:
                            airspeed *= 4
                        airspeed_type = (input[7] & 0b10000000) >> 7
                        if airspeed_type:
                            message["TAS"] = airspeed
                        else:
                            message["IAS"] = airspeed

            elif type in [20, 21, 22]:
                # airborne position (w/GNSS height)

                cpr = self.__getCprData(icao, input, CprRecordType.AIR)
                if cpr is not None:
                    lat, lon = cpr
                    message["lat"] = lat
                    message["lon"] = lon

                altitude = (input[5] << 4) | ((input[6] & 0b1111) >> 4)
                message["altitude"] = altitude * FEET_PER_METER

            elif type == 28:
                # aircraft status
                pass

            elif type == 29:
                # target state and status information
                pass

            elif type == 31:
                # aircraft operation status
                pass

        elif format == 11:
            # Mode-S All-call reply
            message["icao"] = input[1:4].hex()

        self.metrics.inc()

        if "icao" in message and AirplaneLocation.mapKeys & message.keys():
            data = {k: message[k] for k in AirplaneLocation.mapKeys if k in message}
            loc = AirplaneLocation(message["icao"], data)
            Map.getSharedInstance().updateLocation({"icao": message['icao']}, loc, "ADS-B", None)

        return message

    def __getCprData(self, icao: str, input, cprType: CprRecordType):
        self.cprCache.addRecord(icao, {
            "cpr_format": (input[6] & 0b00000100) >> 2,
            "lat_cpr": ((input[6] & 0b00000011) << 15) | (input[7] << 7) | ((input[8] & 0b11111110) >> 1),
            "lon_cpr": ((input[8] & 0b00000001) << 16) | (input[9] << 8) | (input[10]),
        }, cprType)

        records = self.cprCache.getRecentData(icao, cprType)

        try:
            # records are sorted by timestamp, last should be newest
            odd = next(r for r in reversed(records) if r["cpr_format"])
            even = next(r for r in reversed(records) if not r["cpr_format"])
            newest = next(reversed(records))

            lat_cpr_even = even["lat_cpr"] / 2 ** 17
            lat_cpr_odd = odd["lat_cpr"] / 2 ** 17

            # latitude zone index
            j = floor(59 * lat_cpr_even - 60 * lat_cpr_odd + .5)

            nz = 15
            d_lat_even = cprType.baseAngle / (4 * nz)
            d_lat_odd = cprType.baseAngle / (4 * nz - 1)

            lat_even = d_lat_even * ((j % 60) + lat_cpr_even)
            lat_odd = d_lat_odd * ((j % 59) + lat_cpr_odd)

            if lat_even >= 270:
                lat_even -= 360
            if lat_odd >= 270:
                lat_odd -= 360

            def nl(lat):
                if lat == 0:
                    return 59
                elif lat == 87:
                    return 2
                elif lat == -87:
                    return 2
                elif lat > 87:
                    return 1
                elif lat < -87:
                    return 1
                else:
                    return floor((2 * pi) / acos(1 - (1 - cos(pi / (2 * nz))) / (cos((pi / 180) * abs(lat)) ** 2)))

            if nl(lat_even) != nl(lat_odd):
                # latitude zone mismatch.
                return

            lat = lat_odd if newest["cpr_format"] else lat_even

            lon_cpr_even = even["lon_cpr"] / 2 ** 17
            lon_cpr_odd = odd["lon_cpr"] / 2 ** 17

            # longitude zone index
            nl_lat = nl(lat)
            m = floor(lon_cpr_even * (nl_lat - 1) - lon_cpr_odd * nl_lat + .5)

            n_even = max(nl_lat, 1)
            n_odd = max(nl_lat - 1, 1)

            d_lon_even = cprType.baseAngle / n_even
            d_lon_odd = cprType.baseAngle / n_odd

            lon_even = d_lon_even * (m % n_even + lon_cpr_even)
            lon_odd = d_lon_odd * (m % n_odd + lon_cpr_odd)

            lon = lon_odd if newest["cpr_format"] else lon_even
            if lon >= 180:
                lon -= 360

            return lat, lon

        except StopIteration:
            # we don't have both CPR records. better luck next time.
            pass

    def _grayDecode(self, input: int):
        l = input.bit_length()
        previous_bit = 0
        output = 0
        for i in reversed(range(0, l)):
            bit = (previous_bit ^ ((input >> i) & 1))
            output |= bit << i
            previous_bit = bit
        return output
