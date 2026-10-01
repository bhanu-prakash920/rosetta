"""Field profiling: turn sample payloads into numbers a model can learn from.

For every source field we compute three families of features.

  name      hashed character n-grams of the field path ("speed_mph", "fahrt.v")
  values    type mix, range, spread, shape of strings, how values change over time
  physics   how the field relates to movement derived from GPS. A field that is
            always 0.62 times the GPS speed is a speed in miles per hour, whatever
            it is called. This is what lets the mapper read units that no name reveals.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import HashingVectorizer

from ..algorithms.trip_segmentation import haversine_km
from ..engine.decoders import flatten

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
DTC_RE = re.compile(r"(?<![A-Z0-9])[PCBU][0-3][0-9A-F]{3}(?![A-Z0-9])")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

NAME_DIM = 256
_hasher = HashingVectorizer(analyzer="char_wb", ngram_range=(2, 4), n_features=NAME_DIM,
                            alternate_sign=False, norm="l2", lowercase=True)

VALUE_FEATURES = (
    "frac_null", "frac_num", "frac_str", "frac_bool", "frac_list", "frac_numstr",
    "log_abs_median", "log_abs_max", "log_range", "sign_min", "cv", "frac_integer",
    "in_0_1", "in_0_100", "in_m90_90", "in_m180_180", "in_0_360", "in_0_1000", "frac_zero",
    "distinct_ratio", "decimals", "str_len", "str_len_sd", "frac_vin", "frac_dtc", "frac_iso",
    "frac_upper", "frac_empty", "list_len", "mono_up", "mono_strict", "step_median_log",
    "step_const", "changes", "epoch_s_like", "epoch_ms_like", "epoch_us_like", "two_valued",
)
PHYSICS_FEATURES = ("speed_ratio_log", "speed_ratio_fit", "dist_ratio_log", "dist_ratio_fit",
                    "coord_lat_score", "coord_lon_score", "heading_fit", "clock_ratio_log", "clock_fit",
                    "count_step_fit")
FEATURE_NAMES = VALUE_FEATURES + PHYSICS_FEATURES
N_DENSE = len(FEATURE_NAMES)


def name_tokens(path: str) -> str:
    """'motion.speedKmh' -> 'motion speed kmh'. Indices like [0] are dropped."""
    path = re.sub(r"\[\d+\]", "", path)
    return " ".join(t.lower() for t in _SPLIT.split(path) if t)


def name_vector(path: str) -> np.ndarray:
    return _hasher.transform([name_tokens(path)]).toarray()[0].astype(np.float32)


def _as_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        s = v.strip()
        if not s or len(s) > 24:
            return None
        try:
            x = float(s)
            return x if math.isfinite(x) else None
        except ValueError:
            return None
    return None


@dataclass
class FieldProfile:
    path: str
    values: list[Any]                 # one per message, None when the field is absent
    numeric: np.ndarray = field(default_factory=lambda: np.zeros(0))   # NaN where not numeric
    features: np.ndarray = field(default_factory=lambda: np.zeros(N_DENSE, dtype=np.float32))
    examples: list[Any] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        f = dict(zip(FEATURE_NAMES, (round(float(x), 4) for x in self.features)))
        num = self.numeric[~np.isnan(self.numeric)]
        out: dict[str, Any] = {"path": self.path, "examples": self.examples[:4],
                               "present": round(1.0 - f["frac_null"], 3)}
        if num.size:
            out.update({"min": float(num.min()), "max": float(num.max()), "median": float(np.median(num))})
        out["physics"] = {k: f[k] for k in PHYSICS_FEATURES if f[k] != 0.0}
        return out


@dataclass
class Samples:
    """Decoded sample messages, grouped so that each device's messages stay in arrival order."""
    flat: list[dict[str, Any]]
    device: list[str]

    def groups(self) -> list[np.ndarray]:
        by: dict[str, list[int]] = {}
        for i, d in enumerate(self.device):
            by.setdefault(d, []).append(i)
        return [np.asarray(v) for v in by.values() if len(v) >= 2]


