import { Link } from "react-router-dom";
import { Brand } from "../App";
import { Code } from "../components/ui";
import { useSession } from "../lib/hooks";

const DIALECTS = [
  { name: "Nordvik Motors", fmt: "nested JSON · metric · ISO time", sample: '{"motion":{"speedKmh":64.2},\n "recordedAt":"2026-09-25T10:15:02.120Z"}' },
  { name: "Pacifica Auto", fmt: "flat JSON · mph · miles · °F", sample: '{"speed_mph":39.892,"odometer_mi":11330.5,\n "oat_f":98.6,"ts":1790000102.12}' },
  { name: "Stellaris Group", fmt: "pipe-delimited text · m/s · metres", sample: "STL|2|3STGT4B74TA000000|1790000102120|\n88412|21170200|72831100|17.833|18234700" },
  { name: "Kaizen Motor", fmt: "Protobuf · 1e-7 degrees · binary", sample: "0a 11 4a 4b 5a 45 56 31 41 32 39 54 41 30\n10 c0 f2 e1 97 b4 dc e1 02 18 dc b2 05 22" },
  { name: "Voltaic EV", fmt: "signal list · m/s · Kelvin", sample: '{"signals":[{"k":"veh_speed","v":17.833},\n {"k":"env_temp","v":310.15}]}' },
  { name: "Helix Mobility", fmt: "unknown to the platform", sample: '{"fahrt":{"v":17.833,"strecke":18234700},\n "klima":{"aussen":310.15}}' },
];

const CANON = { vin: "1HGCM82633A004352", ts: 1790000102120, speed_kmh: 64.2, odo_km: 18234.7, ambient_c: 37.0, evt: "HARSH_BRAKE", oem: "helix", map_v: 1 };

