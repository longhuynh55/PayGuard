"""Reproduce counterexamples used in the (archived) plan review and docs/.

These are mathematical examples, NOT tests of the absent PayGuard implementation.
Run from the project root: python3 review/verify_plan_claims.py
"""

import json
import math
import random
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


def zero_error_bounds():
    # For zero errors: P(X=0 | p)=(1-p)**n. Set this equal to delta.
    delta = 0.05
    upper = {str(n): 1 - delta ** (1 / n) for n in (24, 36, 299)}
    minimum_n = math.ceil(math.log(delta) / math.log(0.99))
    assert minimum_n == 299
    assert (1 - 0.01) ** 298 > delta
    assert (1 - 0.01) ** 299 <= delta
    rng = random.Random(20261025)
    observations = [0] * 36
    bootstrap = [sum(rng.choices(observations, k=36)) / 36 for _ in range(1000)]
    assert set(bootstrap) == {0.0}
    return {
        "one_sided_95_percent_upper_bounds": upper,
        "minimum_zero_error_trials_for_upper_bound_at_most_1_percent": minimum_n,
        "ordinary_bootstrap_range": [min(bootstrap), max(bootstrap)],
        "assumptions": "fixed policy; independent identically distributed Bernoulli trials",
    }


def marginal_coverage_is_not_selective_accuracy():
    # A possible population with 90% set coverage, but all auto-closures wrong.
    population = [("benign", {"benign", "dangerous"}) for _ in range(90)]
    population += [("dangerous", {"benign"}) for _ in range(10)]
    coverage = sum(y in prediction for y, prediction in population) / len(population)
    closed = [(y, prediction) for y, prediction in population if prediction == {"benign"}]
    dangerous = [(y, prediction) for y, prediction in population if y == "dangerous"]
    false_closures = sum(y == "dangerous" for y, _ in closed)
    assert coverage == 0.9
    assert false_closures / len(population) <= 0.1
    assert false_closures / len(closed) == 1.0
    assert false_closures / len(dangerous) == 1.0
    return {
        "set_coverage": coverage,
        "false_closures_per_all_cases": false_closures / len(population),
        "dangerous_fraction_among_closed_cases": false_closures / len(closed),
        "closure_rate_among_dangerous_cases": false_closures / len(dangerous),
        "note": "Does not contradict marginal coverage or the KnowNo theorem.",
    }


def sql_result_agreement_is_not_semantic_correctness():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE tx (status TEXT, fraud INTEGER)")
        db.executemany("INSERT INTO tx VALUES (?, ?)", [("success", 1), ("success", 0)])
        gold = "SELECT AVG(fraud * 1.0) FROM tx"
        wrong = "SELECT AVG(fraud * 1.0) FROM tx WHERE status = 'success'"
        before = [db.execute(sql).fetchone()[0] for sql in (gold, wrong)]
        assert before[0] == before[1]
        db.execute("INSERT INTO tx VALUES ('failed', 0)")
        after = [db.execute(sql).fetchone()[0] for sql in (gold, wrong)]
        assert after[0] != after[1]
        repeated_wrong = [db.execute(wrong).fetchone()[0] for _ in range(5)]
        consistency = Counter(repeated_wrong).most_common(1)[0][1] / len(repeated_wrong)
        assert consistency == 1.0
        return {
            "question": "Fraud rate across ALL transactions",
            "gold_and_wrong_on_initial_database": before,
            "gold_and_wrong_after_counterexample_row": after,
            "consistency_of_five_wrong_answers": consistency,
        }


def availability_time_counterexample():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE tx (event_at TEXT, available_at TEXT, amount REAL)")
        db.execute("INSERT INTO tx VALUES ('2026-02-01T09:00', '2026-02-01T09:01', 10)")
        cutoff = "2026-02-01T10:00"
        safe = "SELECT SUM(amount) FROM tx WHERE event_at <= ? AND available_at <= ?"
        before = db.execute(safe, (cutoff, cutoff)).fetchone()[0]
        db.executemany(
            "INSERT INTO tx VALUES (?, ?, ?)",
            [
                ("2026-02-02T09:00", "2026-02-02T09:01", 100),
                ("2026-02-01T09:30", "2026-02-02T09:01", 20),
            ],
        )
        after = db.execute(safe, (cutoff, cutoff)).fetchone()[0]
        event_only = db.execute("SELECT SUM(amount) FROM tx WHERE event_at <= ?", (cutoff,)).fetchone()[0]
        unrestricted = db.execute("SELECT SUM(amount) FROM tx").fetchone()[0]
        assert before == after == 10
        assert event_only == 30
        assert unrestricted == 130
        return {"as_of_before": before, "as_of_after": after, "event_time_only": event_only, "unrestricted": unrestricted}


