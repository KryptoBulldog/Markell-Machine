#!/usr/bin/env python3
"""Consolidate all subnet alpha stake into Root (netuid 0).

Rationale
---------
Each subnet's alpha trades against TAO in a constant-product pool, so exiting a
position costs slippage proportional to (your size / pool depth).  Root stake
(netuid 0) is denominated in TAO 1:1 -- no pool, no slippage.  Consolidating
pays the alpha->TAO exit cost once and removes all further slippage exposure.

Note that waiting to accumulate a *larger* alpha position does not reduce
slippage; it increases it.  Only deeper pools, or spreading the exit across
time so arbitrage refills the pool between slices, actually help.

Safety
------
Dry run is the default.  Nothing is broadcast without --execute.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

ROOT_NETUID = 0


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

@dataclass
class Position:
    """One (hotkey, netuid) alpha position and its projected exit cost."""
    hotkey: str
    netuid: int
    alpha: float
    tao_out: float = 0.0          # TAO received after slippage
    tao_ideal: float = 0.0        # TAO at spot price, no price impact
    slippage_tao: float = 0.0
    error: str | None = None

    @property
    def slippage_pct(self) -> float:
        if self.tao_ideal <= 0:
            return 0.0
        return 100.0 * self.slippage_tao / self.tao_ideal


@dataclass
class Report:
    moved: list[Position] = field(default_factory=list)
    skipped: list[tuple[Position, str]] = field(default_factory=list)
    failed: list[Position] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Chain helpers
# --------------------------------------------------------------------------- #

def connect(network: str, wallet_name: str, hotkey_name: str | None):
    try:
        import bittensor as bt
    except ImportError:
        sys.exit("bittensor SDK not installed.  pip install bittensor")

    sub = bt.subtensor(network=network)
    wallet = bt.wallet(name=wallet_name, hotkey=hotkey_name or "default")
    return bt, sub, wallet


def discover_positions(sub, coldkey_ss58: str) -> list[Position]:
    """Every non-root alpha position held by this coldkey."""
    stakes = sub.get_stake_for_coldkey(coldkey_ss58=coldkey_ss58)
    out = []
    for s in stakes or []:
        netuid = int(s.netuid)
        if netuid == ROOT_NETUID:
            continue
        alpha = float(s.stake)
        if alpha <= 0:
            continue
        out.append(Position(hotkey=s.hotkey_ss58, netuid=netuid, alpha=alpha))
    return out


def price_position(sub, pos: Position) -> Position:
    """Fill in projected TAO out and slippage for a position."""
    try:
        info = sub.subnet(pos.netuid)
        alpha_bal = _balance(pos.alpha, pos.netuid)
        tao_out, slip = info.alpha_to_tao_with_slippage(alpha_bal)
        pos.tao_out = float(tao_out)
        pos.slippage_tao = float(slip)
        pos.tao_ideal = pos.tao_out + pos.slippage_tao
    except Exception as exc:  # pricing is advisory, never fatal
        pos.error = f"could not price: {exc}"
    return pos


def _balance(amount: float, netuid: int):
    from bittensor.utils.balance import Balance
    return Balance.from_tao(amount).set_unit(netuid)


def move_to_root(bt, sub, wallet, pos: Position, rate_tolerance: float) -> None:
    """Swap this position's alpha into root stake on the same hotkey."""
    amount = _balance(pos.alpha, pos.netuid)
    kwargs = dict(
        wallet=wallet,
        hotkey_ss58=pos.hotkey,
        origin_netuid=pos.netuid,
        destination_netuid=ROOT_NETUID,
        amount=amount,
        wait_for_inclusion=True,
        wait_for_finalization=False,
    )
    try:
        sub.swap_stake(safe_staking=True, rate_tolerance=rate_tolerance,
                       allow_partial_stake=False, **kwargs)
        return
    except TypeError:
        pass  # older SDK without safe-staking params
    sub.swap_stake(**kwargs)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def print_plan(positions: list[Position], max_slippage_pct: float,
               dust: float) -> None:
    print(f"\n{'netuid':>7}  {'alpha':>16}  {'TAO out':>12}  "
          f"{'slippage':>12}  {'slip %':>8}  hotkey")
    print("-" * 88)
    for p in sorted(positions, key=lambda x: -x.tao_out):
        flag = ""
        if p.error:
            flag = "  <- " + p.error
        elif p.tao_out < dust:
            flag = "  <- DUST, skip"
        elif p.slippage_pct > max_slippage_pct:
            flag = f"  <- EXCEEDS {max_slippage_pct}% cap"
        print(f"{p.netuid:>7}  {p.alpha:>16.9f}  {p.tao_out:>12.6f}  "
              f"{p.slippage_tao:>12.6f}  {p.slippage_pct:>7.2f}%  "
              f"{p.hotkey[:12]}...{flag}")

    tao = sum(p.tao_out for p in positions)
    slip = sum(p.slippage_tao for p in positions)
    ideal = sum(p.tao_ideal for p in positions)
    print("-" * 88)
    print(f"{'TOTAL':>7}  {'':>16}  {tao:>12.6f}  {slip:>12.6f}  "
          f"{(100 * slip / ideal if ideal else 0):>7.2f}%")
    print(f"\n{len(positions)} position(s).  "
          f"Projected root stake gain: {tao:.6f} TAO.  "
          f"Cost of consolidating: {slip:.6f} TAO.")