export default function Landing() {
  const s = useSession();
  const go = s ? "/app" : "/login";
  return (
    <>
      <div className="notice">
        Six car makers, six dialects, one event. A new one joins without a restart. <Link to={go}>Open the console →</Link>
      </div>
      <div className="topbar" style={{ position: "static", background: "transparent", border: 0, backdropFilter: "none" }}>
        <div className="wrap">
          <Brand />
          <nav className="row" style={{ margin: "0 auto", gap: "clamp(18px, 5vw, 90px)", fontWeight: 700, fontSize: 13 }} aria-label="Page">
            <a href="#problem">Why</a>
            <a href="#how">How</a>
            <a href="#proof">Proof</a>
          </nav>
          <Link to={go} className="btn" style={{ background: "var(--clay)", color: "var(--ink)" }}>{s ? "Open console" : "Sign in"}</Link>
        </div>
      </div>

      <section className="wrap ruled land-hero">
        <h1 className="display" aria-label="We translate every vehicle">
          <span className="l1">
            we
            <span className="tile a"><img src="/img/truck-road.webp" alt="" /></span>
            translate
          </span>
          <span className="l2">
            every
            <span className="land-side">
              Every car maker reports speed, position and battery in its own format and its own units.
              Rosetta turns all of them into one canonical event, and keeps running while the formats change.
            </span>
          </span>
          <span className="l3">
            <span className="paren">(</span>
            <span className="fan"><i /><i /><img src="/img/fleet-cars.webp" alt="A row of white fleet cars in a car park at sunset" /></span>
            <span className="paren">)</span>
            vehicle
          </span>
        </h1>
        <div style={{ textAlign: "center", marginTop: "clamp(28px, 4vw, 56px)" }}>
          <Link to={go} className="btn flame lg">Explore the live pipeline</Link>
        </div>
        <div className="scrollcue">Scroll to discover</div>
      </section>

      <section className="wrap" id="problem" style={{ paddingBottom: 28 }}>
        <div className="panel ink">
          <img className="ring-deco" src="/img/ring.svg" alt="" />
          <div className="kicker" style={{ color: "rgba(252,252,244,.7)" }}>The problem</div>
          <h2 className="title" style={{ maxWidth: "16ch" }}>the same moment, written <em>six ways</em></h2>
          <p className="lede" style={{ color: "rgba(252,252,244,.75)", borderColor: "var(--flame)", marginBottom: 30 }}>
            One vehicle braking hard at 64.2 km/h. Each maker below describes that moment differently. Adding a maker,
            or surviving a firmware update that renames a field, normally means new code, a release and lost data.
          </p>
          <div className="dialects">
            {DIALECTS.map((d) => (
              <div className="dialect" key={d.name}>
                <b>{d.name}</b>
                <div className="small" style={{ opacity: 0.6 }}>{d.fmt}</div>
                <pre className="code">{d.sample}</pre>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="wrap" style={{ paddingBottom: 28 }}>
        <div className="panel clay">
          <div className="grid g2" style={{ alignItems: "center", gap: 36 }}>
            <div>
              <div className="kicker">The answer</div>
              <h2 className="title">one event, whatever the source</h2>
              <p className="lede" style={{ marginBottom: 24 }}>
                Mappings are data, not code. They are versioned, tested against known answers, released to a share of
                vehicles first, and swapped into running workers between two batches.
              </p>
              <Link to={go} className="btn">See it run</Link>
            </div>
            <div className="card" style={{ background: "var(--paper)" }}>
              <div className="row between" style={{ marginBottom: 10 }}>
                <b>Canonical event</b><span className="pill active">validated</span>
              </div>
              <Code value={CANON} light />
            </div>
          </div>
        </div>
      </section>

      <section className="wrap" id="how" style={{ padding: "50px var(--gutter)" }}>
        <div className="kicker">How a new maker joins</div>
        <h2 className="title" style={{ marginBottom: 34 }}>zero downtime, <em>step by step</em></h2>
        <div className="steps">
          <div><h3>Park</h3><p className="soft">Messages nobody can read yet go to a dead-letter queue with a reason code. Nothing is dropped.</p></div>
          <div><h3>Study</h3><p className="soft">An agent profiles the fields and checks them against physics. A value that is always 0.62 times the GPS speed is a speed in mph.</p></div>
          <div><h3>Prove</h3><p className="soft">The proposed mapping has to reproduce known answers, including a half of them the agent never saw.</p></div>
          <div><h3>Approve</h3><p className="soft">A person reviews and approves. The agent cannot. Every step lands in a tamper-evident audit log.</p></div>
          <div><h3>Replay</h3><p className="soft">Workers load the mapping without restarting and the parked messages flow through, in order.</p></div>
        </div>
      </section>

      <section className="wrap" id="proof" style={{ paddingBottom: 60 }}>
        <div className="grid g3">
          <div className="card flat stat"><div className="l">Sustained on one laptop</div><div className="v">100K<small>events / s</small></div><div className="s">Six dialects, ten processes, every event accounted for.</div></div>
          <div className="card flat stat"><div className="l">Ingest to dashboard, p99</div><div className="v">&lt; 1<small>second</small></div><div className="s">The target was two seconds.</div></div>
          <div className="card stat paper"><div className="l">Field mapping, unseen names</div><div className="v">99.6<small>% vs 30.4%</small></div><div className="s" style={{ color: "var(--ink-2)" }}>Model against a name-matching baseline, on synthetic dialects.</div></div>
        </div>
        <p className="small muted" style={{ marginTop: 16 }}>
          Figures come from the evidence files in the repository (docs/evidence). The model figure is measured on simulated
          data and says nothing about real manufacturer feeds.
        </p>
      </section>

      <footer className="foot wrap">
        <div className="row between wrap-x">
          <span>Rosetta. An independent academic project with no affiliation to any company. All data is simulated.</span>
          <a className="linkish" href="/api/docs">API reference</a>
        </div>
        <p style={{ marginTop: 10, fontSize: 11.5 }}>
          Photo:{" "}
          <a className="linkish" href="https://commons.wikimedia.org/wiki/File:Ram_1500_Pickup_Truck_(49376517091).jpg" target="_blank" rel="noreferrer">Ram 1500 Pickup Truck</a> by Tony Webster,{" "}
          <a className="linkish" href="https://creativecommons.org/licenses/by/2.0/" target="_blank" rel="noreferrer">CC BY 2.0</a>, cropped. Other images generated with Google Gemini.
        </p>
      </footer>
    </>
  );
}