def delayed_label_counterexample():
    # Illustrative cohort; 89% of eventually reported positives have arrived.
    # This does NOT reconstruct the actual plan's simulator or quantify its bias.
    true_labels = [1] * 100 + [0] * 900
    observed_labels = [1] * 89 + [None] * 11 + [0] * 900
    naive_labels = [label if label is not None else 0 for label in observed_labels]
    pending = sum(label is None for label in observed_labels)
    assert pending == 11
    return {
        "fraction_of_positive_reports_available": 0.89,
        "true_fraud_rate": sum(true_labels) / len(true_labels),
        "fraud_rate_if_pending_means_negative": sum(naive_labels) / len(naive_labels),
        "pending_count": pending,
        "note": "A possibility proof, not evidence that 89% maturity necessarily invalidates calibration.",
    }


def conditional_versus_end_to_end():
    total_dangerous, detected, correctly_routed = 100, 20, 18
    conditional = correctly_routed / detected
    end_to_end = correctly_routed / total_dangerous
    assert math.isclose(end_to_end, (detected / total_dangerous) * conditional)
    return {"triage_success_given_detection": conditional, "successful_detection_and_routing": end_to_end}


def overlap_is_not_unique_attribution():
    # Both events occupy the same time and segment, with different effects.
    truths = [
        {"cause": "seasonal", "segment": "TRANSFER", "day": 10, "features": {"amount"}},
        {"cause": "new_fraud", "segment": "TRANSFER", "day": 10, "features": {"cashout"}},
    ]
    alerts = [
        {"segment": "TRANSFER", "day": 10, "feature": "amount"},
        {"segment": "TRANSFER", "day": 10, "feature": "cashout"},
    ]
    overlap_matches, feature_matches = [], []
    for alert in alerts:
        candidates = [t for t in truths if (t["segment"], t["day"]) == (alert["segment"], alert["day"])]
        overlap_matches.append([t["cause"] for t in candidates])
        feature_matches.append([t["cause"] for t in candidates if alert["feature"] in t["features"]])
    assert overlap_matches[0] == overlap_matches[1]
    assert feature_matches == [["seasonal"], ["new_fraud"]]
    return {
        "time_and_segment_matches": overlap_matches,
        "matches_with_known_injected_effects": feature_matches,
        "note": "Feature matching is illustrative; indirect effects require a richer attribution policy.",
    }


def early_agreement_counterexample():
    # A possible sample sequence; this is not a measured frequency of early-stop errors.
    samples = ["wrong"] * 3 + ["right"] * 7
    first = Counter(samples[:3]).most_common(1)[0]
    full = Counter(samples).most_common(1)[0]
    assert first == ("wrong", 3)
    assert full == ("right", 7)
    return {
        "after_3_samples": {"winner": first[0], "agreement": first[1] / 3},
        "after_10_samples": {"winner": full[0], "agreement": full[1] / 10},
        "note": "Adaptive stopping must be evaluated as part of the actual policy.",
    }


def label_snapshot_with_revisions():
    # Policy laboratory, not an implementation of PayGuard's missing label store.
    created = datetime(2025, 10, 1)
    events = [
        {"id": "early", "label": 0, "available_at": datetime(2025, 10, 2), "supersedes": None},
        {"id": "late", "label": 1, "available_at": datetime(2026, 2, 2), "supersedes": "early"},
    ]

    def snapshot(at, rows):
        visible = [event for event in rows if event["available_at"] <= at]
        superseded = {event["supersedes"] for event in visible}
        labels = {event["label"] for event in visible if event["id"] not in superseded}
        if len(labels) > 1:
            return "disputed"
        if labels:
            return "confirmed_fraud" if labels == {1} else "confirmed_legit"
        return "matured_negative" if at - created >= timedelta(days=90) else "pending"

    cutoff = datetime(2026, 2, 1)
    before = snapshot(cutoff, events[:1])
    after_future_insert = snapshot(cutoff, events)
    revised = snapshot(datetime(2026, 2, 3), events)
    conflicting = snapshot(cutoff, events[:1] + [
        {"id": "conflict", "label": 1, "available_at": datetime(2026, 1, 31), "supersedes": None},
    ])
    assert before == after_future_insert == "confirmed_legit"
    assert revised == "confirmed_fraud"
    assert conflicting == "disputed"
    assert snapshot(created + timedelta(days=89), []) == "pending"
    assert snapshot(created + timedelta(days=90), []) == "matured_negative"
    return {
        "historical_snapshot_before_and_after_future_revision": [before, after_future_insert],
        "snapshot_when_revision_becomes_available": revised,
        "unresolved_conflict": conflicting,
        "unreported_at_89_and_90_days": ["pending", "matured_negative"],
    }


