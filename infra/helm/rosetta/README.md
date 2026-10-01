# Rosetta Helm chart

Deploys the Rosetta services on any conformant Kubernetes cluster (1.27 or newer).
The chart has no cloud-specific resource in it.

This chart has not been installed on a cluster, and it has not been rendered by
Helm: Helm was not available where it was written. `infra/VERIFICATION.md`
lists what was checked instead and what remains to be checked.

## What is installed

| Kind | Name | Notes |
|---|---|---|
| Deployment | `api` | 2 to 6 replicas (HPA), serves the REST API, the web UI and `/metrics` |
| Deployment | `gateway` | 1 replica, MQTT to Kafka |
| Deployment | `normalizer` | 2 to 16 replicas (HPA), consumer group `normalizer` |
| Deployment | `processor` | 2 replicas, consumer group `processor` |
| Deployment | `dlq` | 1 replica |
| Job (hook) | `kafka-topics` | creates the topics with the configured replication factor |
| Job (hook) | `seed` | schema, then `files/postgres_extras.sql`, then the synthetic fleet |
| Service, Ingress | `api` | |
| HorizontalPodAutoscaler | `api`, `normalizer` | CPU utilisation |
| PodDisruptionBudget | `api`, `normalizer`, `processor` | `minAvailable: 1` |
| NetworkPolicy | `default`, `api` | deny by default, explicit egress |
| ConfigMap | `config` | every non-secret setting |
| ServiceAccount | | token not mounted |

## External dependencies

Nothing stateful is installed by this chart. Kafka, PostgreSQL, Redis, the
object store and the MQTT broker are managed services (or operators you run
yourself), and the chart is told where they are.

| Dependency | Setting in `config` | Key in the Secret |
|---|---|---|
| Kafka | `ROSETTA_KAFKA_BOOTSTRAP` | none |
| PostgreSQL 16 with pgvector, TimescaleDB optional | none | `ROSETTA_DATABASE_URL` |
| Redis 7 | none | `ROSETTA_REDIS_URL` |
| S3-compatible object store | `ROSETTA_S3_ENDPOINT`, `ROSETTA_S3_BUCKET` | `ROSETTA_S3_ACCESS_KEY`, `ROSETTA_S3_SECRET_KEY` |
| MQTT broker with mTLS | `ROSETTA_MQTT_URL` | the Secret named in `mqtt.tls.existingSecret` |

## The Secret

The chart refers to a Secret and never creates one, so that no credential is
ever written into a values file or into the release history of Helm.

Keys, all of them become environment variables:

| Key | Required | Example shape |
|---|---|---|
| `ROSETTA_DATABASE_URL` | yes | `postgresql+psycopg2://USER:PASSWORD@HOST:5432/rosetta?sslmode=require` |
| `ROSETTA_REDIS_URL` | yes | `rediss://:PASSWORD@HOST:6379/0` |
| `ROSETTA_JWT_SECRET` | yes | 32 characters or more, `openssl rand -hex 32` |
| `ROSETTA_S3_ACCESS_KEY` | yes | |
| `ROSETTA_S3_SECRET_KEY` | yes | |
| `ROSETTA_DEMO_PASSWORD` | no | password of the demo accounts created by the seed job |
| `ANTHROPIC_API_KEY` | no | the mapping agent falls back to its deterministic workflow without it |

In production, fill it from your secret manager (External Secrets Operator,
Secrets Store CSI driver, Sealed Secrets). By hand, for a test cluster:

```sh
kubectl -n rosetta create secret generic rosetta-secrets \
  --from-literal=ROSETTA_DATABASE_URL='postgresql+psycopg2://rosetta:PASSWORD@HOST:5432/rosetta?sslmode=require' \
  --from-literal=ROSETTA_REDIS_URL='rediss://:PASSWORD@HOST:6379/0' \
  --from-literal=ROSETTA_JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=ROSETTA_S3_ACCESS_KEY='ACCESS_KEY' \
  --from-literal=ROSETTA_S3_SECRET_KEY='SECRET_KEY'
```

Passwords inside the two URLs must be URL-encoded. Hexadecimal passwords need
no encoding.

The client certificate of the gateway is a second Secret with the keys
`ca.crt`, `tls.crt` and `tls.key` (the layout cert-manager writes).

## Install

```sh
helm lint infra/helm/rosetta
helm upgrade --install rosetta infra/helm/rosetta \
  --namespace rosetta --create-namespace \
  --set image.repository=registry.example.com/rosetta/rosetta \
  --set image.tag=1.0.0 \
  --set config.ROSETTA_KAFKA_BOOTSTRAP=kafka-1.example.internal:9092 \
  --set config.ROSETTA_S3_ENDPOINT=https://s3.eu-west-1.amazonaws.com \
  --set 'ingress.hosts[0].host=rosetta.example.com'
```

## Order of start-up

1. hook, weight -10: `kafka-topics` creates the topics
2. hook, weight -5: the ConfigMaps of the seed job
3. hook, weight 0: `seed` (init `migrate`, init `db-extras`, then `seed`)
4. the Deployments. `api`, `normalizer`, `processor` and `dlq` have an init
   container that waits until the database is seeded, so the order also holds
   when the hooks are skipped (`helm template | kubectl apply`, `--no-hooks`).

## Decisions worth knowing

**Worker ids.** Replicas of a Deployment start with identical arguments. The
entrypoint of the image derives the worker id from the host name
(`infra/docker/entrypoint.sh`), so every replica has its own checkpoint file,
Parquet writer id and metrics identity.

**The normaliser's checkpoint is an `emptyDir`.** The root file system is
read-only. `ROSETTA_DATA_DIR` is an `emptyDir`, which survives a restart of the
container and is lost with the pod. That is safe: offsets live in Kafka, a new
replica repeats at most the records since the last commit, the processor drops
repeats, and late duplicates are caught by the exact store in Redis.

**Gateway: one replica, `Recreate`.** See "Known limits".

**Kafka topics are created by a job.** The application would create them with
replication factor 1.

**Probes.** Only the API has a health endpoint. It answers 200 with status
`degraded` when a dependency is down, so readiness runs
`rosetta-healthcheck api`, which reads the body. The normaliser is probed
through the age of its checkpoint. For the gateway, the processor and the
dead-letter worker the probe can only tell that the process exists.

## Known limits

These come from the application, not from the chart. Each one is described in
`infra/VERIFICATION.md`.

- The gateway cannot run with more than one replica (fixed MQTT client id per container).
- The Kafka client has no TLS or SASL settings, so the brokers must offer a plain-text listener inside the private network.
- The object store client needs static access keys. Workload identity (IRSA, GKE Workload Identity) is not used by the application.
- `/metrics` needs no authentication. The NetworkPolicy limits who can reach the pod, but the Ingress forwards every path. Block `/metrics` at the ingress controller.
- With more than one API replica, every replica holds its own counters. Aggregate with `max`, not `sum`, across replicas.
- The DNS rule of the NetworkPolicy allows port 53 to `kube-system`. A cluster with NodeLocal DNSCache needs its link-local address added to `networkPolicy.egress`.

## Keeping `files/postgres_extras.sql` in step

Helm can only read files inside the chart, so `files/postgres_extras.sql` is a
copy of `infra/sql/postgres_extras.sql`. `make helm-sync` copies it, and the CI
fails when the two differ.