def make_samples(decoded: list[Any], devices: list[str]) -> Samples:
    return Samples([flatten(o) for o in decoded], list(devices))


def _value_features(values: list[Any], numeric: np.ndarray, groups: list[np.ndarray]) -> dict[str, float]:
    n = len(values)
    f = dict.fromkeys(VALUE_FEATURES, 0.0)
    present = [v for v in values if v is not None]
    f["frac_null"] = 1.0 - len(present) / n if n else 1.0
    if not present:
        return f
    m = len(present)
    is_bool = [isinstance(v, bool) for v in present]
    is_num = [isinstance(v, (int, float)) and not isinstance(v, bool) for v in present]
    is_str = [isinstance(v, str) for v in present]
    is_list = [isinstance(v, (list, tuple)) for v in present]
    f["frac_bool"] = sum(is_bool) / m
    f["frac_num"] = sum(is_num) / m
    f["frac_str"] = sum(is_str) / m
    f["frac_list"] = sum(is_list) / m
    strs = [v for v in present if isinstance(v, str)]
    f["frac_numstr"] = sum(1 for s in strs if _as_float(s) is not None) / m
    if any(is_list):
        f["list_len"] = float(np.mean([len(v) for v in present if isinstance(v, (list, tuple))]))
    hashable = [repr(v) for v in present]
    f["distinct_ratio"] = len(set(hashable)) / m
    f["two_valued"] = 1.0 if len(set(hashable)) <= 2 else 0.0

    if strs:
        lens = np.array([len(s) for s in strs], dtype=np.float64)
        f["str_len"] = float(lens.mean()) / 40.0
        f["str_len_sd"] = float(lens.std()) / 10.0
        f["frac_vin"] = sum(1 for s in strs if VIN_RE.match(s.strip().upper())) / m
        f["frac_iso"] = sum(1 for s in strs if ISO_RE.match(s)) / m
        f["frac_upper"] = sum(1 for s in strs if s and s == s.upper() and any(c.isalpha() for c in s)) / m
        f["frac_empty"] = sum(1 for s in strs if not s.strip()) / m
    text = [" ".join(map(str, v)) if isinstance(v, (list, tuple)) else v for v in present
            if isinstance(v, (str, list, tuple))]
    if text:
        f["frac_dtc"] = sum(1 for s in text if DTC_RE.search(str(s).upper())) / m

    x = numeric[~np.isnan(numeric)]
    if x.size:
        ax = np.abs(x)
        med = float(np.median(ax))
        f["log_abs_median"] = math.log10(med + 1e-3) / 16.0
        f["log_abs_max"] = math.log10(float(ax.max()) + 1e-3) / 16.0
        f["log_range"] = math.log10(float(x.max() - x.min()) + 1e-6) / 16.0
        f["sign_min"] = 1.0 if x.min() < 0 else 0.0
        mean = float(x.mean())
        f["cv"] = min(5.0, float(x.std()) / (abs(mean) + 1e-9)) / 5.0
        f["frac_integer"] = float(np.mean(np.abs(x - np.round(x)) < 1e-9))
        f["in_0_1"] = float(np.mean((x >= 0) & (x <= 1)))
        f["in_0_100"] = float(np.mean((x >= 0) & (x <= 100)))
        f["in_m90_90"] = float(np.mean((x >= -90) & (x <= 90)))
        f["in_m180_180"] = float(np.mean((x >= -180) & (x <= 180)))
        f["in_0_360"] = float(np.mean((x >= 0) & (x <= 360)))
        f["in_0_1000"] = float(np.mean((x >= 0) & (x <= 1000)))
        f["frac_zero"] = float(np.mean(x == 0))
        frac = np.abs(x - np.round(x))
        dec = 0
        for d in range(1, 8):
            if np.mean(np.abs(frac * 10 ** d - np.round(frac * 10 ** d)) < 1e-6) > 0.95:
                dec = d
                break
        else:
            dec = 8
        f["decimals"] = (0 if f["frac_integer"] > 0.95 else dec) / 8.0
        f["epoch_s_like"] = float(np.mean((x > 9.4e8) & (x < 4.2e9)))
        f["epoch_ms_like"] = float(np.mean((x > 9.4e11) & (x < 4.2e12)))
        f["epoch_us_like"] = float(np.mean((x > 9.4e14) & (x < 4.2e15)))
        ups, stricts, steps, changes = [], [], [], []
        for g in groups:
            s = numeric[g]
            s = s[~np.isnan(s)]
            if s.size < 2:
                continue
            d = np.diff(s)
            ups.append(np.mean(d >= 0))
            stricts.append(np.mean(d > 0))
            changes.append(np.mean(d != 0))
            steps.extend(np.abs(d[d != 0]).tolist())
        if ups:
            f["mono_up"] = float(np.mean(ups))
            f["mono_strict"] = float(np.mean(stricts))
            f["changes"] = float(np.mean(changes))
        if steps:
            st = np.asarray(steps)
            f["step_median_log"] = math.log10(float(np.median(st)) + 1e-9) / 16.0
            f["step_const"] = float(np.mean(np.abs(st - np.median(st)) < 1e-9 + 1e-6 * np.median(st)))
    return f