def supervised_split_maturity():
    cutoff = datetime(2026, 2, 1)
    ranges = [
        ("fit", datetime(2025, 7, 1), datetime(2025, 9, 22)),
        ("validation", datetime(2025, 9, 22), datetime(2025, 10, 1)),
        # One month shared by isotonic, conformal (on raw scores) and the CBPE reference.
        ("calibration", datetime(2025, 10, 1), datetime(2025, 11, 1)),
    ]
    output = {}
    previous_end = None
    for role, start, end in ranges:
        assert start < end
        if previous_end is not None:
            assert start >= previous_end
        # Last representable transaction in the half-open interval.
        age_days = (cutoff - (end - timedelta(microseconds=1))).total_seconds() / 86400
        assert age_days >= 90
        output[role] = {"start_inclusive": start.isoformat(), "end_exclusive": end.isoformat(), "minimum_age_days": age_days}
        previous_end = end
    output["scope"] = "Checks age/disjointness only; class support cannot be checked without actual data."
    return output


def closure_gate_cases():
    # Action-specific policy laboratory; no actual tools, runbooks or LLM are exercised.
    def gate(action, *, evidence="sufficient", valid=True, mandatory_human=False,
             benign=False, runbook=None, source="agent", score=1, tau=1,
             rule_enabled=True, agent_disabled=False, routing_signal=False,
             prior_recheck_as_of_times=(), current_as_of=None, can_wait=False):
        if not valid or mandatory_human or evidence == "conflicting":
            return "human_review"

        def wait_or_escalate():
            if not can_wait or len(prior_recheck_as_of_times) >= 2:
                return "human_review"
            if current_as_of is not None and prior_recheck_as_of_times:
                latest_prior = max(prior_recheck_as_of_times)
                if current_as_of - latest_prior < timedelta(days=1):
                    return "human_review"
            return "recheck"

        if action == "human_review":
            return action
        if action == "recheck":
            return wait_or_escalate()
        if action in {"data_ticket", "fraud_ops"}:
            supported = (runbook == "unit_or_null_change" if action == "data_ticket"
                         else routing_signal)
            return action if evidence == "sufficient" and supported else "human_review"
        if action != "close" or not benign or runbook != "seasonal_calendar":
            return "human_review"
        authority = (rule_enabled if source == "rule"
                     else not agent_disabled and tau is not None and score >= tau)
        if evidence == "sufficient" and authority:
            return "close"
        return wait_or_escalate()

    close = {"action": "close", "benign": True, "runbook": "seasonal_calendar"}
    current_as_of = datetime(2026, 2, 10, 16, 59, 59, 999999)
    cases = {
        "complete_benign": (close, "close"),
        "small_sample": ({**close, "evidence": "insufficient", "can_wait": True}, "recheck"),
        "seasonal_plus_fraud": ({**close, "mandatory_human": True}, "human_review"),
        "invalid_tool_snapshot": ({**close, "valid": False}, "human_review"),
        "recheck_exhausted": (
            {**close, "evidence": "insufficient", "can_wait": True,
             "current_as_of": current_as_of,
             "prior_recheck_as_of_times": (current_as_of - timedelta(days=2),
                                           current_as_of - timedelta(days=1))},
            "human_review"),
        "recheck_too_soon": (
            {"action": "recheck", "can_wait": True, "current_as_of": current_as_of,
             "prior_recheck_as_of_times": (current_as_of - timedelta(hours=23),)},
            "human_review"),
        "recheck_24_hours_apart": (
            {"action": "recheck", "can_wait": True, "current_as_of": current_as_of,
             "prior_recheck_as_of_times": (current_as_of - timedelta(days=1),)},
            "recheck"),
        "below_threshold": ({**close, "score": 2 / 3, "can_wait": True}, "recheck"),
        "agent_closure_disabled": ({**close, "agent_disabled": True}, "human_review"),
        "rule_closure_disabled": ({**close, "source": "rule", "rule_enabled": False}, "human_review"),
        "rule_disabled_does_not_disable_agent": ({**close, "rule_enabled": False}, "close"),
        "benign_without_runbook": ({**close, "runbook": None}, "human_review"),
        "pipeline_routes_without_benign_or_close_authority": (
            {"action": "data_ticket", "runbook": "unit_or_null_change", "score": 0,
             "tau": None, "agent_disabled": True, "rule_enabled": False}, "data_ticket"),
        "pipeline_and_fraud_conflict": (
            {"action": "data_ticket", "runbook": "unit_or_null_change", "mandatory_human": True}, "human_review"),
        "fraud_signal_routes_without_close_score": (
            {"action": "fraud_ops", "routing_signal": True, "score": 0, "tau": None}, "fraud_ops"),
        "fraud_without_supported_signal": ({"action": "fraud_ops"}, "human_review"),
        "pipeline_without_checklist": ({"action": "data_ticket"}, "human_review"),
        "routing_with_invalid_snapshot": (
            {"action": "fraud_ops", "routing_signal": True, "valid": False}, "human_review"),
        "conflicting_routing_evidence": (
            {"action": "fraud_ops", "routing_signal": True, "evidence": "conflicting"}, "human_review"),
        "explicit_human_review": ({"action": "human_review", "score": 0, "tau": None}, "human_review"),
        "recheck_needs_a_waiting_reason": ({"action": "recheck"}, "human_review"),
        "explicit_recheck_with_waiting_reason": ({"action": "recheck", "can_wait": True}, "recheck"),
    }
    actual = {name: gate(**args) for name, (args, _) in cases.items()}
    for name, (_, expected) in cases.items():
        assert actual[name] == expected, name
    return {"illustrative_gate_outcomes": actual, "scope": "Toy action router; no actual tool, rule, fraud model or LLM was exercised."}


