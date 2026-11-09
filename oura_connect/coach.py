"""17-Week Publix Atlanta Sub-2:00 Half-Marathon Plan, Readiness Scorer, and Tetris Auto-Adapter."""

from __future__ import annotations

import copy
import datetime as dt
import statistics
from typing import Any

from .storage import OuraStorage

RACE_NAME = "Publix Atlanta Half Marathon"
RACE_DATE = "2027-03-06"  # Saturday
PLAN_START_DATE = "2026-11-09"  # Monday (17 weeks)
GOAL_TIME = "Sub-2:00:00 (< 9:09/mi)"

PACE_ZONES: dict[str, str] = {
    "recovery": "10:45–11:30/mi (Zone 1–2 easy shakeout)",
    "easy": "10:15–11:00/mi (Conversational Zone 2)",
    "steady_hill": "9:35–10:05/mi (Controlled rolling effort)",
    "hmp": "9:00–9:08/mi (Sub-2:00 Half-Marathon Goal Pace)",
    "tempo": "8:38–8:52/mi (Lactate Threshold effort)",
    "hills": "5K effort uphill / easy jog recovery down",
}

STRENGTH_TUE = "15 min Runner Prehab A: Bent-knee soleus raises (3x15), straight-leg calf raises (3x12), tibialis raises, short-foot arch holds"
STRENGTH_THU = "15 min Runner Prehab B: Single-leg RDL (3x10), lateral step-downs (3x10 — downhill quad armor), side-plank hip abduction, glute bridges"

