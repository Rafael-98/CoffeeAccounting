"""Profile extras: status text, profile picture, header buttons, own-profile link, leaderboard, moderation."""

from __future__ import annotations

import io
import re
import uuid
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from app.db import read_session, write_session
from app.models import AuditLog, UserProfile
from app.services import ledger, overview, profiles, users
from app.services.errors import Forbidden, InvalidInput, UserInactive
from app.timeutil import local_month_bounds, to_local, utcnow
from tests import factories as f
from tests.admin_support import fast_hasher, login_as, make_real_admin, new_client, page_csrf  # noqa: F401
from tests.user_support import field, make_account


def image_bytes(fmt="JPEG", size=(640, 480), color=(200, 30, 30), mode="RGB", exif_gps=False) -> bytes:
    img = Image.new(mode, size, color)
    out = io.BytesIO()
    kw = {}
    if exif_gps:
        exif = Image.Exif()
        exif[0x010F] = "SENTINEL-CAMERA"  # Make
        exif[0x8825] = {1: "N", 2: (48.0, 9.0, 0.0)}  # GPS IFD
        kw["exif"] = exif
    img.save(out, format=fmt, **kw)
    return out.getvalue()


@pytest.fixture
def old_price(db):
    return f.set_price(db, 50, effective_from=utcnow() - timedelta(days=400))


@pytest.fixture
def viewer(db, chair):
    return make_account(db, "Viewer Person", chair, username="viewer")


def edit_csrf(client) -> str:
    return field(client.get("/account/edit").text, "csrf_token")


# ----- picture processing -------------------------------------------------------------------------------
def test_picture_is_square_small_jpeg_without_metadata():
    raw = image_bytes(size=(1200, 600), exif_gps=True)
    assert b"SENTINEL-CAMERA" in raw
    stored = profiles.process_avatar(raw)
    assert b"SENTINEL-CAMERA" not in stored and b"Exif" not in stored
    with Image.open(io.BytesIO(stored)) as img:
        assert img.format == "JPEG" and img.size == (profiles.AVATAR_SIZE, profiles.AVATAR_SIZE)
        assert not img.getexif()


@pytest.mark.parametrize("fmt,mode", [("PNG", "RGBA"), ("WEBP", "RGB"), ("GIF", "P")])
def test_other_formats_are_converted(fmt, mode):
    color = (10, 200, 10, 128) if mode == "RGBA" else (10, 200, 10) if mode == "RGB" else 3
    stored = profiles.process_avatar(image_bytes(fmt, size=(50, 80), color=color, mode=mode))
    with Image.open(io.BytesIO(stored)) as img:
        assert img.format == "JPEG" and img.size == (256, 256)


@pytest.mark.parametrize("data,reason", [
    (b"", "empty"),
    (b"not a picture at all", "not_image"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "not_image"),
    (b"\xff\xd8\xff" + b"0" * 100, "not_image"),  # truncated JPEG
    (b"0" * (profiles.AVATAR_UPLOAD_MAX_BYTES + 1), "too_large"),
])
def test_bad_uploads_are_refused(data, reason):
    with pytest.raises(InvalidInput) as exc:
        profiles.process_avatar(data)
    assert exc.value.context["reason"] == reason


def test_huge_dimensions_are_refused(monkeypatch):
    monkeypatch.setattr(profiles, "AVATAR_MAX_PIXELS", 100)
    with pytest.raises(InvalidInput) as exc:
        profiles.process_avatar(image_bytes(size=(20, 20)))
    assert exc.value.context["reason"] == "too_large"


# ----- status validation --------------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("  in a   meeting ", "in a meeting"),
    ("☕", "☕"),
    ("👩‍💻 coding", "👩‍💻 coding"),  # zero width joiner kept
    ("", None),
    ("   ", None),
])
def test_status_cleaning(text, expected):
    assert profiles.clean_status(text) == expected


@pytest.mark.parametrize("text,reason", [("x" * 61, "too_long"), ("bad‮ltr", "invalid_chars"),
                                         ("bell\x07", "invalid_chars")])
