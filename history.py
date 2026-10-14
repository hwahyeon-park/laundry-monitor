"""사용자가 요청할 때만 Table 기록 조회. 실시간 상태나 Blob은 건드리지 않는다."""
import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import quote

from flask import jsonify, request

log = logging.getLogger(__name__)
ALLOWED_MINUTES = (5, 30, 60)
MAX_ROWS = 2000


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def numeric(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid number")
    return float(value)


def sequence(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError("invalid sequence")
    return str(value)


def history_source(env):
    explicit = env.get("HISTORY_SOURCE") or env.get("EVENTHUB_NAME")
    if explicit:
        return explicit
    parts = dict(part.split("=", 1) for part in env.get("EVENTHUB_CONNECTION_STRING", "").split(";") if "=" in part)
    return parts.get("EntityPath")


@lru_cache(maxsize=1)
def table_client():
    from azure.data.tables import TableClient
    from azure.identity import AzureCliCredential, ManagedIdentityCredential
    # 로컬은 az login, Azure 웹앱은 system-assigned Managed Identity.
    credential = ManagedIdentityCredential() if os.getenv("WEBSITE_HOSTNAME") else AzureCliCredential()
    return TableClient(
        endpoint=os.environ["TABLE_ACCOUNT_URL"],
        table_name=os.environ.get("TABLE_NAME", "LaundryTelemetry"),
        credential=credential,
        connection_timeout=5, read_timeout=10, retry_total=1,
    )


def row_point(row):
    if row.get("SchemaVersion") != 1:
        raise ValueError("unknown summary schema")
    if type(row["SampleCount"]) is not int or row["SampleCount"] < 1:
        raise ValueError("invalid sample count")
    stamp = parse_time(row["ReceivedAt"])
    values = [numeric(row[key]) for key in ("XMean", "YMean", "ZMean", "VibrationRms")]
    if values[3] < 0:
        raise ValueError("negative RMS")
    return {
        "deviceId": row["DeviceId"], "sequenceNumber": sequence(row["SequenceNumber"]),
        "enqueuedTime": iso(stamp), "values": values,
        "temperature": numeric(row["Temperature"]), "pressure": numeric(row["Pressure"]),
        "origin": "history",
    }


def live_point(record):
    payload = record["payload"]
    samples = payload["samples"]
    if not samples:
        raise ValueError("empty batch")
    values = [[numeric(sample[axis]) for sample in samples] for axis in "xyz"]
    means = [math.fsum(axis) / len(samples) for axis in values]
    rms = math.sqrt(math.fsum(math.fsum((v-m) ** 2 for v in axis) / len(samples)
                              for axis, m in zip(values, means)))
    return {"deviceId": record["deviceId"], "sequenceNumber": sequence(record["sequenceNumber"]),
            "enqueuedTime": iso(parse_time(record["enqueuedTime"])), "values": means + [numeric(rms)],
            "temperature": numeric(payload["temperature"]), "pressure": numeric(payload["pressure"]),
            "origin": "live"}


def point_key(point):
    return (point["deviceId"], point["sequenceNumber"], point["enqueuedTime"])


class HistoryStore:
    def __init__(self, get_client, env, clock):
        self.get_client, self.env, self.clock = get_client, env, clock
        self.cache = {}
        self.lock = threading.Lock()

    def read(self, minutes):
        device = self.env.get("DEVICE_ID")
        source = history_source(self.env)
        if not device or not source:
            raise ValueError("history device/source not configured")
        # 여러 브라우저가 동시에 열려도 같은 범위 조회는 8초간 공유.
        with self.lock:
            cached = self.cache.get(minutes)
            if cached and time.monotonic() - cached[0] < 8:
                return cached[1]
            end = self.clock()
            start = end - timedelta(minutes=minutes)
            day = start.date()
            client = self.get_client()
            points, skipped, inspected = [], 0, 0
            while day <= end.date():
                params = {
                    "pk": quote(device, safe="") + "|" + day.strftime("%Y%m%d"),
                    "low": start.strftime("%Y%m%dT%H%M%S%fZ"),
                    "high": end.strftime("%Y%m%dT%H%M%S%fZ") + "~",
                    "source": source,
                }
                rows = client.query_entities(
                    query_filter="PartitionKey eq @pk and RowKey ge @low and RowKey le @high and Source eq @source",
                    parameters=params, results_per_page=500,
                    select=["SchemaVersion", "DeviceId", "SequenceNumber", "ReceivedAt", "SampleCount",
                            "XMean", "YMean", "ZMean", "VibrationRms", "Temperature", "Pressure"],
                )
                # SDK의 페이지 iterator를 끝까지 순회. 첫 페이지만 사용하지 않는다.
                for row in rows:
                    inspected += 1
                    if inspected > MAX_ROWS:
                        raise OverflowError("too many rows; choose shorter range")
                    try:
                        point = row_point(row)
                        if point["deviceId"] != device or not start <= parse_time(point["enqueuedTime"]) <= end:
                            raise ValueError("wrong device/time")
                        points.append(point)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        skipped += 1
                day += timedelta(days=1)
            result = (points, skipped)
            self.cache[minutes] = (time.monotonic(), result)
            return result


def install_history(app, get_client=table_client, clock=utc_now, env=None):
    env = os.environ if env is None else env
    store = HistoryStore(get_client, env, clock)
    app.extensions["history"] = store

    @app.get("/api/history")
    def history():
        try:
            minutes = int(request.args.get("minutes", "30"))
            if minutes not in ALLOWED_MINUTES:
                raise ValueError()
        except ValueError:
            return jsonify({"error": "minutes must be 5, 30 or 60"}), 400

        stored, skipped, error, http_status = [], 0, None, 200
        try:
            stored, skipped = store.read(minutes)
        except OverflowError:
            error, http_status = "too_many_records", 422
        except Exception as exc:
            # 예외 원문/연결 문자열 대신 오류 종류만 기록.
            log.warning("History query failed: %s", type(exc).__name__)
            error, http_status = "history_unavailable", 503

        # Function 저장 지연 동안 서버가 이미 받은 최근 메시지로 끝부분을 보완.
        # broker.publish()/상태 갱신은 호출하지 않는다.
        broker = app.extensions["realtime"]["broker"]
        with broker.condition:
            records = [record for _, record in broker.events]
        merged = {point_key(point): point for point in stored}
        for record in records:
            try:
                if record.get("deviceId") == env.get("DEVICE_ID"):
                    point = live_point(record)
                    merged[point_key(point)] = point
            except (ValueError, KeyError, TypeError, OverflowError):
                continue

        end = clock()
        start = end - timedelta(minutes=minutes)
        points = sorted((p for p in merged.values() if start <= parse_time(p["enqueuedTime"]) <= end),
                        key=lambda p: (parse_time(p["enqueuedTime"]), p["sequenceNumber"]))
        response = jsonify({"points": points, "minutes": minutes, "serverTime": iso(end),
                            "from": iso(start), "to": iso(end), "error": error,
                            "skippedRows": skipped, "tablePointCount": len(stored)})
        response.headers["Cache-Control"] = "no-store"
        return response, http_status
