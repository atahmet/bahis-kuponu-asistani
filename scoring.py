"""Bir bahis seçiminin (veya kombine kuponun) yaygın bahis tekniklerine ne kadar
uygun olduğunu 0-100 arası puanlayan sezgisel (heuristic) puanlama sistemi.

Tekli seçim puanı 4 bileşenden oluşur (toplam 100):
  - Value/Edge puanı   (0-40): model olasılığı ima edilen olasılıktan ne kadar
    yüksek (margin arındırılmış). >= %10 edge tam puan.
  - Oran aralığı puanı  (0-25): çok düşük oran (kuponu şişirmeye yaramaz) veya
    çok yüksek oran (düşük olasılık, yüksek varyans) cezalandırılır. 1.50-3.50
    "tatlı nokta" kabul edilir.
  - Kelly/bankroll puanı (0-20): önerilen kesirli Kelly payı pozitif ve makul
    (aşırı agresif değil) ise yüksek puan.
  - Örneklem güveni puanı (0-15): her iki takımın da bu sezon yeterli maç
    (>=5) oynamış olması, erken sezon gürültüsüne karşı güven verir.
"""
from dataclasses import dataclass

from betting_logic import ValuePick

SWEET_SPOT_LOW = 1.50
SWEET_SPOT_HIGH = 3.50

# Maksimum bileşen puanları (score_pick'teki ağırlıklarla eşleşir)
EDGE_SCORE_MAX = 40.0
ODDS_RANGE_SCORE_MAX = 25.0
KELLY_SCORE_MAX = 20.0
SAMPLE_SCORE_MAX = 15.0

# "Oynanabilir" (bütün bileşenlerde güçlü) eşiği: her bileşenin kendi
# maksimumunun en az bu oranına ulaşması gerekir. Tek bir bileşenin (ör.
# sadece yüksek edge) toplamı şişirdiği ama diğer sinyallerin zayıf kaldığı
# durumlar "oynanabilir" sayılmaz — arayüzde mor renkle vurgulanan satırlar
# bu yüzden yalnızca dört bileşenin de aynı anda güçlü olduğu seçimlerdir.
PLAYABLE_MIN_RATIO = 0.6


@dataclass
class ScoreBreakdown:
    edge_score: float
    odds_range_score: float
    kelly_score: float
    sample_score: float
    total: float

    def is_playable(self) -> bool:
        return (
            self.edge_score >= EDGE_SCORE_MAX * PLAYABLE_MIN_RATIO
            and self.odds_range_score >= ODDS_RANGE_SCORE_MAX * PLAYABLE_MIN_RATIO
            and self.kelly_score >= KELLY_SCORE_MAX * PLAYABLE_MIN_RATIO
            and self.sample_score >= SAMPLE_SCORE_MAX * PLAYABLE_MIN_RATIO
        )


def _edge_score(edge: float) -> float:
    return max(0.0, min(edge / 0.10, 1.0)) * 40


def _odds_range_score(odd: float) -> float:
    if SWEET_SPOT_LOW <= odd <= SWEET_SPOT_HIGH:
        return 25.0
    if odd < SWEET_SPOT_LOW:
        # Çok düşük oran: kuponun toplam getirisine katkısı zayıf
        distance = (SWEET_SPOT_LOW - odd) / SWEET_SPOT_LOW
        return max(0.0, 25 * (1 - distance * 2))
    # Çok yüksek oran: gerçekleşme olasılığı düşük, varyans yüksek
    distance = (odd - SWEET_SPOT_HIGH) / SWEET_SPOT_HIGH
    return max(0.0, 25 * (1 - distance))


def _kelly_score(kelly_frac: float, cap: float = 0.05) -> float:
    if kelly_frac <= 0:
        return 0.0
    return min(kelly_frac / cap, 1.0) * 20


def _sample_score(confident: bool) -> float:
    return 15.0 if confident else 5.0


def score_pick(pick: ValuePick, confident: bool) -> ScoreBreakdown:
    edge_score = _edge_score(pick.edge)
    odds_score = _odds_range_score(pick.odd)
    kelly_score = _kelly_score(pick.kelly_fraction)
    sample_score = _sample_score(confident)
    total = edge_score + odds_score + kelly_score + sample_score
    return ScoreBreakdown(
        edge_score=round(edge_score, 1),
        odds_range_score=round(odds_score, 1),
        kelly_score=round(kelly_score, 1),
        sample_score=round(sample_score, 1),
        total=round(total, 1),
    )


@dataclass
class ComboScore:
    avg_leg_score: float
    leg_count_penalty: float
    correlation_penalty: float
    diversification_bonus: float
    total: float


def score_combo(leg_scores: list[float], fixture_ids: list[int], leagues: list[str]) -> ComboScore:
    """Kombine kupon puanı: bacakların ortalama puanından başlar, aşırı bacak
    sayısını ve aynı maçtan birden fazla seçim (korelasyon riski) cezalandırır,
    farklı lig/maçlara yayılmayı ödüllendirir."""
    if not leg_scores:
        return ComboScore(0, 0, 0, 0, 0)

    avg_score = sum(leg_scores) / len(leg_scores)

    n_legs = len(leg_scores)
    if n_legs <= 3:
        leg_count_penalty = 0.0
    elif n_legs <= 5:
        leg_count_penalty = 8.0
    else:
        leg_count_penalty = 8.0 + (n_legs - 5) * 6.0

    duplicate_fixtures = len(fixture_ids) - len(set(fixture_ids))
    correlation_penalty = duplicate_fixtures * 15.0

    unique_leagues = len(set(leagues))
    diversification_bonus = min(unique_leagues - 1, 4) * 3.0 if n_legs > 1 else 0.0

    total = avg_score - leg_count_penalty - correlation_penalty + diversification_bonus
    total = max(0.0, min(total, 100.0))

    return ComboScore(
        avg_leg_score=round(avg_score, 1),
        leg_count_penalty=round(leg_count_penalty, 1),
        correlation_penalty=round(correlation_penalty, 1),
        diversification_bonus=round(diversification_bonus, 1),
        total=round(total, 1),
    )
