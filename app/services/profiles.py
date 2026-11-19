"""Public profile extras: status text and profile picture (`user_profiles`).

Not part of the ledger. Owners change their own status and picture (`actor_type="user"`); admins may
remove either for moderation (`actor_type="admin"`, scope-checked like every admin action on a user, and
audited). Removing an account's identity (anonymizing deactivation) deletes its profile row.

Pictures are never stored as uploaded: `process_avatar` decodes the upload with Pillow, applies the EXIF
orientation, crops the centre square, scales it to `AVATAR_SIZE` pixels and re-encodes it as a JPEG with
no metadata (so no camera data or GPS position survives). Services never commit.
"""

from __future__ import annotations

import io
import unicodedata
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AVATAR_MAX_BYTES, STATUS_TEXT_MAX, AdminUser, User, UserProfile
from app.services import audit
from app.services.authz import ensure_admin_can_access_user
from app.services.errors import Conflict, Forbidden, InvalidInput, NotFound, UserInactive
from app.timeutil import ensure_utc, utcnow

AVATAR_SIZE = 256
AVATAR_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
AVATAR_MAX_PIXELS = 40_000_000  # decoded size limit (decompression bombs)
AVATAR_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
AVATAR_CONTENT_TYPE = "image/jpeg"
_JPEG_QUALITY = 85

# Format characters allowed in a status despite category Cf: they glue emoji together
# (zero width joiner) and build subdivision flags (tag characters).
_ALLOWED_FORMAT_CHARS = {"‍"} | {chr(c) for c in range(0xE0020, 0xE0080)}


@dataclass(frozen=True)
class ProfileInfo:
    user_id: uuid.UUID
    status_text: str | None
    has_avatar: bool
    avatar_version: str  # changes whenever the picture changes (cache-busting query value)


def _now(now: datetime | None) -> datetime:
    return ensure_utc(now) if now is not None else utcnow()


def _version(updated_at: datetime | None) -> str:
    return str(int(updated_at.timestamp())) if updated_at is not None else ""


# ----- validation ---------------------------------------------------------------------------
def clean_status(text: str | None) -> str | None:
    """Normalized status text, or None for an empty status. Raises `InvalidInput` when too long or when it
    contains control characters."""
    text = " ".join(unicodedata.normalize("NFC", text or "").split())
    if not text:
        return None
    if len(text) > STATUS_TEXT_MAX:
        raise InvalidInput(f"status longer than {STATUS_TEXT_MAX} characters", reason="too_long")
    for c in text:
        cat = unicodedata.category(c)
        if cat in ("Cc", "Co", "Cs") or (cat == "Cf" and c not in _ALLOWED_FORMAT_CHARS):
            raise InvalidInput("status contains invalid characters", reason="invalid_chars")
    return text


def process_avatar(data: bytes) -> bytes:
    """Validate an uploaded picture and return the stored JPEG (square, `AVATAR_SIZE` px, no metadata).

    Raises `InvalidInput` (`reason` too_large, not_image) for anything that is not a readable picture
    in one of `AVATAR_FORMATS` within the size limits."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise InvalidInput("no picture uploaded", reason="empty")
    if len(data) > AVATAR_UPLOAD_MAX_BYTES:
        raise InvalidInput("picture file too large", reason="too_large")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                if img.format not in AVATAR_FORMATS:
                    raise InvalidInput("unsupported picture format", reason="not_image")
                width, height = img.size
                if width < 1 or height < 1 or width * height > AVATAR_MAX_PIXELS:
                    raise InvalidInput("picture dimensions out of range", reason="too_large")
                img.seek(0)  # first frame of an animated GIF/WebP
                img.load()
                img = ImageOps.exif_transpose(img)
                if img.mode in ("RGBA", "LA", "P", "PA"):
                    img = img.convert("RGBA")
                    background = Image.new("RGBA", img.size, (255, 255, 255, 255))
                    img = Image.alpha_composite(background, img)
                img = img.convert("RGB")
                img = ImageOps.fit(img, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS)
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)  # no exif/icc passed: none stored
    except InvalidInput:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError,
            ValueError, SyntaxError) as exc:
        raise InvalidInput("not a readable picture", reason="not_image") from exc
    result = out.getvalue()
    if len(result) > AVATAR_MAX_BYTES:  # cannot happen for 256x256 at this quality; guard the table check
        raise InvalidInput("processed picture too large", reason="too_large")
    return result


# ----- reads ----------------------------------------------------------------------------------
def get_profile(db: Session, user_id: uuid.UUID) -> ProfileInfo:
    row = db.execute(
        select(UserProfile.status_text, UserProfile.avatar_updated_at).where(UserProfile.user_id == user_id)
    ).first()
    if row is None:
        return ProfileInfo(user_id, None, False, "")
    return ProfileInfo(user_id, row.status_text, row.avatar_updated_at is not None, _version(row.avatar_updated_at))


def profiles_for(db: Session, user_ids) -> dict[uuid.UUID, ProfileInfo]:
    """Profiles of several accounts in one statement (ids without a row are left out)."""
    ids = list(user_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(UserProfile.user_id, UserProfile.status_text, UserProfile.avatar_updated_at)
        .where(UserProfile.user_id.in_(ids))
    ).all()
    return {r.user_id: ProfileInfo(r.user_id, r.status_text, r.avatar_updated_at is not None,
                                   _version(r.avatar_updated_at)) for r in rows}


def get_avatar(db: Session, user_id: uuid.UUID) -> tuple[bytes, str]:
    """`(jpeg bytes, version)` of the account's picture; `NotFound` when it has none."""
    row = db.execute(
        select(UserProfile.avatar, UserProfile.avatar_updated_at).where(UserProfile.user_id == user_id)
    ).first()
    if row is None or row.avatar is None:
        raise NotFound("no picture")
    return bytes(row.avatar), _version(row.avatar_updated_at)


