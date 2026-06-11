"""Offline validation runner: correlation P/R/F1 + new-logic invariants.

    docker compose exec -T api python -m api.validation.run

Exit code is non-zero if any invariant fails or F1 falls below the floor, so the
suite can gate changes to the scoring model.
"""
from __future__ import annotations

import sys

from api.services.correlation import (
    CorrelationEngine,
    THRESHOLD_HIGH,
    THRESHOLD_MEDIUM,
    THRESHOLD_LOW,
)
from api.validation.synthetic import (
    acct, scenarios, PHASH, _EMB_A, _EMB_B, _STYLE_A, _STYLE_B,
    _IMG_A, _IMG_B, _IMG_DISSIM, _FACE_A, _FACE_B, _FACE_DISSIM,
)

F1_FLOOR = 0.80


# ----------------------------------------------------------------- metrics ----
def _evaluate_corpus() -> dict:
    engine = CorrelationEngine()
    rows = []
    tp = fp = fn = tn = 0
    hi_tp = hi_fp = hi_fn = 0  # high-confidence (HIGH/MEDIUM) threshold

    for sc in scenarios():
        res = engine.compute_confidence(sc.units_a, sc.units_b)
        tier = res["confidence_tier"]
        linked = tier != "DISCARD"
        high = res["confidence_score"] >= THRESHOLD_MEDIUM and tier != "DISCARD"

        if sc.same_person and linked:
            tp += 1
        elif sc.same_person and not linked:
            fn += 1
        elif (not sc.same_person) and linked:
            fp += 1
        else:
            tn += 1

        if sc.same_person and high:
            hi_tp += 1
        elif sc.same_person and not high:
            hi_fn += 1
        elif (not sc.same_person) and high:
            hi_fp += 1

        rows.append({
            "name": sc.name,
            "label": "SAME" if sc.same_person else "DIFF",
            "score": res["confidence_score"],
            "tier": tier,
            "prob": res["probability"],
            "signals": res["signal_count"],
            "linked": linked,
            "correct": linked == sc.same_person,
        })

    def prf(t, f_p, f_n):
        p = t / (t + f_p) if (t + f_p) else 0.0
        r = t / (t + f_n) if (t + f_n) else 0.0
        f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
        return p, r, f1

    p, r, f1 = prf(tp, fp, fn)
    hp, hr, hf1 = prf(hi_tp, hi_fp, hi_fn)
    total = tp + fp + fn + tn
    return {
        "rows": rows,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": p, "recall": r, "f1": f1,
        "accuracy": (tp + tn) / total if total else 0.0,
        "hi_precision": hp, "hi_recall": hr, "hi_f1": hf1,
    }


