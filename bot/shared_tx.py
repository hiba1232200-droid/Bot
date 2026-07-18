"""
جدول مشترك لأرقام العمليات بين الموقع والبوت.

الفكرة: البوت يبقى على قاعدته الخاصة، لكنه يفتح اتصالاً إضافياً بقاعدة
بيانات الموقع لجدول واحد فقط اسمه `shared_tx_codes`. هذا الجدول جديد
ولا يتعارض مع أي جدول موجود في الموقع أو البوت.

النتيجة: أي رقم عملية يُستخدم في الموقع لا يمكن استخدامه في البوت والعكس،
لأن الحجز يتم ذرّياً عبر PRIMARY KEY في قاعدة واحدة.

الإعداد: ضع متغير البيئة SITE_DATABASE_URL = نفس DATABASE_URL الخاص بالموقع.
إذا لم يُضبط، تُعطَّل الميزة بهدوء ويبقى البوت يعمل على حمايته المحلية فقط.
"""
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SITE_DATABASE_URL = os.environ.get("SITE_DATABASE_URL", "").strip()

_init_done = False


def is_enabled() -> bool:
    return bool(SITE_DATABASE_URL)


def _connect():
    import psycopg2
    return psycopg2.connect(SITE_DATABASE_URL, connect_timeout=10)


def _ensure_table(conn) -> None:
    """ينشئ الجدول المشترك إن لم يكن موجوداً (مرة واحدة لكل تشغيل)."""
    global _init_done
    if _init_done:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_tx_codes (
                tx_code   TEXT PRIMARY KEY,
                source    TEXT,
                ref       TEXT,
                amount    DOUBLE PRECISION DEFAULT 0,
                used_at   TIMESTAMP DEFAULT NOW()
            )
        """)
    conn.commit()
    _init_done = True


def _norm(tx_code) -> str:
    return str(tx_code or "").strip()


def claim(tx_code, source: str = "bot", ref: str = "", amount: float = 0.0):
    """
    يحاول حجز رقم العملية في الجدول المشترك.

    يرجع:
      True  = تم الحجز (الرقم جديد، يمكن إضافة الرصيد)
      False = الرقم مستخدم مسبقاً (في الموقع أو البوت) → ارفض
      None  = تعذّر الوصول للقاعدة المشتركة (عطل مؤقت) → القرار للمنادي
    """
    code = _norm(tx_code)
    if not code:
        return False
    if not is_enabled():
        return None  # الميزة غير مفعّلة

    conn = None
    try:
        conn = _connect()
        _ensure_table(conn)
        with conn.cursor() as cur:
            # ON CONFLICT DO NOTHING يجعل الحجز ذرّياً بالكامل
            cur.execute(
                "INSERT INTO shared_tx_codes (tx_code, source, ref, amount, used_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tx_code) DO NOTHING",
                (code, source, str(ref), float(amount or 0),
                 datetime.now(timezone.utc)),
            )
            claimed = cur.rowcount > 0
        conn.commit()
        if not claimed:
            logger.info(f"shared_tx: رقم العملية {code} مستخدم مسبقاً (رُفض)")
        return claimed
    except Exception as e:
        logger.warning(f"shared_tx claim failed: {e}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_settings_cache = {"at": 0.0, "data": {}}
_SETTINGS_TTL = 60  # ثانية — كاش قصير حتى لا نُثقل قاعدة الموقع


def get_site_setting(key: str, default=None):
    """
    يقرأ إعداداً من جدول settings في قاعدة الموقع.
    يُستخدم ليكون الموقع هو المصدر الوحيد لسعر الدولار وهامش الربح.
    يرجع default إذا لم تكن الميزة مفعّلة أو تعذّر الوصول.
    """
    import time
    if not is_enabled():
        return default

    now = time.time()
    if now - _settings_cache["at"] < _SETTINGS_TTL and _settings_cache["data"]:
        return _settings_cache["data"].get(key, default)

    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM settings")
            rows = cur.fetchall()
        data = {str(r[0]): r[1] for r in rows}
        _settings_cache["data"] = data
        _settings_cache["at"] = now
        return data.get(key, default)
    except Exception as e:
        logger.warning(f"get_site_setting failed: {e}")
        # نُبقي الكاش القديم إن وُجد
        if _settings_cache["data"]:
            return _settings_cache["data"].get(key, default)
        return default
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def release(tx_code) -> bool:
    """يحرّر رقم عملية محجوز (يُستخدم عند فشل إضافة الرصيد بعد الحجز)."""
    code = _norm(tx_code)
    if not code or not is_enabled():
        return False
    conn = None
    try:
        conn = _connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM shared_tx_codes WHERE tx_code = %s", (code,))
        conn.commit()
        return True
    except Exception as e:
        logger.warning(f"shared_tx release failed: {e}")
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
