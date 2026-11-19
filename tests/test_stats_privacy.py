"""Stats privacy opt-out: peers lose balance/shots/achievements/leaderboard; self and chair admin keep them."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.db import read_session, write_session
from app.models import User
from app.services import achievements, ledger, overview, registration, users
from app.timeutil import utcnow
from tests import factories as f
from tests.user_support import field, make_account


@pytest.fixture
def old_price(db):
    return f.set_price(db, 50, effective_from=utcnow() - timedelta(days=30))


@pytest.fixture
def private_user(db, chair, old_price):
    user = make_account(db, "Private Person", chair, username="privperson")
    with write_session() as w:
        users.set_stats_private(w, user_id=user.id, stats_private=True)
        ledger.record_coffee(w, user_id=user.id, kind="double", nonce="priv-d")
        ledger.record_coffee(w, user_id=user.id, kind="single", nonce="priv-s")
        achievements.sync_user(w, user.id)
    db.refresh(user)
    return user


@pytest.fixture
def peer(db, chair):
    return make_account(db, "Peer Person", chair, username="peerperson")


def make_group_admin(db, chair, name="Gina Group", username="gina"):
    admin = f.make_admin(db, role="chair", chairs=[chair], username=f"adm-{username}")
    admin.user.display_name = name
    admin.user.normalized_name = users.normalize_name(name)
    db.commit()
    return admin


def test_can_see_personal_stats_matrix(db, chair, other_chair, private_user, peer):
    assert not overview.can_see_personal_stats(
        db, peer.id, person_id=private_user.id, chair_id=chair.id, stats_private=True)
    assert overview.can_see_personal_stats(
        db, private_user.id, person_id=private_user.id, chair_id=chair.id, stats_private=True)
    gina = make_group_admin(db, chair)
    assert overview.can_see_personal_stats(
        db, gina.user.id, person_id=private_user.id, chair_id=chair.id, stats_private=True)
    otto = make_group_admin(db, other_chair, name="Otto Other", username="otto")
    assert not overview.can_see_personal_stats(
        db, otto.user.id, person_id=private_user.id, chair_id=chair.id, stats_private=True)
    assert overview.can_see_personal_stats(
        db, peer.id, person_id=private_user.id, chair_id=chair.id, stats_private=False)


def test_peer_sees_name_but_not_stats_or_achievements(client, db, private_user, peer, login):
    login(peer)
    people = client.get("/overview/people").text
    assert "Private Person" in people
    assert "hidden their coffee statistics" not in people
    # masked cells use the private placeholder (em dash), not the real shot counts
    row_start = people.index("Private Person")
    row = people[row_start:people.index("</tr>", row_start)]
    assert "—" in row or "–" in row or "&mdash;" in row
    assert "Owes" not in row and "Credit" not in row and "Even" not in row

    page = client.get(f"/overview/people/{private_user.id}").text
    assert "Private Person" in page
    assert "hidden their coffee statistics" in page
    assert "tab=achievements" not in page
    assert "Coffees this month" not in page
    assert "Total coffees" not in page
    assert client.get(f"/overview/people/{private_user.id}?tab=achievements").text.count(
        "hidden their coffee statistics") >= 1


def test_owner_still_sees_own_private_stats(client, private_user, login):
    login(private_user)
    own = client.get(f"/overview/people/{private_user.id}").text
    assert "Coffees this month" in own and "Total coffees" in own
    assert "tab=achievements" in own
    assert "hidden their coffee statistics" not in own


def test_chair_admin_still_sees_private_stats(client, db, chair, private_user, login):
    gina = make_group_admin(db, chair)
    login(gina.user)
    admin_view = client.get(f"/overview/people/{private_user.id}").text
    assert "Coffees this month" in admin_view and "Total coffees" in admin_view
    assert "hidden their coffee statistics" not in admin_view


def test_leaderboard_omits_private_users_but_totals_include_them(client, db, chair, private_user, peer, old_price,
                                                                login):
    with write_session() as w:
        ledger.record_coffee(w, user_id=peer.id, kind="single", nonce="peer-s")
    login(peer)
    board = client.get("/leaderboard?period=all").text
    assert "Peer Person" in board
    assert "Private Person" not in board

    with read_session() as r:
        site = overview.site_totals(r)
    # private user booked a double (2) + single (1) = 3; peer booked 1
    assert site.total_shots >= 4


def test_people_sort_by_balance_does_not_leak_private_order(db, chair, old_price, peer):
    low = make_account(db, "Low Owes", chair, username="lowowes")
    high = make_account(db, "High Owes", chair, username="highowes")
    hidden = make_account(db, "Hidden Owes", chair, username="hiddenowes")
    with write_session() as w:
        ledger.record_coffee(w, user_id=low.id, kind="single", nonce="low-1")  # owes 50
        for i in range(5):
            ledger.record_coffee(w, user_id=high.id, kind="single", nonce=f"high-{i}")  # owes 250
        for i in range(10):
            ledger.record_coffee(w, user_id=hidden.id, kind="single", nonce=f"hid-{i}")  # owes 500
        users.set_stats_private(w, user_id=hidden.id, stats_private=True)
    page = overview.people(db, sort="balance", viewer_user_id=peer.id, per_page=50)
    names = [r.display_name for r in page.rows]
    assert names.index("High Owes") < names.index("Low Owes")
    # hidden must not sort ahead of public rows by its real (higher) balance
    assert names.index("Hidden Owes") > names.index("Low Owes")


def test_edit_privacy_toggle_and_csrf(client, db, peer, login):
    login(peer)
    assert client.post("/account/privacy", data={"stats_private": "1"}).status_code == 403
    csrf = field(client.get("/account/edit").text, "csrf_token")
    r = client.post("/account/privacy", data={"csrf_token": csrf, "stats_private": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "privacy_saved" in r.headers["location"]
    with read_session() as db_r:
        assert db_r.get(User, peer.id).stats_private is True
    csrf = field(client.get("/account/edit").text, "csrf_token")
    client.post("/account/privacy", data={"csrf_token": csrf}, follow_redirects=False)
    with read_session() as db_r:
        assert db_r.get(User, peer.id).stats_private is False


def test_register_stats_private_survives_confirm(db, chair, monkeypatch):
    monkeypatch.setenv("MAIL_BACKEND", "memory")
    with write_session() as w:
        result = registration.begin_member(
            w, username="newpriv", display_name="New Private", chair_id=chair.id,
            email="newpriv@tum.de", password="a-long-enough-password", privacy_ack=True,
            ip="127.0.0.1", stats_private=True)
        assert result.token
        token = result.token
    with write_session() as w:
        onboard = registration.confirm_member(w, token, ip="127.0.0.1")
        user_id = onboard.user.id
    with read_session() as r:
        assert r.get(User, user_id).stats_private is True


def test_privacy_page_mentions_opt_out(client):
    html = client.get("/privacy").text
    assert "Hide" in html or "hide" in html
    assert "leaderboard" in html.lower() or "Leaderboard" in html
    assert "achievement" in html.lower()
    assert "picture" in html.lower()
    assert "history" in html.lower()
