"""nosyapi.com üzerinden İddaa (Türkiye'nin tek yasal spor bahis operatörü)
programını ve oranlarını çeker.

Türkiye'de yasal bahis yalnızca Spor Toto Teşkilatı lisanslı 6 "e-bayi"
üzerinden yapılabilir (iddaa.com, Nesine, Bilyoner, Misli, Oley, Birebin) ve
bunların HEPSİ aynı merkezi İddaa oranını kullanır (yalnızca bazı özel
maçlarda küçük promosyon farkları olabilir). Bu, API-Football'daki
"onlarca global şirket" modelinden farklı olarak burada TEK bir kaynağın
hem 'piyasa fiyatı' hem 'gerçekte oynanabilir fiyat' olması anlamına gelir.

Kredi modeli: nosyapi'de kredi İSTEK BAŞINA değil, DÖNEN SONUÇ SAYISINA göre
düşer (dokümantasyon: "sonuç sayısı kadar kredi düşülür"). Bu yüzden tüm
sorgular tarih/lig filtresiyle daraltılır ve api_football.py ile aynı
disk-cache deseniyle agresif cache'lenir.

DURUM: Takım adı eşleştirmesi (İddaa bültenleri genelde kısaltmalı Türkçe
isim kullanır, ör. 'G.Saray') gerçek API verisi görülmeden %100 güvenilir
kurulamaz — bkz. normalize_team_name / match_team. İlk sürüm bilinen büyük
kulüpler için bir takma ad tablosu içerir; gerçek veriyle test edildikçe
genişletilmesi gerekir."""
import unicodedata
from datetime import datetime, timezone

import requests

import api_football as api  # disk-cache yardımcı fonksiyonlarını (_read_named_cache vb.) yeniden kullanmak için
import config
import db

BASE_URL = "https://www.nosyapi.com/apiv2/service/"


class IddaaApiError(Exception):
    pass


class NosyapiBudgetExceeded(IddaaApiError):
    pass


def _month_key() -> str:
    return "nosyapi_credits_" + datetime.now(timezone.utc).strftime("%Y-%m")


def get_credits_used_this_month() -> int:
    return int(db.get_setting(_month_key(), "0"))


def get_credits_remaining() -> int:
    return max(config.NOSYAPI_MONTHLY_CREDIT_BUDGET - get_credits_used_this_month(), 0)


def has_budget_remaining() -> bool:
    return get_credits_remaining() > 0


def _record_credit_usage(n: int) -> None:
    if n <= 0:
        return
    db.set_setting(_month_key(), str(get_credits_used_this_month() + n))


def _cache_prefix(endpoint: str, params: dict) -> str:
    parts = "_".join(f"{k}-{v}" for k, v in sorted(params.items()) if v is not None)
    return f"iddaa_{endpoint.strip('/')}_{parts}"


def _get(endpoint: str, params: dict, ttl_hours: float | None = None, force: bool = False):
    if not config.NOSYAPI_API_KEY:
        raise IddaaApiError(
            "NOSYAPI_API_KEY .env dosyasında tanımlı değil. Ücretsiz key almak "
            "için README.md'deki 'Türkiye oranları (İddaa)' bölümüne bakın."
        )

    ttl_hours = config.NOSYAPI_CACHE_TTL_HOURS if ttl_hours is None else ttl_hours
    prefix = _cache_prefix(endpoint, params)
    if not force:
        cached = api._read_named_cache(prefix, ttl_hours)
        if cached is not None:
            return cached

    if not has_budget_remaining():
        raise NosyapiBudgetExceeded(
            f"nosyapi aylık kredi bütçesi doldu ({config.NOSYAPI_MONTHLY_CREDIT_BUDGET}). "
            "Yeni istek yapılmadı — mevcut cache'lenmiş veri veya global konsensüs kullanılacak."
        )

    url = f"{BASE_URL}{endpoint}"
    query = {k: v for k, v in params.items() if v is not None}
    query["apiKey"] = config.NOSYAPI_API_KEY
    resp = requests.get(url, params=query, timeout=20)
    if resp.status_code == 401:
        try:
            detail = resp.json().get("messageTR") or resp.json().get("message")
        except ValueError:
            detail = resp.text[:200]
        raise IddaaApiError(
            f"nosyapi: yetkilendirme hatası ({detail}). Hesabınızın bu API ürününe "
            "(nosyapi.com panelinde 'İddaa Oranları ve Programları API') abone/etkin "
            "olduğundan emin olun — genel hesap açmak tek başına yetmeyebilir."
        )
    if resp.status_code == 429:
        raise IddaaApiError("nosyapi: kredi/istek limiti doldu.")
    resp.raise_for_status()
    payload = resp.json()

    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if data is None:
        data = []
    # nosyapi cevabı 'creditUsed' alanını açıkça döndürüyor — varsa o resmi
    # değer kullanılır, yoksa (bazı uç noktalarda olmayabilir) dönen kayıt
    # sayısı kadar kredi harcandığı varsayılır (dokümante edilen davranış).
    credit_used = payload.get("creditUsed") if isinstance(payload, dict) else None
    if credit_used is None:
        credit_used = len(data) if isinstance(data, list) else 1
    _record_credit_usage(credit_used)
    api._write_named_cache(prefix, data)
    return data