# ----- writes ---------------------------------------------------------------------------------
def _row(db: Session, user_id: uuid.UUID, now: datetime) -> UserProfile:
    row = db.get(UserProfile, user_id)
    if row is None:
        row = UserProfile(user_id=user_id, updated_at=now)
        db.add(row)
    return row


def _drop_if_empty(db: Session, row: UserProfile) -> None:
    if row.status_text is None and row.avatar is None:
        db.delete(row)


def _owner(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound("user not found")
    if user.status == "merged":
        raise Conflict("profile has been merged")
    if user.status != "active":
        raise UserInactive("profile is not active")
    return user


def set_status(db: Session, *, user_id: uuid.UUID, text: str | None, now: datetime | None = None) -> str | None:
    """The owner sets (or, with an empty text, clears) their status. Returns the stored text."""
    now = _now(now)
    _owner(db, user_id)
    cleaned = clean_status(text)
    row = _row(db, user_id, now)
    row.status_text = cleaned
    row.updated_at = now
    db.flush()
    _drop_if_empty(db, row)
    db.flush()
    return cleaned


def set_avatar(db: Session, *, user_id: uuid.UUID, data: bytes, now: datetime | None = None) -> None:
    """The owner uploads a new picture (raw upload bytes; processed here)."""
    now = _now(now)
    _owner(db, user_id)
    stored = process_avatar(data)
    row = _row(db, user_id, now)
    row.avatar = stored
    # A strictly newer version, even for two uploads within one second (cache-busting value).
    previous = ensure_utc(row.avatar_updated_at) if row.avatar_updated_at is not None else None
    if previous is not None and int(previous.timestamp()) >= int(now.timestamp()):
        now = previous + timedelta(seconds=1)
    row.avatar_updated_at = now
    row.updated_at = now
    db.flush()


def remove_avatar_own(db: Session, *, user_id: uuid.UUID, now: datetime | None = None) -> bool:
    """The owner removes their picture. Returns whether there was one."""
    now = _now(now)
    _owner(db, user_id)
    row = db.get(UserProfile, user_id)
    if row is None or row.avatar is None:
        return False
    row.avatar = None
    row.avatar_updated_at = None
    row.updated_at = now
    db.flush()
    _drop_if_empty(db, row)
    db.flush()
    return True


def admin_remove(db: Session, *, admin: AdminUser, user_id: uuid.UUID, what: str, ip: str | None = None,
                 now: datetime | None = None) -> bool:
    """Moderation: an admin (in scope for the user) removes the user's picture (`what="avatar"`) or status
    (`what="status"`). Audited as `user.avatar_removed` / `user.status_text_removed` (the removed status text is
    not copied into the audit log). Returns whether anything was removed."""
    if what not in ("avatar", "status"):
        raise InvalidInput("unknown profile field")
    if admin is None:
        raise Forbidden("unknown admin")
    now = _now(now)
    if db.get(User, user_id) is None:
        raise NotFound("user not found")
    ensure_admin_can_access_user(db, admin, user_id)
    row = db.get(UserProfile, user_id)
    if row is None:
        return False
    if what == "avatar":
        if row.avatar is None:
            return False
        row.avatar = None
        row.avatar_updated_at = None
        action = "user.avatar_removed"
    else:
        if row.status_text is None:
            return False
        row.status_text = None
        action = "user.status_text_removed"
    row.updated_at = now
    db.flush()
    _drop_if_empty(db, row)
    audit.log(db, actor_type="admin", actor_id=admin.id, action=action, target_type="user", target_id=user_id,
              ip=ip, now=now)
    return True


def delete_profile(db: Session, user_id: uuid.UUID) -> None:
    """Drop status and picture (anonymizing deactivation, merges). No audit entry of its own: the caller's
    action is audited."""
    db.execute(delete(UserProfile).where(UserProfile.user_id == user_id))
