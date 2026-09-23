"""Value betting, komisyon (vig) arındırma ve Kelly kriteri hesaplamaları.

Devig yöntemi olarak "power method" kullanılır: akademik literatürde uzun
süre standart kabul edilen Shin's method, güncel ampirik testlerde futbol
1X2 piyasaları için basit yöntemlerden belirgin şekilde üstün çıkmıyor ve
çözümü daha karmaşık (bkz. https://algorithmicsportsbetting.substack.com/p/its-time-to-retire-shins-method).
Power method, favori-uzun-oran (favourite-longshot) önyargısını proportional
yönteme göre daha iyi düzeltir ve tek boyutlu kök bulma ile basitçe çözülür.

Değer (edge), tek bir bahis şirketinin oranı yerine BİRDEN FAZLA şirketin
devig'lenmiş olasılıklarının ortalaması olan "konsensüs" olasılığa göre
hesaplanır (piyasanın kolektif bilgeliği, tek şirketten daha az gürültülü);
gerçek bahis ise bulunan en iyi fiyattan (line shopping) önerilir.
"""
from dataclasses import dataclass


def _power_method_fair_probs(raw_odds: dict) -> dict:
    """Tek bir bahis şirketinin oranlarından, power method ile komisyon
    arındırılmış (toplamı 1'e eşit) 'gerçek' olasılıkları çıkarır.
    raw_odds: {'Home': 2.1, 'Draw': 3.4, 'Away': 3.2} gibi."""
    implied = {k: (1 / v) for k, v in raw_odds.items() if v}
    total = sum(implied.values())
    if total <= 0:
        return {k: 0.0 for k in implied}
    if total <= 1:
        # Komisyon yok/negatif (nadir, underround) - düzeltmeye gerek yok
        return implied

    # sum(implied_i ^ k) = 1 olacak k'yı ikili arama ile bul (k>1 için azalan bir fonksiyon)
    lo, hi = 1.0, 20.0
    for _ in range(80):
        mid = (lo + hi) / 2
        s = sum(v**mid for v in implied.values())
        if s > 1:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2

    fair = {sel: v**k for sel, v in implied.items()}
    fair_total = sum(fair.values())
    if fair_total <= 0:
        return implied
    return {sel: v / fair_total for sel, v in fair.items()}


def remove_margin(raw_odds: dict, method: str = "power") -> dict:
    """Bahis şirketinin komisyonunu (overround) arındırarak 'gerçek' ima edilen
    olasılıkları döndürür. method='power' (varsayılan, önerilen) veya
    'proportional' (basit, eski davranış — yedek olarak tutulur)."""
    if method == "power":
        return _power_method_fair_probs(raw_odds)

    implied = {k: (1 / v) for k, v in raw_odds.items() if v}
    total = sum(implied.values())
    if total == 0:
        return {k: 0.0 for k in raw_odds}
    return {k: v / total for k, v in implied.items()}


def consensus_market(odds_by_bookmaker: dict) -> tuple[dict, dict]:
    """odds_by_bookmaker: {bookmaker_name: {selection: odd}} (bir pazar için
    tüm şirketler). Döner:
      - consensus_fair_prob: {selection: prob} — şirketlerin devig'lenmiş
        olasılıklarının ortalaması (piyasa konsensüsü, edge/value bunun
        üzerinden hesaplanır)
      - best_price: {selection: (odd, bookmaker_name)} — her seçim için
        bulunan en yüksek (en avantajlı) oran ve hangi şirkette olduğu
    """
    fair_prob_samples: dict[str, list[float]] = {}
    best_price: dict[str, tuple[float, str]] = {}

    for bookmaker, raw_odds in odds_by_bookmaker.items():
        if not raw_odds or not all(raw_odds.values()):
            continue
        fair = _power_method_fair_probs(raw_odds)
        for sel, prob in fair.items():
            fair_prob_samples.setdefault(sel, []).append(prob)
        for sel, odd in raw_odds.items():
            if odd and (sel not in best_price or odd > best_price[sel][0]):
                best_price[sel] = (odd, bookmaker)

    consensus = {sel: sum(probs) / len(probs) for sel, probs in fair_prob_samples.items() if probs}
    return consensus, best_price


@dataclass
class ValuePick:
    market: str
    selection: str
    odd: float
    bookmaker: str
    model_prob: float
    implied_prob: float  # konsensüs (piyasa) olasılığı
    edge: float  # model_prob - implied_prob
    expected_value: float  # model_prob * odd - 1 (en iyi fiyatla)
    kelly_fraction: float


def expected_value(model_prob: float, odd: float) -> float:
    return model_prob * odd - 1


def kelly_fraction(model_prob: float, odd: float, fraction: float = 0.25, cap: float = 0.05) -> float:
    """Kesirli Kelly (varsayılan %25 Kelly) ile tavsiye edilen bankroll oranı.
    cap: tek bahiste bankroll'un en fazla bu kadarının riske edilmesi (güvenlik limiti)."""
    b = odd - 1
    if b <= 0:
        return 0.0
    q = 1 - model_prob
    full_kelly = (b * model_prob - q) / b
    if full_kelly <= 0:
        return 0.0
    return min(full_kelly * fraction, cap)


