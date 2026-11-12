"""Track selected mathematical combinations; prices are values, not identity."""

import json
from hashlib import sha256

from betting_scanner.live.models import LiveScanReport
from betting_scanner.live.monitor_models import CandidateTrack, MonitoringScan


def track_candidates(
    records: dict[str, CandidateTrack], report: LiveScanReport, point: MonitoringScan
) -> MonitoringScan:
    """A failed/malformed observation is not proof that a candidate disappeared."""
    prior = dict(records)
    for key, record in prior.items():
        if record.sport_key in report.plan.sport_keys:
            records[key] = record.model_copy(update={"status": "not_checked"})
    new: set[str] = set()
    continuing: set[str] = set()
    gone: set[str] = set()
    seen: set[str] = set()
    for observation in report.observations:
        for result in observation.analyses:
            calculation = result.candidate
            if calculation is None or not calculation.mathematical_arbitrage:
                continue
            assert result.event_id is not None and result.market_id is not None
            combination = tuple(
                sorted(
                    (
                        leg.quote.outcome_id,
                        leg.quote.bookmaker_id,
                        leg.quote.provider_id,
                    )
                    for leg in calculation.legs
                )
            )
            identity = json.dumps((result.event_id, result.market_id, combination))
            candidate_id = "candidate-" + sha256(identity.encode()).hexdigest()[:32]
            previous = records.get(candidate_id)
            if (
                previous is not None
                and observation.observed_at < previous.last_checked_at
            ):
                continue
            (continuing if previous is not None else new).add(candidate_id)
            seen.add(candidate_id)
            records[candidate_id] = CandidateTrack(
                id=candidate_id,
                sport_key=observation.sport_key,
                event_id=result.event_id,
                market_id=result.market_id,
                combination=combination,
                provider_event_reference=calculation.legs[
                    0
                ].quote.provider_event_reference,
                first_observed_at=previous.first_observed_at
                if previous
                else observation.observed_at,
                most_recent_observed_at=observation.observed_at,
                last_checked_at=observation.observed_at,
                status="observed",
                theoretical_roi=calculation.theoretical_roi,
                rounded_worst_case_profit=calculation.worst_case_profit,
                verification_status=result.verification_status,
                latest_result=result,
            )
        failed_events = {
            issue.provider_event_id for issue in observation.normalised.issues
        }
        for candidate_id, record in list(records.items()):
            if candidate_id in seen or record.sport_key != observation.sport_key:
                continue
            if (
                None in failed_events
                or record.provider_event_reference in failed_events
                or observation.observed_at < record.last_checked_at
            ):
                continue
            records[candidate_id] = record.model_copy(
                update={
                    "status": "no_longer_observed",
                    "last_checked_at": observation.observed_at,
                }
            )
            if (
                prior.get(candidate_id) is not None
                and prior[candidate_id].status != "no_longer_observed"
            ):
                gone.add(candidate_id)
    return point.model_copy(
        update={
            "report": report,
            "new_candidates": tuple(sorted(new)),
            "continuing_candidates": tuple(sorted(continuing)),
            "no_longer_observed": tuple(sorted(gone)),
            "not_checked": tuple(
                sorted(
                    key
                    for key, r in records.items()
                    if r.sport_key in report.plan.sport_keys
                    and r.status == "not_checked"
                )
            ),
        }
    )