def build_profiles(samples: Samples) -> dict[str, FieldProfile]:
    paths: dict[str, None] = {}
    for m in samples.flat:
        for k in m:
            paths.setdefault(k, None)
    groups = samples.groups()
    out: dict[str, FieldProfile] = {}
    for p in paths:
        vals = [m.get(p) for m in samples.flat]
        num = np.array([np.nan if (x := _as_float(v)) is None else x for v in vals], dtype=np.float64)
        prof = FieldProfile(p, vals, num)
        vf = _value_features(vals, num, groups)
        prof.features[: len(VALUE_FEATURES)] = [vf[k] for k in VALUE_FEATURES]
        seen: list[Any] = []
        for v in vals:
            if v is not None and v != "" and v not in seen:
                seen.append(v if not isinstance(v, str) else v[:60])
            if len(seen) >= 5:
                break
        prof.examples = seen
        out[p] = prof
    add_physics(out, samples)
    return out


# --------------------------------------------------------------------- physics
ANGLE_SCALES = (("deg", 1.0), ("microdeg", 1e-6), ("e7", 1e-7))


def _coord_candidates(profiles: dict[str, FieldProfile]) -> list[tuple[str, str, np.ndarray]]:
    """Numeric fields that could be a coordinate, once per plausible scale.

    A field can fit more than one scale: 130,000,000 is 13 degrees in 1e-7 units
    and 130 degrees in micro-degrees. Both are kept, and the pair search below,
    which checks movement against physics, decides."""
    out = []
    for p, pr in profiles.items():
        x = pr.numeric
        ok = ~np.isnan(x)
        if ok.mean() < 0.9:
            continue
        raw = x[ok]
        integral = float(np.mean(np.abs(raw - np.round(raw)) < 1e-9))
        for unit, scale in ANGLE_SCALES:
            vv = raw * scale
            if not (np.all(np.abs(vv) <= 180.0) and np.ptp(vv) > 1e-4 and np.median(np.abs(vv)) > 0.5):
                continue
            if scale == 1.0 and integral > 0.5:
                continue      # whole numbers in degrees are not GPS positions
            if scale != 1.0 and integral < 0.95:
                continue      # scaled integers must be integers
            out.append((p, unit, x * scale))
    return out


def _clock(profiles: dict[str, FieldProfile], groups: list[np.ndarray]) -> tuple[str | None, np.ndarray | None]:
    """Best guess for the event time, as seconds. Needed to turn distance into speed."""
    best, best_t, best_score = None, None, 0.0
    for p, pr in profiles.items():
        f = dict(zip(VALUE_FEATURES, pr.features[: len(VALUE_FEATURES)]))
        for key, div in (("epoch_ms_like", 1000.0), ("epoch_s_like", 1.0), ("epoch_us_like", 1e6)):
            score = float(f[key]) * (0.5 + 0.5 * float(f["mono_up"]))
            if score > best_score and score > 0.8:
                best, best_t, best_score = p, pr.numeric / div, score
        if f["frac_iso"] > 0.9:
            from ..domain.transforms import _iso_to_ms

            try:
                t = np.array([_iso_to_ms(v) / 1000.0 if isinstance(v, str) else np.nan for v in pr.values])
                if best_score < 0.95:
                    best, best_t, best_score = p, t, 0.95
            except Exception:
                pass
    return best, best_t