def print_summary(report: Report) -> None:
    print("\n" + "=" * 88)
    print("RESULT")
    print("=" * 88)
    if report.moved:
        total = sum(p.tao_out for p in report.moved)
        slip = sum(p.slippage_tao for p in report.moved)
        print(f"  moved   : {len(report.moved)} position(s), "
              f"~{total:.6f} TAO into root, ~{slip:.6f} TAO lost to slippage")
    for p, why in report.skipped:
        print(f"  skipped : netuid {p.netuid} ({p.alpha:.9f} alpha) -- {why}")
    for p in report.failed:
        print(f"  FAILED  : netuid {p.netuid} ({p.alpha:.9f} alpha) -- {p.error}")
    if report.failed:
        print("\n  Re-run to retry the failed positions.")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallet", required=True, help="coldkey wallet name")
    ap.add_argument("--hotkey", default=None, help="hotkey name (for signing)")
    ap.add_argument("--network", default="finney")
    ap.add_argument("--execute", action="store_true",
                    help="actually broadcast; omit for a dry run")
    ap.add_argument("--max-slippage-pct", type=float, default=5.0,
                    help="skip positions costing more than this %% (default 5)")
    ap.add_argument("--dust", type=float, default=0.0005,
                    help="skip positions worth less than this in TAO")
    ap.add_argument("--rate-tolerance", type=float, default=0.005,
                    help="safe-staking price tolerance (default 0.5%%)")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between extrinsics")
    ap.add_argument("--only", type=int, nargs="*",
                    help="restrict to these netuids")
    args = ap.parse_args()

    bt, sub, wallet = connect(args.network, args.wallet, args.hotkey)
    coldkey = wallet.coldkeypub.ss58_address
    print(f"network : {args.network}")
    print(f"coldkey : {coldkey}")
    print(f"mode    : {'EXECUTE' if args.execute else 'DRY RUN'}")

    positions = discover_positions(sub, coldkey)
    if args.only:
        positions = [p for p in positions if p.netuid in set(args.only)]
    if not positions:
        print("\nNo non-root alpha positions found.  Nothing to do.")
        return 0

    positions = [price_position(sub, p) for p in positions]
    print_plan(positions, args.max_slippage_pct, args.dust)

    if not args.execute:
        print("\nDry run -- nothing broadcast.  Re-run with --execute to move.")
        return 0

    # Largest first: those dominate total slippage, so a mid-run abort leaves
    # the cheap tail behind rather than the expensive head.
    report = Report()
    for pos in sorted(positions, key=lambda x: -x.tao_out):
        if pos.tao_out < args.dust and not pos.error:
            report.skipped.append((pos, f"below dust threshold {args.dust} TAO"))
            continue
        if pos.slippage_pct > args.max_slippage_pct:
            report.skipped.append(
                (pos, f"slippage {pos.slippage_pct:.2f}% over "
                      f"{args.max_slippage_pct}% cap"))
            continue

        print(f"\n-> netuid {pos.netuid}: moving {pos.alpha:.9f} alpha "
              f"(~{pos.tao_out:.6f} TAO) to root ...")
        try:
            move_to_root(bt, sub, wallet, pos, args.rate_tolerance)
            report.moved.append(pos)
            print("   ok")
        except Exception as exc:
            pos.error = str(exc)
            report.failed.append(pos)
            print(f"   FAILED: {exc}")
        time.sleep(args.delay)

    print_summary(report)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
