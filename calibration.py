"""Model olasılıklarının kalibrasyonu.

teknik_inceleme raporunun 9. maddesi: "model %60 diyor" ifadesinin GERÇEKTEN
%60 isabet oranına karşılık gelip gelmediğini DB'deki sonuçlanmış tahminlerden
(db.get_calibration_pairs — her satır: (model_prob, correct)) öğrenip düzeltir.

İki yöntem sunulur:
  - Platt scaling (birincil/varsayılan): calibrated = sigmoid(a*logit(p)+b).
    Yalnızca 2 parametre öğrenir, az veriyle bile kararlıdır.
  - Isotonic regression (PAVA): adım fonksiyonu, çok daha esnektir ama çok
    daha fazla veri gerektirir — azken aşırı uyum (overfitting) riski
    taşıdığından çok daha yüksek bir örnek eşiği (config.
    CALIBRATION_ISOTONIC_MIN_MATCHES) altında hiç kullanılmaz.

Bilinen basitleştirme: kalibrasyon TÜM pazarlar (1X2/Alt-Üst/KG) için TEK bir
eğri ile yapılır — pazar bazlı ayrı kalibrasyon, veri hacmi yeterince
birikince mantıklı bir sonraki adımdır (bkz. PROJE_DURUMU.md)."""
import math

import config


def _logit(p: float, eps: float = 1e-6) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1 + ex)


def fit_platt(pairs: list[tuple[float, int]], iterations: int = 300, lr: float = 0.3) -> tuple[float, float] | None:
    """(a, b) sigmoid(a*logit(p)+b) parametrelerini basit gradyan inişiyle
    (log-loss'u minimize ederek) öğrenir. a=1,b=0 ile başlar — model zaten
    iyi kalibreyse öğrenilen parametreler buna yakın kalır (kimlik dönüşümü)."""
    if len(pairs) < config.CALIBRATION_PLATT_MIN_MATCHES:
        return None
    a, b = 1.0, 0.0
    n = len(pairs)
    xs = [_logit(p) for p, _ in pairs]
    ys = [float(y) for _, y in pairs]
    for _ in range(iterations):
        grad_a = grad_b = 0.0
        for x, y in zip(xs, ys):
            err = _sigmoid(a * x + b) - y
            grad_a += err * x
            grad_b += err
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return round(a, 4), round(b, 4)


def apply_platt(p: float, params: tuple[float, float]) -> float:
    a, b = params
    return _sigmoid(a * _logit(p) + b)


def fit_isotonic(pairs: list[tuple[float, int]]) -> list[tuple[float, float]] | None:
    """PAVA (pool adjacent violators) ile monoton artan bir adım fonksiyonu
    öğrenir. Döner: [(x_sınır, kalibre_y), ...] artan x'e göre sıralı."""
    if len(pairs) < config.CALIBRATION_ISOTONIC_MIN_MATCHES:
        return None
    pairs_sorted = sorted(pairs, key=lambda t: t[0])
    xs = [p for p, _ in pairs_sorted]
    ys = [float(y) for _, y in pairs_sorted]

    blocks = []  # [sum_y, sum_w, end_x]
    for idx in range(len(ys)):
        blocks.append([ys[idx], 1.0, xs[idx]])
        while len(blocks) > 1 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            last = blocks.pop()
            blocks[-1][0] += last[0]
            blocks[-1][1] += last[1]
            blocks[-1][2] = last[2]

    return [(end_x, sum_y / sum_w) for sum_y, sum_w, end_x in blocks]


def apply_isotonic(p: float, curve: list[tuple[float, float]]) -> float:
    for x_boundary, y in curve:
        if p <= x_boundary:
            return y
    return curve[-1][1] if curve else p


def calibrate_probs(probs: dict[str, float], platt_params, isotonic_curve) -> dict[str, float]:
    """Bir pazarın {seçim: model_prob} sözlüğüne, mevcutsa isotonic yoksa
    Platt'i uygular ve toplamı 1'e yeniden normalize eder (her sınıf
    bağımsız kalibre edildiği için normalize olmadan toplam 1'den sapabilir).
    İkisi de yoksa (yetersiz veri) sözlüğü olduğu gibi döndürür."""
    if isotonic_curve is not None:
        calibrated = {k: apply_isotonic(v, isotonic_curve) for k, v in probs.items()}
    elif platt_params is not None:
        calibrated = {k: apply_platt(v, platt_params) for k, v in probs.items()}
    else:
        return probs

    total = sum(calibrated.values())
    if total <= 0:
        return probs
    return {k: v / total for k, v in calibrated.items()}