# 17-week progressive blueprint: (phase, tue_mi, thu_spec, sat_long_mi, sat_note, sun_easy_mi)
# Every 4th week (W4, W8, W13) is a deliberate recovery cutback week to protect tendons ramping from <10 mpw.
WEEKLY_BLUEPRINT: list[tuple[str, float, dict[str, Any], float, str, float]] = [
    # Phase 1: Aerobic & Tendon Base (Weeks 1–4)
    (
        "Phase 1: Aerobic & Tendon Base",
        2.0,
        {"type": "easy", "title": "Easy Aerobic Run", "miles": 2.5, "pace": PACE_ZONES["easy"], "hill": "Flat-to-gentle route", "quality": False, "desc": "Relaxed conversational cadence (~170+ spm); protect calves & shins during ramp-up."},
        3.5,
        "Gentle rolling route (~150 ft gain) at relaxed Zone 2.",
        1.5,
    ),
    (
        "Phase 1: Aerobic & Tendon Base",
        2.5,
        {"type": "strides", "title": "Easy Run + 4x20s Uphill Strides", "miles": 3.0, "pace": PACE_ZONES["easy"], "hill": "4x20s @ 4–5% grade (walk back)", "quality": False, "desc": "Easy Zone 2 miles finishing with 4 crisp, relaxed 20-second uphill strides for neuromuscular economy."},
        4.0,
        "Rolling Zone 2 long run (~200 ft gain).",
        1.5,
    ),
    (
        "Phase 1: Aerobic & Tendon Base",
        3.0,
        {"type": "strides", "title": "Easy Run + 6x20s Uphill Strides", "miles": 3.5, "pace": PACE_ZONES["easy"], "hill": "6x20s @ 4–5% grade", "quality": False, "desc": "Build tendon stiffness safely with short uphill accelerations after easy miles."},
        4.5,
        "Rolling Zone 2 long run (~250 ft gain).",
        1.5,
    ),
    (
        "Phase 1: Cutback & Adaptation",
        2.5,
        {"type": "easy", "title": "Cutback Easy Aerobic Run", "miles": 2.5, "pace": PACE_ZONES["easy"], "hill": "Flat/soft surface if available", "quality": False, "desc": "Planned 4th-week tissue adaptation cutback so tendons & bones supercompensate."},
        3.5,
        "Relaxed cutback long run.",
        1.5,
    ),
    # Phase 2: Atlanta Hill & Threshold Build (Weeks 5–10)
    (
        "Phase 2: Atlanta Hill & Threshold Build",
        3.0,
        {"type": "hills", "title": "Atlanta Hill Repeats (5x60s)", "miles": 3.5, "pace": PACE_ZONES["hills"], "hill": "5x60s uphill @ 5–6% grade", "quality": True, "desc": "1.5 mi warm-up, 5x60s strong controlled uphill climbs (jog easy down), 1 mi cool-down."},
        5.5,
        "Rolling Atlanta terrain (~300 ft gain), conversational throughout.",
        2.0,
    ),
    (
        "Phase 2: Atlanta Hill & Threshold Build",
        3.5,
        {"type": "tempo", "title": "Rolling Threshold Tempo (2x8 min)", "miles": 4.0, "pace": PACE_ZONES["tempo"], "hill": "Undulating route (~150 ft gain)", "quality": True, "desc": "1.25 mi warm-up, 2x8 min @ 8:40–8:52/mi effort (2 min easy jog between), 1 mi cool-down."},
        6.5,
        "Rolling long run (~350 ft gain); practice fueling/hydration.",
        2.0,
    ),
    (
        "Phase 2: Atlanta Hill & Threshold Build",
        3.5,
        {"type": "hills", "title": "Atlanta Hill Repeats (6x75s)", "miles": 4.5, "pace": PACE_ZONES["hills"], "hill": "6x75s uphill @ 5–6% grade", "quality": True, "desc": "1.5 mi warm-up, 6x75s uphill repeats focusing on tall posture & glute drive, 1.25 mi cool-down."},
        7.5,
        "Rolling long run (~425 ft gain), steady final mile @ 9:40/mi.",
        2.5,
    ),
    (
        "Phase 2: Cutback & Adaptation",
        3.0,
        {"type": "strides", "title": "Cutback Easy + 5x20s Strides", "miles": 3.5, "pace": PACE_ZONES["easy"], "hill": "5x20s gentle uphill", "quality": False, "desc": "Every-4th-week recovery week to absorb hill & threshold training."},
        5.5,
        "Easy cutback long run (~250 ft gain).",
        2.0,
    ),
    (
        "Phase 2: Atlanta Hill & Threshold Build",
        4.0,
        {"type": "tempo", "title": "Continuous Tempo (20 min @ Threshold)", "miles": 5.0, "pace": PACE_ZONES["tempo"], "hill": "Rolling route (~200 ft gain)", "quality": True, "desc": "1.5 mi warm-up, 20 min continuous @ 8:40–8:50/mi effort (even effort over rollers), 1.2 mi cool-down."},
        8.5,
        "Rolling long run (~500 ft gain); practice gel at 45 min.",
        2.5,
    ),
    (
        "Phase 2: Atlanta Hill & Threshold Build",
        4.0,
        {"type": "hills", "title": "Hill Repeats + HMP Float (6x60s + 1 mi @ HMP)", "miles": 5.5, "pace": PACE_ZONES["hmp"], "hill": "6x60s uphill + 1 mi rolling @ 9:05/mi", "quality": True, "desc": "1.5 mi warm-up, 6x60s hills, 3 min easy, then 1 mile smooth at 9:05/mi goal pace on tired legs, 1 mi cool-down."},
        9.5,
        "Atlanta hills long run (~575 ft gain); controlled downhill quad mechanics.",
        3.0,
    ),
    # Phase 3: Sub-2:00 Race-Pace Peak (Weeks 11–15)
    (
        "Phase 3: Sub-2:00 Specific Peak",
        4.5,
        {"type": "hmp", "title": "Sub-2:00 Goal Pace Blocks (2x2 mi @ HMP)", "miles": 6.0, "pace": PACE_ZONES["hmp"], "hill": "Rolling route (~220 ft gain)", "quality": True, "desc": "1.25 mi warm-up, 2 x 2.0 mi @ 9:00–9:08/mi (3 min easy jog recovery), 0.75 mi cool-down."},
        10.5,
        "Piedmont-style rolling long run (~625 ft gain), final 2 mi @ 9:15/mi.",
        3.0,
    ),
    (
        "Phase 3: Sub-2:00 Specific Peak",
        4.5,
        {"type": "tempo", "title": "Threshold Cruise Intervals (3x10 min)", "miles": 6.0, "pace": PACE_ZONES["tempo"], "hill": "Rolling (~200 ft gain)", "quality": True, "desc": "1.25 mi warm-up, 3x10 min @ 8:38–8:50/mi (2 min jog recovery), 1 mi cool-down."},
        11.5,
        "Rolling long run (~675 ft gain) w/ miles 9–11 @ 9:05–9:10/mi HMP; test race-day breakfast & gels.",
        3.5,
    ),
    (
        "Phase 3: Cutback & Absorption",
        3.5,
        {"type": "hmp", "title": "Cutback Rhythm Run (2 mi @ HMP)", "miles": 4.5, "pace": PACE_ZONES["hmp"], "hill": "Moderate rollers", "quality": True, "desc": "1.5 mi warm-up, 2.0 mi smooth @ 9:05/mi, 1.0 mi cool-down."},
        8.5,
        "Relaxed Zone 2 cutback long run (~400 ft gain).",
        3.0,
    ),
    (
        "Phase 3: Sub-2:00 Specific Peak",
        4.5,
        {"type": "hmp", "title": "Continuous Goal HMP Tempo (4 mi @ 9:03/mi)", "miles": 6.5, "pace": PACE_ZONES["hmp"], "hill": "Rolling Atlanta simulation (~275 ft gain)", "quality": True, "desc": "1.5 mi warm-up, 4.0 miles continuous @ 9:00–9:06/mi over rolling terrain, 1.0 mi cool-down."},
        12.0,
        "Peak Course-Simulator Long Run (~725 ft gain): 9 mi easy Zone 2 + final 3 mi @ 9:05/mi!",
        3.5,
    ),
    (
        "Phase 3: Sub-2:00 Specific Peak",
        4.5,
        {"type": "hmp", "title": "Dress Rehearsal HMP (3x1.5 mi @ 9:00/mi)", "miles": 6.5, "pace": PACE_ZONES["hmp"], "hill": "Rolling (~250 ft gain)", "quality": True, "desc": "1.25 mi warm-up, 3 x 1.5 mi @ 9:00–9:05/mi w/ 2 min float jog, 0.75 mi cool-down."},
        12.5,
        "Longest Run (12.5 mi, ~750 ft gain): keep effort conversational & practice gels at 35 and 70 min.",
        3.5,
    ),
    # Phase 4: Race Taper & Publix Atlanta Half Marathon (Weeks 16–17)
    (
        "Phase 4: Race Taper",
        3.5,
        {"type": "hmp", "title": "Taper Sharpener (2x1.5 mi @ HMP)", "miles": 5.0, "pace": PACE_ZONES["hmp"], "hill": "Light rollers", "quality": True, "desc": "Cut volume ~33% while keeping neuromuscular rhythm at 9:03/mi; pause heavy calf/glute weights."},
        7.5,
        "Relaxed 7.5 mi taper long run—finish feeling like you have plenty in the tank.",
        2.5,
    ),
    (
        "Phase 4: Race Week — Publix Atlanta Half",
        3.0,
        {"type": "strides", "title": "Race-Week Shakeout + 3x1 min @ 9:05/mi", "miles": 2.0, "pace": PACE_ZONES["easy"], "hill": "Flat & relaxed", "quality": False, "desc": "2.0 mi super easy shakeout with 3 crisp 60-second pickups at 9:05/mi race rhythm. Hydrate & sleep early!"},
        13.1,
        "RACE DAY! Miles 1–4 controlled 9:12–9:15/mi over early downtown hills -> Miles 5–10 locked @ 9:02–9:07/mi -> Miles 11–13.1 Piedmont finish kick under 2:00:00!",
        0.0,
    ),
]