# -------------------------------------------------------------- invariants ----
def _invariants() -> list[tuple[str, bool, str]]:
    eng = CorrelationEngine()
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # 1. Two-signal minimum: one very strong signal alone must not link.
    one = eng.compute_confidence(
        acct(handle="lonewolf", platform="github"),
        acct(handle="lonewolf", platform="gitlab"),
    )
    add("two_signal_minimum",
        one["signal_count"] == 1 and one["confidence_tier"] == "DISCARD",
        f"signals={one['signal_count']} tier={one['confidence_tier']}")

    # 2. Calibration is monotonic and centred at the MEDIUM threshold.
    c_hi, c_mid, c_lo = (
        eng._calibrate(THRESHOLD_HIGH),
        eng._calibrate(THRESHOLD_MEDIUM),
        eng._calibrate(THRESHOLD_LOW),
    )
    add("calibration_monotonic",
        c_hi > c_mid > c_lo and abs(c_mid - 0.5) < 1e-6,
        f"{c_lo} < {c_mid} < {c_hi}")

    # 3. Stylometry fires for same-style/different-text bios (and bio_similarity
    #    does not double-count the same text).
    sty = eng.compute_confidence(
        acct(handle="quill", bio=_STYLE_A, bio_embedding=_EMB_A, platform="github"),
        acct(handle="quill", bio=_STYLE_B, bio_embedding=_EMB_B, platform="medium"),
    )
    bd = sty["signal_breakdown"]
    add("stylometry_fires",
        "stylometry_match" in bd and "bio_similarity" not in bd,
        f"keys={[k for k in bd if not k.startswith('_')]}")

    # 4. Creation proximity fires for close dates, not for distant ones.
    near = eng.compute_confidence(
        acct(handle="tk", join_date="2020-03-01", platform="github"),
        acct(handle="tk", join_date="2020-03-20", platform="reddit"),
    )
    far = eng.compute_confidence(
        acct(handle="tk", join_date="2020-03-01", platform="github"),
        acct(handle="tk", join_date="2022-09-01", platform="reddit"),
    )
    add("temporal_proximity",
        "creation_proximity" in near["signal_breakdown"]
        and "creation_proximity" not in far["signal_breakdown"],
        f"near={'creation_proximity' in near['signal_breakdown']} far={'creation_proximity' in far['signal_breakdown']}")

    # 5. Reserved _meta travels with the breakdown but is not counted as a signal.
    add("meta_not_counted",
        "_meta" in bd and sty["signal_count"] == len([k for k in bd if not k.startswith("_")]),
        f"signal_count={sty['signal_count']}")

    # 6. Persona calibration is monotonic.
    try:
        from api.services.persona_resolver import _calibrate_persona as cp
        add("persona_calibration_monotonic",
            cp(60) > cp(40) > cp(20),
            f"{cp(20)} < {cp(40)} < {cp(60)}")
    except Exception as exc:  # noqa: BLE001
        add("persona_calibration_monotonic", False, f"import failed: {exc}")

    # 7. Dynamic pivot bounds scale by category and never exceed the ceilings.
    try:
        from api.services.pivot_engine import (
            PivotEngine, PIVOT_DEPTH_CEILING, PIVOT_TOTAL_CEILING, PIVOT_PER_HOP_CEILING,
        )
        cyber = PivotEngine.compute_bounds({"target_category": "cybercrime"})
        research = PivotEngine.compute_bounds({"target_category": "research"})
        ok = (
            cyber.max_depth >= research.max_depth
            and cyber.max_total >= research.max_total
            and cyber.max_per_hop >= research.max_per_hop
            and cyber.max_depth <= PIVOT_DEPTH_CEILING
            and cyber.max_total <= PIVOT_TOTAL_CEILING
            and cyber.max_per_hop <= PIVOT_PER_HOP_CEILING
        )
        add("pivot_bounds_dynamic", ok, f"cyber={cyber} research={research}")
    except Exception as exc:  # noqa: BLE001
        add("pivot_bounds_dynamic", False, f"import failed: {exc}")

    # 8. CLIP reverse-image embedding: the same avatar (re-encoded) matches,
    #    a different image does not.
    add("image_embedding_photo_match",
        eng._image_embedding_match(_IMG_A, _IMG_B)
        and not eng._image_embedding_match(_IMG_A, _IMG_DISSIM),
        f"same={eng._image_embedding_match(_IMG_A, _IMG_B)} "
        f"diff={eng._image_embedding_match(_IMG_A, _IMG_DISSIM)}")

    # 9. FaceNet face embedding: the same person (different photo) matches,
    #    a different person does not.
    add("face_embedding_match",
        eng._face_match(_FACE_A, _FACE_B)
        and not eng._face_match(_FACE_A, _FACE_DISSIM),
        f"same={eng._face_match(_FACE_A, _FACE_B)} "
        f"diff={eng._face_match(_FACE_A, _FACE_DISSIM)}")

    return checks


# ------------------------------------------------------------------ report ----
def main() -> int:
    print("=" * 72)
    print("SOCMINT OFFLINE VALIDATION SUITE")
    print("=" * 72)

    m = _evaluate_corpus()
    print("\nPer-scenario correlation results:")
    print(f"  {'scenario':<22}{'label':<6}{'score':>6}{'tier':>9}{'prob':>7}{'sig':>5}  result")
    for row in m["rows"]:
        flag = "ok " if row["correct"] else "MISS"
        print(f"  {row['name']:<22}{row['label']:<6}{row['score']:>6.0f}"
              f"{row['tier']:>9}{row['prob']:>7.2f}{row['signals']:>5}  {flag}")

    print("\nConfusion matrix (decision = tier != DISCARD):")
    print(f"  TP={m['tp']}  FP={m['fp']}  FN={m['fn']}  TN={m['tn']}")
    print(f"  precision={m['precision']:.3f}  recall={m['recall']:.3f}  "
          f"F1={m['f1']:.3f}  accuracy={m['accuracy']:.3f}")
    print("\nHigh-confidence threshold (tier in HIGH/MEDIUM):")
    print(f"  precision={m['hi_precision']:.3f}  recall={m['hi_recall']:.3f}  F1={m['hi_f1']:.3f}")

    print("\nInvariant checks:")
    checks = _invariants()
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<32}{detail}")

    failed = [c for c in checks if not c[1]]
    f1_ok = m["f1"] >= F1_FLOOR

    print("\n" + "-" * 72)
    print(f"F1 {m['f1']:.3f} (floor {F1_FLOOR:.2f}): {'OK' if f1_ok else 'BELOW FLOOR'}")
    print(f"Invariants: {len(checks) - len(failed)}/{len(checks)} passed")
    verdict = f1_ok and not failed
    print(f"RESULT: {'PASS' if verdict else 'FAIL'}")
    print("-" * 72)
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
