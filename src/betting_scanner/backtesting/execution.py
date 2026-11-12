"""Hypothetical fill handling; delegates all opportunity checks to the engine."""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from betting_scanner.backtesting.models import (
    BetDisposition,
    ExecutionInstruction,
    ExecutionStatus,
    PaperBet,
    PaperTrade,
    ReplayConfig,
)
from betting_scanner.core.models import OddsQuote, OddsSnapshot
from betting_scanner.strategies.arbitrage import detect_arbitrage
from betting_scanner.strategies.arbitrage.calculation import PENNY
from betting_scanner.strategies.arbitrage.results import (
    ActionabilityStatus,
    ArbitrageCalculation,
)


@dataclass(frozen=True)
class PendingOrder:
    snapshot: OddsSnapshot
    market_id: str
    calculation: ArbitrageCalculation
    decided_at: datetime
    due_at: datetime


def execute_order(
    order: PendingOrder,
    observed_snapshot: OddsSnapshot | None,
    config: ReplayConfig,
    instructions: tuple[ExecutionInstruction, ...],
    *,
    now: datetime,
    trade_id: str,
    cash: Decimal,
    event_exposure: Decimal,
    aggregate_exposure: Decimal,
) -> PaperTrade:
    """Confirm all prices before attempting legs; fills can still be rejected.

    Fixed decision-time stakes are used after delay/slippage. A partial fill is
    one-sided exposure, never an arbitrage guarantee. Limits apply per bet.
    """
    legs = order.calculation.legs
    reasons: list[str] = []
    current: list[OddsQuote] = []
    instruction_map = {
        (i.bookmaker_id, i.outcome_id): i
        for i in instructions
        if i.execution_at == now
        and i.event_id == order.snapshot.event.id
        and i.market_id == order.market_id
    }
    if observed_snapshot is None:
        reasons.append("no_execution_observation")
    else:
        original_market = next(
            m for m in order.snapshot.markets if m.id == order.market_id
        )
        current_market = next(
            (m for m in observed_snapshot.markets if m.id == order.market_id), None
        )
        if current_market != original_market:
            reasons.append("market_definition_changed")
        for leg in legs:
            planned = leg.quote
            available = [
                q
                for q in observed_snapshot.quotes
                if (
                    q.market_id == planned.market_id
                    and q.outcome_id == planned.outcome_id
                    and q.bookmaker_id == planned.bookmaker_id
                    and q.provider_id == planned.provider_id
                    and q.settlement_rule == planned.settlement_rule
                )
            ]
            if not available:
                reasons.append(
                    f"quote_unavailable:{planned.bookmaker_id}:{planned.outcome_id}"
                )
                continue
            quote = max(available, key=lambda q: (q.decimal_odds, q.model_dump_json()))
            instruction = instruction_map.get((quote.bookmaker_id, quote.outcome_id))
            price = (
                instruction.odds_override
                if instruction and instruction.odds_override is not None
                else quote.decimal_odds
            )
            price *= Decimal(1) - config.slippage
            if price <= 1:
                reasons.append("slippage_invalid_price")
                continue
            data = quote.model_dump()
            data["decimal_odds"] = price
            current.append(OddsQuote.model_validate(data))
        if not reasons:
            data = observed_snapshot.model_dump()
            data["quotes"] = tuple(q.model_dump() for q in current)
            check = detect_arbitrage(
                OddsSnapshot.model_validate(data),
                order.market_id,
                total_stake=order.calculation.total_stake,
                evaluated_at=now,
                config=config.engine,
            )
            if check.actionability_status != ActionabilityStatus.ACTIONABLE:
                reasons.extend(f"execution_check:{r.code}" for r in check.reasons)
                if not check.reasons:
                    reasons.append("execution_not_actionable")
            # Reprice the fixed submitted stakes, rather than silently replacing
            # them with the engine's newly optimal allocation.
            worst = min(
                (leg.stake * quote.decimal_odds).quantize(PENNY, rounding=ROUND_FLOOR)
                for leg, quote in zip(legs, current, strict=True)
            )
            profit = worst - order.calculation.total_stake
            roi = profit / order.calculation.total_stake * 100
            if (
                profit <= 0
                or profit < config.minimum_profit
                or roi < config.minimum_roi
            ):
                reasons.append("price_change_or_slippage_below_threshold")
    budget = order.calculation.total_stake
    if cash < budget:
        reasons.append("insufficient_funds")
    if event_exposure + budget > config.max_event_exposure:
        reasons.append("event_exposure_limit")
    if aggregate_exposure + budget > config.max_aggregate_exposure:
        reasons.append("aggregate_exposure_limit")
    bets: list[PaperBet] = []
    for index, leg in enumerate(legs):
        execution_quote = current[index] if len(current) == len(legs) else None
        rejected = ";".join(reasons) if reasons else None
        instruction = instruction_map.get(
            (leg.quote.bookmaker_id, leg.quote.outcome_id)
        )
        if rejected is None:
            if (config.execution_mode == "reject_last" and index == len(legs) - 1) or (
                instruction and not instruction.accepted
            ):
                rejected = "simulated_bookmaker_rejection"
            limits = config.bookmaker_limits.get(leg.quote.bookmaker_id)
            if limits is not None and (
                (limits.minimum is not None and leg.stake < limits.minimum)
                or (limits.maximum is not None and leg.stake > limits.maximum)
            ):
                rejected = "bookmaker_stake_limit"
        bets.append(
            PaperBet(
                planned_quote=leg.quote,
                execution_quote=execution_quote,
                stake=leg.stake,
                accepted=rejected is None,
                rejection_reason=rejected,
                disposition=BetDisposition.OPEN
                if rejected is None
                else BetDisposition.NOT_PLACED,
            )
        )
    accepted = [bet for bet in bets if bet.accepted]
    status = (
        ExecutionStatus.FULL
        if len(accepted) == len(bets)
        else ExecutionStatus.PARTIAL
        if accepted
        else ExecutionStatus.FAILED
    )
    stake = sum((bet.stake for bet in accepted), Decimal(0))
    worst_payout = (
        min(
            (bet.stake * bet.execution_quote.decimal_odds).quantize(
                PENNY, rounding=ROUND_FLOOR
            )
            for bet in accepted
            if bet.execution_quote is not None
        )
        if status == ExecutionStatus.FULL
        else Decimal(0)
    )
    return PaperTrade(
        id=trade_id,
        event_id=order.snapshot.event.id,
        market_id=order.market_id,
        decided_at=order.decided_at,
        executed_at=now,
        event_start=(
            observed_snapshot.event.scheduled_start
            if observed_snapshot is not None
            else order.snapshot.event.scheduled_start
        ),
        theoretical_roi=order.calculation.theoretical_roi,
        execution_status=status,
        bets=tuple(bets),
        accepted_stake=stake,
        expected_worst_case_payout=worst_payout,
        reasons=tuple(reasons),
    )