def add_physics(profiles: dict[str, FieldProfile], samples: Samples, lag: int = 8) -> dict[str, Any]:
    """Fill the physics features in place and return what was inferred, for the trace."""
    groups = [g for g in samples.groups() if len(g) >= 3]
    info: dict[str, Any] = {"devices": len(groups)}
    if not groups:
        return info
    off = len(VALUE_FEATURES)
    ix = {k: off + i for i, k in enumerate(PHYSICS_FEATURES)}
    clock_path, t = _clock(profiles, groups)
    info["clock"] = clock_path
    cands = _coord_candidates(profiles)
    info["coordinate_candidates"] = [(p, u) for p, u, _ in cands]
    if t is None or len(cands) < 2:
        return info

    # --- which pair of candidates is (lat, lon)? Try both orders, keep the one whose
    # movement is physically plausible and, when a heading exists, agrees with it.
    def track(lat_v: np.ndarray, lon_v: np.ndarray):
        d_km, dt, bearing, a_idx, b_idx, k_steps = [], [], [], [], [], []
        for g in groups:
            k = min(lag, len(g) - 1)
            a, b = g[:-k], g[k:]
            la1, la2, lo1, lo2 = lat_v[a], lat_v[b], lon_v[a], lon_v[b]
            if np.any(np.abs(la1) > 90) or np.any(np.abs(la2) > 90):
                return None
            d_km.append(haversine_km(la1, lo1, la2, lo2))
            dt.append(t[b] - t[a])
            y = np.sin(np.radians(lo2 - lo1)) * np.cos(np.radians(la2))
            x = np.cos(np.radians(la1)) * np.sin(np.radians(la2)) - \
                np.sin(np.radians(la1)) * np.cos(np.radians(la2)) * np.cos(np.radians(lo2 - lo1))
            bearing.append(np.degrees(np.arctan2(y, x)) % 360.0)
            a_idx.append(a)
            b_idx.append(b)
            k_steps.append(np.full(len(a), k))
        return (np.concatenate(d_km), np.concatenate(dt), np.concatenate(bearing),
                np.concatenate(a_idx), np.concatenate(b_idx), np.concatenate(k_steps))

    heading_cands = []
    for p, pr in profiles.items():
        x = pr.numeric
        ok = ~np.isnan(x)
        if ok.mean() < 0.9:
            continue
        for unit, scale in (("deg", 1.0), ("centideg", 0.01), ("rad", 180.0 / math.pi)):
            v = x * scale
            vv = v[ok]
            if vv.min() >= 0 and vv.max() <= 360.0 and np.ptp(vv) > 90.0:
                heading_cands.append((p, unit, v))
                break

    best = None
    for i, (pa_, ua, va) in enumerate(cands):
        for j, (pb_, ub, vb) in enumerate(cands):
            if i == j or pa_ == pb_ or ua != ub:      # two different fields, one unit for both
                continue
            tr = track(va, vb)
            if tr is None:
                continue
            d_km, dt, brg, a_idx, b_idx, _k = tr
            ok = (dt > 0) & np.isfinite(d_km)
            if ok.sum() < 5:
                continue
            speed = d_km[ok] / dt[ok] * 3600.0
            plausible = float(np.mean(speed < 250.0))
            moving = ok & (d_km > 0.04)
            h_fit, h_path, h_unit = 0.0, None, None
            if moving.sum() >= 5:
                for hp, hu, hv in heading_cands:
                    if hp in (pa_, pb_):
                        continue
                    hm = 0.5 * (hv[a_idx[moving]] + hv[b_idx[moving]])
                    fit = float(np.nanmean(np.cos(np.radians(brg[moving] - hm))))
                    if fit > h_fit:
                        h_fit, h_path, h_unit = fit, hp, hu
            score = plausible + h_fit
            if best is None or score > best[0]:
                best = (score, pa_, ua, va, pb_, ub, vb, h_fit, h_path, h_unit, tr)
    if best is None:
        return info
    _, lat_p, lat_u, lat_v, lon_p, lon_u, lon_v, h_fit, h_path, h_unit, tr = best
    d_km, dt, brg, a_idx, b_idx, k_steps = tr
    info.update({"lat": lat_p, "lat_unit": lat_u, "lon": lon_p, "lon_unit": lon_u,
                 "heading": h_path, "heading_fit": round(h_fit, 3)})
    conf = 0.5 + 0.5 * max(0.0, h_fit) if h_path else 0.5
    profiles[lat_p].features[ix["coord_lat_score"]] = conf
    profiles[lon_p].features[ix["coord_lon_score"]] = conf
    if h_path:
        profiles[h_path].features[ix["heading_fit"]] = h_fit

    ok = (dt > 1.0) & np.isfinite(d_km)
    gps_speed = np.where(ok, d_km / np.where(ok, dt, 1.0) * 3600.0, np.nan)   # km/h
    moving = ok & (d_km > 0.05)
    for p, pr in profiles.items():
        if p in (lat_p, lon_p, clock_path):
            continue
        x = pr.numeric
        if np.isnan(x).mean() > 0.2:
            continue
        a, b = x[a_idx], x[b_idx]
        # speed: the mean of the field over the span against the GPS speed over the span
        if moving.sum() >= 5:
            mean_v = 0.5 * (a + b)[moving]
            g = gps_speed[moving]
            good = (mean_v > 0) & (g > 3.0)
            if good.sum() >= 5:
                r = mean_v[good] / g[good]
                med = float(np.median(r))
                spread = float(np.median(np.abs(r - med)) / (med + 1e-12))
                if med > 0:
                    pr.features[ix["speed_ratio_log"]] = math.log10(med) / 4.0
                    pr.features[ix["speed_ratio_fit"]] = max(0.0, 1.0 - spread)
            # distance: the growth of the field against the distance travelled
            delta = (b - a)[moving]
            dk = d_km[moving]
            good = delta > 0
            if good.mean() > 0.9:
                r = delta[good] / dk[good]
                med = float(np.median(r))
                spread = float(np.median(np.abs(r - med)) / (med + 1e-12))
                pr.features[ix["dist_ratio_log"]] = math.log10(med) / 4.0
                pr.features[ix["dist_ratio_fit"]] = max(0.0, 1.0 - spread)
        # counters: grows by a constant number of steps per message, regardless of movement
        steps = (b - a) / np.maximum(1, k_steps)
        fin = np.isfinite(steps)
        if fin.sum() >= 5:
            pr.features[ix["count_step_fit"]] = float(np.mean(np.abs(steps[fin] - 1.0) < 1e-6))
        # clocks: grows at a constant rate against the chosen clock
        good = ok & np.isfinite(b - a) & ((b - a) > 0)
        if good.sum() >= 5 and good.mean() > 0.9:
            r = (b - a)[good] / dt[good]
            med = float(np.median(r))
            spread = float(np.median(np.abs(r - med)) / (med + 1e-12))
            if med > 0 and spread < 0.05:
                pr.features[ix["clock_ratio_log"]] = math.log10(med) / 8.0
                pr.features[ix["clock_fit"]] = 1.0 - spread
    if clock_path:
        profiles[clock_path].features[ix["clock_fit"]] = 1.0
    return info


def feature_matrix(profiles: dict[str, FieldProfile], with_names: bool = True) -> tuple[list[str], np.ndarray]:
    paths = list(profiles)
    dense = np.stack([profiles[p].features for p in paths]) if paths else np.zeros((0, N_DENSE), np.float32)
    if not with_names:
        return paths, dense
    names = _hasher.transform([name_tokens(p) for p in paths]).toarray().astype(np.float32) \
        if paths else np.zeros((0, NAME_DIM), np.float32)
    return paths, np.hstack([dense, names])