def separated_rule_agent_authority():
    # Calibration cases: (source, runbook, close_score, requires_intervention).
    # Rule proposals carry a fixed score of 1; runbook "seasonal_calendar" once closes a fraud.
    rule_cases = [
        ("rule", "seasonal_calendar", 1, False),
        ("rule", "seasonal_calendar", 1, True),
    ]
    agent_cases = [
        ("agent", None, 1, False),
        ("agent", None, 1, False),
        ("agent", None, 2 / 3, True),
    ]
    taus = (2 / 3, 1)

    def select_tau(cases):
        # Max correct benign closures with zero observed unsafe closures; ties -> larger tau.
        best = None
        for tau in taus:
            closed = [c for c in cases if c[2] >= tau]
            if any(c[3] for c in closed):
                continue
            good = sum(not c[3] for c in closed)
            if good and (best is None or good >= best[1]):
                best = (tau, good)
        return None if best is None else best[0]

    joint_tau = select_tau(rule_cases + agent_cases)
    assert joint_tau is None  # One bad runbook disables every closure.

    runbooks = sorted({c[1] for c in rule_cases})
    enabled = {}
    for runbook in runbooks:
        closed = [c for c in rule_cases if c[1] == runbook]
        enabled[runbook] = not any(c[3] for c in closed) and any(not c[3] for c in closed)
    # Cases of a disabled runbook go to the agent, which re-scores them.
    rerouted = [("agent", r, 2 / 3 if unsafe else 1, unsafe)
                for _, r, _, unsafe in rule_cases if not enabled[r]]
    agent_tau = select_tau(agent_cases + rerouted)
    assert enabled == {"seasonal_calendar": False}
    assert agent_tau == 1
    return {
        "joint_tau": joint_tau,
        "runbook_close_enabled": enabled,
        "agent_tau_after_rule_lock": agent_tau,
        "note": "Toy scores; the rerouted agent scores are assumed, not measured.",
    }


def decision_time_scoring():
    # Seasonal effect available from day 1, fraud in the same segment from day 4.
    incidents_truth = [
        {"cause": "seasonal", "requires_intervention": False, "effect_available_day": 1},
        {"cause": "new_fraud", "requires_intervention": True, "effect_available_day": 4},
    ]
    decisions = [
        {"incident": "I1", "parent": None, "decision_day": 1, "action": "close"},
        {"incident": "I2", "parent": "I1", "decision_day": 4, "action": "human_review"},
    ]

    def unsafe_at(decision, horizon_day):
        active = [t for t in incidents_truth if t["effect_available_day"] <= horizon_day]
        return decision["action"] == "close" and any(t["requires_intervention"] for t in active)

    final_label = {d["incident"]: unsafe_at(d, 10**9) for d in decisions}
    at_decision = {d["incident"]: unsafe_at(d, d["decision_day"]) for d in decisions}
    assert final_label["I1"] is True       # Retroactive penalty from a later incident.
    assert at_decision["I1"] is False      # Correct at the time it was made.
    fraud_truth = {"accepted_actions": {"human_review", "fraud_ops"}, "intervention_start_day": 4,
                   "matched_incidents": {"I2"}}
    fraud_truth["deadline_day"] = fraud_truth["intervention_start_day"] + 7

    def routed_in_time(decision_log, truth):
        # Matched incident, start <= decision <= deadline, accepted action; recheck/close never count.
        return any(d["incident"] in truth["matched_incidents"]
                   and truth["intervention_start_day"] <= d["decision_day"] <= truth["deadline_day"]
                   and d["action"] in truth["accepted_actions"]
                   for d in decision_log)

    fraud_routed = routed_in_time(decisions, fraud_truth)
    assert fraud_routed                    # Fraud still counted through child incident I2.
    only_recheck = [{"incident": "I2", "decision_day": 4, "action": "recheck"},
                    {"incident": "I2", "decision_day": 5, "action": "recheck"}]
    assert not routed_in_time(only_recheck, fraud_truth)
    too_late = [{"incident": "I2", "decision_day": 12, "action": "human_review"}]
    assert not routed_in_time(too_late, fraud_truth)
    other_incident_before_start = [{"incident": "I9", "decision_day": 1, "action": "human_review"}]
    assert not routed_in_time(other_incident_before_start, fraud_truth)
    matched_before_start = [{"incident": "I2", "decision_day": 3, "action": "human_review"}]
    assert not routed_in_time(matched_before_start, fraud_truth)
    late_close = {"incident": "I1", "decision_day": 5, "action": "close"}
    assert unsafe_at(late_close, late_close["decision_day"])  # Closing after fraud is available is unsafe.
    no_child = [d for d in decisions if d["parent"] is None]
    assert not any(d["decision_day"] >= 4 for d in no_child)  # Without I2 the fraud is a monitoring miss.
    return {
        "unsafe_by_final_label": final_label,
        "unsafe_at_decision_time": at_decision,
        "fraud_routed_via_child_incident": fraud_routed,
        "recheck_only_counts_as_routed": False,
        "routed_after_deadline_counts": False,
        "other_incident_or_before_start_counts": False,
        "close_after_fraud_available_is_unsafe": True,
    }


