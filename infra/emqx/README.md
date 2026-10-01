# EMQX: device intake over MQTT with mutual TLS

Vehicles, or the clouds of their makers, publish to

```
oem/<source>/<device>/telemetry          for example  oem/helix/hx-0000012/telemetry
```

and the Rosetta gateway reads all of it through the shared subscription
`$share/gateway/oem/+/+/telemetry` (`rosetta/pipeline/mqtt_gateway.py`).

| File | Purpose |
|---|---|
| `emqx.conf` | EMQX 5.8 configuration: TLS listener on 8883 that requires a client certificate, no plain-text listener, identity from the certificate, deny by default |
| `acl.conf` | who may publish and subscribe where |
| `docker-compose.mtls.yml` | Compose overlay that mounts both files and the certificates |
| `certs/` | created by `scripts/make_certs.sh`, ignored by git, never committed |

**Not tested.** EMQX has not been started with these files: no Docker daemon
was available where they were written. What was tested is the certificates
themselves, with a TLS server and a client built the way the gateway builds
its TLS context. See `infra/VERIFICATION.md`.

## Use

```sh
make certs          # scripts/make_certs.sh gateway simulator
docker compose -f docker-compose.yml -f infra/emqx/docker-compose.mtls.yml up --build

# a certificate for one device
scripts/make_certs.sh helix.hx-0000012
```

Without the overlay the Compose stack runs EMQX with its default
configuration: a plain-text listener on 1883 that is reachable only inside the
Compose network and is not published on the host. That is acceptable on a
laptop and nowhere else.

## The listener

```hocon
listeners.ssl.default {
  bind = "0.0.0.0:8883"
  ssl_options {
    cacertfile = ".../ca.pem"
    certfile   = ".../server.pem"
    keyfile    = ".../server.key"
    verify = verify_peer             # ask the client for a certificate
    fail_if_no_peer_cert = true      # and refuse the handshake without one
    versions = ["tlsv1.3", "tlsv1.2"]
  }
}
```

Both sides prove who they are. The client checks the server certificate
against the CA and against the host name it connected to, which is why the
server certificate carries the names `emqx`, `emqx.rosetta.internal`,
`localhost` and `127.0.0.1`. The gateway and the simulator accept TLS 1.3 only.

## The access rule: a certificate is bound to one topic

The point of mTLS here is not only encryption. It is that a device cannot
speak for another device.

1. **The identity comes from the certificate, not from the client.**
   `mqtt.peer_cert_as_username = cn` replaces whatever user name the client
   sends with the common name (CN) of its certificate.
2. **The common name of a device certificate is `<source>.<device>`**, for
   example `helix.hx-0000012`. `mqtt.client_attrs_init` splits it at the dot
   into two client attributes, `source` and `device`. Source keys contain no
   dot, device ids contain hyphens, so the dot is unambiguous.
3. **The rule uses both attributes:**

   ```erlang
   {allow, all, publish, ["oem/${client_attrs.source}/${client_attrs.device}/telemetry"]}.
   {deny, all}.
   ```

   The certificate `helix.hx-0000012` may publish to
   `oem/helix/hx-0000012/telemetry` and to nothing else. It cannot publish as
   `hx-0000013`, it cannot publish as another source, and it cannot subscribe
   to anything.
4. **`authorization.no_match = deny`**: what no rule allows is refused, and
   `deny_action = disconnect` drops the connection of a client that tries.

Two service identities exist besides the devices:

| Common name | May | Why |
|---|---|---|
| `gateway` | subscribe to `$share/gateway/oem/+/+/telemetry` | the Rosetta gateway |
| `simulator` | publish to `oem/+/+/telemetry` | the load generator uses one connection for the whole fleet. Test environments only |

### If your EMQX is older than 5.7

`client_attrs_init` and `${client_attrs.*}` need EMQX 5.7 or newer. On an older
broker, issue device certificates with the device id alone as common name and
use the rule below. It binds the device, but not the source:

```erlang
{allow, all, publish, ["oem/+/${username}/telemetry"]}.
```

## What production needs beyond this demo

- Certificates from a real PKI (cert-manager, Vault, AWS Private CA), with the
  private key generated on the device and never leaving it.
- Revocation. Add a CRL or OCSP check to the listener
  (`ssl_options.enable_crl_check`, `ssl_options.ocsp`), and keep device
  certificates short-lived.
- The demo keys are world-readable so that containers running as other users
  can read them through a bind mount. In Kubernetes they are a Secret mounted
  with mode 0440 (`mqtt.tls` in the Helm chart).

## Known limits in the application

- The gateway's MQTT client id is `gateway-<pid>`. In a container the pid is
  the same for every replica, so a second gateway replica would take over the
  session of the first. Run one gateway until the client id includes the host
  name.
- Over MQTT the content type of a payload is not carried: neither the
  simulator's MQTT transport nor the MQTT gateway sets the record header `ct`.
  Normalisation does not use it. The only effect is that dead-letter groups
  created from MQTT traffic have an empty content type.
