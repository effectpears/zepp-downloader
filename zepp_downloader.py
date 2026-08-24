#!/usr/bin/env python3

import fcntl
import json
import logging
import math
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from datetime import datetime

try:
    import requests
except ImportError:
    print(
        "Fehler: Das Python-Modul 'requests' fehlt.\n"
        "Installation:\n"
        "  python3 -m pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)

try:
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.activity_message import ActivityMessage
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.profile_type import (
        FileType,
        Manufacturer,
        Sport,
        SubSport,
    )
except ImportError:
    print(
        "Fehler: Das Python-Modul 'fit-tool' fehlt.\n"
        "Installation:\n"
        "  python3 -m pip install fit-tool",
        file=sys.stderr,
    )
    sys.exit(1)


# =============================================================================
# Pfade und .env
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"


def load_env_file(env_file: Path) -> None:
    """
    Lädt eine einfache .env-Datei.

    Unterstützt:
      SCHLUESSEL=WERT
      export SCHLUESSEL=WERT
      einfache oder doppelte Anführungszeichen

    Bereits gesetzte Umgebungsvariablen werden nicht überschrieben.
    """

    if not env_file.is_file():
        print(
            f"Fehler: Die Konfigurationsdatei wurde nicht gefunden: {env_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with env_file.open("r", encoding="utf-8") as file_handle:
            for line_number, raw_line in enumerate(file_handle, start=1):
                line = raw_line.strip()

                if not line or line.startswith("#"):
                    continue

                if line.startswith("export "):
                    line = line[7:].strip()

                if "=" not in line:
                    print(
                        f"Warnung: Ungültige Zeile {line_number} in "
                        f"{env_file} wird ignoriert.",
                        file=sys.stderr,
                    )
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                if not key:
                    continue

                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in ("'", '"')
                ):
                    value = value[1:-1]

                os.environ.setdefault(key, value)

    except OSError as exc:
        print(
            f"Fehler beim Lesen der Konfigurationsdatei {env_file}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


load_env_file(ENV_FILE)


# =============================================================================
# Konfiguration
# =============================================================================

APP_TOKEN = os.getenv("APP_TOKEN", "").strip()

BASE_URL = os.getenv(
    "HUAMI_BASE_URL",
    "https://api-mifit-de2.huami.com/v1/sport/run",
).strip().rstrip("/")

ACTIVITY_SOURCE = os.getenv(
    "HUAMI_ACTIVITY_SOURCE",
    "run.fit.huami.com",
).strip()

DOWNLOAD_PATH = Path(
    os.getenv(
        "DOWNLOAD_PATH",
        str(SCRIPT_DIR / "fit_files"),
    )
).expanduser()

HISTORY_FILE = Path(
    os.getenv(
        "HISTORY_FILE", 
        str(DOWNLOAD_PATH / "download_history.txt"),
    )
).expanduser()

LOG_PATH = Path(
    os.getenv(
        "LOG_PATH",
        str(SCRIPT_DIR / "logs"),
    )
).expanduser()


def get_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = int(raw_value)
    except ValueError:
        print(
            f"Fehler: {name} muss eine Ganzzahl sein. "
            f"Aktueller Wert: {raw_value!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    if value < 1:
        print(
            f"Fehler: {name} muss mindestens 1 sein.",
            file=sys.stderr,
        )
        sys.exit(1)

    return value

def load_history() -> set[str]:
    """
    Lädt alle bereits heruntergeladenen Track-IDs aus der History-Datei in ein Set.
    """
    if not HISTORY_FILE.is_file():
        return set()
    with HISTORY_FILE.open("r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}

def save_to_history(track_id: str):
    """
    Speichert eine erfolgreich heruntergeladene Track-ID in der History-Datei.
    """
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{track_id}\n")

def get_nonnegative_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()

    try:
        value = float(raw_value)
    except ValueError:
        print(
            f"Fehler: {name} muss eine Zahl sein. "
            f"Aktueller Wert: {raw_value!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    if value < 0:
        print(
            f"Fehler: {name} darf nicht negativ sein.",
            file=sys.stderr,
        )
        sys.exit(1)

    return value


FETCH_LIMIT = get_positive_int("FETCH_LIMIT", 20)
REQUEST_DELAY = get_nonnegative_float("REQUEST_DELAY", 1.0)
REQUEST_TIMEOUT = get_positive_int("REQUEST_TIMEOUT", 30)
MAX_RETRIES = get_positive_int("MAX_RETRIES", 3)

LOG_MAX_BYTES = get_positive_int("LOG_MAX_BYTES", 5 * 1024 * 1024)
LOG_BACKUP_COUNT = get_positive_int("LOG_BACKUP_COUNT", 5)
LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").strip().upper()

VALID_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

LOG_LEVEL = VALID_LOG_LEVELS.get(LOG_LEVEL_NAME)

if LOG_LEVEL is None:
    print(
        f"Fehler: Ungültiger LOG_LEVEL: {LOG_LEVEL_NAME!r}",
        file=sys.stderr,
    )
    sys.exit(1)

if not APP_TOKEN or APP_TOKEN == "dein_zepp_apptoken":
    print(
        f"Fehler: In {ENV_FILE} ist kein gültiger APP_TOKEN eingetragen.",
        file=sys.stderr,
    )
    sys.exit(1)

if not BASE_URL.startswith(("https://", "http://")):
    print(
        "Fehler: HUAMI_BASE_URL muss mit https:// oder http:// beginnen.",
        file=sys.stderr,
    )
    sys.exit(1)

DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
LOG_PATH.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Logging mit Rotation
# =============================================================================

LOG_FILE = LOG_PATH / "zepp_downloader.log"

logger = logging.getLogger("zepp_downloader")
logger.setLevel(LOG_LEVEL)
logger.propagate = False

log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

rotating_handler = RotatingFileHandler(
    filename=LOG_FILE,
    maxBytes=LOG_MAX_BYTES,
    backupCount=LOG_BACKUP_COUNT,
    encoding="utf-8",
)

rotating_handler.setLevel(LOG_LEVEL)
rotating_handler.setFormatter(log_formatter)
logger.addHandler(rotating_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(LOG_LEVEL)
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)


# =============================================================================
# Sperrdatei gegen parallele Cronjob-Ausführungen
# =============================================================================

LOCK_FILE = SCRIPT_DIR / ".zepp_downloader.lock"
lock_handle = None


def acquire_lock() -> bool:
    """
    Verhindert, dass zwei Cronjob-Ausführungen gleichzeitig laufen.
    """

    global lock_handle

    lock_handle = LOCK_FILE.open("w", encoding="utf-8")

    try:
        fcntl.flock(
            lock_handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        return False

    lock_handle.write(str(os.getpid()))
    lock_handle.flush()

    return True


# =============================================================================
# HTTP-Verbindung
# =============================================================================

http_session = requests.Session()
http_session.headers.update(
    {
        "apptoken": APP_TOKEN,
        "appPlatform": "web",
        "appname": "com.xiaomi.hm.health",
        "Accept": "application/json",
        "User-Agent": "zepp-fit-downloader/1.0",
    }
)


# =============================================================================
# Allgemeine Hilfsfunktionen
# =============================================================================

def safe_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default

    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default

    try:
        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError, OverflowError):
        return default


def sanitize_timestamp(value: Any) -> int:
    """
    Konvertiert Unix-Timestamps bei Bedarf von Millisekunden in Sekunden.
    """

    timestamp = safe_int(value, 0)

    if timestamp > 100_000_000_000:
        timestamp //= 1000

    return timestamp


def split_values(raw_value: Any) -> list:
    """
    Zerlegt semikolongetrennte Telemetriedaten.
    """

    if raw_value is None:
        return []

    if isinstance(raw_value, list):
        return [
            str(item).strip()
            for item in raw_value
            if str(item).strip()
        ]

    raw_string = str(raw_value).strip()

    if not raw_string:
        return []

    return [
        item.strip()
        for item in raw_string.split(";")
        if item.strip()
    ]


def parse_float_list(raw_value: Any) -> list[Optional[float]]:
    result: list[Optional[float]] = []

    for value in split_values(raw_value):
        try:
            parsed_value = float(value)

            if math.isfinite(parsed_value):
                result.append(parsed_value)
            else:
                result.append(None)

        except (TypeError, ValueError):
            result.append(None)

    return result


def parse_int_list(raw_value: Any) -> list[Optional[int]]:
    result: list[Optional[int]] = []

    for value in split_values(raw_value):
        try:
            result.append(int(float(value)))
        except (TypeError, ValueError):
            result.append(None)

    return result


def parse_coordinates(
    raw_value: Any,
) -> list[Optional[tuple[float, float]]]:
    """
    Dekodiert Zepp-GPS-Daten.

    Format:
        latitude,longitude;
        latitude_delta,longitude_delta;
        ...

    Der erste Punkt ist absolut und mit 100.000.000 skaliert.
    Alle weiteren Punkte sind Differenzen zum vorherigen Punkt.

    Rückgabewert:
        Liste aus (longitude, latitude), passend zum FIT-Konverter.
    """

    if raw_value is None:
        return []

    if isinstance(raw_value, list):
        raw_points = [
            "" if item is None else str(item).strip()
            for item in raw_value
        ]
    else:
        raw_string = str(raw_value)

        if not raw_string:
            return []

        raw_points = [
            item.strip()
            for item in raw_string.split(";")
        ]

    result: list[Optional[tuple[float, float]]] = []

    previous_latitude_raw: Optional[int] = None
    previous_longitude_raw: Optional[int] = None

    coordinate_scale = 100_000_000.0

    for point in raw_points:
        if not point or "," not in point:
            result.append(None)
            continue

        parts = point.split(",", 1)

        try:
            latitude_value = int(float(parts[0].strip()))
            longitude_value = int(float(parts[1].strip()))
        except (TypeError, ValueError, OverflowError):
            result.append(None)
            continue

        if (
            previous_latitude_raw is None
            or previous_longitude_raw is None
        ):
            latitude_raw = latitude_value
            longitude_raw = longitude_value
        else:
            latitude_raw = previous_latitude_raw + latitude_value
            longitude_raw = previous_longitude_raw + longitude_value

        latitude = latitude_raw / coordinate_scale
        longitude = longitude_raw / coordinate_scale

        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and -90.0 <= latitude <= 90.0
            and -180.0 <= longitude <= 180.0
        ):
            result.append(None)
            continue

        previous_latitude_raw = latitude_raw
        previous_longitude_raw = longitude_raw

        if latitude == 0.0 and longitude == 0.0:
            result.append(None)
            continue

        result.append((longitude, latitude))

    return result

def map_huami_sport(huami_type: int) -> tuple[Sport, SubSport]:
    """
    Mappt Huami-Aktivitäts-IDs (fokussiert auf Amazfit Stratos 3) 
    auf offizielle Garmin FIT-Sportarten.
    """
    # Entfernt den Multisport-Präfix für Triathlon-Etappen (z.B. 1015 -> 15)
    if 1000 < huami_type < 2000:
        huami_type = huami_type % 1000
    
    mapping: dict[int, tuple[Sport, SubSport]] = {
        # Laufen / Gehen
        1: (Sport.RUNNING, SubSport.GENERIC),        # Laufen (Outdoor)
        6: (Sport.WALKING, SubSport.GENERIC),        # Gehen / Spazieren
        7: (Sport.RUNNING, SubSport.TRAIL),          # Trailrunning
        8: (Sport.RUNNING, SubSport.TREADMILL),      # Laufband
        
        # Radfahren
        9: (Sport.CYCLING, SubSport.GENERIC),        # Radfahren (Outdoor)
        10: (Sport.CYCLING, SubSport.INDOOR_CYCLING),# Indoor Cycling
        
        # Wassersport
        14: (Sport.SWIMMING, SubSport.LAP_SWIMMING), # Beckenschwimmen
        15: (Sport.SWIMMING, SubSport.OPEN_WATER),   # Freiwasserschwimmen
        
        # Outdoor & Wintersport
        13: (Sport.MOUNTAINEERING, SubSport.GENERIC),# Bergsteigen
        17: (Sport.HIKING, SubSport.GENERIC),        # Wandern
        18: (Sport.ALPINE_SKIING, SubSport.GENERIC), # Skifahren
        19: (Sport.CROSS_COUNTRY_SKIING, SubSport.GENERIC), # Skilanglauf
        
        # Fitness / Hallensport
        11: (Sport.FITNESS_EQUIPMENT, SubSport.ELLIPTICAL), # Crosstrainer
        12: (Sport.FITNESS_EQUIPMENT, SubSport.INDOOR_ROWING), # Rudergerät
        24: (Sport.FITNESS_EQUIPMENT, SubSport.GENERIC),    # Indoor Fitness
        27: (Sport.TRAINING, SubSport.YOGA),         # Yoga
        
        # Sonstiges & Multisport
        23: (Sport.TENNIS, SubSport.GENERIC),        # Tennis
        39: (Sport.MULTISPORT, SubSport.GENERIC),    # Triathlon
        
        # Fallbacks (zur Sicherheit)
        16: (Sport.MOUNTAINEERING, SubSport.GENERIC),
        20: (Sport.SNOWBOARDING, SubSport.GENERIC),
    }

    return mapping.get(huami_type, (Sport.GENERIC, SubSport.GENERIC))
    
def get_sport_name(huami_type: int) -> str:
    """
    Gibt den lesbaren Sportart-Namen für das Dateinamens-Schema zurück.
    Spezifisch angepasst auf die Amazfit Stratos 3.
    """
    # Entfernt den Multisport-Präfix für Dateinamen (z.B. 1009 -> 9)
    if 1000 < huami_type < 2000:
        huami_type = huami_type % 1000
    
    mapping: dict[int, str] = {
        1: "running",
        6: "walking",
        7: "trail_running",
        8: "treadmill",
        9: "cycling",
        10: "indoor_cycling",
        11: "elliptical",
        12: "indoor_rowing",
        13: "mountaineering",
        14: "swimming",
        15: "open_water_swimming",
        16: "mountaineering",
        17: "hiking",
        18: "alpine_skiing",
        19: "cross_country_skiing",
        20: "snowboarding",
        23: "tennis",
        24: "indoor_fitness",
        27: "yoga",
        39: "triathlon",
    }

    return mapping.get(huami_type, "generic")
    
def get_summary_start_time(summary_item: dict[str, Any]) -> int:
    """
    Liest den Start-Zeitstempel (Unix in Sekunden) aus dem Summary-Item aus.
    """
    candidates = [
        summary_item.get("start_time"),
        summary_item.get("startTime"),
        summary_item.get("start_timestamp"),
        summary_item.get("startTimestamp"),
        summary_item.get("trackid"),
        summary_item.get("track_id"),
    ]
    for candidate in candidates:
        normalized = sanitize_timestamp(candidate)
        if normalized >= 946684800:
            return normalized
    return int(time.time())

def to_semicircles(degrees: float) -> int:
    return int(round(degrees * ((2**31) / 180.0)))


def build_url(endpoint: str, parameters: dict[str, Any]) -> str:
    return f"{BASE_URL}/{endpoint}?{urlencode(parameters)}"


# =============================================================================
# API-Aufrufe
# =============================================================================

def get_json(
    url: str,
    description: str,
) -> dict[str, Any]:
    """
    Führt einen API-Aufruf mit Wiederholungsversuchen aus.
    """

    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = http_session.get(
                url,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 401:
                raise PermissionError(
                    "HTTP 401 Unauthorized: APP_TOKEN ist ungültig "
                    "oder abgelaufen."
                )

            if response.status_code == 403:
                raise PermissionError(
                    "HTTP 403 Forbidden: Zugriff wurde verweigert. "
                    "Token, API-Region und API-Endpunkt prüfen."
                )

            if response.status_code == 429:
                raise requests.HTTPError(
                    "HTTP 429: Zu viele API-Anfragen.",
                    response=response,
                )

            response.raise_for_status()

            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"API-Antwort für '{description}' ist kein "
                    f"gültiges JSON."
                ) from exc

            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"Unerwartetes Antwortformat für '{description}'."
                )

            return payload

        except PermissionError:
            raise

        except requests.RequestException as exc:
            last_error = exc

            if attempt >= MAX_RETRIES:
                break

            retry_delay = min(2 ** (attempt - 1), 30)

            logger.warning(
                "%s fehlgeschlagen. Versuch %s von %s: %s. "
                "Nächster Versuch nach %s Sekunde(n).",
                description,
                attempt,
                MAX_RETRIES,
                exc,
                retry_delay,
            )

            time.sleep(retry_delay)

    raise RuntimeError(
        f"{description} nach {MAX_RETRIES} Versuch(en) "
        f"fehlgeschlagen: {last_error}"
    )


# =============================================================================
# Zeitstempel
# =============================================================================

def normalize_record_timestamps(
    raw_timestamps: list[Optional[int]],
    start_time: int,
    record_count: int,
) -> list:
    """
    Unterstützt relative Sekunden, Unix-Sekunden und Unix-Millisekunden.
    """

    result: list[int] = []
    last_timestamp = start_time - 1

    for index in range(record_count):
        timestamp_value: Optional[int] = None

        if index < len(raw_timestamps):
            timestamp_value = raw_timestamps[index]

        if timestamp_value is None:
            timestamp = start_time + index
        else:
            normalized = sanitize_timestamp(timestamp_value)

            if normalized < 100_000_000:
                timestamp = start_time + normalized
            else:
                timestamp = normalized

        if timestamp <= last_timestamp:
            timestamp = last_timestamp + 1

        result.append(timestamp)
        last_timestamp = timestamp

    return result


# =============================================================================
# FIT-Konvertierung
# =============================================================================

def convert_to_fit(
    summary_item: dict[str, Any],
    detail_data: dict[str, Any],
    output_file: Path,
) -> None:
    """
    Erstellt eine FIT-Aktivitätsdatei mit File-ID, Records, Session
    und Activity Message.
    """

    # Je nach Zepp-Gerät und Activity-Source fehlt start_time im Summary.
    # Mögliche Startzeitfelder werden deshalb der Reihe nach geprüft.
    start_time_candidates = [
        summary_item.get("start_time"),
        summary_item.get("startTime"),
        summary_item.get("start_timestamp"),
        summary_item.get("startTimestamp"),
        detail_data.get("start_time"),
        detail_data.get("startTime"),
        detail_data.get("start_timestamp"),
        detail_data.get("startTimestamp"),
        summary_item.get("trackid"),
        summary_item.get("track_id"),
    ]

    start_time = 0
    start_time_source = None

    for candidate in start_time_candidates:
        normalized_candidate = sanitize_timestamp(candidate)

        # Plausibler Unix-Zeitstempel ab dem Jahr 2000.
        if normalized_candidate >= 946684800:
            start_time = normalized_candidate
            start_time_source = candidate
            break

    if start_time <= 0:
        raise ValueError(
            "Aktivität besitzt keinen gültigen Startzeitpunkt. "
            f"Verfügbare Summary-Felder: {sorted(summary_item.keys())}; "
            f"verfügbare Detail-Felder: {sorted(detail_data.keys())}"
        )

    logger.debug(
        "Startzeit für Track-ID %s: %s, ermittelt aus Wert %r.",
        summary_item.get("trackid", summary_item.get("track_id")),
        start_time,
        start_time_source,
    )

    total_duration = safe_float(
        summary_item.get(
            "run_time",
            summary_item.get("duration", 0),
        ),
        0.0,
    )

    supplied_end_time = sanitize_timestamp(
        summary_item.get(
            "end_time",
            summary_item.get("endTime", 0),
        )
    )

    if total_duration <= 0 and supplied_end_time > start_time:
        total_duration = float(supplied_end_time - start_time)

    coordinates = parse_coordinates(
        detail_data.get("longitude_latitude", "")
    )

    altitudes = parse_float_list(
        detail_data.get("altitude", "")
    )

    # --- NEU: ROHDATEN-LOGGING (Hier einfügen) ---
    if altitudes:
        logger.info(
            "Rohdaten-Check Höhenwerte für Track %s (erste 10): %s",
            summary_item.get("trackid", summary_item.get("track_id")),
            altitudes[:10],
        )

    # Die Zepp-API liefert bei fehlenden oder ungültigen Höhenmessungen
    # teilweise extreme Platzhalterwerte, beispielsweise -2000000.
    # Nur plausible Höhenwerte werden in die FIT-Datei übernommen.
    altitude_count = len(altitudes)

    valid_altitude_count = sum(
        1
        for altitude in altitudes
        if (
            altitude is not None
            and math.isfinite(altitude)
            and -500.0 <= altitude <= 10000.0
        )
    )

    cleaned_altitudes = []
    valid_altitude_found = False

    for altitude in altitudes:
        # Prüfen, ob der Wert grundsätzlich im erlaubten Bereich liegt
        if altitude is not None and math.isfinite(altitude) and -500.0 <= altitude <= 10000.0:
            # Die 0m-Phase zu Beginn blockieren
            if not valid_altitude_found:
                if altitude != 0.0:
                    valid_altitude_found = True
                    cleaned_altitudes.append(altitude)
                else:
                    # Noch kein echter Fix, wir ersetzen die 0 durch None
                    cleaned_altitudes.append(None)
            else:
                # 0m-Phase ist vorbei, ab jetzt alle validen Werte nehmen
                # (Auch eine echte 0, falls du mal am Strand spazieren gehst)
                cleaned_altitudes.append(altitude)
        else:
            # Wert ist extrem (-2000000) oder None
            cleaned_altitudes.append(None)

    altitude_count = len(altitudes)
    cleaned_altitudes = []
    valid_altitude_found = False

    for raw_alt in altitudes:
        # 1. Ungültige/Extreme Platzhalter (wie -2000000.0 oder None) abfangen
        if raw_alt is None or not math.isfinite(raw_alt) or raw_alt <= -200000.0:
            cleaned_altitudes.append(None)
            continue

        # 2. Umrechnung von Zentimetern in Meter (z.B. 54600.0 -> 546.0m)
        altitude_m = raw_alt / 100.0

        # 3. Plausibilitäts-Check in Metern (-500m bis 10.000m)
        if -500.0 <= altitude_m <= 10000.0:
            # Die 0m-Phase zu Beginn blockieren
            if not valid_altitude_found:
                if altitude_m != 0.0:
                    valid_altitude_found = True
                    cleaned_altitudes.append(altitude_m)
                else:
                    cleaned_altitudes.append(None)
            else:
                cleaned_altitudes.append(altitude_m)
        else:
            cleaned_altitudes.append(None)

    altitudes = cleaned_altitudes

    # Zählt die verbliebenen echten Höhenwerte für das Log
    valid_altitude_count = sum(1 for a in altitudes if a is not None)
    invalid_altitude_count = altitude_count - valid_altitude_count

    # --- Hier beginnt wieder dein originaler Logging-Teil ---
    if invalid_altitude_count:
        logger.info(
            "%s von %s Höhenwerten bei Track-ID %s "
            "als ungültig verworfen.",
            invalid_altitude_count,
            altitude_count,
            summary_item.get(
                "trackid",
                summary_item.get("track_id"),
            ),
        )

    # Wenn kein einziger gültiger Höhenwert vorhanden ist, wird die
    # Höhenserie vollständig entfernt. GPS, Zeit und Herzfrequenz
    # bleiben davon unberührt.
    if valid_altitude_count == 0:
        altitudes = []

        logger.info(
            "Keine gültigen Höhenwerte bei Track-ID %s vorhanden. "
            "FIT-Datei wird ohne Höhenangaben erstellt.",
            summary_item.get(
                "trackid",
                summary_item.get("track_id"),
            ),
        )

    heart_rates = parse_int_list(
        detail_data.get("heart_rate", "")
    )

    raw_timestamps = parse_int_list(
        detail_data.get("time", "")
    )

    record_count = max(
        len(coordinates),
        len(altitudes),
        len(heart_rates),
        len(raw_timestamps),
    )

    if record_count == 0:
        record_count = 2 if total_duration > 0 else 1

    timestamps = normalize_record_timestamps(
        raw_timestamps=raw_timestamps,
        start_time=start_time,
        record_count=record_count,
    )

    if total_duration <= 0:
        total_duration = float(
            max(1, timestamps[-1] - start_time)
        )

    calculated_end_time = int(start_time + total_duration)
    end_time = max(calculated_end_time, timestamps[-1])

    if end_time > calculated_end_time:
        total_duration = float(end_time - start_time)

    total_distance = safe_float(
        summary_item.get(
            "dis",
            summary_item.get("distance", 0),
        ),
        0.0,
    )

    huami_sport_type = safe_int(
        summary_item.get(
            "type",
            summary_item.get("sport_type", 0),
        ),
        0,
    )

    sport, sub_sport = map_huami_sport(huami_sport_type)

    if sport == Sport.GENERIC:
        logger.warning(
            "Unbekannte Huami-Sportart-ID '%s' bei Track-ID %s. "
            "Die Aktivität wird als 'Generisch' exportiert.",
            huami_sport_type,
            summary_item.get("trackid", summary_item.get("track_id"))
        )

    builder = FitFileBuilder()

    # Dynamisches Auslesen des Gerätenamens aus dem JSON-Payload
    raw_device_name = str(
        summary_item.get("device_name") or 
        summary_item.get("deviceName") or 
        detail_data.get("device_name") or 
        detail_data.get("deviceName") or 
        summary_item.get("device_model") or
        summary_item.get("deviceModel") or
        ""
    ).strip()

    # Falls kein Name geliefert wird oder nur der Server-String (z.B. run.411.huami.com) enthalten ist:
    if not raw_device_name or "huami.com" in raw_device_name.lower():
        dynamic_device_name = "Amazfit Stratos 3"
    else:
        dynamic_device_name = raw_device_name

    # 1. File ID mit Huami-Hersteller-ID (294)
    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = 294 
    file_id.time_created = start_time * 1000
    builder.add(file_id)

    # 2. Dynamische Geräteinformationen hinzufügen
    device_info = DeviceInfoMessage()
    device_info.timestamp = start_time * 1000
    device_info.manufacturer = 294
    device_info.product_name = dynamic_device_name
    builder.add(device_info)

    for index in range(record_count):
        record = RecordMessage()
        record.timestamp = timestamps[index] * 1000

        if index < len(coordinates):
            coordinate = coordinates[index]

            if coordinate is not None:
                longitude, latitude = coordinate
                # fit-tool übernimmt die FIT-Semicircle-Kodierung.
                # Deshalb Dezimalgrad direkt an den Setter übergeben.
                record.position_lat = latitude
                record.position_long = longitude

        if index < len(altitudes):
            altitude = altitudes[index]

            if altitude is not None:
                try:
                    record.altitude = altitude
                except Exception as exc:
                    logger.warning(
                        "Höhenwert bei Track-ID %s, Record %s "
                        "konnte nicht kodiert werden und wird "
                        "übersprungen: Wert=%r, Fehler=%s",
                        summary_item.get(
                            "trackid",
                            summary_item.get("track_id"),
                        ),
                        index,
                        altitude,
                        exc,
                    )

        if index < len(heart_rates):
            heart_rate = heart_rates[index]

            if heart_rate is not None and 0 < heart_rate <= 255:
                record.heart_rate = heart_rate

        builder.add(record)

    fit_session = SessionMessage()
    fit_session.timestamp = end_time * 1000
    fit_session.start_time = start_time * 1000
    fit_session.total_elapsed_time = total_duration
    fit_session.total_timer_time = total_duration
    fit_session.total_distance = max(0.0, total_distance)
    fit_session.sport = sport
    fit_session.sub_sport = sub_sport

    calories = safe_int(
        summary_item.get(
            "calorie",
            summary_item.get("calories", 0),
        ),
        0,
    )

    if calories > 0:
        fit_session.total_calories = calories

    average_heart_rate = safe_int(
        summary_item.get(
            "avg_hr",
            summary_item.get("avg_heart_rate", 0),
        ),
        0,
    )

    if 0 < average_heart_rate <= 255:
        fit_session.avg_heart_rate = average_heart_rate

    maximum_heart_rate = safe_int(
        summary_item.get(
            "max_hr",
            summary_item.get("max_heart_rate", 0),
        ),
        0,
    )

    if 0 < maximum_heart_rate <= 255:
        fit_session.max_heart_rate = maximum_heart_rate

    builder.add(fit_session)

    activity = ActivityMessage()
    activity.timestamp = end_time * 1000
    activity.num_sessions = 1
    builder.add(activity)

    temporary_file = output_file.with_suffix(".fit.tmp")

    try:
        temporary_file.unlink(missing_ok=True)

        fit_file = builder.build()
        fit_file.to_file(str(temporary_file))

        if not temporary_file.is_file():
            raise RuntimeError(
                "Temporäre FIT-Datei wurde nicht erstellt."
            )

        if temporary_file.stat().st_size == 0:
            raise RuntimeError(
                "Die erstellte FIT-Datei ist leer."
            )

        temporary_file.replace(output_file)

    except Exception:
        temporary_file.unlink(missing_ok=True)
        raise


# =============================================================================
# Hauptprogramm
# =============================================================================

def main() -> int:
    if not acquire_lock():
        logger.warning(
            "Eine andere Instanz läuft bereits. "
            "Dieser Cronjob-Lauf wird beendet."
        )
        return 0

    # --- NEU: Lade die Historie in den Arbeitsspeicher ---
    downloaded_tracks = load_history()

    logger.info(
        "Starte Zepp-Sync. Prüfe die letzten %s Aktivitäten.",
        FETCH_LIMIT,
    )

    history_url = build_url(
        "history.json",
        {
            "source": ACTIVITY_SOURCE,
            "limit": FETCH_LIMIT,
        },
    )

    try:
        history_response = get_json(
            history_url,
            "Abruf der Aktivitätshistorie",
        )
    except PermissionError as exc:
        logger.error("%s", exc)
        return 1
    except Exception as exc:
        logger.exception(
            "Fehler beim Abrufen der Historie: %s",
            exc,
        )
        return 1

    response_data = history_response.get("data", {})

    if not isinstance(response_data, dict):
        logger.error(
            "Die API-Antwort enthält kein gültiges data-Objekt."
        )
        return 1

    summary = response_data.get("summary", [])

    if not isinstance(summary, list):
        logger.error(
            "Die API-Antwort enthält keine gültige summary-Liste."
        )
        return 1

    if not summary:
        logger.info(
            "Keine Aktivitäten in der Zepp-Cloud gefunden."
        )
        return 0

    logger.info(
        "%s Aktivität(en) von der API geliefert.",
        len(summary),
    )

    created_count = 0
    skipped_count = 0
    error_count = 0

    for item in summary:
        if not isinstance(item, dict):
            logger.warning(
                "Ungültiger Aktivitätseintrag wird übersprungen."
            )
            error_count += 1
            continue

        track_id_value = item.get(
            "trackid",
            item.get("track_id"),
        )

        if (
            track_id_value is None
            or not str(track_id_value).strip()
        ):
            logger.warning(
                "Aktivität ohne Track-ID wird übersprungen."
            )
            error_count += 1
            continue

        track_id = str(track_id_value).strip()

        safe_track_id = "".join(
            character
            for character in track_id
            if character.isalnum() or character in ("-", "_")
        )

        if not safe_track_id:
            logger.warning(
                "Track-ID %r kann nicht als Dateiname verwendet werden.",
                track_id,
            )
            error_count += 1
            continue

        # --- NEU ---
        huami_sport_type = safe_int(
            item.get("type", item.get("sport_type", 0)), 0
        )
        sport_name = get_sport_name(huami_sport_type)
        start_timestamp = get_summary_start_time(item)

        # Konvertiert den Unix-Zeitstempel in das Format YYYYMMDDhhmmss
        formatted_time = datetime.fromtimestamp(start_timestamp).strftime("%Y%m%d%H%M%S")

        # Schema: [yyyymmddhhmmss]_[sport]_[id].fit
        filename = f"{formatted_time}_{sport_name}_{safe_track_id}.fit"
        filepath = DOWNLOAD_PATH / filename

        # Prüft, ob eine Datei mit dieser Track-ID existiert
        if safe_track_id in downloaded_tracks:
            logger.info(
                "Übersprungen: Track-ID %s ist laut Log bereits heruntergeladen.",
                track_id,
            )
            skipped_count += 1
            continue

        if filepath.exists():
            logger.warning(
                "Leere oder ungültige Zieldatei wird neu erstellt: %s",
                filepath,
            )
            filepath.unlink()

        logger.info(
            "Neue Aktivität gefunden. "
            "Lade Details für Track-ID %s.",
            track_id,
        )

        activity_source = str(
            item.get("source", ACTIVITY_SOURCE)
        ).strip()

        detail_url = build_url(
            "detail.json",
            {
                "trackid": track_id,
                "source": activity_source,
            },
        )

        try:
            if REQUEST_DELAY > 0:
                time.sleep(REQUEST_DELAY)

            detail_response = get_json(
                detail_url,
                f"Detailabruf für Track-ID {track_id}",
            )

            detail_data = detail_response.get("data", {})

            if not isinstance(detail_data, dict):
                raise RuntimeError(
                    "Detailantwort enthält kein gültiges data-Objekt."
                )

            convert_to_fit(
                summary_item=item,
                detail_data=detail_data,
                output_file=filepath,
            )

            logger.info(
                "FIT-Datei erfolgreich erstellt: %s",
                filepath,
            )

            created_count += 1
                  
            # --- NEU: In die History eintragen ---
            save_to_history(safe_track_id)
            downloaded_tracks.add(safe_track_id)

        except PermissionError as exc:
            logger.error("%s", exc)
            return 1

        except PermissionError as exc:
            logger.error("%s", exc)
            return 1

        except Exception as exc:
            error_count += 1

            logger.exception(
                "Fehler bei Track-ID %s: %s",
                track_id,
                exc,
            )

    logger.info(
        "Sync abgeschlossen. Erstellt: %s, "
        "übersprungen: %s, Fehler: %s.",
        created_count,
        skipped_count,
        error_count,
    )

    return 2 if error_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
