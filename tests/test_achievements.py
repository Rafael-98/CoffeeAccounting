"""Achievements: rule evaluation, Bavarian holidays, queues, unlock storage, undo, showcase and pages."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import cli
from app.db import read_session, write_session
from app.models import AchievementShowcase, AchievementUnlock, ConsumptionEvent, User
from app.services import achievements as ach
from app.services import ledger
from app.services.achievements import Booking, evaluate, find_queues
from app.services.errors import InvalidInput
from app.strings import CATALOGS
from app.timeutil import local_tz, utcnow
from tests import factories as f
from tests.user_support import field, make_account

ROOT = Path(__file__).resolve().parent.parent


def L(y, mo, d, h=12, mi=0, s=0) -> datetime:
    """A local (Europe/Berlin) wall-clock time as UTC."""
    return datetime(y, mo, d, h, mi, s, tzinfo=local_tz()).astimezone(UTC)


def B(at, kind="single", price="p1") -> Booking:
    return Booking(uuid.uuid4(), at, kind, 2 if kind == "double" else 1, price)


def keys(bookings, **kw) -> set[str]:
    return set(evaluate(bookings, **kw))


# ----- definitions --------------------------------------------------------------------------------------
def test_definitions_have_strings_and_icons():
    assert len(ach.ACHIEVEMENTS) == 36 and len(ach.BY_KEY) == 36
    assert "founding-member" not in ach.BY_KEY
    catalog = CATALOGS["en"]
    for a in ach.ACHIEVEMENTS:
        assert a.category in ach.CATEGORIES
        assert catalog.get(f"ach.{a.key}.name") and catalog.get(f"ach.{a.key}.desc"), a.key
        assert len(a.key) <= 40
    assert ach.BY_KEY["tenpi"].icon_is_image and (ROOT / "static/achievements/tenpi.png").is_file()
    assert ach.BY_KEY["schroedinger"].hidden


# ----- holidays ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("year,easter", [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)),
                                         (2026, date(2026, 4, 5)), (2027, date(2027, 3, 28)),
                                         (2038, date(2038, 4, 25))])
def test_easter(year, easter):
    assert ach.easter_sunday(year) == easter


def test_bavarian_holidays_2026():
    h = ach.bavarian_holidays(2026)
    assert len(h) == 13
    for d in (date(2026, 1, 6), date(2026, 4, 3), date(2026, 4, 6), date(2026, 5, 14), date(2026, 5, 25),
              date(2026, 6, 4), date(2026, 8, 15), date(2026, 11, 1), date(2026, 12, 26)):
        assert d in h
    assert date(2026, 8, 8) not in h  # Augsburger Friedensfest is not included
    assert not ach.is_working_day(date(2026, 5, 14)) and ach.is_working_day(date(2026, 5, 15))
    assert ach.previous_working_day(date(2026, 4, 7)) == date(2026, 4, 2)  # over Good Friday and Easter Monday


# ----- milestones and shots ------------------------------------------------------------------------------
def test_milestones_count_shots_and_a_double_can_jump_past():
    bookings = [B(L(2026, 3, 2, 9) + timedelta(hours=i)) for i in range(30)]
    jump = B(L(2026, 3, 9, 9), "double")
    got = evaluate(bookings + [jump])
    assert "starter-pack" in got and got["starter-pack"].event_id == bookings[0].id
    assert got["tenpi"].event_id == jump.id  # 30 -> 32
    assert got["double-trouble"].event_id == jump.id
    assert "answer-42" not in got


def test_high_milestones_and_lone_wolf():
    doubles = [B(L(2026, 1, 5, 9) + timedelta(minutes=30 * i), "double") for i in range(500)]
    got = evaluate(doubles)
    for k in ("answer-42", "three-digits", "fine-structure", "euler", "planck", "coffee-god", "lone-wolf"):
        assert k in got, k
    assert got["lone-wolf"].event_id == doubles[49].id
    assert got["three-digits"].event_id == doubles[49].id  # 100 shots
    assert got["fine-structure"].event_id == doubles[68].id  # 138 >= 137
    assert got["coffee-god"].event_id == doubles[499].id


# ----- time of day ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("h,mi,expected", [(3, 59, "night-owl"), (4, 0, "early-bird"), (6, 59, "early-bird"),
                                           (7, 0, None), (19, 59, None), (20, 0, "night-owl"), (23, 30, "night-owl")])
def test_time_windows(h, mi, expected):
    got = keys([B(L(2026, 3, 3, h, mi))]) & {"early-bird", "night-owl"}
    assert got == ({expected} if expected else set())


def test_afternoon_slump_needs_20():
    days = [L(2026, 3, 2, 14, 30) + timedelta(days=i) for i in range(19)]
    assert "afternoon-slump" not in keys([B(d) for d in days] + [B(L(2026, 4, 1, 16, 0))])
    assert "afternoon-slump" in keys([B(d) for d in days] + [B(L(2026, 4, 1, 15, 59))])


# ----- streaks -------------------------------------------------------------------------------------------
def test_flush_skips_weekends_and_holidays():
    # Wed 1 Apr, Thu 2 Apr, (Good Friday 3, weekend, Easter Monday 6), Tue 7, Wed 8, Thu 9 Apr 2026
    days = [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 7), date(2026, 4, 8), date(2026, 4, 9)]
    got = evaluate([B(L(d.year, d.month, d.day, 10)) for d in days])
    assert "flush" in got
    broken = [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 8), date(2026, 4, 9), date(2026, 4, 10)]
    assert "flush" not in keys([B(L(d.year, d.month, d.day, 10)) for d in broken])  # 7 April missing


def test_unstoppable_20_working_days():
    days, d = [], date(2026, 9, 1)
    while len(days) < 20:
        if ach.is_working_day(d):
            days.append(d)
        d += timedelta(days=1)
    bookings = [B(L(x.year, x.month, x.day, 9)) for x in days]
    assert "unstoppable" in keys(bookings) and "unstoppable" not in keys(bookings[:-1])


def test_royal_flush_seven_calendar_days():
    bookings = [B(L(2026, 3, 2 + i, 11)) for i in range(7)]
    assert "royal-flush" in keys(bookings) and "royal-flush" not in keys(bookings[:6])
    assert "weekend-warrior" in keys(bookings)


def test_monday_survivor_counts_distinct_mondays_including_holidays():
    mondays = [date(2026, 3, 2) + timedelta(weeks=i) for i in range(10)]  # includes Easter Monday 6 April
    assert date(2026, 4, 6) in mondays
    bookings = [B(L(m.year, m.month, m.day, 9)) for m in mondays]
    assert "monday-survivor" in keys(bookings)
    assert "monday-survivor" not in keys(bookings[:9] + [B(L(2026, 3, 2, 15))])


def test_clockwork_needs_one_coffee_per_day_in_one_15_minute_window():
    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(5)]  # Mon-Fri
    ok = [B(L(d.year, d.month, d.day, 13, 5 + 3 * i)) for i, d in enumerate(days)]  # 13:05..13:17
    extra = [B(L(d.year, d.month, d.day, 8, 0)) for d in days]  # other coffees that day do not matter
    assert "clockwork" in keys(ok + extra)
    spread = [B(L(d.year, d.month, d.day, 13, 0 + 4 * i)) for i, d in enumerate(days)]  # 13:00..13:16
    assert "clockwork" not in keys(spread)


# ----- intensity, calendar, loyalty --------------------------------------------------------------------
def test_coffee_addict_and_back_to_back():
    day = [B(L(2026, 3, 3, 8)), B(L(2026, 3, 3, 10), "double"), B(L(2026, 3, 3, 12), "double")]
    assert "coffee-addict" in keys(day) and "back-to-back" not in keys(day)
    assert "coffee-addict" not in keys(day[:2] + [B(L(2026, 3, 4, 1), "double")])
    assert "back-to-back" in keys([B(L(2026, 3, 3, 8)), B(L(2026, 3, 3, 8, 10))])
    assert "back-to-back" not in keys([B(L(2026, 3, 3, 8)), B(L(2026, 3, 3, 8, 10, 1))])


@pytest.mark.parametrize("at,key", [
    (L(2027, 1, 1, 0, 30), "new-years-brew"), (L(2026, 12, 24, 9), "christmas-coffee"),
    (L(2026, 12, 26, 9), "christmas-coffee"), (L(2026, 11, 13, 13, 59), "friday-13"),
    (L(2026, 11, 17, 13, 37, 59), "leet"), (L(2027, 3, 14, 15, 14), "pi-day"),
    (L(2026, 5, 14, 10), "holiday-hero"), (L(2026, 6, 4, 10), "holiday-hero"),
])
def test_calendar_badges(at, key):
    assert key in keys([B(at)])


@pytest.mark.parametrize("at,key", [
    (L(2026, 11, 13, 12, 59), "friday-13"), (L(2026, 1, 13, 13, 30), "friday-13"),  # a Tuesday
    (L(2026, 11, 17, 13, 38), "leet"), (L(2027, 3, 14, 15, 15), "pi-day"), (L(2026, 12, 27, 9), "christmas-coffee"),
    (L(2026, 8, 8, 10), "holiday-hero"),
])
def test_calendar_badges_negative(at, key):
    assert key not in keys([B(at)])


def test_loyalty():
    first = B(L(2025, 1, 10, 9))
    assert "one-year-in" not in keys([first, B(L(2026, 1, 9, 9))])
    assert "one-year-in" in keys([first, B(L(2026, 1, 10, 9))])
    assert "welcome-back" in keys([B(L(2026, 1, 1, 9)), B(L(2026, 1, 31, 9))])
    assert "welcome-back" not in keys([B(L(2026, 1, 1, 9)), B(L(2026, 1, 30, 9))])
    year = [B(L(2025 + (m > 6), m, 15, 10)) for m in range(1, 13)]
    assert "seasons" in keys(year) and "seasons" not in keys(year[:11])


def test_oddities():
    assert "inflation-survivor" in keys([B(L(2026, 1, 1), price="a"), B(L(2026, 2, 1), price="b")])
    assert "inflation-survivor" not in keys([B(L(2026, 1, 1), price="a"), B(L(2026, 2, 1), price="a")])
    rid = uuid.uuid4()
    got = evaluate([], own_undos=[(L(2026, 1, 1), rid)])
    assert got["schroedinger"].event_id == rid
    got = evaluate([], balance_points=[(L(2026, 1, 1), 50, None), (L(2026, 1, 2), 0, None)])
    assert got["ground-state"].at == L(2026, 1, 2)
    assert "plus-one" in evaluate([], guest_confirmations=[L(2026, 1, 3)])


# ----- queues --------------------------------------------------------------------------------------------
def chain(start, people, gap_min=1):
    return [(uuid.uuid4(), start + timedelta(minutes=gap_min * i), p) for i, p in enumerate(people)]


def test_queue_needs_8_people_and_is_awarded_only_when_over():
    people = [uuid.uuid4() for _ in range(8)]
    rows = chain(L(2026, 3, 3, 13), people + [people[0]])  # 9 bookings by 8 people
    end = rows[-1][1]
    assert find_queues(rows, end + timedelta(minutes=4)) == []  # still open
    awards = find_queues(rows, end + timedelta(minutes=6))
    assert {(a.key, a.user_id) for a in awards} == {("first-in-line", people[0]), ("last-in-line", people[0])}
    assert awards[0].at == end + ach.QUEUE_GAP
    assert find_queues(chain(L(2026, 3, 3, 13), people[:7]), L(2026, 3, 4)) == []


def test_queue_gap_of_more_than_5_minutes_splits():
    people = [uuid.uuid4() for _ in range(16)]
    rows = chain(L(2026, 3, 3, 13), people[:8], gap_min=5) + chain(L(2026, 3, 3, 13, 41), people[8:], gap_min=5)
    awards = find_queues(rows, L(2026, 3, 4))
    assert len(awards) == 4
    assert [a.user_id for a in awards if a.key == "first-in-line"] == [people[0], people[8]]


# ----- database: booking, undo, admin reversal, payment ----------------------------------------------
@pytest.fixture
def old_price(db):
    return f.set_price(db, 50, effective_from=utcnow() - timedelta(days=400))


def book(user, kind="single", now=None, session_id=None):
    with write_session() as w:
        return ledger.record_coffee(w, user_id=user.id, kind=kind, nonce=uuid.uuid4().hex, now=now,
                                    session_id=session_id).event.id


def unlocks(user_id) -> dict[str, AchievementUnlock]:
    with read_session() as r:
        rows = r.execute(select(AchievementUnlock.key, AchievementUnlock.event_id, AchievementUnlock.unlocked_at)
                         .where(AchievementUnlock.user_id == user_id)).all()
        return {row.key: row for row in rows}


def test_booking_unlocks_with_event_and_time(db, user, old_price):
    eid = book(user)
    got = unlocks(user.id)
    assert got["starter-pack"].event_id == eid
    with read_session() as r:
        assert ach.new_for_events(r, [eid])[0].key == "starter-pack"


def test_immediate_undo_revokes_but_keeps_schroedinger(db, user, old_price):
    session = f.make_session(db, user)
    eid = book(user, session_id=session.id)
    with write_session() as w:
        ledger.undo_coffee(w, user_id=user.id, session_id=session.id, event_id=eid)
    got = unlocks(user.id)
    assert "starter-pack" not in got and "schroedinger" in got
    # a new coffee earns Starter Pack again; the undo did not cost Schrödinger
    eid2 = book(user, session_id=session.id)
    got = unlocks(user.id)
    assert got["starter-pack"].event_id == eid2 and "schroedinger" in got


def test_undo_keeps_badges_still_earned_by_other_coffees(db, user, old_price):
    session = f.make_session(db, user)
    book(user, session_id=session.id)
    eid = book(user, "double", session_id=session.id)
    with write_session() as w:
        ledger.undo_coffee(w, user_id=user.id, session_id=session.id, event_id=eid)
    got = unlocks(user.id)
    assert "starter-pack" in got and "double-trouble" not in got


def test_admin_reversal_never_revokes(db, user, global_admin, old_price):
    eid = book(user)
    ledger.admin_reverse_event(db, admin=global_admin, event_id=eid, note="mistake")
    db.commit()
    assert "starter-pack" in unlocks(user.id)


def test_payment_to_zero_unlocks_ground_state(db, user, global_admin, old_price):
    book(user)
    assert "ground-state" not in unlocks(user.id)
    ledger.add_payment(db, admin=global_admin, user_id=user.id, amount_cents=50)
    db.commit()
    assert "ground-state" in unlocks(user.id)


def test_inactive_accounts_get_nothing_new(db, chair):
    gone = f.make_user(db, "Gone", chair, status="inactive")
    with write_session() as w:
        assert ach.sync_user(w, gone.id) == []


def test_plus_one_for_confirmed_guest(db, chair, old_price):
    sponsor = f.make_member(db, "Sponsor", chair)
    f.make_guest(db, sponsor, "Guest", username="guest1")
    with write_session() as w:
        assert "plus-one" in ach.sync_user(w, sponsor.id)


def test_queue_badges_from_bookings_and_last_moves_after_undo(db, chair, old_price):
    people = [f.make_member(db, f"Q{i}", chair) for i in range(9)]
    sessions = [f.make_session(db, p) for p in people]
    t0 = utcnow() - timedelta(minutes=30)
    ids = [book(p, now=t0 + timedelta(minutes=i), session_id=s.id) for i, (p, s) in enumerate(zip(people, sessions))]
    # the 9th person undoes straight away (within the undo window, before the queue is over)
    with write_session() as w:
        ledger.undo_coffee(w, user_id=people[8].id, session_id=sessions[8].id, event_id=ids[8],
                           now=t0 + timedelta(minutes=8, seconds=30))
    assert "first-in-line" not in unlocks(people[0].id)  # never awarded live
    with write_session() as w:
        ach.sweep_queues(w, since=None)
    assert unlocks(people[0].id)["first-in-line"].event_id == ids[0]
    assert unlocks(people[7].id)["last-in-line"].event_id == ids[7]
    assert "last-in-line" not in unlocks(people[8].id)


def test_cli_backfill_uses_real_dates(db, user, old_price, capsys):
    at = utcnow() - timedelta(days=40)
    with write_session() as w:
        w.add(ConsumptionEvent(user_id=user.id, chair_id=user.chair_id, quantity=2, type="double",
                               unit_price_cents=50, amount_cents=100, price_id=old_price.id,
                               idempotency_key="legacy-1", created_at=at))
    assert unlocks(user.id) == {}
    assert cli.main(["achievements-sync"]) == 0
    assert "Achievements synced" in capsys.readouterr().out
    got = unlocks(user.id)
    assert abs(got["double-trouble"].unlocked_at - at) < timedelta(seconds=1)
    assert cli.main(["achievements-sync"]) == 0  # idempotent
    assert len(unlocks(user.id)) == len(got)


# ----- showcase --------------------------------------------------------------------------------------
def grant(user_id, *keys_):
    with write_session() as w:
        for k in keys_:
            w.add(AchievementUnlock(user_id=user_id, key=k, unlocked_at=utcnow()))


def test_showcase_rules(db, user):
    ten = [a.key for a in ach.ACHIEVEMENTS[:11]]
    grant(user.id, *ten)
    with write_session() as w:
        assert ach.set_showcase(w, user_id=user.id, keys=ten[:10]) == ten[:10]
    with pytest.raises(InvalidInput) as exc, write_session() as w:
        ach.set_showcase(w, user_id=user.id, keys=ten)
    assert exc.value.context["reason"] == "too_many"
    with pytest.raises(InvalidInput) as exc, write_session() as w:
        ach.set_showcase(w, user_id=user.id, keys=["coffee-god"] if "coffee-god" not in ten else ["clockwork"])
    assert exc.value.context["reason"] == "not_unlocked"
    with read_session() as r:
        assert [a.key for a in ach.showcase(r, user.id)] == ten[:10]


def test_undo_removes_revoked_badge_from_showcase(db, user, old_price):
    session = f.make_session(db, user)
    eid = book(user, "double", session_id=session.id)
    with write_session() as w:
        ach.set_showcase(w, user_id=user.id, keys=["starter-pack", "double-trouble"])
    with write_session() as w:
        ledger.undo_coffee(w, user_id=user.id, session_id=session.id, event_id=eid)
    with read_session() as r:
        assert ach.showcase(r, user.id) == []
        assert r.scalars(select(AchievementShowcase)).all() == []


# ----- pages -----------------------------------------------------------------------------------------
@pytest.fixture
def viewer(db, chair):
    return make_account(db, "Viewer Person", chair, username="viewer")


def test_achievements_tab_own_and_other(client, db, viewer, chair, login):
    other = f.make_member(db, "Other Person", chair)
    grant(viewer.id, "starter-pack", "tenpi")
    grant(other.id, "leet")
    login(viewer)
    html = client.get(f"/overview/people/{viewer.id}?tab=achievements").text
    assert "Starter Pack" in html and "TenPi" in html and "/static/achievements/tenpi.png" in html
    assert "Coffee God" not in html and "Schrödinger" not in html  # locked: never shown, not even greyed out
    assert "Unlocked " in html and 'name="keys"' in html  # dates and showcase form only for the owner
    html = client.get(f"/overview/people/{other.id}?tab=achievements").text
    assert "Leet" in html and "Unlocked " not in html and 'name="keys"' not in html
    assert 'action="/account/showcase"' not in html
    overview = client.get(f"/overview/people/{other.id}").text
    assert "Leet" not in overview  # the overview tab lists none (nothing in the showcase)
    assert 'aria-current="page" class="is-current">Overview' in overview


def test_showcase_form_and_display(client, db, viewer, chair, login):
    grant(viewer.id, "starter-pack", "leet", "night-owl")
    login(viewer)
    page = client.get(f"/overview/people/{viewer.id}?tab=achievements").text
    r = client.post("/account/showcase", data={"csrf_token": field(page, "csrf_token"), "keys": ["leet", "night-owl"]},
                    follow_redirects=False)
    assert r.status_code == 303 and "showcase=saved" in r.headers["location"]
    assert "Showcase saved." in client.get(r.headers["location"]).text
    html = client.get(f"/overview/people/{viewer.id}").text
    show = html[html.index('class="showcase"'):html.index("</ul>", html.index('class="showcase"'))]
    assert "Leet" in show and "Night Owl" in show and "Starter Pack" not in show
    # other people see it too
    client.cookies.clear()
    stranger = make_account(db, "Stranger", chair, username="stranger")
    login(stranger)
    assert 'class="showcase"' in client.get(f"/overview/people/{viewer.id}").text
    # too many
    login(viewer)
    grant(viewer.id, *[a.key for a in ach.ACHIEVEMENTS if a.key not in ("starter-pack", "leet", "night-owl")][:9])
    page = client.get(f"/overview/people/{viewer.id}?tab=achievements").text
    allk = re.findall(r'name="keys" value="([^"]+)"', page)
    assert len(allk) == 12
    r = client.post("/account/showcase", data={"csrf_token": field(page, "csrf_token"), "keys": allk},
                    follow_redirects=False)
    assert "showcase=too_many" in r.headers["location"]
    assert "at most 10" in client.get(r.headers["location"]).text


def test_showcase_needs_csrf(client, viewer, login):
    login(viewer)
    assert client.post("/account/showcase", data={"keys": "leet"}).status_code == 403


def test_done_page_announces_new_achievement(client, db, viewer, login, old_price):
    from tests.user_support import scan
    login(viewer)
    r = scan(client, "single")
    html = client.get(r.headers["location"]).text
    assert "New achievement" in html and "Starter Pack" in html  # (plus time-of-day badges, depending on the clock)
    r = scan(client, "single")
    html = client.get(r.headers["location"]).text
    assert "Back to Back" in html and "Starter Pack" not in html  # only what THIS coffee unlocked


def test_unknown_tab_falls_back_to_overview(client, viewer, login):
    login(viewer)
    html = client.get(f"/overview/people/{viewer.id}?tab=<script>").text
    assert 'aria-current="page" class="is-current">Overview' in html and "<script>" not in html


def test_merged_profile_unlocks_follow_the_target(db, chair, global_admin, old_price):
    from app.services import users
    keep = f.make_member(db, "Keep", chair)
    dup = f.make_member(db, "Dup", chair)
    f.add_events(db, dup, ["double"], at=utcnow() - timedelta(days=2))
    users.merge_users(db, admin=global_admin, duplicate_id=dup.id, target_id=keep.id)
    db.commit()
    with write_session() as w:
        new = ach.sync_user(w, dup.id)  # resolves to the target
    assert "double-trouble" in new and "double-trouble" in unlocks(keep.id)
    with read_session() as r:
        assert r.get(User, dup.id).status == "merged"

