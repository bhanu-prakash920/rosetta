"""Create the telemetry bucket in the S3-compatible store, then prove it is writable.

Run by the `minio-init` service of docker-compose.yml inside the Rosetta image,
so it uses the same client library (pyarrow) and the same settings
(ROSETTA_S3_ENDPOINT, ROSETTA_S3_BUCKET, ROSETTA_S3_ACCESS_KEY,
ROSETTA_S3_SECRET_KEY) as the application. If this job succeeds, the archive
writer of the processor can reach the bucket with the credentials it was given.

It waits for the store to come up (BUCKET_WAIT_S, default 120 seconds), which is
why the MinIO container needs no health check of its own. It is idempotent.

On a cloud provider the bucket is created by Terraform, not by this script.
"""
from __future__ import annotations

import os
import sys
import time

from pyarrow import fs

from rosetta.config import get_settings


def connect() -> tuple[fs.S3FileSystem, str]:
    st = get_settings()
    scheme, _, host = st.s3_endpoint.partition("://")
    s3 = fs.S3FileSystem(endpoint_override=host or st.s3_endpoint, scheme=scheme or "https",
                         access_key=st.s3_access_key, secret_key=st.s3_secret_key,
                         allow_bucket_creation=True, connect_timeout=3, request_timeout=10,
                         retry_strategy=fs.AwsStandardS3RetryStrategy(max_attempts=1))
    return s3, st.s3_bucket


def ensure(s3: fs.S3FileSystem, bucket: str) -> str:
    created = s3.get_file_info(bucket).type == fs.FileType.NotFound
    if created:
        s3.create_dir(bucket)
    # The call the application makes when it opens the archive (ParquetArchive.__init__).
    s3.create_dir(f"{bucket}/telemetry", recursive=True)
    return "created" if created else "already there"


def main() -> int:
    wait_s = float(os.environ.get("BUCKET_WAIT_S", "120"))
    deadline = time.monotonic() + wait_s
    while True:
        try:
            s3, bucket = connect()
            print(f"bucket {bucket}: {ensure(s3, bucket)}, writable")
            return 0
        except Exception as e:
            if time.monotonic() >= deadline:
                print(f"object store not usable after {wait_s:.0f}s: {type(e).__name__}: {str(e)[:300]}",
                      file=sys.stderr)
                return 1
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
