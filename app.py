import io
import json
import os
from functools import lru_cache

from flask import Flask, jsonify, render_template
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from fastavro import reader
from realtime import install_realtime
from notifications import install_notifications
from cycle_detector import install_detector
from history import install_history


from local_settings import load_local_settings

load_local_settings()

app = Flask(__name__)
consumer = install_realtime(app)
install_history(app)
notifications = install_notifications(app)
detector = install_detector(app, notifications, consumer)

STORAGE_ACCOUNT = os.getenv("STORAGE_ACCOUNT", "testresourcegroupb5d7")
CONTAINER_NAME = os.getenv("STORAGE_CONTAINER", "telemetry")

ACCOUNT_URL = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"

@lru_cache(maxsize=1)
def get_archive_container():
    # Called only by the explicit history API; the live path never reads Blob.
    blob_service = BlobServiceClient(
        account_url=ACCOUNT_URL, credential=DefaultAzureCredential()
    )
    return blob_service.get_container_client(CONTAINER_NAME)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/history/latest")
def telemetry_history():
    container_client = get_archive_container()

    # 1. Container 안의 AVRO 파일 찾기
    blobs = [
        blob
        for blob in container_client.list_blobs()
        if blob.name.endswith(".avro")
    ]

    if not blobs:
        return jsonify({"error": "No AVRO files found"}), 404

    # 2. 최근 수정된 AVRO 파일부터 정렬
    blobs.sort(
        key=lambda blob: blob.last_modified,
        reverse=True
    )

    # 최근 파일 5개만 확인
    recent_blobs = blobs[:5]

    records = []

    # 3. 최근 AVRO 파일들을 모두 읽기
    for blob in recent_blobs:

        print("Reading:", blob.name)

        blob_client = container_client.get_blob_client(blob.name)
        avro_data = blob_client.download_blob().readall()

        avro_file = io.BytesIO(avro_data)

        for record in reader(avro_file):

            body = record.get("Body")

            if body is None:
                continue

            try:
                if isinstance(body, bytes):
                    body = body.decode("utf-8")
                payload = json.loads(body)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                continue

            enqueued_time = record.get("EnqueuedTimeUtc")

            if enqueued_time is None:
                continue

            records.append({
                "enqueuedTime": enqueued_time,
                "payload": payload,
                "blob": blob.name
            })

    if not records:
        return jsonify({"error": "No telemetry records found"}), 404

    # 4. 실제 telemetry 시간 기준으로 정렬
    records.sort(
        key=lambda record: record["enqueuedTime"]
    )

    # 가장 최신 record
    latest_record = records[-1]

    print(
        "Latest telemetry:",
        latest_record["enqueuedTime"],
        "from",
        latest_record["blob"]
    )

    return jsonify({
        "blob": latest_record["blob"],
        "records": records
    })


if __name__ == "__main__":
    consumer.start()
    app.run(
        host="0.0.0.0",
        port=5001,
        debug=False,
        threaded=True,
        use_reloader=False
    )