def test_status_refused(text, reason):
    with pytest.raises(InvalidInput) as exc:
        profiles.clean_status(text)
    assert exc.value.context["reason"] == reason


def test_status_set_clear_and_row_cleanup(db, user):
    with write_session() as w:
        assert profiles.set_status(w, user_id=user.id, text="Brauche Kaffee ☕") == "Brauche Kaffee ☕"
    with read_session() as r:
        assert profiles.get_profile(r, user.id).status_text == "Brauche Kaffee ☕"
    with write_session() as w:
        assert profiles.set_status(w, user_id=user.id, text="") is None
    with read_session() as r:
        assert r.get(UserProfile, user.id) is None  # nothing left: the row is gone


def test_inactive_user_cannot_change_profile(db, chair):
    gone = f.make_user(db, "Gone", chair, status="inactive")
    with pytest.raises(UserInactive), write_session() as w:
        profiles.set_status(w, user_id=gone.id, text="hi")


# ----- pages ----------------------------------------------------------------------------------------------
def test_header_buttons_only_when_signed_in(client, viewer, login):
    html = client.get("/").text
    assert 'href="/profile"' not in html and 'href="/leaderboard"' not in html
    login(viewer)
    html = client.get("/").text
    assert 'class="header-actions"' in html and 'href="/profile"' in html and 'href="/leaderboard"' in html
    assert "My profile" in html and "Leaderboard" in html


def test_admin_pages_have_no_user_header_buttons(app, db):
    admin_id, _ = make_real_admin(username="boss")
    c = new_client(app)
    login_as(c, admin_id)
    assert 'class="header-actions"' not in c.get("/admin").text