def incident_grouping_window_boundaries():
    window_days = 2

    def placement(*, status, latest_alert_day, alert_day):
        within_window = 0 <= alert_day - latest_alert_day <= window_days
        if not within_window:
            return "independent"
        return "merge" if status in {"open", "recheck"} else "child"

    # The open window slides from the most recent alert, including exactly day 2.
    assert placement(status="open", latest_alert_day=2, alert_day=4) == "merge"
    assert placement(status="open", latest_alert_day=2, alert_day=4.000001) == "independent"
    # Finalization does not move the anchor from the incident's latest alert.
    assert placement(status="close", latest_alert_day=2, alert_day=4) == "child"
    assert placement(status="close", latest_alert_day=2, alert_day=4.000001) == "independent"
    return {
        "open_exact_two_day_gap_merges": True,
        "final_exact_two_day_gap_creates_child": True,
        "gap_over_two_days_is_independent": True,
        "window_anchor": "latest_alert_at",
    }


def gate_ablation_same_evidence():
    # Three traces conclude dangerous, benign, benign. Both arms share the majority conclusion
    # and the same checklist; only the close_score condition differs.
    votes = ["dangerous", "benign", "benign"]
    first_trace = votes[0]
    majority = max(set(votes), key=votes.count) if max(votes.count(v) for v in votes) >= 2 else None
    assert first_trace != majority  # Reading only trace 1 would change the evidence, not just the gate.
    v_benign = votes.count("benign") / len(votes)
    min_sql_consistency = 1.0
    close_score = min(v_benign, min_sql_consistency)
    checklist_ok = True

    def arm(use_gate, tau):
        if majority != "benign" or not checklist_ok:
            return "human_review"
        return "close" if not use_gate or close_score >= tau else "recheck"

    outcomes = {"no_gate": arm(False, None), "gate_tau_2_3": arm(True, 2 / 3), "gate_tau_1": arm(True, 1)}
    assert outcomes == {"no_gate": "close", "gate_tau_2_3": "close", "gate_tau_1": "recheck"}
    return {
        "first_trace": first_trace,
        "majority_conclusion": majority,
        "outcomes": outcomes,
        "note": "With tau=2/3 and non-binding SQL consistency the arms coincide; tau=1 differs.",
    }


