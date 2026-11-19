"""Coffee achievements (badges): definitions, unlock rules, storage and the public showcase.

Spec: approved list "Coffee achievements: final spec" (2026-11-17). Rules in short:

- Every achievement is defined once in `ACHIEVEMENTS` (key, icon, category, hidden flag); its display name and
  description are `t("ach.<key>.name")` / `t("ach.<key>.desc")`. Each level is its own achievement.
- Unlocks are rows in `achievement_unlocks` (one per account and key) with the time and the coffee that unlocked
  them. Rules are evaluated by REPLAYING the account's whole history in time order (`evaluate`), so the stored
  unlock time is the real moment the condition first held, also for the backfill of existing accounts.
- `sync_user` only ever ADDS unlocks. The one exception is an immediate undo by the owner (`on_undo`): unlocks
  that the undone coffee triggered are removed (except Schrödinger's Coffee, which the undo unlocks), then the
  account is synced again. Later admin corrections never remove anything.
- Coffees that were undone (by the owner or reversed by an admin) never count. Shots: single 1, double 2.
  Adjustments are not coffees. All times are local (`LOCAL_TIMEZONE`). Totals follow merges: an account's
  history includes the profiles merged into it.
- Queue badges (First / Last in Line) look at everybody's coffees: a queue is a chain of bookings, each at most
  `QUEUE_GAP` after the previous one, by at least `QUEUE_MIN_PEOPLE` different accounts. It is awarded only when
  it is over (no booking for more than `QUEUE_GAP`), by `sweep_queues` (run on every booking for the recent
  days, and over the whole history by `sync_all`, the CLI backfill `achievements-sync`).
- Locked achievements are never shown. Up to `SHOWCASE_MAX` unlocked ones can be shown on the public profile.

Services never commit.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    COFFEE_KINDS,
    SHOWCASE_MAX,
    AchievementShowcase,
    AchievementUnlock,
    ConsumptionEvent,
    PaymentEvent,
    User,
    effective_users,
)
from app.services.errors import InvalidInput
from app.timeutil import ensure_utc, local_tz, to_local, utcnow

QUEUE_GAP = timedelta(minutes=5)
QUEUE_MIN_PEOPLE = 8
QUEUE_SWEEP_LOOKBACK = timedelta(days=7)
TENPI_ICON = "/static/achievements/tenpi.png"


@dataclass(frozen=True)
class Achievement:
    key: str
    icon: str  # an emoji, or a /static path to an image
    category: str
    hidden: bool = False  # never listed anywhere until unlocked (all locked badges are hidden anyway)

    @property
    def icon_is_image(self) -> bool:
        return self.icon.startswith("/static/")


CATEGORIES = ("milestones", "shots", "time", "streaks", "intensity", "calendar", "loyalty", "queue", "oddities")

MILESTONES = {  # key -> shots
    "starter-pack": 1, "tenpi": 31, "answer-42": 42, "three-digits": 100, "fine-structure": 137,
    "euler": 271, "planck": 622, "coffee-god": 1000,
}

ACHIEVEMENTS: tuple[Achievement, ...] = (
    Achievement("starter-pack", "🎒", "milestones"),
    Achievement("tenpi", TENPI_ICON, "milestones"),
    Achievement("answer-42", "🌌", "milestones"),
    Achievement("three-digits", "💯", "milestones"),
    Achievement("fine-structure", "⚛️", "milestones"),
    Achievement("euler", "📈", "milestones"),
    Achievement("planck", "🔬", "milestones"),
    Achievement("coffee-god", "👑", "milestones"),
    Achievement("double-trouble", "✌️", "shots"),
    Achievement("lone-wolf", "🐺", "shots"),
    Achievement("early-bird", "🐦", "time"),
    Achievement("night-owl", "🦉", "time"),
    Achievement("afternoon-slump", "🥱", "time"),
    Achievement("flush", "🃏", "streaks"),
    Achievement("royal-flush", "♠️", "streaks"),
    Achievement("unstoppable", "🚂", "streaks"),
    Achievement("monday-survivor", "🧟", "streaks"),
    Achievement("weekend-warrior", "🛡️", "streaks"),
    Achievement("clockwork", "⏰", "streaks"),
    Achievement("coffee-addict", "⚡", "intensity"),
    Achievement("back-to-back", "⏩", "intensity"),
    Achievement("new-years-brew", "🎆", "calendar"),
    Achievement("christmas-coffee", "🎄", "calendar"),
    Achievement("friday-13", "🐈‍⬛", "calendar"),
    Achievement("leet", "👾", "calendar"),
    Achievement("pi-day", "🥧", "calendar"),
    Achievement("holiday-hero", "🏖️", "calendar"),
    Achievement("one-year-in", "🎂", "loyalty"),
    Achievement("seasons", "🍂", "loyalty"),
    Achievement("welcome-back", "👋", "loyalty"),
    Achievement("first-in-line", "🥇", "queue"),
    Achievement("last-in-line", "🐢", "queue"),
    Achievement("schroedinger", "🐈", "oddities", hidden=True),
    Achievement("ground-state", "⚖️", "oddities"),
    Achievement("inflation-survivor", "📉", "oddities"),
    Achievement("plus-one", "🤝", "oddities"),
)
BY_KEY = {a.key: a for a in ACHIEVEMENTS}
ORDER = {a.key: i for i, a in enumerate(ACHIEVEMENTS)}
NOT_REVOKED_BY_UNDO = frozenset({"schroedinger"})
QUEUE_KEYS = ("first-in-line", "last-in-line")

# Rule thresholds (spec values).
LONE_WOLF_DOUBLES = 50
AFTERNOON_SLUMP_COUNT = 20
FLUSH_DAYS = 5
UNSTOPPABLE_DAYS = 20
ROYAL_FLUSH_DAYS = 7
MONDAY_SURVIVOR_COUNT = 10
CLOCKWORK_DAYS = 5
CLOCKWORK_WINDOW = timedelta(minutes=15)
COFFEE_ADDICT_SHOTS = 5
BACK_TO_BACK = timedelta(minutes=10)
ONE_YEAR = timedelta(days=365)
WELCOME_BACK = timedelta(days=30)


# ----- Bavarian public holidays ---------------------------------------------------------------
def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def bavarian_holidays(year: int) -> frozenset[date]:
    """Statutory holidays in Bavaria (Mariä Himmelfahrt included, as in Munich; the Augsburg Friedensfest not)."""
    e = easter_sunday(year)
    days = [date(year, 1, 1), date(year, 1, 6), e - timedelta(days=2), e + timedelta(days=1), date(year, 5, 1),
            e + timedelta(days=39), e + timedelta(days=50), e + timedelta(days=60), date(year, 8, 15),
            date(year, 10, 3), date(year, 11, 1), date(year, 12, 25), date(year, 12, 26)]
    return frozenset(days)


def is_holiday(d: date) -> bool:
    return d in bavarian_holidays(d.year)


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def previous_working_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_working_day(d):
        d -= timedelta(days=1)
    return d


# ----- rule evaluation (pure) -----------------------------------------------------------------
@dataclass(frozen=True)
class Booking:
    id: uuid.UUID
    at: datetime  # UTC
    kind: str  # single | double
    shots: int
    price_key: str  # the price stored on the coffee


@dataclass(frozen=True)
class Unlock:
    at: datetime  # UTC
    event_id: uuid.UUID | None


def evaluate(bookings, *, balance_points=(), own_undos=(), guest_confirmations=()) -> dict[str, Unlock]:
    """When each (non-queue) achievement was first earned, from one account's history.

    `bookings`: the account's coffees that were NOT undone, any order. `balance_points`: `(at, balance_cents,
    event_id or None)` after each booking or payment (undone coffees and their undos left out). `own_undos`:
    `(at, reversal_event_id)` of the owner's own undos. `guest_confirmations`: times sponsored guests were
    confirmed. Returns key -> first `Unlock`."""
    out: dict[str, Unlock] = {}

    def grant(key: str, b: Booking | None, at: datetime | None = None) -> None:
        if key not in out:
            out[key] = Unlock(at if at is not None else b.at, b.id if b is not None else None)

    tz = local_tz()
    shots = doubles = slump = 0
    day_shots: dict[date, int] = defaultdict(int)
    day_times: dict[date, list[time]] = defaultdict(list)
    working_streak: dict[date, int] = {}
    calendar_streak: dict[date, int] = {}
    mondays: set[date] = set()
    months: set[int] = set()
    prices: set[str] = set()
    first: Booking | None = None
    prev: Booking | None = None
    for b in sorted(bookings, key=lambda x: (x.at, str(x.id))):
        local = to_local(b.at, tz)
        d, tod = local.date(), local.time()
        shots += b.shots
        for key, need in MILESTONES.items():
            if shots >= need:
                grant(key, b)
        if b.kind == "double":
            doubles += 1
            grant("double-trouble", b)
            if doubles >= LONE_WOLF_DOUBLES:
                grant("lone-wolf", b)
        # time of day
        if time(4) <= tod < time(7):
            grant("early-bird", b)
        if tod >= time(20) or tod < time(4):
            grant("night-owl", b)
        if time(14) <= tod < time(16):
            slump += 1
            if slump >= AFTERNOON_SLUMP_COUNT:
                grant("afternoon-slump", b)
        # streaks
        new_day = d not in day_times
        day_times[d].append(tod)
        if new_day:
            calendar_streak[d] = calendar_streak.get(d - timedelta(days=1), 0) + 1
            if calendar_streak[d] >= ROYAL_FLUSH_DAYS:
                grant("royal-flush", b)
            if is_working_day(d):
                working_streak[d] = working_streak.get(previous_working_day(d), 0) + 1
                if working_streak[d] >= FLUSH_DAYS:
                    grant("flush", b)
                if working_streak[d] >= UNSTOPPABLE_DAYS:
                    grant("unstoppable", b)
            if d.weekday() == 0:
                mondays.add(d)
                if len(mondays) >= MONDAY_SURVIVOR_COUNT:
                    grant("monday-survivor", b)
        if d.weekday() >= 5:
            grant("weekend-warrior", b)
        if is_working_day(d) and "clockwork" not in out and _clockwork(d, day_times):
            grant("clockwork", b)
        # intensity
        day_shots[d] += b.shots
        if day_shots[d] >= COFFEE_ADDICT_SHOTS:
            grant("coffee-addict", b)
        if prev is not None and b.at - prev.at <= BACK_TO_BACK:
            grant("back-to-back", b)
        # calendar
        if (d.month, d.day) == (1, 1):
            grant("new-years-brew", b)
        if d.month == 12 and d.day in (24, 25, 26):
            grant("christmas-coffee", b)
        if d.weekday() == 4 and d.day == 13 and tod.hour == 13:
            grant("friday-13", b)
        if (tod.hour, tod.minute) == (13, 37):
            grant("leet", b)
        if (d.month, d.day) == (3, 14) and (tod.hour, tod.minute) == (15, 14):
            grant("pi-day", b)
        if is_holiday(d):
            grant("holiday-hero", b)
        # loyalty
        if first is None:
            first = b
        elif b.at - first.at >= ONE_YEAR:
            grant("one-year-in", b)
        months.add(d.month)
        if len(months) == 12:
            grant("seasons", b)
        if prev is not None and b.at - prev.at >= WELCOME_BACK:
            grant("welcome-back", b)
        # odds and ends
        prices.add(b.price_key)
        if len(prices) >= 2:
            grant("inflation-survivor", b)
        prev = b

    for at, reversal_id in sorted(own_undos, key=lambda x: x[0]):
        out.setdefault("schroedinger", Unlock(at, reversal_id))
        break
    for at, balance, event_id in sorted(balance_points, key=lambda x: x[0]):
        if balance == 0:
            out.setdefault("ground-state", Unlock(at, event_id))
            break
    for at in sorted(guest_confirmations):
        out.setdefault("plus-one", Unlock(at, None))
        break
    return out


def _clockwork(d: date, day_times: dict[date, list[time]]) -> bool:
    """`d` and the 4 working days before it all have a coffee, and one coffee per day fits in one 15-minute
    window of time of day."""
    days = [d]
    while len(days) < CLOCKWORK_DAYS:
        days.append(previous_working_day(days[-1]))
    if any(x not in day_times for x in days):
        return False
    secs = [sorted(t.hour * 3600 + t.minute * 60 + t.second for t in day_times[x]) for x in days]
    window = int(CLOCKWORK_WINDOW.total_seconds())
    for start in sorted({s for day in secs for s in day}):
        if all(any(start <= s <= start + window for s in day) for day in secs):
            return True
    return False


@dataclass(frozen=True)
class QueueAward:
    key: str  # first-in-line | last-in-line
    user_id: uuid.UUID  # effective account
    event_id: uuid.UUID
    at: datetime  # when the queue was over (last booking + QUEUE_GAP)


def find_queues(rows, now: datetime) -> list[QueueAward]:
    """Finished queues among `rows` = `(event_id, at, effective_user_id)` of coffees not undone (any order)."""
    rows = sorted(rows, key=lambda r: (r[1], str(r[0])))
    awards: list[QueueAward] = []
    chain: list = []

    def close(chain):
        if len(chain) < 2 or len({r[2] for r in chain}) < QUEUE_MIN_PEOPLE:
            return
        end = chain[-1][1] + QUEUE_GAP
        if now - chain[-1][1] <= QUEUE_GAP:
            return  # still open: never awarded live
        awards.append(QueueAward("first-in-line", chain[0][2], chain[0][0], end))
        awards.append(QueueAward("last-in-line", chain[-1][2], chain[-1][0], end))

    for r in rows:
        if chain and r[1] - chain[-1][1] > QUEUE_GAP:
            close(chain)
            chain = []
        chain.append(r)
    if chain:
        close(chain)
    return awards


# ----- database side --------------------------------------------------------------------------
def _now(now: datetime | None) -> datetime:
    return ensure_utc(now) if now is not None else utcnow()


def effective_id(db: Session, user_id: uuid.UUID) -> uuid.UUID | None:
    return db.scalar(select(effective_users.c.effective_id).where(effective_users.c.id == user_id))


def _history(db: Session, eff: uuid.UUID):
    members = select(effective_users.c.id).where(effective_users.c.effective_id == eff)
    ce = ConsumptionEvent
    rows = db.execute(
        select(ce.id, ce.created_at, ce.type, ce.quantity, ce.amount_cents, ce.price_id, ce.unit_price_cents,
               ce.reversal_of_id, ce.admin_id)
        .where(ce.user_id.in_(members))
    ).all()
    undone = {r.reversal_of_id for r in rows if r.reversal_of_id is not None}
    bookings = [Booking(r.id, ensure_utc(r.created_at), r.type, int(r.quantity),
                        str(r.price_id) if r.price_id is not None else f"cents:{r.unit_price_cents}")
                for r in rows if r.type in COFFEE_KINDS and r.id not in undone]
    own_undos = [(ensure_utc(r.created_at), r.id) for r in rows if r.type == "reversal" and r.admin_id is None]
    payments = db.execute(
        select(PaymentEvent.created_at, PaymentEvent.amount_cents).where(PaymentEvent.user_id.in_(members))
    ).all()
    # Balance after each booking and payment; an undone coffee and its undo are left out entirely.
    timeline = [(ensure_utc(r.created_at), int(r.amount_cents), r.id if r.type in COFFEE_KINDS else None,
                 r.type in COFFEE_KINDS)
                for r in rows if r.id not in undone and r.type != "reversal"]
    timeline += [(ensure_utc(p.created_at), -int(p.amount_cents), None, True) for p in payments]
    timeline.sort(key=lambda x: (x[0], x[2] is None))
    balance, points = 0, []
    for at, delta, event_id, is_point in timeline:
        balance += delta
        if is_point:
            points.append((at, balance, event_id))
    guests = db.scalars(
        select(User.verified_at).where(User.sponsor_id.in_(members), User.account_kind == "guest",
                                       User.verified_at.is_not(None))
    ).all()
    return bookings, points, own_undos, [ensure_utc(g) for g in guests]


def _existing(db: Session, user_id: uuid.UUID) -> set[str]:
    return set(db.scalars(select(AchievementUnlock.key).where(AchievementUnlock.user_id == user_id)))


def _store(db: Session, user_id: uuid.UUID, key: str, unlock: Unlock, have: set[str]) -> bool:
    if key in have or key not in BY_KEY:
        return False
    db.add(AchievementUnlock(user_id=user_id, key=key, unlocked_at=unlock.at, event_id=unlock.event_id))
    have.add(key)
    return True


def sync_user(db: Session, user_id: uuid.UUID) -> list[str]:
    """Add every achievement the account has earned and does not have yet (never removes). Returns new keys.
    Inactive and merged accounts are left alone."""
    eff = effective_id(db, user_id)
    if eff is None:
        return []
    user = db.get(User, eff)
    if user is None or user.status != "active":
        return []
    bookings, points, undos, guests = _history(db, eff)
    earned = evaluate(bookings, balance_points=points, own_undos=undos, guest_confirmations=guests)
    have = _existing(db, eff)
    new = [k for k in ACHIEVEMENTS_KEYS_ORDERED if k in earned and _store(db, eff, k, earned[k], have)]
    db.flush()
    return new


ACHIEVEMENTS_KEYS_ORDERED = tuple(a.key for a in ACHIEVEMENTS)


def sweep_queues(db: Session, *, since: datetime | None = None, now: datetime | None = None) -> int:
    """Award First / Last in Line for every finished queue with a booking after `since` (None: all history).
    Returns the number of new unlocks."""
    now = _now(now)
    ce, eu = ConsumptionEvent, effective_users
    q = select(ce.id, ce.created_at, eu.c.effective_id, ce.type).join_from(ce, eu, eu.c.id == ce.user_id)
    rev = select(ce.reversal_of_id).where(ce.reversal_of_id.is_not(None))
    q = q.where(ce.type.in_(COFFEE_KINDS), ce.id.not_in(rev))
    if since is not None:
        q = q.where(ce.created_at >= since)
    rows = [(r[0], ensure_utc(r[1]), r[2]) for r in db.execute(q).all()]
    if since is not None and rows:
        # A chain that started before `since` is only partly visible: leave it to an earlier/full sweep.
        earliest = min(r[1] for r in rows)
        before = db.scalar(select(ce.id).where(ce.type.in_(COFFEE_KINDS), ce.created_at < earliest,
                                               ce.created_at >= earliest - QUEUE_GAP).limit(1))
        if before is not None:
            first_chain_end = _first_chain_end(rows)
            rows = [r for r in rows if r[1] > first_chain_end]
    added = 0
    cache: dict[uuid.UUID, tuple[set[str], bool]] = {}
    for award in find_queues(rows, now):
        if award.user_id not in cache:
            user = db.get(User, award.user_id)
            cache[award.user_id] = (_existing(db, award.user_id), user is not None and user.status == "active")
        have, active = cache[award.user_id]
        if active and _store(db, award.user_id, award.key, Unlock(award.at, award.event_id), have):
            added += 1
    db.flush()
    return added


def _first_chain_end(rows) -> datetime:
    ordered = sorted(r[1] for r in rows)
    end = ordered[0]
    for at in ordered[1:]:
        if at - end > QUEUE_GAP:
            break
        end = at
    return end


def on_coffee(db: Session, event: ConsumptionEvent, now: datetime | None = None) -> list[str]:
    """After a coffee was booked: sync the account and look for finished queues of the last days."""
    now = _now(now)
    new = sync_user(db, event.user_id)
    sweep_queues(db, since=now - QUEUE_SWEEP_LOOKBACK, now=now)
    return new


def on_undo(db: Session, original: ConsumptionEvent, reversal: ConsumptionEvent,
            now: datetime | None = None) -> list[str]:
    """After the owner's immediate undo: remove what the undone coffee unlocked (Schrödinger's Coffee stays), drop
    removed badges from the showcase, sync again (another coffee may still earn the same badge, and the undo
    earns Schrödinger's Coffee) and re-check the queues."""
    now = _now(now)
    revoked = db.scalars(
        select(AchievementUnlock).where(AchievementUnlock.event_id == original.id,
                                        AchievementUnlock.key.not_in(NOT_REVOKED_BY_UNDO))
    ).all()
    for row in revoked:
        db.delete(row)
    db.flush()
    eff = effective_id(db, original.user_id)
    new = sync_user(db, original.user_id)
    sweep_queues(db, since=now - QUEUE_SWEEP_LOOKBACK, now=now)
    if revoked:
        _prune_showcase(db, {r.user_id for r in revoked} | ({eff} if eff else set()))
    return new


def on_payment(db: Session, user_id: uuid.UUID) -> list[str]:
    return sync_user(db, user_id)


def sync_all(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Backfill: sync every active account and sweep all queues of the whole history."""
    ids = db.scalars(select(User.id).where(User.status == "active")).all()
    users_added = sum(len(sync_user(db, uid)) for uid in ids)
    queue_added = sweep_queues(db, since=None, now=now)
    return {"accounts": len(ids), "unlocks": users_added + queue_added}


# ----- reads and showcase ---------------------------------------------------------------------
@dataclass(frozen=True)
class Earned:
    achievement: Achievement
    unlocked_at: datetime  # UTC
    in_showcase: bool


def unlocked(db: Session, user_id: uuid.UUID) -> list[Earned]:
    """The account's unlocked achievements in definition order (locked ones are never returned)."""
    rows = db.execute(
        select(AchievementUnlock.key, AchievementUnlock.unlocked_at).where(AchievementUnlock.user_id == user_id)
    ).all()
    shown = set(db.scalars(select(AchievementShowcase.key).where(AchievementShowcase.user_id == user_id)))
    out = [Earned(BY_KEY[r.key], ensure_utc(r.unlocked_at), r.key in shown) for r in rows if r.key in BY_KEY]
    out.sort(key=lambda e: ORDER[e.achievement.key])
    return out


def showcase(db: Session, user_id: uuid.UUID) -> list[Achievement]:
    """Showcased achievements that are (still) unlocked, in the order chosen."""
    have = set(db.scalars(select(AchievementUnlock.key).where(AchievementUnlock.user_id == user_id)))
    rows = db.execute(
        select(AchievementShowcase.key).where(AchievementShowcase.user_id == user_id)
        .order_by(AchievementShowcase.position)
    ).all()
    return [BY_KEY[r.key] for r in rows if r.key in have and r.key in BY_KEY][:SHOWCASE_MAX]


def new_for_events(db: Session, event_ids) -> list[Achievement]:
    """Achievements unlocked by these coffees / undos (for the confirmation page)."""
    ids = [i for i in event_ids if i is not None]
    if not ids:
        return []
    keys = db.scalars(select(AchievementUnlock.key).where(AchievementUnlock.event_id.in_(ids))).all()
    return sorted((BY_KEY[k] for k in set(keys) if k in BY_KEY), key=lambda a: ORDER[a.key])


def set_showcase(db: Session, *, user_id: uuid.UUID, keys) -> list[str]:
    """The owner chooses which unlocked achievements to show (at most `SHOWCASE_MAX`, in the given order).
    Raises `InvalidInput` (reason too_many / not_unlocked)."""
    chosen: list[str] = []
    for k in keys:
        if k not in chosen:
            chosen.append(k)
    if len(chosen) > SHOWCASE_MAX:
        raise InvalidInput("too many achievements in the showcase", reason="too_many")
    have = set(db.scalars(select(AchievementUnlock.key).where(AchievementUnlock.user_id == user_id)))
    if any(k not in have for k in chosen):
        raise InvalidInput("achievement not unlocked", reason="not_unlocked")
    db.execute(delete(AchievementShowcase).where(AchievementShowcase.user_id == user_id))
    for i, k in enumerate(chosen):
        db.add(AchievementShowcase(user_id=user_id, key=k, position=i))
    db.flush()
    return chosen


def _prune_showcase(db: Session, user_ids) -> None:
    for uid in user_ids:
        have = set(db.scalars(select(AchievementUnlock.key).where(AchievementUnlock.user_id == uid)))
        q = delete(AchievementShowcase).where(AchievementShowcase.user_id == uid)
        if have:
            q = q.where(AchievementShowcase.key.not_in(have))
        db.execute(q)
    db.flush()
