#!/usr/bin/env python3
"""One-time consolidation: move all subnet alpha into Root (SN0) stake.

Each subnet's alpha trades against TAO in a constant-product pool, so exiting
costs slippage proportional to (position size / pool depth).  Root stake is
denominated in TAO 1:1 -- no pool, no further price impact.  Consolidating
pays the exit cost once and ends ongoing slippage exposure.

Note: every alpha->TAO move loses *some* TAO to price impact; there is no move
that gains TAO.  --max-slippage-pct is therefore a tolerance, not a >0 test.

The existing root position is never touched: only alpha (netuid != 0) is read,
and root is only ever added to.

Dry run is the default.  Nothing is broadcast without --execute.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

ROOT_NETUID = 0
SECRETS_PATH = os.path.expanduser("~/.tao_secrets")
LOG_PATH = os.path.expanduser("~/.tao_consolidation.jsonl")


# --------------------------------------------------------------------------- #
# Secrets / notification
# --------------------------------------------------------------------------- #

def load_secrets(path: str = SECRETS_PATH) -> dict:
    """Read ~/.tao_secrets.  Accepts JSON or shell-style KEY=VALUE lines."""
    if not os.path.exists(path):
        return {}

    if os.name == "posix":
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            print(f"WARNING: {path} is mode {mode:o}; tighten with "
                  f"chmod 600 {path}", file=sys.stderr)
    else:
        # POSIX mode bits are meaningless on Windows; os.stat reports 0o666
        # for every file, so the check above would warn unconditionally.
        # Restrict via ACL instead:
        #   icacls "%USERPROFILE%\\.tao_secrets" /inheritance:r /grant:r "%USERNAME%:R"
        print(f"NOTE: on Windows, restrict {path} with icacls "
              f"(see comment in load_secrets).", file=sys.stderr)

    raw = open(path).read()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError:
        pass

    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:].lstrip() if line.startswith("export ") else line
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def send_telegram(secrets: dict, text: str) -> bool:
    """Post to Telegram.  Never raises -- a failed notify must not look like
    a failed consolidation."""
    token = secrets.get("TELEGRAM_BOT_TOKEN") or secrets.get("TELEGRAM_TOKEN")
    chat = secrets.get("TELEGRAM_CHAT_ID") or secrets.get("TELEGRAM_CHAT")
    if not token or not chat:
        print("No Telegram credentials in secrets; skipping notification.",
              file=sys.stderr)
        return False

    payload = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "Markdown",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=payload), timeout=15) as r:
                if r.status == 200:
                    return True
        except Exception as exc:
            if attempt == 2:
                print(f"Telegram notify failed: {exc}", file=sys.stderr)
            else:
                time.sleep(2 ** attempt)
    return False


def audit(entry: dict) -> None:
    """Append one JSON line.  This is the durable record of each move."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:
        print(f"WARNING: could not write audit log: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

@dataclass
class Position:
    hotkey: str
    netuid: int
    alpha: float
    price: float = 0.0            # TAO per alpha, spot
    tao_before: float = 0.0       # alpha * spot price, no price impact
    tao_expected: float = 0.0     # projected TAO after slippage
    tao_actual: float | None = None   # measured root-stake delta
    slippage_tao: float = 0.0
    error: str | None = None

    @property
    def slippage_pct(self) -> float:
        if self.tao_before <= 0:
            return 0.0
        return 100.0 * self.slippage_tao / self.tao_before

    @property
    def realized_loss(self) -> float | None:
        if self.tao_actual is None:
            return None
        return self.tao_before - self.tao_actual


@dataclass
class Report:
    root_before: float = 0.0
    root_after: float = 0.0
    moved: list[Position] = field(default_factory=list)
    skipped: list[tuple[Position, str]] = field(default_factory=list)
    failed: list[Position] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Chain
# --------------------------------------------------------------------------- #

def connect(network: str, wallet_name: str, hotkey_name: str | None):
    try:
        import bittensor as bt
    except ImportError:
        sys.exit("bittensor SDK not installed.  pip install bittensor")
    return bt, bt.subtensor(network=network), bt.wallet(
        name=wallet_name, hotkey=hotkey_name or "default")


def _balance(amount: float, netuid: int | None = None):
    from bittensor.utils.balance import Balance
    bal = Balance.from_tao(amount)
    return bal.set_unit(netuid) if netuid is not None else bal


def all_stakes(sub, coldkey_ss58):
    return sub.get_stake_for_coldkey(coldkey_ss58=coldkey_ss58) or []


def root_stake(sub, coldkey_ss58: str) -> float:
    """Total root (SN0) stake across all hotkeys.  Read-only."""
    return sum(float(s.stake) for s in all_stakes(sub, coldkey_ss58)
               if int(s.netuid) == ROOT_NETUID)


def discover_alpha(sub, coldkey_ss58: str) -> list[Position]:
    """Every non-root alpha position.  Root is excluded by construction."""
    out = []
    for s in all_stakes(sub, coldkey_ss58):
        netuid = int(s.netuid)
        if netuid == ROOT_NETUID:      # never touch root
            continue
        alpha = float(s.stake)
        if alpha > 0:
            out.append(Position(hotkey=s.hotkey_ss58, netuid=netuid,
                                alpha=alpha))
    return out


def price_position(sub, pos: Position) -> Position:
    try:
        info = sub.subnet(pos.netuid)
        tao_out, slip = info.alpha_to_tao_with_slippage(
            _balance(pos.alpha, pos.netuid))
        pos.tao_expected = float(tao_out)
        pos.slippage_tao = float(slip)
        pos.price = float(info.price)
        pos.tao_before = pos.tao_expected + pos.slippage_tao
    except Exception as exc:
        pos.error = f"could not price: {exc}"
    return pos


def move_to_root(sub, wallet, pos: Position, rate_tolerance: float) -> None:
    kwargs = dict(
        wallet=wallet, hotkey_ss58=pos.hotkey,
        origin_netuid=pos.netuid, destination_netuid=ROOT_NETUID,
        amount=_balance(pos.alpha, pos.netuid),
        wait_for_inclusion=True, wait_for_finalization=False,
    )
    try:
        sub.swap_stake(safe_staking=True, rate_tolerance=rate_tolerance,
                       allow_partial_stake=False, **kwargs)
        return
    except TypeError:
        pass  # older SDK without safe-staking kwargs
    sub.swap_stake(**kwargs)


def settled_root(sub, coldkey: str, baseline: float,
                 tries: int = 6, delay: float = 2.0) -> float:
    """Poll root stake until it moves off baseline, so the logged delta is the
    real credited amount rather than the estimate."""
    latest = baseline
    for _ in range(tries):
        time.sleep(delay)
        latest = root_stake(sub, coldkey)
        if abs(latest - baseline) > 1e-9:
            return latest
    return latest


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def print_plan(positions, cap, dust, root_now):
    print(f"\nroot stake now : {root_now:.6f} TAO  (will not be touched)")
    print(f"\n{'netuid':>7}  {'alpha':>16}  {'TAO before':>12}  "
          f"{'TAO out':>12}  {'slip':>11}  {'slip %':>8}  hotkey")
    print("-" * 100)
    for p in sorted(positions, key=lambda x: -x.tao_expected):
        note = ""
        if p.error:
            note = "  <- " + p.error
        elif p.tao_expected < dust:
            note = "  <- DUST, skip"
        elif p.slippage_pct > cap:
            note = f"  <- SKIP, over {cap}% cap"
        print(f"{p.netuid:>7}  {p.alpha:>16.9f}  {p.tao_before:>12.6f}  "
              f"{p.tao_expected:>12.6f}  {p.slippage_tao:>11.6f}  "
              f"{p.slippage_pct:>7.2f}%  {p.hotkey[:10]}...{note}")
    before = sum(p.tao_before for p in positions)
    out = sum(p.tao_expected for p in positions)
    slip = sum(p.slippage_tao for p in positions)
    print("-" * 100)
    print(f"{'TOTAL':>7}  {'':>16}  {before:>12.6f}  {out:>12.6f}  "
          f"{slip:>11.6f}  {(100*slip/before if before else 0):>7.2f}%")
    print(f"\n{len(positions)} alpha position(s).  Projected root after: "
          f"{root_now + out:.6f} TAO.  Projected slippage cost: {slip:.6f} TAO.")


def build_message(rep: Report) -> str:
    gained = sum(p.tao_actual or 0.0 for p in rep.moved)
    lost = sum(p.realized_loss or 0.0 for p in rep.moved)
    lines = [
        "*TAO consolidation complete*",
        "",
        f"Moved to root: {len(rep.moved)} position(s)",
        f"TAO credited to root: `{gained:.6f}`",
        f"Lost to slippage: `{lost:.6f}` TAO",
        "",
        f"Root before: `{rep.root_before:.6f}` TAO",
        f"Root after (new total): `{rep.root_after:.6f}` TAO",
    ]
    if rep.skipped:
        lines += ["", "*Skipped:*"] + [
            f"- SN{p.netuid}: {why}" for p, why in rep.skipped]
    if rep.failed:
        lines += ["", "*Failed:*"] + [
            f"- SN{p.netuid}: {p.error}" for p in rep.failed]
    return "\n".join(lines)


def print_summary(rep: Report) -> None:
    print("\n" + "=" * 100)
    print("RESULT")
    print("=" * 100)
    for p in rep.moved:
        print(f"  SN{p.netuid:<4} {p.alpha:.9f} alpha | "
              f"before {p.tao_before:.6f} TAO -> credited "
              f"{(p.tao_actual or 0):.6f} TAO | "
              f"lost {(p.realized_loss or 0):.6f}")
    for p, why in rep.skipped:
        print(f"  SN{p.netuid:<4} SKIPPED -- {why}")
    for p in rep.failed:
        print(f"  SN{p.netuid:<4} FAILED  -- {p.error}")
    print(f"\n  root {rep.root_before:.6f} -> {rep.root_after:.6f} TAO")
    print(f"  audit log: {LOG_PATH}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--hotkey", default=None)
    ap.add_argument("--network", default="finney")
    ap.add_argument("--execute", action="store_true",
                    help="broadcast; omit for dry run")
    ap.add_argument("--max-slippage-pct", type=float, default=5.0,
                    help="skip a position costing more than this %%")
    ap.add_argument("--dust", type=float, default=0.0005)
    ap.add_argument("--rate-tolerance", type=float, default=0.005)
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--only", type=int, nargs="*")
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()

    secrets = load_secrets()
    _, sub, wallet = connect(args.network, args.wallet, args.hotkey)
    coldkey = wallet.coldkeypub.ss58_address

    print(f"network : {args.network}")
    print(f"coldkey : {coldkey}")
    print(f"mode    : {'EXECUTE' if args.execute else 'DRY RUN'}")

    rep = Report(root_before=root_stake(sub, coldkey))
    positions = discover_alpha(sub, coldkey)
    if args.only:
        positions = [p for p in positions if p.netuid in set(args.only)]
    if not positions:
        print("\nNo alpha positions found.  Root untouched.  Nothing to do.")
        return 0

    positions = [price_position(sub, p) for p in positions]
    print_plan(positions, args.max_slippage_pct, args.dust, rep.root_before)

    if not args.execute:
        print("\nDry run -- nothing broadcast.  Re-run with --execute.")
        return 0

    # Largest first: they dominate total slippage, so a mid-run abort leaves
    # the cheap tail behind rather than the expensive head.
    running_root = rep.root_before
    for pos in sorted(positions, key=lambda x: -x.tao_expected):
        if pos.error:
            rep.skipped.append((pos, pos.error))
            continue
        if pos.tao_expected < args.dust:
            rep.skipped.append((pos, f"below dust {args.dust} TAO"))
            continue
        if pos.slippage_pct > args.max_slippage_pct:
            rep.skipped.append(
                (pos, f"slippage {pos.slippage_pct:.2f}% over "
                      f"{args.max_slippage_pct}% cap"))
            audit({"event": "skip", "netuid": pos.netuid,
                   "slippage_pct": pos.slippage_pct})
            continue

        print(f"\n-> SN{pos.netuid}: {pos.alpha:.9f} alpha "
              f"(~{pos.tao_before:.6f} TAO at spot) -> root ...")
        try:
            move_to_root(sub, wallet, pos, args.rate_tolerance)
            after = settled_root(sub, coldkey, running_root)
            pos.tao_actual = max(0.0, after - running_root)
            running_root = after
            rep.moved.append(pos)
            print(f"   credited {pos.tao_actual:.6f} TAO "
                  f"(lost {pos.realized_loss:.6f})")
            audit({"event": "move", **asdict(pos)})
        except Exception as exc:
            pos.error = str(exc)
            rep.failed.append(pos)
            print(f"   FAILED: {exc}")
            audit({"event": "fail", "netuid": pos.netuid, "error": str(exc)})
        time.sleep(args.delay)

    rep.root_after = root_stake(sub, coldkey)
    print_summary(rep)
    audit({"event": "summary", "root_before": rep.root_before,
           "root_after": rep.root_after, "moved": len(rep.moved),
           "skipped": len(rep.skipped), "failed": len(rep.failed)})

    if not args.no_telegram:
        send_telegram(secrets, build_message(rep))

    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
