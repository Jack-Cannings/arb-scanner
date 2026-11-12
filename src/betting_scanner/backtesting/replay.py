"""Chronological replay without quote carry-forward or future-result access."""

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Context, Decimal, localcontext

from betting_scanner.backtesting.execution import PendingOrder, execute_order
from betting_scanner.backtesting.models import (
    BacktestMetrics,
    BacktestReport,
    BankrollPoint,
    BetDisposition,
    ExecutionStatus,
    HistoricalDataset,
    HistoricalSnapshot,
    PaperBet,
    PaperTrade,
    ReplayConfig,
    ReplayObservation,
    SettlementRecord,
)
from betting_scanner.core.models import OddsSnapshot, normalise_utc
from betting_scanner.strategies.arbitrage import detect_arbitrage
from betting_scanner.strategies.arbitrage.calculation import PENNY
from betting_scanner.strategies.arbitrage.results import (
    ActionabilityStatus,
    ArbitrageCalculation,
)

ZERO = Decimal(0)
SettlementKey = tuple[str, str, str, str]


@dataclass
class ReplayClock:
    current: datetime | None = None

    def advance(self, timestamp: datetime) -> datetime:
        timestamp = normalise_utc(timestamp)
        if self.current is not None and timestamp < normalise_utc(self.current):
            raise ValueError("Replay clock cannot move backwards")
        self.current = timestamp
        return timestamp


def _fingerprint(
    snapshot: OddsSnapshot, market_id: str, calculation: ArbitrageCalculation
) -> str:
    market = next(m for m in snapshot.markets if m.id == market_id)
    definition = market.model_dump(mode="json")
    definition["outcomes"] = sorted(definition["outcomes"], key=lambda o: o["id"])
    definition["expected_outcome_ids"] = sorted(market.expected_outcome_ids or ())
    return json.dumps(
        {
            "event": snapshot.event.id,
            "market": definition,
            "legs": [
                {
                    "outcome": leg.quote.outcome_id,
                    "bookmaker": leg.quote.bookmaker_id,
                    "provider": leg.quote.provider_id,
                    "odds": str(
                        leg.quote.decimal_odds.normalize(
                            context=Context(
                                prec=len(leg.quote.decimal_odds.as_tuple().digits)
                            )
                        )
                    ),
                    "rule": leg.quote.settlement_rule.model_dump(mode="json"),
                }
                for leg in calculation.legs
            ],
        },
        sort_keys=True,
    )


def _available(
    record: HistoricalSnapshot, now: datetime
) -> tuple[OddsSnapshot, tuple[str, ...]]:
    known = [
        q for q in record.snapshot.quotes if (q.observed_at or q.collected_at) <= now
    ]
    unavailable = tuple(
        f"unobserved_quote:{q.bookmaker_id}:{q.outcome_id}"
        for q in record.snapshot.quotes
        if (q.observed_at or q.collected_at) > now
    )
    data = record.snapshot.model_dump()
    data["quotes"] = tuple(q.model_dump() for q in known)
    return OddsSnapshot.model_validate(data), unavailable


def _open_stake(trades: list[PaperTrade], event_id: str | None = None) -> Decimal:
    return sum(
        (
            t.accepted_stake
            for t in trades
            if t.accepted_stake > 0
            and t.settled_at is None
            and (event_id is None or t.event_id == event_id)
        ),
        ZERO,
    )


def _settle(
    trade: PaperTrade, known: dict[SettlementKey, SettlementRecord], now: datetime
) -> PaperTrade:
    if trade.settled_at is not None or trade.accepted_stake == 0:
        return trade
    accepted = [b for b in trade.bets if b.accepted]
    records = [
        known.get(
            (
                trade.event_id,
                trade.market_id,
                b.planned_quote.bookmaker_id,
                b.planned_quote.outcome_id,
            )
        )
        for b in accepted
    ]
    if any(r is None for r in records):
        return trade
    for bet, record in zip(accepted, records, strict=True):
        assert record is not None and bet.execution_quote is not None
        if (
            not record.settlement_rule.verified
            or record.settlement_rule != bet.execution_quote.settlement_rule
            or record.observed_at < trade.event_start
            or record.observed_at < trade.executed_at
        ):
            reason = "unverified_or_incompatible_settlement_evidence"
            return trade.model_copy(
                update={"reasons": tuple(dict.fromkeys((*trade.reasons, reason)))}
            )
    bets: list[PaperBet] = []
    for bet in trade.bets:
        if not bet.accepted:
            bets.append(bet)
            continue
        record = known[
            (
                trade.event_id,
                trade.market_id,
                bet.planned_quote.bookmaker_id,
                bet.planned_quote.outcome_id,
            )
        ]
        assert bet.execution_quote is not None
        payout = (
            bet.stake
            if record.disposition == BetDisposition.VOID
            else (bet.stake * bet.execution_quote.decimal_odds).quantize(
                PENNY, rounding=ROUND_FLOOR
            )
            if record.disposition == BetDisposition.WON
            else ZERO
        )
        bets.append(
            bet.model_copy(
                update={
                    "disposition": record.disposition,
                    "payout": payout,
                    "settlement_source": record.source,
                }
            )
        )
    total_payout = sum((b.payout for b in bets if b.payout is not None), ZERO)
    return trade.model_copy(
        update={
            "bets": tuple(bets),
            "settled_at": now,
            "realised_profit": total_payout - trade.accepted_stake,
        }
    )