def seasonal_runbook_daily_check():
    window = 7
    max_overlap = {f"{d}_day_event": round(d / window, 3) for d in (1, 3, 4)}
    assert [v >= 0.5 for v in max_overlap.values()] == [False, False, True]

    expected_effect, tolerance = 0.40, 0.5

    def expected_window(kind, numerator, numerator_change, denominator=None, denominator_change=None):
        if len(numerator) != window or len(numerator_change) != window:
            return None
        expected_numerator = sum(n * (1 + r) for n, r in zip(numerator, numerator_change))
        if kind == "sum":
            return expected_numerator
        if kind not in {"ratio", "mean"} or denominator is None or denominator_change is None:
            return None
        if len(denominator) != window or len(denominator_change) != window:
            return None
        expected_denominator = sum(d * (1 + r) for d, r in zip(denominator, denominator_change))
        return expected_numerator / expected_denominator if expected_denominator > 0 else None

    def passes(daily, baseline, impact_days):
        if len(daily) != window or len(baseline) != window:
            return False
        # Location uses the daily reference, allowing unequal daily traffic.
        others_ok = all(abs(v - baseline[i]) <= max(1.0, .05 * abs(baseline[i]))
                        for i, v in enumerate(daily) if i not in impact_days)
        changes = [expected_effect if day in impact_days else 0 for day in range(window)]
        expected = expected_window("sum", baseline, changes)
        if expected is None:
            return False
        expected_delta = expected - sum(baseline)
        # Illustrative absolute tolerance + relative tolerance on the expected change.
        return others_ok and abs(sum(daily) - expected) <= 1.0 + tolerance * abs(expected_delta)

    payday = [100, 100, 140, 100, 100, 100, 100]
    payday_plus_fraud = [100, 100, 140, 100, 125, 120, 100]
    assert passes(payday, [100] * window, {2})
    assert not passes(payday_plus_fraud, [100] * window, {2})
    unequal_baseline = [1000, 10, 10, 10, 10, 10, 10]
    unequal_payday = [1400, 10, 10, 10, 10, 10, 10]
    assert passes(unequal_payday, unequal_baseline, {0})
    changes = [expected_effect, 0, 0, 0, 0, 0, 0]
    expected_total = expected_window("sum", unequal_baseline, changes)
    observed_lift = sum(unequal_payday) / sum(unequal_baseline) - 1
    weighted_lift = (expected_total - sum(unequal_baseline)) / sum(unequal_baseline)
    wrong_uniform_lift = expected_effect / window
    assert math.isclose(observed_lift, weighted_lift)
    assert abs(observed_lift - wrong_uniform_lift) > tolerance * wrong_uniform_lift

    # A rate with twice the exposure on one day must aggregate numerator/denominator.
    no_change = [0] * window
    doubled_exposure = [1, 0, 0, 0, 0, 0, 0]
    expected_rate = expected_window("ratio", [10] * window, no_change,
                                    [100] * window, doubled_exposure)
    daily_rate_average = (.05 + .1 * 6) / window
    assert math.isclose(expected_rate, 70 / 800)
    assert not math.isclose(expected_rate, daily_rate_average)
    expected_mean = expected_window("mean", [100] * window, changes,
                                    [1] * window, doubled_exposure)
    assert math.isclose(expected_mean, 740 / 8)
    assert expected_window("ratio", [10] * window, no_change, [0] * window, no_change) is None
    assert expected_window("ratio", [10] * window, no_change) is None
    assert expected_window("psi", [100] * window, changes) is None
    return {
        "max_overlap_by_event_length": max_overlap,
        "one_day_payday_passes_daily_check": True,
        "payday_plus_fraud_passes_daily_check": False,
        "unequal_daily_baseline_passes": True,
        "unequal_baseline_observed_lift": observed_lift,
        "weighted_expected_lift": weighted_lift,
        "incorrect_uniform_expected_lift": wrong_uniform_lift,
        "ratio_of_totals": expected_rate,
        "incorrect_mean_of_daily_rates": daily_rate_average,
        "expected_mean": expected_mean,
        "invalid_denominator_or_unsupported_metric": "insufficient",
        "note": "Toy daily series; tolerances are illustrative. Same-day hidden fraud may still pass.",
    }


