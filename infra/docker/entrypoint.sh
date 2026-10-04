#!/bin/sh
# Role selector for the Rosetta image. One image, the first argument picks the service.
#
#   api                      uvicorn rosetta.api.main:app --host 0.0.0.0 --port 8000
#   gateway                  python -m rosetta.pipeline.mqtt_gateway
#   normalizer               python -m rosetta.pipeline.normalizer_worker <id> 1
#   processor                python -m rosetta.pipeline.processor <id> 1
#   dlq                      python -m rosetta.pipeline.dlq
#   simulator [shard shards] python -m rosetta.simulator.run <shard> <shards>
#   migrate                  create the relational schema (SQLAlchemy create_all), nothing else
#   seed [args]              python -m rosetta seed --vehicles $ROSETTA_VEHICLES
#   anything else            executed as given
#
# Worker id. With Kafka the consumer group assigns partitions, so the id does not
# select partitions. It still names the checkpoint file, the Parquet writer and
# the metrics snapshot, and two replicas with the same id would overwrite each
# other's lag in the API's aggregation. Replicas of one Deployment (or of one
# Compose service) start with identical arguments, so the id is derived from the
# host name, which is unique per replica and stable across container restarts.
# ROSETTA_WORKER_ID overrides it. It is read here only, not by the application.
set -eu

worker_id() {
    if [ -n "${ROSETTA_WORKER_ID:-}" ]; then
        printf '%s' "$ROSETTA_WORKER_ID"
        return
    fi
    crc=$(hostname | cksum | cut -d ' ' -f 1)
    printf '%s' "$((crc % 1000000))"
}

role="${1:-api}"
[ "$#" -gt 0 ] && shift

case "$role" in
    api)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-api}"
        exec uvicorn rosetta.api.main:app --host 0.0.0.0 --port "${ROSETTA_PORT:-8000}" "$@"
        ;;
    gateway)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-gateway}"
        exec python -m rosetta.pipeline.mqtt_gateway "$@"
        ;;
    normalizer)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-normalizer}"
        if [ "$#" -eq 0 ]; then set -- "$(worker_id)" 1; fi
        exec python -m rosetta.pipeline.normalizer_worker "$@"
        ;;
    processor)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-processor}"
        if [ "$#" -eq 0 ]; then set -- "$(worker_id)" 1; fi
        exec python -m rosetta.pipeline.processor "$@"
        ;;
    dlq)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-dlq}"
        exec python -m rosetta.pipeline.dlq "$@"
        ;;
    simulator)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-simulator}"
        if [ "$#" -eq 0 ]; then set -- 0 1; fi
        exec python -m rosetta.simulator.run "$@"
        ;;
    migrate)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-migrate}"
        exec python -m rosetta migrate
        ;;
    seed)
        export ROSETTA_SERVICE="${ROSETTA_SERVICE:-seed}"
        if [ "$#" -eq 0 ]; then set -- --vehicles "${ROSETTA_VEHICLES:-100000}"; fi
        exec python -m rosetta seed "$@"
        ;;
    *)
        exec "$role" "$@"
        ;;
esac