def _metrics(
    dataset: HistoricalDataset,
    observations: list[ReplayObservation],
    trades: list[PaperTrade],
    history: list[BankrollPoint],
) -> BacktestMetrics:
    theoretical = [
        o.detection.eligible.theoretical_roi
        for o in observations
        if o.new_eligible_opportunity and o.detection.eligible is not None
    ]
    settled = [t for t in trades if t.settled_at is not None]
    realised = [
        t.realised_profit / t.accepted_stake * 100
        for t in settled
        if t.realised_profit is not None
    ]
    peak = history[0].equity_at_cost
    drawdown = ZERO
    for point in history:
        peak = max(peak, point.equity_at_cost)
        drawdown = max(drawdown, peak - point.equity_at_cost)
    return BacktestMetrics(
        total_snapshots=len(dataset.snapshots),
        unique_events=len({r.snapshot.event.id for r in dataset.snapshots}),
        mathematical_candidates=sum(o.new_candidate for o in observations),
        eligible_opportunities=sum(o.new_eligible_opportunity for o in observations),
        execution_attempts=len(trades),
        fully_executed=sum(t.execution_status == ExecutionStatus.FULL for t in trades),
        partially_executed=sum(
            t.execution_status == ExecutionStatus.PARTIAL for t in trades
        ),
        failed_executions=sum(
            t.execution_status == ExecutionStatus.FAILED for t in trades
        ),
        settled_positions=len(settled),
        unsettled_positions=sum(
            t.accepted_stake > 0 and t.settled_at is None for t in trades
        ),
        average_theoretical_roi=sum(theoretical, ZERO) / len(theoretical)
        if theoretical
        else None,
        average_realised_roi=sum(realised, ZERO) / len(realised) if realised else None,
        total_stakes=sum((t.accepted_stake for t in trades), ZERO),
        realised_pnl=sum(
            (t.realised_profit for t in settled if t.realised_profit is not None), ZERO
        ),
        unsettled_exposure=_open_stake(trades),
        ending_cash=history[-1].cash,
        ending_equity_at_cost=history[-1].equity_at_cost,
        maximum_drawdown=drawdown,
        largest_simulated_loss=max(
            (
                max(ZERO, -t.realised_profit)
                for t in settled
                if t.realised_profit is not None
            ),
            default=ZERO,
        ),
    )


def run_replay(
    dataset: HistoricalDataset,
    config: ReplayConfig | None = None,
    *,
    clock: ReplayClock | None = None,
) -> BacktestReport:
    """Run an independent deterministic simulation; no files, network or wall clock.

    Each event observation is a complete replacement, never a delta. Delayed
    orders require an observation at the exact execution timestamp. No old price
    is assumed executable in an observation gap. Settlements become visible only
    at their observation times and are credited only for fully resolved positions.
    """
    dataset = HistoricalDataset.model_validate(dataset.model_dump())
    config = ReplayConfig.model_validate((config or ReplayConfig()).model_dump())
    clock = clock or ReplayClock()
    # The ledger context never inspects prices in future observations. The
    # arbitrage engine retains its independent input-sized calculation context.
    with localcontext(Context(prec=80)):
        return _run(dataset, config, clock)