def generate_atlanta_sub2_plan(start_date_str: str = PLAN_START_DATE) -> list[dict[str, Any]]:
    """Build all 119 daily rows (17 weeks) from Monday 2026-11-09 through Sunday 2027-03-07."""
    start_dt = dt.date.fromisoformat(start_date_str)
    plan: list[dict[str, Any]] = []

    for w_idx, (phase, tue_mi, thu_spec, sat_mi, sat_note, sun_mi) in enumerate(WEEKLY_BLUEPRINT, start=1):
        week_monday = start_dt + dt.timedelta(days=(w_idx - 1) * 7)
        in_taper = w_idx >= 16
        is_race_week = w_idx == 17

        for offset in range(7):
            day_dt = week_monday + dt.timedelta(days=offset)
            day_iso = day_dt.isoformat()
            row: dict[str, Any] = {
                "day": day_iso,
                "week_num": w_idx,
                "phase": phase,
                "workout_type": "rest",
                "title": "Rest & Mobility",
                "description": "Full recovery day—easy walking, light ankle/hip mobility, and sleep priority.",
                "planned_miles": 0.0,
                "target_pace": "-",
                "hill_focus": "",
                "strength_addon": "",
                "is_quality": False,
                "status": "scheduled",
            }

            if offset == 0:  # Monday
                row["title"] = "Rest & Recovery"
            elif offset == 1:  # Tuesday — Easy Aerobic + Prehab A
                row["workout_type"] = "easy"
                row["title"] = f"Easy Aerobic Run ({tue_mi:g} mi)"
                row["description"] = (
                    "Race-week easy leg-freshener with 3x20s strides."
                    if is_race_week
                    else "Relaxed Zone 2 aerobic mileage—nose-breathing conversational effort."
                )
                row["planned_miles"] = tue_mi
                row["target_pace"] = PACE_ZONES["easy"]
                row["hill_focus"] = "Light undulating terrain"
                row["strength_addon"] = "" if in_taper else STRENGTH_TUE
            elif offset == 2:  # Wednesday — Flex Rest
                row["title"] = "Rest / Flex Swap Slot"
                row["description"] = "Scheduled rest day (available for automatic workout swap if Tuesday or Thursday recovery dips)."
            elif offset == 3:  # Thursday — Quality / Hill / Strides + Prehab B
                row["workout_type"] = str(thu_spec["type"])
                row["title"] = str(thu_spec["title"])
                row["description"] = str(thu_spec["desc"])
                row["planned_miles"] = float(thu_spec["miles"])
                row["target_pace"] = str(thu_spec["pace"])
                row["hill_focus"] = str(thu_spec["hill"])
                row["is_quality"] = bool(thu_spec["quality"])
                row["strength_addon"] = "" if in_taper else STRENGTH_THU
            elif offset == 4:  # Friday — Pre-Long-Run / Pre-Race Rest
                row["title"] = "Pre-Race Carb & Hydration Rest" if is_race_week else "Rest / Flex Swap Slot"
                row["description"] = (
                    "Feet up, electrolyte hydration, carb-rich meals, lay out race kit & bib!"
                    if is_race_week
                    else "Rest before Saturday Long Run (or flex slot if weekend Long Run shifts to Sunday)."
                )
            elif offset == 5:  # Saturday — Long Run or Publix Atlanta Half Marathon!
                if is_race_week:
                    row["workout_type"] = "race"
                    row["title"] = f"{RACE_NAME} (13.1 mi — Sub-2:00!)"
                    row["description"] = sat_note
                    row["planned_miles"] = 13.1
                    row["target_pace"] = "9:05/mi avg (< 2:00:00 finish)"
                    row["hill_focus"] = "Publix Atlanta Rolling Hills (~700+ ft gain)"
                    row["is_quality"] = True
                else:
                    row["workout_type"] = "long_run"
                    row["title"] = f"Saturday Rolling Long Run ({sat_mi:g} mi)"
                    row["description"] = sat_note
                    row["planned_miles"] = sat_mi
                    row["target_pace"] = PACE_ZONES["easy"] if w_idx < 11 else "10:15/mi -> 9:05/mi finish"
                    row["hill_focus"] = sat_note
                    row["is_quality"] = True
            elif offset == 6:  # Sunday — Easy Shakeout or Post-Race Rest
                if is_race_week or sun_mi <= 0:
                    row["workout_type"] = "rest"
                    row["title"] = "Post-Race Celebration & Recovery!" if is_race_week else "Rest & Recovery"
                    row["description"] = "Celebrate breaking 2 hours at the Publix Atlanta Half!"
                else:
                    row["workout_type"] = "recovery"
                    row["title"] = f"Easy Recovery Shakeout ({sun_mi:g} mi)"
                    row["description"] = "Super gentle Zone 1–2 recovery jog (or brisk walk if calves feel tight after Saturday's hills)."
                    row["planned_miles"] = sun_mi
                    row["target_pace"] = PACE_ZONES["recovery"]
                    row["hill_focus"] = "Flat route"

            plan.append(row)

    return plan