def policy_independent_risk_units():
    truths = {
        "F1": {"episode": "E1", "requires_intervention": True, "effect_day": 1},
        "F2": {"episode": "E2", "requires_intervention": True, "effect_day": 1},
        "B1": {"episode": "E3", "requires_intervention": False, "effect_day": 1},
    }

    def summarize(decisions):
        dangerous_truths = {key for key, value in truths.items() if value["requires_intervention"]}
        dangerous_episodes = {truths[key]["episode"] for key in dangerous_truths}
        unsafe_truths, unsafe_incidents, dangerous_incidents = set(), set(), set()
        detected_truths, routed_truths, rewarded_routes, rewarded_benign = set(), set(), set(), set()
        unadjudicated = 0
        for decision in decisions:
            matched = decision["truth_ids"]
            if not matched:
                unadjudicated += decision["action"] == "close"
                continue
            active = {key for key in matched if key in dangerous_truths
                      and truths[key]["effect_day"] <= decision["day"]}
            if decision["action"] == "close" and not active:
                rewarded_benign.update(key for key in matched
                                      if not truths[key]["requires_intervention"]
                                      and truths[key]["effect_day"] <= decision["day"])
            if active:
                detected_truths.update(active)
                dangerous_incidents.add(decision["incident"])
                if decision["action"] == "close":
                    unsafe_truths.update(active)
                    unsafe_incidents.add(decision["incident"])
                if decision["action"] == "fraud_ops":
                    rewarded_routes.update(active)
                    routed_truths.update(key for key in active
                                         if decision["day"] <= truths[key]["effect_day"] + 7)
        unsafe_episodes = {truths[key]["episode"] for key in unsafe_truths}
        return {
            "dangerous_incident_count": len(dangerous_incidents),
            "dangerous_closure_rate": len(unsafe_incidents) / len(dangerous_incidents) if dangerous_incidents else None,
            "unsafe_truth_rate": len(unsafe_truths) / len(dangerous_truths),
            "unsafe_episode_rate": len(unsafe_episodes) / len(dangerous_episodes),
            "detection_recall": len(detected_truths) / len(dangerous_truths),
            "timely_routing_recall": len(routed_truths) / len(dangerous_truths),
            "distinct_routing_rewards": len(rewarded_routes),
            "distinct_benign_closure_rewards": len(rewarded_benign),
            "unsafe_truth_ids": sorted(unsafe_truths),
            "unadjudicated_closures": unadjudicated,
            "assessment": "observed_lower_bound" if unadjudicated else "fully_matched_example",
        }

    daily_final = [{"incident": f"I{index}", "day": index + 1, "truth_ids": {"F1"},
                    "action": "close" if index == 0 else "fraud_ops"} for index in range(7)]
    grouped = [{"incident": incident, "day": day, "truth_ids": {"F1"}, "action": action}
               for day, incident, action in [
                   (1, "I0", "close"),
                   (2, "I1", "recheck"), (3, "I1", "recheck"), (4, "I1", "fraud_ops"),
                   (5, "I2", "recheck"), (6, "I2", "recheck"), (7, "I2", "fraud_ops"),
               ]]
    # Same seven daily alerts; rechecks keep each child open until its final routing.
    # Both policies miss F2 and close F1 incorrectly once, with different routing times.
    assert len(daily_final) == len(grouped) == 7
    many_children = summarize(daily_final)
    fewer_children = summarize(grouped)
    assert math.isclose(many_children["dangerous_closure_rate"], 1 / 7)
    assert math.isclose(fewer_children["dangerous_closure_rate"], 1 / 3)
    for result in (many_children, fewer_children):
        assert result["unsafe_truth_rate"] == result["unsafe_episode_rate"] == .5
        assert result["detection_recall"] == result["timely_routing_recall"] == .5
        assert result["distinct_routing_rewards"] == 1
    repeated = summarize(daily_final + [
        {"incident": "repeated", "day": 8, "truth_ids": {"F1"}, "action": "close"},
        {"incident": "safe_benign", "day": 2, "truth_ids": {"B1"}, "action": "close"},
        {"incident": "same_benign_child", "day": 3, "truth_ids": {"B1"}, "action": "close"},
    ])
    assert repeated["unsafe_truth_ids"] == ["F1"]
    assert repeated["unsafe_truth_rate"] == .5
    assert repeated["distinct_benign_closure_rewards"] == 1
    mixed = summarize([
        {"incident": "mixed", "day": 2, "truth_ids": {"B1", "F1"}, "action": "close"},
    ])
    assert mixed["distinct_benign_closure_rewards"] == 0
    assert mixed["unsafe_truth_ids"] == ["F1"]
    unresolved = summarize(grouped + [
        {"incident": "unmatched", "day": 4, "truth_ids": set(), "action": "close"},
    ])
    assert unresolved["assessment"] == "observed_lower_bound"
    assert unresolved["unadjudicated_closures"] == 1
    no_alerts = summarize([])
    assert no_alerts["dangerous_closure_rate"] is None
    assert no_alerts["unsafe_truth_rate"] == 0  # Must be accompanied by detection/routing recall = 0.
    assert no_alerts["detection_recall"] == no_alerts["timely_routing_recall"] == 0
    return {
        "seven_child_cases": many_children,
        "three_child_cases": fewer_children,
        "repeated_unsafe_closure_same_truth": repeated,
        "mixed_cause_closure_gets_no_benign_reward": mixed,
        "unadjudicated_closure": unresolved,
        "no_alerts": no_alerts,
        "note": "Fixed truth/episode denominators; zero closures do not establish detection success.",
    }


def agent_closure_reachability():
    def path(has_benign_runbook, checklist_passed, rule_enabled):
        eligible = has_benign_runbook and checklist_passed
        rule_closed = eligible and rule_enabled
        return {"rule_closed": rule_closed,
                "agent_received": not rule_closed,
                "agent_can_consider_closing": not rule_closed and eligible}

    rule_handles_supported = path(True, True, True)
    outside_runbook = path(False, True, True)
    failed_checklist = path(True, False, True)
    disabled_runbook = path(True, True, False)
    assert rule_handles_supported == {"rule_closed": True, "agent_received": False,
                                      "agent_can_consider_closing": False}
    assert outside_runbook["agent_received"] and not outside_runbook["agent_can_consider_closing"]
    assert failed_checklist["agent_received"] and not failed_checklist["agent_can_consider_closing"]
    assert disabled_runbook["agent_can_consider_closing"]
    return {
        "supported_rule_enabled": rule_handles_supported,
        "outside_benign_runbook": outside_runbook,
        "failed_checklist": failed_checklist,
        "supported_rule_disabled": disabled_runbook,
        "note": "Reachability under identical scope/snapshot, not evidence of LLM capability or actual closures.",
    }