def _apply_tr_simulation(odd: float, bookmaker: str, tr_simulation_factor: float | None) -> tuple[float, str]:
    """Türkiye (İddaa) oranı simülasyonu: gerçek nosyapi entegrasyonu (bkz.
    iddaa_api.py) hesap aktivasyonu beklerken geçici bir yaklaşım olarak,
    kullanıcının bet365/Bilyoner karşılaştırmasından (Norveç-Danimarka
    1.73→1.54, Türkiye-Fransa 2.50→2.29, Hollanda-Almanya 1.57→1.37; oran
    oranları ortalaması ≈0.893) TÜRETİLEN bir çarpanla global en iyi fiyatı
    aşağı çeker. Yalnızca GÖSTERİLEN/OYNANACAK fiyatı (odd) etkiler — edge
    hesabı hâlâ gerçek global konsensüse göre yapılır, değer tespiti bozulmaz.
    Gerçek veri DEĞİLDİR, tahminidir — bookmaker adına '[TR sim.]' eklenir."""
    if tr_simulation_factor is None:
        return odd, bookmaker
    return round(odd * tr_simulation_factor, 2), f"{bookmaker} [TR sim.]"


def evaluate_market(
    market_name: str, odds_by_bookmaker: dict, model_probs: dict, tr_simulation_factor: float | None = None
) -> list[ValuePick]:
    """Bir pazar için (ör. 'Maç Sonucu') tüm bahis şirketlerinden konsensüs
    olasılık ve en iyi fiyatı çıkarıp, model olasılığıyla karşılaştırarak
    ValuePick listesi döndürür (sıralanmamış, ham liste).

    tr_simulation_factor verilirse (0-1 arası, ör. 0.89), gösterilecek
    oran/EV/Kelly bu çarpanla küçültülür (bkz. _apply_tr_simulation) — edge
    hesabı etkilenmez."""
    consensus, best_price = consensus_market(odds_by_bookmaker)
    picks = []
    for selection, model_prob in model_probs.items():
        if selection not in consensus or selection not in best_price:
            continue
        implied = consensus[selection]
        odd, bookmaker = best_price[selection]
        odd, bookmaker = _apply_tr_simulation(odd, bookmaker, tr_simulation_factor)
        edge = model_prob - implied
        picks.append(
            ValuePick(
                market=market_name,
                selection=selection,
                odd=odd,
                bookmaker=bookmaker,
                model_prob=model_prob,
                implied_prob=implied,
                edge=edge,
                expected_value=expected_value(model_prob, odd),
                kelly_fraction=kelly_fraction(model_prob, odd),
            )
        )
    return picks


def evaluate_market_per_bookmaker(
    market_name: str, odds_by_bookmaker: dict, model_probs: dict, tr_simulation_factor: float | None = None
) -> list[ValuePick]:
    """evaluate_market'in aksine, her seçim için yalnızca en iyi fiyatı değil,
    TARANAN TÜM BAHİS ŞİRKETLERİNİN kendi oranlarıyla AYRI AYRI ValuePick
    üretir. Kombine kupon oluşturmak için gereklidir: bir kombine kupon tek
    bir sitede oynanabilir, farklı şirketlerin oranları aynı kuponda
    birleştirilemez — bu yüzden kupon inşası, her şirketin kendi oran
    setiyle ayrı ayrı denenmelidir (bkz. coupon_builder.build_combo).
    Edge/konsensüs hesabı yine tüm şirketlerin ortalamasına (piyasa
    konsensüsü) göre yapılır, yalnızca 'odd' ve 'bookmaker' şirkete özeldir.

    tr_simulation_factor verilirse, her şirketin oranı aynı çarpanla
    küçültülür (bkz. evaluate_market / _apply_tr_simulation)."""
    consensus, _ = consensus_market(odds_by_bookmaker)
    picks = []
    for bookmaker, raw_odds in odds_by_bookmaker.items():
        if not raw_odds or not all(raw_odds.values()):
            continue
        for selection, model_prob in model_probs.items():
            odd = raw_odds.get(selection)
            implied = consensus.get(selection)
            if not odd or implied is None:
                continue
            odd, sim_bookmaker = _apply_tr_simulation(odd, bookmaker, tr_simulation_factor)
            edge = model_prob - implied
            picks.append(
                ValuePick(
                    market=market_name,
                    selection=selection,
                    odd=odd,
                    bookmaker=sim_bookmaker,
                    model_prob=model_prob,
                    implied_prob=implied,
                    edge=edge,
                    expected_value=expected_value(model_prob, odd),
                    kelly_fraction=kelly_fraction(model_prob, odd),
                )
            )
    return picks