def ensure_plan_initialized(storage: OuraStorage) -> bool:
    """Seed the 17-week Publix Atlanta Half-Marathon plan if not already in SQLite."""
    if storage.count_plan_days() > 0:
        return False
    days = generate_atlanta_sub2_plan()
    storage.upsert_plan_days(days, overwrite_existing=False)
    return True


def evaluate_readiness(
    storage: OuraStorage,
    target_day: str | None = None,
) -> dict[str, Any]:
    """Compute personal Readiness-to-Train signal (`GREEN` | `YELLOW` | `RED`) using rolling 28d Oura + Strava baselines."""
    ensure_plan_initialized(storage)
    today_str = target_day or dt.date.today().isoformat()
    today_dt = dt.date.fromisoformat(today_str)
    lookback_start = (today_dt - dt.timedelta(days=30)).isoformat()

    timeline = storage.get_daily_timeline(start_date=lookback_start, end_date=today_str)
    plan_today = storage.get_plan_day(today_str)

    if not timeline:
        return {
            "target_day": today_str,
            "anchor_oura_day": None,
            "signal": "GREEN",
            "score": 80,
            "headline": "No recent Oura data in window — defaulting to scheduled plan",
            "reasons": ["Sync your Oura Ring to enable HRV/RHR adaptive coaching."],
            "metrics": {},
        }

    # Anchor on today's Oura row if synced, otherwise yesterday's morning row
    anchor = timeline[-1]
    anchor_day = str(anchor["day"])
    prior_rows = [r for r in timeline if r["day"] < anchor_day][-28:]
    baseline_pool = prior_rows if len(prior_rows) >= 5 else timeline

    hrv_vals = [float(r["average_hrv"]) for r in baseline_pool if r.get("average_hrv") is not None]
    rhr_vals = [float(r["lowest_heart_rate"]) for r in baseline_pool if r.get("lowest_heart_rate") is not None]

    hrv_mean = statistics.mean(hrv_vals) if hrv_vals else 75.0
    hrv_sd = statistics.stdev(hrv_vals) if len(hrv_vals) >= 3 else 12.0
    hrv_sd = max(hrv_sd, 5.0)

    rhr_mean = statistics.mean(rhr_vals) if rhr_vals else 55.0

    cur_hrv = float(anchor["average_hrv"]) if anchor.get("average_hrv") is not None else None
    cur_rhr = float(anchor["lowest_heart_rate"]) if anchor.get("lowest_heart_rate") is not None else None
    cur_sleep_h = float(anchor["total_sleep_hours"]) if anchor.get("total_sleep_hours") is not None else None
    cur_sleep_score = anchor.get("sleep_score")
    cur_readiness = anchor.get("readiness_score")
    temp_dev = float(anchor["temperature_deviation"]) if anchor.get("temperature_deviation") is not None else 0.0

    hrv_z = round((cur_hrv - hrv_mean) / hrv_sd, 2) if cur_hrv is not None else 0.0
    rhr_delta = round(cur_rhr - rhr_mean, 1) if cur_rhr is not None else 0.0

    # Check yesterday's non-running leg/stress load & Strava run load
    yday_str = (today_dt - dt.timedelta(days=1)).isoformat()
    yday_oura = next((r for r in reversed(timeline) if r["day"] <= yday_str), None)
    yday_steps = int(yday_oura.get("steps") or 0) if yday_oura else 0
    yday_stress_min = float(yday_oura.get("stress_high_min") or 0.0) if yday_oura else 0.0

    recent_runs = storage.get_strava_runs(
        start_date=(today_dt - dt.timedelta(days=2)).isoformat(),
        end_date=today_str,
    )
    yday_run_miles = sum(float(r["distance_miles"]) for r in recent_runs if r["day"] == yday_str)

    soreness = int(plan_today["checkin_soreness"]) if plan_today and plan_today.get("checkin_soreness") is not None else 0

    signal = "GREEN"
    score = 88
    reasons: list[str] = []

    # 1. Illness / Fever & High-Pain Circuit Breakers (RED)
    if temp_dev >= 0.40 and rhr_delta >= 2.0:
        signal = "RED"
        score = 32
        reasons.append(
            f"Illness circuit-breaker: skin temp +{temp_dev:.2f}°C with Resting HR +{rhr_delta:.1f} bpm above baseline."
        )
    if soreness >= 7:
        signal = "RED"
        score = min(score, 30)
        reasons.append(f"Reported musculoskeletal soreness/tightness is high ({soreness}/10) — protecting tendons.")
    if cur_hrv is not None and hrv_z <= -1.6:
        signal = "RED"
        score = min(score, 38)
        reasons.append(
            f"Deep autonomic suppression: Nightly HRV is {cur_hrv:.0f} ms ({hrv_z:+.2f}σ vs your {hrv_mean:.0f} ms baseline)."
        )
    elif cur_hrv is not None and hrv_z <= -1.2 and rhr_delta >= 3.0:
        signal = "RED"
        score = min(score, 42)
        reasons.append(
            f"Combined HRV drop ({cur_hrv:.0f} ms, {hrv_z:+.2f}σ) + elevated Resting HR ({cur_rhr:.0f} bpm, +{rhr_delta:.1f} bpm)."
        )

    # 2. Moderate Fatigue / High Hidden Load (YELLOW)
    if signal != "RED":
        if soreness in (5, 6):
            signal = "YELLOW"
            score = min(score, 62)
            reasons.append(f"Moderate muscle/tendon tightness logged ({soreness}/10).")
        if cur_hrv is not None and -1.6 < hrv_z <= -0.85:
            signal = "YELLOW"
            score = min(score, 65)
            reasons.append(
                f"HRV dipped below baseline window ({cur_hrv:.0f} ms vs {hrv_mean:.0f} ms 28d avg, {hrv_z:+.2f}σ)."
            )
        if rhr_delta >= 3.0:
            signal = "YELLOW"
            score = min(score, 64)
            reasons.append(f"Resting HR elevated +{rhr_delta:.1f} bpm ({cur_rhr:.0f} vs {rhr_mean:.1f} bpm avg).")
        if cur_sleep_h is not None and cur_sleep_h < 6.0 and hrv_z < -0.4:
            signal = "YELLOW"
            score = min(score, 63)
            reasons.append(f"Short sleep ({cur_sleep_h:.1f}h) paired with suppressed HRV ({hrv_z:+.2f}σ).")
        elif cur_sleep_h is not None and cur_sleep_h < 6.5 and hrv_z >= -0.3:
            reasons.append(
                f"Short sleep ({cur_sleep_h:.1f}h), but autonomic recovery stayed strong (HRV {cur_hrv:.0f} ms, {hrv_z:+.2f}σ) — cleared to run with extra warm-up."
            )

        if yday_steps >= 20000 and yday_stress_min >= 120:
            signal = "YELLOW"
            score = min(score, 68)
            reasons.append(
                f"High non-running leg & stress load yesterday ({yday_steps:,} steps + {yday_stress_min:.0f}m stress)."
            )
        elif yday_run_miles >= 6.0:
            signal = "YELLOW"
            score = min(score, 68)
            reasons.append(f"Heavy GPS run load yesterday ({yday_run_miles:.1f} mi) — avoid back-to-back intensity.")

    if signal == "GREEN" and not reasons:
        if cur_hrv is not None:
            score = min(98, int(85 + max(-3.0, min(12.0, hrv_z * 6))))
            reasons.append(
                f"Autonomic baseline solid: HRV {cur_hrv:.0f} ms (28d avg {hrv_mean:.0f} ms, {hrv_z:+.2f}σ), Resting HR {cur_rhr or 0:.0f} bpm."
            )
        else:
            reasons.append("Recovery indicators within normal training corridor.")

    headlines = {
        "GREEN": "Green Light — Full Go for Planned Training",
        "YELLOW": "Yellow Caution — Reshuffle Intensity / Prioritize Aerobic Flow",
        "RED": "Red Recovery Alert — Protect Recovery & Tendons Today",
    }

    return {
        "target_day": today_str,
        "anchor_oura_day": anchor_day,
        "signal": signal,
        "score": score,
        "headline": headlines[signal],
        "reasons": reasons,
        "metrics": {
            "hrv_ms": cur_hrv,
            "hrv_28d_mean": round(hrv_mean, 1),
            "hrv_28d_sd": round(hrv_sd, 1),
            "hrv_z": hrv_z,
            "rhr_bpm": cur_rhr,
            "rhr_28d_mean": round(rhr_mean, 1),
            "rhr_delta": rhr_delta,
            "sleep_hours": cur_sleep_h,
            "sleep_score": cur_sleep_score,
            "readiness_score": cur_readiness,
            "temperature_deviation": temp_dev,
            "yesterday_steps": yday_steps,
            "yesterday_stress_min": yday_stress_min,
            "yesterday_strava_miles": round(yday_run_miles, 2),
            "checkin_soreness": soreness,
        },
    }


