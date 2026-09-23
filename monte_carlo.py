"""Kombine kupon risk simülasyonu.

teknik_inceleme raporunun 6. maddesi: kombine kuponun "ortalama bacak puanı"
ile gerçek risk/getiri profili farklı kavramlar; `coupon_builder.combined_ev`
bunu TEK bir sayıyla (nokta tahmini) çözer ama varyansı göstermez.

Bacaklar arası korelasyon: farklı maçlar istatistiksel olarak bağımsız kabul
edilir (bahis şirketlerinin kombine/parlay fiyatlaması da endüstri standardı
olarak aynı varsayımı yapar) — paylaşılan bir gizli faktör (ör. aynı şehir/
hava durumu) için elimizde veri yok, bu yüzden UYDURMA bir korelasyon modeli
kurmak yerine bu varsayım açıkça belgelenir. Monte Carlo'nun asıl kattığı
değer korelasyon değil: (a) analitik çarpım formülünün bir simülasyonla
doğrulanması, (b) "kaç bacağın tuttuğu" dağılımı (near-miss bilgisi — tek bir
kazanma olasılığı bunu göstermez), (c) aynı kupon tekrar tekrar oynansa
bankroll'un ne kadar dalgalanacağı (varyans/ruin riski)."""
import random


def simulate_combo(legs_probs: list[float], n_simulations: int = 20000, seed: int | None = None) -> dict:
    """legs_probs: her bacağın (kalibre edilmiş) kazanma olasılığı. Bacaklar
    bağımsız Bernoulli olarak örneklenir. Döner: simüle edilmiş kazanma
    olasılığı (analitik çarpımla karşılaştırma için) + "tam olarak k bacak
    tuttu" dağılımı."""
    rng = random.Random(seed)
    n_legs = len(legs_probs)
    hits_histogram = [0] * (n_legs + 1)
    for _ in range(n_simulations):
        correct = sum(1 for p in legs_probs if rng.random() < p)
        hits_histogram[correct] += 1

    hits_distribution = [h / n_simulations for h in hits_histogram]
    return {
        "n_simulations": n_simulations,
        "n_legs": n_legs,
        "simulated_win_probability": round(hits_distribution[n_legs], 4),
        "hits_distribution": [round(h, 4) for h in hits_distribution],
    }


def simulate_bankroll(
    win_prob: float, odd: float, stake_fraction: float,
    n_bets: int = 50, n_paths: int = 2000, seed: int | None = None,
) -> dict:
    """Aynı kombinasyon (aynı olasılık/oran/stake payıyla) tekrar tekrar
    (n_bets kez, kazanınca/kaybedince bankroll'un o payı kadar) oynansa ortaya
    çıkacak bankroll çarpanı dağılımını simüle eder — TEK bir kuponun değil,
    'bu tarz kuponları sürekli oynama stratejisinin' varyansını gösterir."""
    rng = random.Random(seed)
    final_multipliers = []
    for _ in range(n_paths):
        bankroll = 1.0
        for _ in range(n_bets):
            if bankroll <= 0:
                break
            stake = bankroll * stake_fraction
            if rng.random() < win_prob:
                bankroll += stake * (odd - 1)
            else:
                bankroll -= stake
        final_multipliers.append(max(bankroll, 0.0))

    final_multipliers.sort()
    n = len(final_multipliers)

    def _pct(p: float) -> float:
        idx = min(n - 1, int(p * n))
        return round(final_multipliers[idx], 3)

    return {
        "n_bets": n_bets,
        "n_paths": n_paths,
        "median_multiplier": _pct(0.5),
        "p5_multiplier": _pct(0.05),
        "p95_multiplier": _pct(0.95),
        "prob_ruin": round(sum(1 for m in final_multipliers if m <= 0.01) / n, 4),
    }