def get_bettable_matches(
    date_str: str | None = None, league: str | None = None, sport_type: int = 1, force: bool = False
) -> list:
    """İddaa programındaki oynanabilir maçları döndürür. sport_type=1 futbol.
    date_str (YYYY-MM-DD) ve league verilerek sorgu daraltılmalı — aksi halde
    tüm günün/tüm sporların bülteni dönebilir ve kredi israfına yol açar."""
    return _get(
        "bettable-matches",
        {"type": sport_type, "date": date_str, "league": league},
        force=force,
    )


TURKISH_CHAR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# Bilinen büyük kulüpler için takma ad tablosu — İddaa bültenlerinde sık
# görülen kısaltmalarla API-Football'ın tam isimlerini eşler. Gerçek veriyle
# test edildikçe genişletilmesi gerekir (bkz. modül docstring'i). Anahtarlar
# _basic_normalize'dan geçirilmeden önce yazıldığı için (ör. 'g.saray'),
# aşağıda modül yüklenirken TEAM_ALIASES'e normalize edilerek aktarılır —
# aksi halde çalışma zamanında noktası boşluğa çevrilmiş bir sorgu
# ('g saray') sözlükteki 'g.saray' anahtarıyla asla eşleşmezdi.
_TEAM_ALIASES_RAW = {
    "galatasaray": "galatasaray", "g.saray": "galatasaray", "gs": "galatasaray",
    "fenerbahce": "fenerbahce", "fenerbahçe": "fenerbahce", "fb": "fenerbahce",
    "besiktas": "besiktas", "beşiktaş": "besiktas", "bjk": "besiktas",
    "trabzonspor": "trabzonspor", "trabzon": "trabzonspor", "ts": "trabzonspor",
    "basaksehir": "basaksehir", "başakşehir": "basaksehir", "rams basaksehir": "basaksehir",
    "manchester united": "manchester united", "man utd": "manchester united", "man united": "manchester united",
    "manchester city": "manchester city", "man city": "manchester city",
    "real madrid": "real madrid",
    "barcelona": "barcelona", "fc barcelona": "barcelona",
    "bayern munich": "bayern munich", "bayern münih": "bayern munich",
}


def _basic_normalize(name: str) -> str:
    normalized = name.translate(TURKISH_CHAR_MAP).lower().strip()
    normalized = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.replace(".", " ").replace("-", " ")
    return " ".join(normalized.split())


TEAM_ALIASES = {_basic_normalize(k): v for k, v in _TEAM_ALIASES_RAW.items()}


def normalize_team_name(name: str) -> str:
    """Takım adını karşılaştırılabilir hale getirir: Türkçe karakterleri
    ASCII'ye çevirir, küçük harfe indirir, noktalama sadeleştirir, bilinen
    bir takma ad varsa onu kullanır."""
    if not name:
        return ""
    return TEAM_ALIASES.get(_basic_normalize(name), _basic_normalize(name))


def _name_similarity(a: str, b: str) -> float:
    """Basit, bağımlılıksız bir benzerlik ölçütü: kelime kümeleri arasındaki
    Jaccard benzerliği. Kısaltmalı isimlerde (ör. 'g saray' vs 'galatasaray')
    tam örtüşme olmayabileceğinden, alias tablosu ilk elenen katman,
    burası ikinci (daha gevşek) katmandır."""
    set_a, set_b = set(a.split()), set(b.split())
    if not set_a or not set_b:
        return 0.0
    if a == b:
        return 1.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def match_fixture(home_team: str, away_team: str, date_str: str, iddaa_matches: list, min_similarity: float = 0.5):
    """Bir API-Football fikstürünü (ev/deplasman takım adı + tarih),
    nosyapi'den çekilen İddaa maç listesiyle eşleştirmeye çalışır. Tarih
    HER ZAMAN eşleşmelidir (yanlış-pozitif riskini azaltmak için); takım
    isimleri önce alias tablosuyla, olmazsa kelime-benzerliğiyle karşılaştırılır.
    Eşleşme bulunamazsa None döner — asla belirsiz bir tahminde bulunmaz."""
    home_norm = normalize_team_name(home_team)
    away_norm = normalize_team_name(away_team)

    best_match, best_score = None, 0.0
    for m in iddaa_matches:
        m_date = (m.get("Date") or (m.get("DateTime") or "")[:10])
        if m_date != date_str:
            continue

        m_home = normalize_team_name(m.get("Team1") or (m.get("Teams") or "").split(" - ")[0])
        m_away = normalize_team_name(m.get("Team2") or "")

        home_score = 1.0 if m_home == home_norm else _name_similarity(m_home, home_norm)
        away_score = 1.0 if m_away == away_norm else _name_similarity(m_away, away_norm)
        combined = (home_score + away_score) / 2

        if combined > best_score:
            best_score, best_match = combined, m

    if best_match is not None and best_score >= min_similarity:
        return best_match
    return None


def extract_odds(match: dict) -> dict:
    """Bir İddaa maç kaydından temel oranları çıkarır (Maç Sonucu + Alt/Üst 2.5)."""
    result = {"match_winner": {}, "over_under_2_5": {}}
    if match.get("HomeWin"):
        result["match_winner"] = {
            "Home": _safe_float(match.get("HomeWin")),
            "Draw": _safe_float(match.get("Draw")),
            "Away": _safe_float(match.get("AwayWin")),
        }
    if match.get("Under25") or match.get("Over25"):
        result["over_under_2_5"] = {
            "Over": _safe_float(match.get("Over25")),
            "Under": _safe_float(match.get("Under25")),
        }
    return result


def _safe_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