def _swap_workout_fields(day_a: dict[str, Any], day_b: dict[str, Any]) -> None:
    """Swap workout prescription fields between two calendar dates while keeping their `day` & `week_num` intact."""
    keys = (
        "workout_type",
        "title",
        "description",
        "planned_miles",
        "target_pace",
        "hill_focus",
        "strength_addon",
        "is_quality",
    )
    for k in keys:
        day_a[k], day_b[k] = day_b[k], day_a[k]


def auto_adapt_plan(
    storage: OuraStorage,
    target_day: str | None = None,
) -> dict[str, Any]:
    """Evaluate readiness for `target_day` and automatically apply Tetris day-swaps or recovery downshifts if needed."""
    ensure_plan_initialized(storage)
    today_str = target_day or dt.date.today().isoformat()
    readiness = evaluate_readiness(storage, target_day=today_str)
    signal = readiness["signal"]

    plan_today = storage.get_plan_day(today_str)
    if not plan_today or plan_today.get("status") == "completed" or plan_today.get("workout_type") == "race":
        return {"adapted": False, "readiness": readiness, "adaptation_id": None}

    if signal == "GREEN":
        return {"adapted": False, "readiness": readiness, "adaptation_id": None}

    # Avoid re-adapting the exact same day + signal if an active (non-undone) adaptation already ran
    for recent in storage.get_adaptations(limit=10):
        if (
            recent["target_day"] == today_str
            and recent["signal_level"] == signal
            and recent["reverted_at"] is None
        ):
            return {
                "adapted": False,
                "already_adapted": True,
                "readiness": readiness,
                "adaptation_id": recent["id"],
            }

    week_num = int(plan_today["week_num"])
    week_days = storage.get_plan_days(week_num=week_num)
    by_date = {d["day"]: copy.deepcopy(d) for d in week_days}
    original_snapshot = copy.deepcopy(week_days)

    today_dt = dt.date.fromisoformat(today_str)
    weekday = today_dt.weekday()  # 0=Mon .. 6=Sun
    cur = by_date[today_str]
    planned_mi = float(cur.get("planned_miles") or 0.0)

    if planned_mi <= 0.0:
        return {"adapted": False, "readiness": readiness, "adaptation_id": None}

    action_summary = ""
    touched_dates: list[str] = []

    if signal == "RED":
        metrics = readiness["metrics"]
        severe = (metrics.get("temperature_deviation") or 0) >= 0.4 or (metrics.get("checkin_soreness") or 0) >= 7
        if severe or planned_mi <= 3.0:
            cur["workout_type"] = "rest"
            cur["title"] = "Adaptive Recovery Rest (Coach Auto-Adjusted)"
            cur["description"] = f"Switched to full rest ({'; '.join(readiness['reasons'])}). Missed volume is intentionally dropped—never crammed into later days."
            cur["planned_miles"] = 0.0
            cur["target_pace"] = "-"
            cur["hill_focus"] = ""
            cur["strength_addon"] = ""
            cur["is_quality"] = False
            action_summary = f"RED alert on {today_str}: converted '{plan_today['title']}' to Full Recovery Rest."
        else:
            shakeout_mi = 2.0
            cur["workout_type"] = "recovery"
            cur["title"] = f"Adaptive Zone 1 Shakeout ({shakeout_mi:g} mi)"
            cur["description"] = f"Downshifted from '{plan_today['title']}' due to RED recovery alert ({'; '.join(readiness['reasons'])})."
            cur["planned_miles"] = shakeout_mi
            cur["target_pace"] = PACE_ZONES["recovery"]
            cur["hill_focus"] = "Flat soft surface"
            cur["strength_addon"] = ""
            cur["is_quality"] = False
            action_summary = f"RED alert on {today_str}: downshifted '{plan_today['title']}' to {shakeout_mi:g} mi Zone 1 Shakeout."
        touched_dates = [today_str]

    elif signal == "YELLOW":
        # Only reshape quality workouts, long runs, or moderate runs >= 4 mi; gentle Zone 2 < 4 mi stays easy!
        if not cur.get("is_quality") and planned_mi < 4.0:
            return {"adapted": False, "readiness": readiness, "adaptation_id": None}

        # Case A: Tuesday (weekday 1) -> swap into Wednesday (weekday 2) if Wednesday is Rest & (not quality or Thu not quality)
        wed_str = (today_dt + dt.timedelta(days=1)).isoformat() if weekday == 1 else ""
        fri_str = (today_dt + dt.timedelta(days=1)).isoformat() if weekday == 3 else ""
        sat_str = (today_dt + dt.timedelta(days=2)).isoformat() if weekday == 3 else (today_str if weekday == 5 else "")
        sun_str = (today_dt + dt.timedelta(days=3)).isoformat() if weekday == 3 else ((today_dt + dt.timedelta(days=1)).isoformat() if weekday == 5 else "")

        if weekday == 1 and wed_str in by_date and by_date[wed_str]["planned_miles"] == 0.0 and not cur["is_quality"]:
            _swap_workout_fields(by_date[today_str], by_date[wed_str])
            touched_dates = [today_str, wed_str]
            action_summary = (
                f"YELLOW caution on Tue {today_str}: shifted '{plan_today['title']}' to Wed {wed_str} for 24h extra recovery."
            )
        elif (
            weekday == 3
            and cur.get("is_quality")
            and fri_str in by_date
            and sat_str in by_date
            and sun_str in by_date
            and by_date[fri_str]["planned_miles"] == 0.0
            and by_date[sat_str]["workout_type"] == "long_run"
            and by_date[sun_str]["workout_type"] in ("recovery", "easy", "rest")
        ):
            # Tetris double-swap: Thu Quality -> Fri, and Sat Long Run <-> Sun Easy/Recovery (preserves 48h Fri->Sun buffer!)
            _swap_workout_fields(by_date[today_str], by_date[fri_str])
            _swap_workout_fields(by_date[sat_str], by_date[sun_str])
            touched_dates = [today_str, fri_str, sat_str, sun_str]
            action_summary = (
                f"YELLOW caution on Thu {today_str}: Tetris-shifted '{plan_today['title']}' to Fri {fri_str} "
                f"and moved Long Run from Sat {sat_str} to Sun {sun_str} to preserve a full 48h recovery buffer."
            )
        elif (
            weekday == 5
            and cur.get("workout_type") == "long_run"
            and sun_str in by_date
            and by_date[sun_str]["workout_type"] in ("recovery", "easy", "rest")
        ):
            _swap_workout_fields(by_date[today_str], by_date[sun_str])
            touched_dates = [today_str, sun_str]
            action_summary = (
                f"YELLOW caution on Sat {today_str}: swapped '{plan_today['title']}' with Sun {sun_str} for 24h extra recovery."
            )
        else:
            # Fallback: convert quality intensity to relaxed Zone 2 aerobic miles (-15% volume) so Saturday Long Run stays protected
            easy_mi = round(max(2.5, planned_mi * 0.85) * 2) / 2.0
            cur["workout_type"] = "easy"
            cur["title"] = f"Adaptive Easy Zone 2 ({easy_mi:g} mi)"
            cur["description"] = (
                f"Converted from '{plan_today['title']}' due to YELLOW caution ({'; '.join(readiness['reasons'])}) "
                "to protect weekend Long Run quality."
            )
            cur["planned_miles"] = easy_mi
            cur["target_pace"] = PACE_ZONES["easy"]
            cur["hill_focus"] = "Gentle flat/rolling Zone 2"
            cur["is_quality"] = False
            touched_dates = [today_str]
            action_summary = (
                f"YELLOW caution on {today_str}: converted '{plan_today['title']}' to {easy_mi:g} mi Easy Zone 2."
            )

    prev_subset = [d for d in original_snapshot if d["day"] in touched_dates]
    updated_subset = [by_date[dt_key] for dt_key in touched_dates]
    storage.upsert_plan_days(updated_subset, overwrite_existing=True)

    full_reason = f"{action_summary} Signals: {' '.join(readiness['reasons'])}"
    adaptation_id = storage.record_adaptation(
        target_day=today_str,
        signal_level=signal,
        reason=full_reason,
        metrics=readiness["metrics"],
        previous_days=prev_subset,
        updated_days=updated_subset,
    )
    return {
        "adapted": True,
        "adaptation_id": adaptation_id,
        "action_summary": action_summary,
        "touched_dates": touched_dates,
        "readiness": readiness,
    }