def _run(
    dataset: HistoricalDataset, config: ReplayConfig, clock: ReplayClock
) -> BacktestReport:
    observations_by_time: dict[datetime, list[HistoricalSnapshot]] = {}
    settlements_by_time: dict[datetime, list[SettlementRecord]] = {}
    for record in dataset.snapshots:
        observations_by_time.setdefault(record.observed_at, []).append(record)
    for settlement in dataset.settlements:
        settlements_by_time.setdefault(settlement.observed_at, []).append(settlement)
    timeline = set(observations_by_time) | set(settlements_by_time)
    pending: dict[datetime, list[PendingOrder]] = {}
    known_results: dict[SettlementKey, SettlementRecord] = {}
    observations: list[ReplayObservation] = []
    trades: list[PaperTrade] = []
    history: list[BankrollPoint] = []
    seen_candidates: set[str] = set()
    seen_eligible: set[str] = set()
    cash = config.starting_bankroll

    def record_balance(now: datetime, reason: str) -> None:
        exposure = _open_stake(trades)
        history.append(
            BankrollPoint(
                timestamp=now,
                reason=reason,
                cash=cash,
                open_stake=exposure,
                equity_at_cost=cash + exposure,
            )
        )

    def execute(
        order: PendingOrder, current: dict[str, OddsSnapshot], now: datetime
    ) -> None:
        nonlocal cash
        trade = execute_order(
            order,
            current.get(order.snapshot.event.id),
            config,
            dataset.execution_instructions,
            now=now,
            trade_id=f"paper-{len(trades) + 1:06d}",
            cash=cash,
            event_exposure=_open_stake(trades, order.snapshot.event.id),
            aggregate_exposure=_open_stake(trades),
        )
        trades.append(trade)
        cash -= trade.accepted_stake
        record_balance(now, f"execution:{trade.id}:{trade.execution_status}")

    while timeline:
        now = clock.advance(min(timeline))
        timeline.remove(now)
        if not history:
            record_balance(now, "starting_bankroll")
        batch = sorted(
            observations_by_time.get(now, []), key=lambda r: r.snapshot.event.id
        )
        current: dict[str, OddsSnapshot] = {}
        notes_by_event: dict[str, tuple[str, ...]] = {}
        for record in batch:
            (
                current[record.snapshot.event.id],
                notes_by_event[record.snapshot.event.id],
            ) = _available(record, now)
        for settlement in settlements_by_time.get(now, []):
            known_results[
                (
                    settlement.event_id,
                    settlement.market_id,
                    settlement.bookmaker_id,
                    settlement.outcome_id,
                )
            ] = settlement
        for index, old_trade in enumerate(trades):
            updated = _settle(old_trade, known_results, now)
            trades[index] = updated
            if old_trade.settled_at is None and updated.settled_at is not None:
                cash += sum(
                    (b.payout for b in updated.bets if b.payout is not None), ZERO
                )
                record_balance(now, f"settlement:{updated.id}")
        for order in pending.pop(now, []):
            execute(order, current, now)
        for event_id in sorted(current):
            snapshot = current[event_id]
            for market in sorted(snapshot.markets, key=lambda m: m.id):
                detection = detect_arbitrage(
                    snapshot,
                    market.id,
                    total_stake=config.stake_budget,
                    evaluated_at=now,
                    config=config.engine,
                )
                candidate = detection.candidate
                new_candidate = False
                if candidate is not None and candidate.mathematical_arbitrage:
                    fingerprint = _fingerprint(snapshot, market.id, candidate)
                    new_candidate = fingerprint not in seen_candidates
                    seen_candidates.add(fingerprint)
                calculation = detection.eligible
                new_eligible = False
                notes = list(notes_by_event[event_id])
                if (
                    detection.actionability_status == ActionabilityStatus.ACTIONABLE
                    and calculation is not None
                ):
                    if (
                        calculation.worst_case_roi < config.minimum_roi
                        or calculation.worst_case_profit < config.minimum_profit
                    ):
                        notes.append("below_minimum_roi_or_profit")
                    else:
                        fingerprint = _fingerprint(snapshot, market.id, calculation)
                        new_eligible = fingerprint not in seen_eligible
                        seen_eligible.add(fingerprint)
                        if not new_eligible:
                            notes.append("unchanged_opportunity")
                observations.append(
                    ReplayObservation(
                        observed_at=now,
                        detection=detection,
                        new_candidate=new_candidate,
                        new_eligible_opportunity=new_eligible,
                        notes=tuple(notes),
                    )
                )
                if new_eligible and calculation is not None:
                    due = now + config.execution_delay
                    order = PendingOrder(snapshot, market.id, calculation, now, due)
                    if due == now:
                        execute(order, current, now)
                    else:
                        pending.setdefault(due, []).append(order)
                        timeline.add(due)
        record_balance(now, "replay_step")
    for index, trade in enumerate(trades):
        if trade.accepted_stake > 0 and trade.settled_at is None:
            trades[index] = trade.model_copy(
                update={"reasons": (*trade.reasons, "incomplete_settlement_evidence")}
            )
    return BacktestReport(
        name=dataset.name,
        config=config,
        metrics=_metrics(dataset, observations, trades, history),
        observations=tuple(observations),
        trades=tuple(trades),
        bankroll_history=tuple(history),
    )