def test_profile_button_goes_to_own_public_profile(client, viewer, login):
    r = client.get("/profile", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    login(viewer)
    r = client.get("/profile", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/overview/people/{viewer.id}"
    html = client.get(r.headers["location"]).text
    assert 'href="/account/edit"' in html and "No status set." in html


def test_other_peoples_profile_has_no_edit_link(client, db, viewer, chair, login):
    other = f.make_member(db, "Other One", chair)
    login(viewer)
    html = client.get(f"/overview/people/{other.id}").text
    assert 'href="/account/edit"' not in html and "No status set." not in html


def test_status_round_trip_and_escaping(client, db, viewer, chair, login):
    login(viewer)
    r = client.post("/account/status", data={"csrf_token": edit_csrf(client), "status_text": "<b>busy</b> ☕"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/edit?notice=status_saved"
    assert "Status saved." in client.get(r.headers["location"]).text
    profile_html = client.get(f"/overview/people/{viewer.id}").text
    assert "&lt;b&gt;busy&lt;/b&gt; ☕" in profile_html and "<b>busy" not in profile_html
    people_html = client.get("/overview/people").text
    assert "&lt;b&gt;busy&lt;/b&gt; ☕" in people_html


def test_status_too_long_shows_error(client, viewer, login):
    login(viewer)
    r = client.post("/account/status", data={"csrf_token": edit_csrf(client), "status_text": "x" * 61})
    assert r.status_code == 400 and "The status is too long" in r.text


def test_status_needs_csrf(client, viewer, login):
    login(viewer)
    r = client.post("/account/status", data={"status_text": "hi"})
    assert r.status_code == 403


def test_picture_upload_view_and_remove(client, db, viewer, chair, login):
    login(viewer)
    assert 'avatar-initials' in client.get(f"/overview/people/{viewer.id}").text  # "VP" placeholder
    r = client.post("/account/avatar", data={"csrf_token": edit_csrf(client)},
                    files={"picture": ("me.jpg", image_bytes(exif_gps=True), "image/jpeg")}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/edit?notice=avatar_saved"
    html = client.get(f"/overview/people/{viewer.id}").text
    m = re.search(rf'src="(/avatar/{viewer.id}\?v=\d+)"', html)
    assert m
    img = client.get(m.group(1))
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert b"SENTINEL-CAMERA" not in img.content
    assert img.headers["cache-control"].startswith("private")
    # the header's profile button now shows the picture
    assert f'src="/avatar/{viewer.id}' in client.get("/overview").text
    # anonymous visitors get nothing
    client.cookies.clear()
    assert client.get(m.group(1)).status_code == 403
    login(viewer)
    r = client.post("/account/avatar/remove", data={"csrf_token": edit_csrf(client)}, follow_redirects=False)
    assert r.headers["location"] == "/account/edit?notice=avatar_removed"
    assert client.get(m.group(1)).status_code == 404


def test_picture_upload_errors(client, viewer, login):
    login(viewer)
    r = client.post("/account/avatar", data={"csrf_token": edit_csrf(client)},
                    files={"picture": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 400 and "could not be read as a picture" in r.text


def test_picture_larger_than_64k_passes_the_body_limit_but_not_above_6mb(client, viewer, login):
    login(viewer)
    big = image_bytes("PNG", size=(800, 800), color=(1, 2, 3))
    noisy = Image.effect_noise((600, 600), 100).convert("RGB")
    out = io.BytesIO()
    noisy.save(out, format="PNG")
    big = out.getvalue()
    assert len(big) > 64 * 1024
    r = client.post("/account/avatar", data={"csrf_token": edit_csrf(client)},
                    files={"picture": ("big.png", big, "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/account/avatar", data={"csrf_token": edit_csrf(client)},
                    files={"picture": ("huge.png", b"0" * (6 * 1024 * 1024 + 1), "image/png")})
    assert r.status_code == 413
    # other forms keep the small limit
    r = client.post("/account/status", data={"csrf_token": edit_csrf(client), "status_text": "x" * (70 * 1024)})
    assert r.status_code == 413


def test_bad_avatar_ids_are_404(client, viewer, login):
    login(viewer)
    for bad in ("nope", str(uuid.uuid4()), str(viewer.id)):
        assert client.get(f"/avatar/{bad}").status_code == 404


def test_deactivation_and_merge_delete_profile(db, chair, global_admin):
    a = f.make_member(db, "Anna", chair)
    b = f.make_member(db, "Bert", chair)
    keep = f.make_member(db, "Keep", chair)
    with write_session() as w:
        for u in (a, b):
            profiles.set_status(w, user_id=u.id, text="hi")
            profiles.set_avatar(w, user_id=u.id, data=image_bytes())
    with write_session() as w:
        users.anonymize_deactivate(w, user=w.get(type(a), a.id), reason="admin", actor_type="admin",
                                   actor_id=global_admin.id, notify=False)
        admin = w.get(type(global_admin), global_admin.id)
        users.merge_users(w, admin=admin, duplicate_id=b.id, target_id=keep.id)
    with read_session() as r:
        assert r.get(UserProfile, a.id) is None and r.get(UserProfile, b.id) is None


# ----- moderation -----------------------------------------------------------------------------------------
def test_admin_removes_picture_and_status_and_it_is_audited(app, db, chair, viewer):
    with write_session() as w:
        profiles.set_status(w, user_id=viewer.id, text="SENTINEL-STATUS")
        profiles.set_avatar(w, user_id=viewer.id, data=image_bytes())
    admin_id, _ = make_real_admin(role="chair", chairs=[chair], username="gadm")
    c = new_client(app)
    csrf = login_as(c, admin_id)
    page = c.get(f"/admin/users/{viewer.id}").text
    assert "SENTINEL-STATUS" in page and f"/admin/users/{viewer.id}/avatar?v=" in page
    assert c.get(f"/admin/users/{viewer.id}/avatar").headers["content-type"] == "image/jpeg"
    r = c.post(f"/admin/users/{viewer.id}/avatar/remove", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "msg=avatar_removed" in r.headers["location"]
    r = c.post(f"/admin/users/{viewer.id}/status-text/remove", data={"csrf_token": csrf}, follow_redirects=False)
    assert "msg=status_text_removed" in r.headers["location"]
    with read_session() as s:
        assert s.get(UserProfile, viewer.id) is None
        actions = s.scalars(select(AuditLog.action).where(AuditLog.target_id == str(viewer.id))).all()
        assert "user.avatar_removed" in actions and "user.status_text_removed" in actions
        metas = s.scalars(select(AuditLog.meta).where(AuditLog.target_id == str(viewer.id))).all()
        assert "SENTINEL-STATUS" not in repr(metas)


def test_out_of_scope_group_admin_cannot_remove(db, chair, other_chair, viewer):
    with write_session() as w:
        profiles.set_status(w, user_id=viewer.id, text="keep me")
    other_admin = f.make_admin(db, role="chair", chairs=[other_chair], username="adm-other")
    with pytest.raises(Forbidden), write_session() as w:
        profiles.admin_remove(w, admin=w.get(type(other_admin), other_admin.id), user_id=viewer.id, what="status")
    with read_session() as r:
        assert profiles.get_profile(r, viewer.id).status_text == "keep me"


# ----- leaderboard ----------------------------------------------------------------------------------------
@pytest.fixture
def board(db, chair, other_chair, global_admin, old_price):
    start = local_month_bounds(utcnow())[0]
    d = {n: f.make_member(db, n, chair if n != "Dora" else other_chair) for n in ("Anna", "Ben", "Cleo", "Dora")}
    f.add_events(db, d["Anna"], ["double", "single"], at=start + timedelta(seconds=5))  # 3 this month
    f.add_events(db, d["Ben"], ["double", "single"], at=start + timedelta(seconds=6))  # 3 this month
    f.add_events(db, d["Cleo"], ["single"], at=start + timedelta(seconds=7))  # 1 this month
    f.add_events(db, d["Dora"], ["double"] * 5, at=start - timedelta(days=3))  # 10 last month
    ev = f.add_events(db, d["Cleo"], "double", at=start + timedelta(seconds=8))[0]
    ledger.admin_reverse_event(db, admin=global_admin, event_id=ev.id, note="undo", now=start + timedelta(seconds=9))
    db.commit()
    return d


def test_leaderboard_service_month_ties_and_all_time(db, board):
    lb = overview.leaderboard(db)
    assert lb.period == to_local(utcnow()).strftime("%Y-%m")
    assert [(r.rank, r.display_name, r.shots) for r in lb.rows] == [(1, "Anna", 3), (1, "Ben", 3), (3, "Cleo", 1)]
    all_time = overview.leaderboard(db, "all")
    assert [(r.rank, r.display_name) for r in all_time.rows][0] == (1, "Dora")
    last = to_local(local_month_bounds(utcnow())[0] - timedelta(hours=1)).strftime("%Y-%m")
    assert [r.display_name for r in overview.leaderboard(db, last).rows] == ["Dora"]
    assert last in lb.months and lb.months[0] == lb.period


@pytest.mark.parametrize("value", ["", "garbage", "2999-01", "2026-13", "1999-12", "'; drop", "20261-1"])
def test_leaderboard_bad_period_falls_back_to_current_month(value):
    assert overview.parse_period(value) == to_local(utcnow()).strftime("%Y-%m")


def test_leaderboard_page(client, db, viewer, board, login):
    assert client.get("/leaderboard", follow_redirects=False).headers["location"] == "/login"
    login(viewer)
    r = client.get("/leaderboard")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
    html = r.text
    assert html.index("Anna") < html.index("Ben") < html.index("Cleo") and "Dora" not in html
    assert "€" not in html.split('class="leaderboard"', 1)[1]  # shots only, no money
    assert 'aria-current="page" class="is-current">Leaderboard</a>' in html
    assert "Dora" in client.get("/leaderboard", params={"period": "all"}).text
    assert client.get("/leaderboard", params={"period": "x" * 500}).status_code == 200


def test_today_counter_on_profile(client, db, viewer, chair, login, old_price):
    login(viewer)
    from app.timeutil import local_day_bounds
    f.add_events(db, viewer, ["double", "single"], at=max(local_day_bounds()[0], utcnow() - timedelta(seconds=30)))
    html = client.get("/profile", follow_redirects=True).text
    flat = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    assert "Coffees today 3" in flat