def manual_swap_days(
    storage: OuraStorage,
    day_a: str,
    day_b: str,
    reason: str = "Manual schedule swap",
) -> dict[str, Any]:
    """Swap workout prescriptions between `day_a` and `day_b` with full Undo support."""
    ensure_plan_initialized(storage)
    row_a = storage.get_plan_day(day_a)
    row_b = storage.get_plan_day(day_b)
    if not row_a or not row_b:
        raise ValueError(f"Both {day_a} and {day_b} must be within the 17-week training plan ({PLAN_START_DATE}..2027-03-07).")
    if row_a.get("workout_type") == "race" or row_b.get("workout_type") == "race":
        raise ValueError("Publix Atlanta Half Marathon race day (2027-03-06) cannot be moved!")

    prev = [copy.deepcopy(row_a), copy.deepcopy(row_b)]
    upd_a = copy.deepcopy(row_a)
    upd_b = copy.deepcopy(row_b)
    _swap_workout_fields(upd_a, upd_b)
    storage.upsert_plan_days([upd_a, upd_b], overwrite_existing=True)

    adap_id = storage.record_adaptation(
        target_day=day_a,
        signal_level="MANUAL",
        reason=f"{reason}: swapped {day_a} ('{row_a['title']}') <-> {day_b} ('{row_b['title']}').",
        metrics={},
        previous_days=prev,
        updated_days=[upd_a, upd_b],
    )
    return {"adaptation_id": adap_id, "swapped": [day_a, day_b]}