def episode_risk_and_zero_denominator():
    # 20 correlated unsafe incident closures occur in only one of two episodes.
    per_episode = [[1] * 20, [0] * 20]
    episode_losses = [max(incidents) for incidents in per_episode]
    assert episode_losses == [1, 0]
    independent_trial_count = len(episode_losses)
    assert independent_trial_count == 2

    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None

    assert ratio(0, 0) is None
    assert ratio(0, 20) == 0.0
    return {
        "incident_count": sum(map(len, per_episode)),
        "episode_bernoulli_trial_count": independent_trial_count,
        "episode_losses": episode_losses,
        "zero_denominator_rate": ratio(0, 0),
        "note": "40 incidents are not 40 independent episode-level Bernoulli trials.",
    }


def pilot_episode_allocation_arithmetic():
    # One two-cause episode is one split unit, although it contributes two truth rows.
    config_path = Path(__file__).resolve().parents[1] / "configs" / "episodes.yaml"
    lines = config_path.read_text(encoding="utf-8").splitlines()
    in_pilot = False
    in_role_episode_counts = False
    in_assignments = False
    role_totals = {}
    assignments = []
    current_role = None
    for line in lines:
        if line == "  pilot:":
            in_pilot = True
            continue
        if not in_pilot:
            continue
        if line == "    role_episode_counts:":
            in_role_episode_counts = True
            continue
        if line == "    clean_episode_counts:":
            in_role_episode_counts = False
            continue
        role_match = re.fullmatch(r"      (development|calibration|test):", line)
        if in_role_episode_counts and role_match:
            current_role = role_match.group(1)
            continue
        value_match = re.fullmatch(r"        value: (\d+)", line)
        if in_role_episode_counts and value_match and current_role is not None:
            role_totals[current_role] = int(value_match.group(1))
            continue
        if line == "    assignments:":
            in_assignments = True
            continue
        if in_assignments:
            assignment_match = re.fullmatch(
                r"      - \{episode_id: ([^,]+), role: ([^,]+), template: ([^}]+)\}",
                line,
            )
            if assignment_match:
                assignments.append(
                    {
                        "episode_id": assignment_match.group(1),
                        "role": assignment_match.group(2),
                        "template": assignment_match.group(3),
                    }
                )
    if set(role_totals) != {"development", "calibration", "test"} or not assignments:
        raise AssertionError("could not read pilot role totals and assignments using the stdlib parser")
    roles = ("development", "calibration", "test")
    matrix = {}
    for assignment in assignments:
        template = assignment["template"]
        role = assignment["role"]
        counts = matrix.setdefault(template, {name: 0 for name in roles})
        counts[role] += 1
    assert {
        role: sum(counts[role] for counts in matrix.values()) for role in roles
    } == role_totals
    assert sum(role_totals.values()) == len(assignments)
    two_cause_by_role = matrix["two_cause_fraud_seasonal"]
    fraud_calendar_by_role = matrix["fraud_calendar"]
    assert sum(two_cause_by_role.values()) == sum(
        assignment["template"] == "two_cause_fraud_seasonal" for assignment in assignments
    )
    return {
        "template_episode_counts_by_role": matrix,
        "two_cause_episode_counts_by_role": two_cause_by_role,
        "fraud_calendar_episode_counts_by_role": fraud_calendar_by_role,
        "role_totals": role_totals,
        "pilot_episode_total": len(assignments),
        "note": "Episode allocations are split units; two-cause episodes do not count as two episodes.",
    }


def main():
    examples = [
        zero_error_bounds,
        marginal_coverage_is_not_selective_accuracy,
        sql_result_agreement_is_not_semantic_correctness,
        availability_time_counterexample,
        delayed_label_counterexample,
        conditional_versus_end_to_end,
        overlap_is_not_unique_attribution,
        early_agreement_counterexample,
        label_snapshot_with_revisions,
        supervised_split_maturity,
        closure_gate_cases,
        separated_rule_agent_authority,
        decision_time_scoring,
        incident_grouping_window_boundaries,
        seasonal_runbook_daily_check,
        gate_ablation_same_evidence,
        policy_independent_risk_units,
        agent_closure_reachability,
        episode_risk_and_zero_denominator,
        pilot_episode_allocation_arithmetic,
    ]
    results = {example.__name__: example() for example in examples}
    results["scope"] = "20 illustrative calculations/counterexamples and protocol checks; not PayGuard implementation validation"
    destination = Path(__file__).with_name("verification_results.json")
    destination.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Verified {len(examples)} illustrative examples. Results: {destination}")


if __name__ == "__main__":
    main()