def get_coach_overview(storage: OuraStorage, today_str: str | None = None) -> dict[str, Any]:
    """Assemble full dashboard/MCP payload: countdown, readiness, today's workout, active week, and audit log."""
    ensure_plan_initialized(storage)
    cur_day = today_str or dt.date.today().isoformat()
    cur_dt = dt.date.fromisoformat(cur_day)
    race_dt = dt.date.fromisoformat(RACE_DATE)
    days_to_race = (race_dt - cur_dt).days

    readiness = evaluate_readiness(storage, target_day=cur_day)
    today_plan = storage.get_plan_day(cur_day)

    if today_plan:
        active_week = int(today_plan["week_num"])
    elif cur_day < PLAN_START_DATE:
        active_week = 1
    else:
        active_week = 17

    week_days = storage.get_plan_days(week_num=active_week)
    planned_week_miles = round(sum(float(d["planned_miles"] or 0.0) for d in week_days), 1)
    completed_week_miles = round(sum(float(d["actual_miles"] or 0.0) for d in week_days), 2)
    completed_week_elev = round(sum(float(d["actual_elevation_ft"] or 0.0) for d in week_days), 0)

    return {
        "race": {
            "name": RACE_NAME,
            "date": RACE_DATE,
            "goal": GOAL_TIME,
            "days_remaining": max(0, days_to_race),
            "weeks_remaining": round(max(0, days_to_race) / 7.0, 1),
            "pace_zones": PACE_ZONES,
        },
        "today": cur_day,
        "readiness": readiness,
        "today_workout": today_plan,
        "active_week_num": active_week,
        "total_weeks": 17,
        "week_summary": {
            "week_num": active_week,
            "phase": week_days[0]["phase"] if week_days else "",
            "planned_miles": planned_week_miles,
            "completed_miles": completed_week_miles,
            "completed_elevation_ft": completed_week_elev,
            "days": week_days,
        },
        "adaptations": storage.get_adaptations(limit=12),
    }
