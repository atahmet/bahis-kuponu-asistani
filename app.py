"""Streamlit arayüzü: Avrupa'nın 5 büyük ligi + Süper Lig için value-bet
tabanlı kupon önerileri ve 100 üzerinden bahis tekniği uygunluk puanı."""
from datetime import time as dt_time

import pandas as pd
import streamlit as st

import api_football as api
import auth
import config
import bulletin as bulletin_module
import coupon_builder as cb
import db
import history
import monte_carlo
import telegram_bot

st.set_page_config(page_title="Bahis Kuponu Asistanı", layout="wide")

auth.require_login()
auth.render_logout_button()

PLAYABLE_MARK = "🟣 "

RESPONSIBLE_GAMBLING_NOTICE = (
    "⚠️ Sorumlu bahis uyarısı: Bu araç yalnızca istatistiksel bir tahmin sunar, "
    "kazanç garantisi vermez. Kaybetmeyi göze alabileceğinizden fazla bahis "
    "yapmayın ve bankroll yönetim kurallarına (tek kuponda bankroll'un %5'inden "
    "fazlasını riske atmama gibi) uyun.\n\n"
    "ℹ️ Gösterilen oranlar API-Football'ın sağladığı global bahis şirketlerinden "
    "(Bet365, Pinnacle, 1xBet vb.) gelir — Türkiye'de yasal olarak erişilebilir "
    "değildir, yalnızca değer/edge tahmini için kullanılır. Gerçek bahis "
    "yapmadan önce kendi yasal sitenizdeki (İddaa/Nesine/Misli) güncel oranla "
    "karşılaştırın."
)


def mark_playable_rows(df: pd.DataFrame, playable_flags: list[bool], label_col: str = "Maç") -> pd.DataFrame:
    """'Oynanabilir seviye' (bkz. scoring.ScoreBreakdown.is_playable) olan
    satırları etiket sütununa 🟣 işareti ekleyerek işaretler.

    Not: Streamlit'te pandas Styler (tam satır renklendirme) ile
    column_config (sütun başlığı tooltip'leri) birlikte güvenilir
    çalışmıyor (Streamlit'in bilinen bir kısıtı) — bu yüzden tooltip'leri
    korumak için satır arka planı yerine metin işareti + aşağıdaki mor
    özet kutusu (bkz. render_playable_callout) kullanılıyor."""
    df = df.copy()
    if label_col in df.columns:
        df[label_col] = [
            (PLAYABLE_MARK + val) if flag else val
            for val, flag in zip(df[label_col], playable_flags)
        ]
    return df


def render_playable_callout(rows: list[dict], playable_flags: list[bool], title: str) -> None:
    """Oynanabilir seviyedeki satırları, tablonun üstünde mor bir kutuda
    ayrıca özetler (satır arka planı boyayamadığımız için en görünür
    alternatif — bkz. mark_playable_rows'taki not)."""
    items = [row for row, flag in zip(rows, playable_flags) if flag]
    if not items:
        return
    lines = "".join(
        f"<div>⚽ {r.get('Maç', '').removeprefix(PLAYABLE_MARK)} — {r.get('Pazar', '')}: "
        f"<b>{r.get('Seçim', '')}</b> @ {r.get('En İyi Oran', r.get('Oran', ''))} "
        f"(Puan: {r.get('Puan /100', r.get('Bacak Puanı /100', ''))}/100)</div>"
        for r in items
    )
    st.markdown(
        f"<div style='background-color:#7c3aed;color:white;padding:10px 14px;"
        f"border-radius:8px;margin-bottom:10px;'>"
        f"<b>{title}</b>{lines}</div>",
        unsafe_allow_html=True,
    )


SINGLE_TABLE_COLUMN_CONFIG = {
    "Lig": st.column_config.TextColumn(help="Maçın oynandığı lig."),
    "Maç": st.column_config.TextColumn(help="Ev sahibi - Deplasman takımı."),
    "Tarih": st.column_config.TextColumn(help="Maçın tarihi ve saati (UTC)."),
    "Pazar": st.column_config.TextColumn(
        help="Bahis pazarı: Maç Sonucu (1X2), Alt/Üst 2.5 gol, veya Karşılıklı Gol (KG var/yok)."
    ),
    "Seçim": st.column_config.TextColumn(help="Bu pazarda modelin/edge analizinin önerdiği seçim."),
    "En İyi Oran": st.column_config.NumberColumn(
        help="Taranan tüm bahis şirketleri arasında bu seçim için bulunan en yüksek (en avantajlı) oran."
    ),
    "Şirket": st.column_config.TextColumn(help="En iyi oranın bulunduğu bahis şirketi (global şirketler; TR yasal sitelerle karşılaştırın)."),
    "Model Olasılık %": st.column_config.NumberColumn(
        help="Zaman ağırlıklı Poisson + Dixon-Coles modelinin (uygunsa Elo ile harmanlanmış) bu seçime verdiği kazanma olasılığı."
    ),
    "Konsensüs %": st.column_config.NumberColumn(
        help="Taranan bahis şirketi oranlarından power method ile komisyonu arındırılmış, ortalanmış 'piyasa' olasılığı. Edge bu değere göre hesaplanır."
    ),
    "Edge %": st.column_config.NumberColumn(
        help="Model Olasılık % − Konsensüs %. Pozitif ve büyük değer, modelin piyasadan daha iyimser olduğunu (value/değer bulunduğunu) gösterir."
    ),
    "EV": st.column_config.NumberColumn(
        help="Beklenen Değer (Expected Value) = Model Olasılık × Oran − 1. Pozitifse, teorik olarak birim bahis başına ortalama kazanç beklenir."
    ),
    "Kelly Stake %": st.column_config.NumberColumn(
        help="Kesirli Kelly kriterine (%25 Kelly, bankroll'un en fazla %5'i ile sınırlı) göre bu seçime ayrılması önerilen bankroll yüzdesi."
    ),
    "Güvenilir Örneklem": st.column_config.TextColumn(
        help="Her iki takımın da zaman ağırlıklı 'etkin' maç örnekleminin yeterli olup olmadığı. 'Hayır' ise tahmin daha az güvenilirdir (az/eski veri)."
    ),
    "Puan /100": st.column_config.NumberColumn(
        help="Bahis tekniklerine uygunluk puanı — 3 kavramsal grup: "
        "Değer/EV (Edge 0-40 + Kelly uygunluğu 0-20 — ikisi de aynı temel sinyalden "
        "türediği için birlikte hareket eder, bu kasıtlıdır) + "
        "Risk/Güvenilirlik (Oran aralığı 1.50-3.50 0-25 + örneklem güveni 0-15). "
        "Tek bir bileşenin şişirdiği ama diğerlerinin zayıf kaldığı seçimler "
        "'oynanabilir' sayılmaz (bkz. mor vurgulu satırlar)."
    ),
}

COMBO_TABLE_COLUMN_CONFIG = {
    "Lig": SINGLE_TABLE_COLUMN_CONFIG["Lig"],
    "Maç": SINGLE_TABLE_COLUMN_CONFIG["Maç"],
    "Pazar": SINGLE_TABLE_COLUMN_CONFIG["Pazar"],
    "Seçim": SINGLE_TABLE_COLUMN_CONFIG["Seçim"],
    "En İyi Oran": SINGLE_TABLE_COLUMN_CONFIG["En İyi Oran"],
    "Şirket": SINGLE_TABLE_COLUMN_CONFIG["Şirket"],
    "Model Olasılık %": SINGLE_TABLE_COLUMN_CONFIG["Model Olasılık %"],
    "Edge %": SINGLE_TABLE_COLUMN_CONFIG["Edge %"],
    "Bacak Puanı /100": st.column_config.NumberColumn(
        help="Bu bacağın tekli puanı (bkz. Tekli Öneriler sekmesindeki 'Puan /100' açıklaması)."
    ),
}

def render_saved_analyses_view() -> None:
    """'💾 Kayıtlı Analizler' görünümü: sol menüden, yeni bir analiz
    çalıştırmaya gerek kalmadan her zaman erişilebilir."""
    st.subheader("💾 Kayıtlı Analizler")
    st.caption(
        "'Yeni Analiz' görünümündeki 'Bu analizi isimle kaydet' kutusuyla "
        "kaydettiğiniz analizler burada listelenir. Bir analizi seçip tekrar "
        "ekrana getirebilir, her kupon varyantı için ayrı ayrı Telegram'a "
        "gönderebilirsiniz."
    )

    saved_analyses = db.list_analyses()
    if not saved_analyses:
        st.info("Henüz kaydedilmiş bir analiz yok. Bir analiz çalıştırıp isimle kaydettiğinizde burada görünecek.")
        return

    options = {
        f"{a['name']} — {a['created_at'][:16].replace('T', ' ')} (ID={a['id']})": a["id"]
        for a in saved_analyses
    }
    selected_label = st.selectbox("Analiz seçin", options=list(options.keys()), key="saved_analysis_select")
    selected_analysis_id = options[selected_label]

    col_load, col_delete = st.columns([3, 1])
    load_clicked = col_load.button("📂 Bu Analizi Görüntüle", key="load_saved_analysis")
    delete_clicked = col_delete.button("🗑️ Sil", key="delete_saved_analysis")

    if delete_clicked:
        db.delete_analysis(selected_analysis_id)
        st.session_state.pop("viewing_analysis_id", None)
        st.success("Analiz silindi.")
        st.rerun()

    if load_clicked:
        st.session_state["viewing_analysis_id"] = selected_analysis_id

    if st.session_state.get("viewing_analysis_id") != selected_analysis_id:
        return

    saved_preds = db.get_predictions_for_analysis(selected_analysis_id)
    saved_coupons = db.get_coupons_for_analysis(selected_analysis_id)

    st.write(f"**{len(saved_preds)} tahmin, {len(saved_coupons)} kupon varyantı**")

    if saved_preds:
        saved_pred_rows = []
        saved_pred_playable = []
        for p in saved_preds:
            if p["result_settled"]:
                sonuc = "✅ Tuttu" if p["correct"] == 1 else "❌ Tutmadı"
            else:
                sonuc = "⏳ Bekliyor"
            saved_pred_rows.append(
                {
                    "Lig": p["league"],
                    "Maç": f"{p['home_team']} - {p['away_team']}",
                    "Tarih": (p["match_date"] or "")[:16].replace("T", " "),
                    "Pazar": p["market"],
                    "Seçim": cb.translate_selection(p["selection"]),
                    "En İyi Oran": p["odd"],
                    "Şirket": p["bookmaker"],
                    "Model Olasılık %": round(p["model_prob"] * 100, 1),
                    "Konsensüs %": round(p["implied_prob"] * 100, 1),
                    "Edge %": round(p["edge"] * 100, 1),
                    "Kelly Stake %": round(p["kelly_fraction"] * 100, 2),
                    "Puan /100": p["score_total"],
                    "Sonuç": sonuc,
                }
            )
            saved_pred_playable.append(bool(p["playable"]))

        render_playable_callout(saved_pred_rows, saved_pred_playable, "🟣 Oynanabilir Seviyede Bulunan Seçim(ler)")
        st.dataframe(
            mark_playable_rows(pd.DataFrame(saved_pred_rows), saved_pred_playable),
            use_container_width=True,
            hide_index=True,
            column_config=SINGLE_TABLE_COLUMN_CONFIG,
        )

    if saved_coupons:
        st.write("**Kupon Varyantları**")
        analysis_row = next(a for a in saved_analyses if a["id"] == selected_analysis_id)
        for c in saved_coupons:
            with st.container(border=True):
                combo_bookmaker = c["legs"][0]["bookmaker"] if c["legs"] else "-"
                combo_ev = c["combined_odd"] * c["combined_probability"] - 1
                st.write(
                    f"**{c['n_legs']}'li Kupon** ({combo_bookmaker}) — Puan: {c['combo_score']}/100 — "
                    f"Toplam Oran: {c['combined_odd']} — Birleşik İhtimal: %{c['combined_probability'] * 100:.1f} — "
                    f"Kombine EV: %{combo_ev * 100:+.1f}"
                )
                st.caption("Kullanılan teknikler: " + ", ".join(c["technique_tags"]))
                leg_rows = [
                    {
                        "Maç": f"{leg['home_team']} - {leg['away_team']}",
                        "Pazar": leg["market"],
                        "Seçim": cb.translate_selection(leg["selection"]),
                        "Oran": leg["odd"],
                        "Şirket": leg["bookmaker"],
                    }
                    for leg in c["legs"]
                ]
                st.dataframe(pd.DataFrame(leg_rows), use_container_width=True, hide_index=True)

                if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
                    st.caption("Telegram bağlantısı kurulmadan gönderemezsiniz.")
                elif st.button(f"📤 Bu {c['n_legs']}'li Kuponu Telegram'a Gönder", key=f"send_saved_coupon_{c['id']}"):
                    try:
                        text = bulletin_module.format_coupon_from_rows(
                            c["n_legs"], c["legs"], c["combined_odd"], c["combined_probability"],
                            c["combo_score"], c["stake_fraction"], title=f"🎟️ {analysis_row['name']}",
                        )
                        telegram_bot.send_message(text)
                        st.success("Gönderildi.")
                    except telegram_bot.TelegramError as exc:
                        st.error(str(exc))


def render_history_view() -> None:
    """'📈 Geçmiş Performans' görünümü: sol menüden, yeni bir analiz
    çalıştırmaya gerek kalmadan her zaman erişilebilir (sonuç senkronizasyonu,
    CLV, kalibrasyon raporu ve model kalibrasyonu DB'deki verilere bakar,
    canlı bir analiz gerektirmez)."""
    st.subheader("📈 Geçmiş Tahmin Performansı")
    st.caption(
        "Bu görünüm, önceki analiz çalıştırmalarında kaydedilen tahminlerin "
        "gerçek sonuçlarla karşılaştırmasını gösterir. Puanlama sistemi "
        "tutarlıysa yüksek puanlı seçimlerin isabet oranı, düşük "
        "puanlılardan belirgin şekilde yüksek olmalı."
    )
    col_a, col_b = st.columns(2)
    if col_a.button("Sonuçlanan Maçları Kontrol Et"):
        with st.spinner("Bekleyen maçların sonucu API'den kontrol ediliyor (Elo de güncellenir)..."):
            settled = history.sync_results()
        st.success(f"{settled} tahmin sonuçlandırıldı, Elo reytingleri güncellendi.")
    if col_b.button("Kapanış Oranlarını Güncelle (CLV)"):
        with st.spinner("Henüz maçı oynanmamış tahminler için güncel oranlar kontrol ediliyor..."):
            updated = history.update_closing_odds()
        st.success(f"{updated} tahmin için kapanış oranı kaydedildi.")

    clv = history.clv_summary()
    if clv:
        st.metric(
            "Ortalama CLV (Closing Line Value)",
            f"%{clv['avg_clv']:.2f}",
            help="(bahis yapılan oran / kapanış oranı - 1) * 100. Uzun vadede "
            "pozitif ortalama CLV, kısa vadeli kazanma oranından daha güvenilir "
            "bir edge kanıtıdır — piyasa sizden sonra sizin lehinize hareket "
            f"ediyor demektir. Örnek sayısı: {clv['n']}.",
        )

    report = history.calibration_report()
    overall = report["overall"]
    if not overall or not overall["n"]:
        st.info(
            "Henüz sonuçlanmış tahmin yok. Birkaç hafta analiz çalıştırıp "
            "'Sonuçlanan Maçları Kontrol Et' butonuna bastıkça burada birikecek."
        )
    else:
        col1, col2, col3 = st.columns(3)
        col1.metric("Sonuçlanan Tahmin Sayısı", overall["n"])
        col2.metric("Genel İsabet Oranı", f"%{overall['hit_rate'] * 100:.1f}")
        col3.metric("Brier Skoru (düşük iyi)", f"{overall['brier']:.3f}")

        st.write("**Puan aralığına göre isabet oranı**")
        bucket_rows = [
            {
                "Puan Aralığı": b["bucket"],
                "Örnek Sayısı": b["n"],
                "İsabet Oranı": f"%{b['hit_rate'] * 100:.1f}",
                "Brier Skoru": round(b["brier"], 3),
            }
            for b in report["buckets"]
        ]
        st.dataframe(pd.DataFrame(bucket_rows), use_container_width=True, hide_index=True)

    st.divider()
    st.write("**🎯 Model Kalibrasyonu**")
    current_rho = db.get_model_param("dixon_coles_rho", default=None)
    n_lambda_matches = len(db.get_settled_matches_with_lambdas())
    with db.get_conn() as _conn:
        n_xg_elo_ready = _conn.execute(
            "SELECT COUNT(*) c FROM team_xg_elo WHERE matches_count >= ?", (config.XG_ELO_MIN_MATCHES,)
        ).fetchone()["c"]
    col_r1, col_r2 = st.columns(2)
    col_r1.metric(
        "Dixon-Coles rho",
        f"{current_rho:.4f}" if current_rho is not None else f"{config.DIXON_COLES_RHO} (varsayılan)",
        help="Düşük skorlu sonuçların (0-0, 1-1) olasılığını düzelten parametre. "
        f"En az {config.DIXON_COLES_FIT_MIN_MATCHES} sonuçlanmış maç birikince gerçek "
        "verilerden yeniden kalibre edilebilir; öncesinde sabit varsayılan kullanılır.",
    )
    col_r2.metric("xG-Elo'su hazır takım sayısı", n_xg_elo_ready)
    st.caption(f"Lambda kaydı olan sonuçlanmış maç: {n_lambda_matches} / {config.DIXON_COLES_FIT_MIN_MATCHES} (kalibrasyon eşiği)")
    if st.button("Modeli Şimdi Kalibre Et (Dixon-Coles rho)"):
        with st.spinner("Kalibre ediliyor..."):
            fitted = history.calibrate_dixon_coles_rho()
        if fitted is None:
            st.warning(f"Yetersiz veri — en az {config.DIXON_COLES_FIT_MIN_MATCHES} sonuçlanmış maç gerekiyor (şu an {n_lambda_matches}).")
        else:
            st.success(f"Yeni rho: {fitted:.4f} (DB'ye kaydedildi, bir sonraki analizden itibaren kullanılacak).")

    st.divider()
    st.write("**🎯 Ensemble Ağırlık Kalibrasyonu (Elo / xG-Elo payları)**")
    st.caption(
        "Poisson/Elo/xG-Elo harman ağırlıkları (config.py'deki sabitler) şu ana "
        "kadar hiç veriden doğrulanmamıştı. Bu, CANLI üretilmiş (o anki Elo "
        "reytingiyle hesaplanmış, veri sızıntısız) sonuçlanmış tahminlerin "
        "log-loss'unu (düşük=iyi) farklı ağırlık kombinasyonlarıyla karşılaştırıp "
        f"en iyisini bulur. En az {config.ENSEMBLE_WEIGHT_FIT_MIN_MATCHES} örnek "
        "gerekir (Elo/xG-Elo hazır olduğu dönemde canlı analiz çalıştırıp "
        "sonuçların sonuçlanmasını beklemek gerekir — bu birikim zaman alır)."
    )
    _settled_signals = db.get_settled_match_signals()
    n_signals_2way = len([r for r in _settled_signals if r["elo_home"] is not None and r["xg_elo_home"] is None])
    n_signals_3way = len([r for r in _settled_signals if r["elo_home"] is not None and r["xg_elo_home"] is not None])
    st.caption(
        f"Sinyal kaydı biriken sonuçlanmış maç — yalnızca Elo hazır: {n_signals_2way} / "
        f"{config.ENSEMBLE_WEIGHT_FIT_MIN_MATCHES}, Elo+xG-Elo ikisi de hazır: {n_signals_3way} / "
        f"{config.ENSEMBLE_WEIGHT_FIT_MIN_MATCHES}"
    )
    if st.button("Ensemble Ağırlıklarını Şimdi Kalibre Et"):
        with st.spinner("Log-loss grid search çalışıyor..."):
            ens_result = history.calibrate_ensemble_weights()
        if not ens_result["two_way"] and not ens_result["three_way"]:
            st.warning("Yetersiz veri — hiçbir grup eşiğe ulaşmadı, mevcut config sabitleri kullanılmaya devam ediyor.")
        else:
            if ens_result["two_way"]:
                tw = ens_result["two_way"]
                st.success(
                    f"[Yalnızca Elo] Elo ağırlığı {tw['current_elo_weight']} → {tw['best_elo_weight']} "
                    f"(log-loss {tw['current_log_loss']} → {tw['best_log_loss']}, n={tw['n']})"
                )
            if ens_result["three_way"]:
                th = ens_result["three_way"]
                st.success(
                    f"[Elo+xG-Elo] Elo {th['current_elo_weight']}→{th['best_elo_weight']}, "
                    f"xG-Elo {th['current_xg_elo_weight']}→{th['best_xg_elo_weight']} "
                    f"(log-loss {th['current_log_loss']} → {th['best_log_loss']}, n={th['n']})"
                )
            st.caption("Yeni ağırlıklar DB'ye kaydedildi, bir sonraki analizden itibaren kullanılacak.")

    st.divider()
    st.write("**🎯 Olasılık Kalibrasyonu (Platt / Isotonic)**")
    st.caption(
        "Model %60 dediğinde gerçekten %60 mı tutuyor? Bu, sonuçlanmış "
        "tahminlerin (model_prob, tuttu/tutmadı) çiftlerinden Platt scaling "
        "(az veriyle kararlı, varsayılan) ve isotonic regresyon (çok daha "
        "fazla veri gerektirir) ile bir düzeltme eğrisi öğrenir. Uygulandıktan "
        "sonra edge/EV/Kelly/puan HEP kalibre edilmiş olasılığa göre hesaplanır "
        "— TR simülasyonunun aksine bu, value tespitini kasıtlı olarak etkiler."
    )
    n_calib_pairs = len(db.get_calibration_pairs())
    current_platt_a = db.get_model_param("calibration_platt_a")
    current_platt_b = db.get_model_param("calibration_platt_b")
    st.caption(
        f"Sonuçlanmış tahmin: {n_calib_pairs} (Platt eşiği: {config.CALIBRATION_PLATT_MIN_MATCHES}, "
        f"Isotonic eşiği: {config.CALIBRATION_ISOTONIC_MIN_MATCHES})"
    )
    if current_platt_a is not None:
        st.caption(f"Aktif Platt parametreleri: a={current_platt_a:.3f}, b={current_platt_b:.3f} (a=1,b=0 = kalibrasyonsuz).")
    if st.button("Olasılık Kalibrasyonunu Şimdi Öğren"):
        with st.spinner("Kalibrasyon eğrisi öğreniliyor..."):
            calib_result = history.calibrate_probabilities()
        if calib_result["platt"] is None:
            st.warning(
                f"Yetersiz veri — en az {config.CALIBRATION_PLATT_MIN_MATCHES} sonuçlanmış tahmin "
                f"gerekiyor (şu an {calib_result['n']})."
            )
        else:
            a, b = calib_result["platt"]
            st.success(f"Platt: a={a}, b={b} öğrenildi ve kaydedildi (n={calib_result['n']}).")
            if calib_result["isotonic_fitted"]:
                st.success(f"Isotonic eğrisi de öğrenildi ({calib_result['isotonic_n_breakpoints']} kırılma noktası) — artık Platt yerine bu kullanılacak.")
            else:
                st.caption(f"Isotonic için yeterli veri yok (eşik: {config.CALIBRATION_ISOTONIC_MIN_MATCHES}) — Platt kullanılmaya devam edecek.")


def render_telegram_settings_view() -> None:
    """'⚙️ Telegram Ayarları' görünümü: günlük/haftalık bültenlerin hangi
    saatte, hangi liglerle paylaşılacağını yapılandırır. Ayarlar DB'ye
    (settings tablosu) kaydedilir; scheduler.py ayrı bir süreç olarak
    çalıştığından, bu ayarları her turda (en geç config.SCHEDULER_TICK_SECONDS
    saniyede bir) yeniden okur — zamanlayıcıyı yeniden başlatmaya gerek yok."""
    st.subheader("⚙️ Telegram Bülten Ayarları")
    st.caption(
        "Günlük ve haftalık kuponların hangi saatte, hangi liglerle otomatik "
        "olarak Telegram kanalınıza gönderileceğini buradan yapılandırın. "
        f"Değişiklikler kaydedildikten sonra, `python scheduler.py` çalışan "
        f"zamanlayıcı en geç {config.SCHEDULER_TICK_SECONDS} saniye içinde "
        "yeni ayarları otomatik uygular — zamanlayıcıyı yeniden başlatmanız gerekmez."
    )

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        st.warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID .env dosyasında tanımlı değil. "
            "Bültenler bu ayarlar olmadan gönderilemez — kurulum adımları için README.md'ye bakın."
        )

    s = bulletin_module.get_schedule_settings()

    st.write("**📅 Günlük Bülten**")
    col_d1, col_d2 = st.columns([1, 2])
    daily_enabled = col_d1.checkbox("Aktif", value=s["daily_enabled"], key="daily_enabled_input")
    daily_time = col_d2.time_input(
        "Gönderim saati (bugün, yarının maçları için)",
        value=dt_time(s["daily_hour"], s["daily_minute"]),
        key="daily_time_input",
        disabled=not daily_enabled,
    )

    st.divider()
    st.write("**🗓️ Haftalık Bülten**")
    col_w1, col_w2, col_w3 = st.columns([1, 1, 1])
    weekly_enabled = col_w1.checkbox("Aktif", value=s["weekly_enabled"], key="weekly_enabled_input")
    weekly_weekday = col_w2.selectbox(
        "Gün",
        options=list(bulletin_module.WEEKDAYS_TR.keys()),
        format_func=lambda d: bulletin_module.WEEKDAYS_TR[d],
        index=s["weekly_weekday"],
        key="weekly_weekday_input",
        disabled=not weekly_enabled,
    )
    weekly_time = col_w3.time_input(
        "Gönderim saati",
        value=dt_time(s["weekly_hour"], s["weekly_minute"]),
        key="weekly_time_input",
        disabled=not weekly_enabled,
    )

    st.divider()
    st.write("**⚽ Bültenlerde Taranacak Ligler**")
    bulletin_leagues = st.multiselect(
        "Ligler",
        options=list(config.LEAGUES.keys()),
        default=s["leagues"],
        key="bulletin_leagues_input",
        help="Günlük ve haftalık bültenler yalnızca burada seçili liglerdeki maçları tarar.",
    )

    st.divider()
    if st.button("💾 Ayarları Kaydet", type="primary"):
        if not bulletin_leagues:
            st.warning("En az bir lig seçmelisiniz.")
        else:
            bulletin_module.save_schedule_settings(
                daily_enabled=daily_enabled,
                daily_hour=daily_time.hour,
                daily_minute=daily_time.minute,
                weekly_enabled=weekly_enabled,
                weekly_weekday=weekly_weekday,
                weekly_hour=weekly_time.hour,
                weekly_minute=weekly_time.minute,
                leagues=bulletin_leagues,
            )
            st.success("Ayarlar kaydedildi.")
            st.rerun()

    st.divider()
    st.caption(
        f"**Şu anki etkin ayarlar:** Günlük — {'Açık' if s['daily_enabled'] else 'Kapalı'}, "
        f"{s['daily_hour']:02d}:{s['daily_minute']:02d} | Haftalık — "
        f"{'Açık' if s['weekly_enabled'] else 'Kapalı'}, "
        f"{bulletin_module.WEEKDAYS_TR[s['weekly_weekday']]} {s['weekly_hour']:02d}:{s['weekly_minute']:02d} | "
        f"Ligler: {', '.join(s['leagues'])}"
    )


st.title("⚽ Bahis Kuponu Asistanı")
st.caption(
    "Zaman ağırlıklı Poisson + Dixon-Coles modeli, Elo ensemble, çoklu bahis "
    "şirketi konsensüsü (power method devig) ve Kelly kriteri ile maç analizi. "
    "Bu araç istatistiksel bir tahmin sunar, kazanç garantisi vermez."
)

VIEW_LABELS = {
    "new": "🆕 Yeni Analiz",
    "saved": "💾 Kayıtlı Analizler",
    "history": "📈 Geçmiş Performans",
    "telegram_settings": "⚙️ Telegram Ayarları",
}

with st.sidebar:
    view_mode = st.radio(
        "Görünüm",
        options=list(VIEW_LABELS.keys()),
        format_func=lambda v: VIEW_LABELS[v],
        key="view_mode",
    )
    st.divider()

if view_mode == "saved":
    with st.sidebar:
        st.caption("Kayıtlı analizlerinizi ana ekranda görüntüleyip Telegram'a gönderebilirsiniz.")
    render_saved_analyses_view()
    st.divider()
    st.caption(RESPONSIBLE_GAMBLING_NOTICE)
    st.stop()

if view_mode == "history":
    with st.sidebar:
        st.caption("Geçmiş performansı ve model kalibrasyonunu ana ekranda görüntüleyebilirsiniz.")
    render_history_view()
    st.divider()
    st.caption(RESPONSIBLE_GAMBLING_NOTICE)
    st.stop()

if view_mode == "telegram_settings":
    with st.sidebar:
        st.caption("Bülten gönderim saatlerini ve liglerini ana ekranda yapılandırabilirsiniz.")
    render_telegram_settings_view()
    st.divider()
    st.caption(RESPONSIBLE_GAMBLING_NOTICE)
    st.stop()

with st.sidebar:
    st.header("Plan")
    plan_choice = st.radio(
        "API-Football planı",
        options=["free", "pro"],
        format_func=lambda p: "Ücretsiz (100 istek/gün)" if p == "free" else "Pro (7500 istek/gün, tüm ligler/endpoint)",
        index=1,
        help="Pro seçilince kota koruma amaçlı sınırlar (tarama derinliği, cache "
        "süreleri, eksik oyuncu analizinin varsayılan kapalı olması) kalkar.",
    )
    config.set_plan(plan_choice)
    limits = config.limits()
    is_pro = plan_choice == "pro"

    st.divider()
    st.header("Veri Modu")
    fixture_mode = st.radio(
        "Kaynak",
        options=["upcoming", "backtest"],
        format_func=lambda m: "Yaklaşan maçlar (canlı)" if m == "upcoming" else "Geçmiş sezon testi (backtest)",
        index=0 if is_pro else 1,
        help="'Yaklaşan maçlar' güncel sezona erişim gerektirir — ücretsiz "
        "API-Football planı bunu desteklemez (sadece 2022-2024 sezonlarına "
        "izin verir). 'Backtest', tamamlanmış geçmiş maçları çeker; sonuç "
        "zaten bilindiğinden model isabeti anında görülür — ücretsiz planda "
        "çalışır, kod/model doğrulaması için idealdir.",
    )
    is_backtest = fixture_mode == "backtest"

    if is_backtest:
        backtest_season = st.number_input(
            "Backtest sezonu",
            min_value=2015, max_value=2026,
            value=config.FREE_PLAN_MAX_SEASON if not is_pro else config.FREE_PLAN_MAX_SEASON,
            step=1,
            help=f"Ücretsiz plan {config.FREE_PLAN_MIN_SEASON}-{config.FREE_PLAN_MAX_SEASON} "
            "arası sezonlara izin veriyor.",
        )
        config.set_season(int(backtest_season))
        if not is_pro and not (config.FREE_PLAN_MIN_SEASON <= backtest_season <= config.FREE_PLAN_MAX_SEASON):
            st.warning(
                f"Ücretsiz plan muhtemelen yalnızca {config.FREE_PLAN_MIN_SEASON}-"
                f"{config.FREE_PLAN_MAX_SEASON} sezonlarına izin veriyor; API bu sezonu reddedebilir."
            )
    else:
        config.set_season(config.current_season())
        if not is_pro:
            st.warning(
                "Ücretsiz plan güncel sezona erişemez — bu modda muhtemelen "
                "'fikstür alınamadı' hatası alırsınız. Soldan 'Backtest' modunu "
                "seçin veya Pro plana geçin."
            )

    st.divider()
    st.header("Ayarlar")
    selected_leagues = st.multiselect(
        "Ligler", options=list(config.LEAGUES.keys()), default=list(config.LEAGUES.keys())
    )
    next_n = st.slider(
        "Lig başına kaç maç taransın?" if is_backtest else "Lig başına kaç yaklaşan maç taransın?",
        3, limits["max_next_n"], min(5, limits["max_next_n"]),
    )
    min_edge_pct = st.slider("Minimum value edge (%)", 0, 10, 2)
    n_legs = st.slider("Kombine kupon bacak sayısı", 2, 6, 4)
    bankroll = st.number_input("Bankroll (₺)", min_value=100, value=1000, step=100)
    force_refresh = st.checkbox(
        "Cache'i yoksay, API'den yeniden çek",
        value=is_pro,
        help="Pro planda kota sorun olmadığı için varsayılan olarak açıktır; her "
        "çalıştırma en güncel veriyi çeker." if is_pro else None,
    )
    use_availability = st.checkbox(
        "Eksik oyuncu (sakat/cezalı) etkisini modele dahil et",
        value=limits["availability_default_on"] and not is_backtest,
        disabled=is_backtest,
        help=(
            "Backtest modunda anlamsız (injuries API'si güncel durumu döner, geçmişi değil) — otomatik atlanır."
            if is_backtest
            else "Pro planda ek istek maliyeti sorun olmadığından varsayılan açıktır."
            if is_pro
            else "Her maç için ek /injuries ve takım başına /players çağrısı gerektirir; "
            "ücretsiz günlük kotayı (100 istek) hızla tüketebilir. Az sayıda maçla test edin."
        ),
    )
    use_elo = st.checkbox(
        "Elo reytingini modele harmanla (ensemble)",
        value=not is_backtest,
        disabled=is_backtest,
        help=(
            "Backtest modunda anlamsız (Elo yalnızca 'şu anki' reytingi tutar, "
            "geçmiş bir tarih için doğru anlık görüntü değildir — veri sızıntısını "
            "önlemek için otomatik atlanır)."
            if is_backtest
            else "Her iki takımın da DB'de en az "
            f"{config.ELO_MIN_MATCHES} sonuçlanmış maçı varsa devreye girer "
            "(soğuk başlangıçta katkısı sıfırdır, sistem birkaç hafta kullanıldıkça "
            "kendiliğinden aktifleşir)."
        ),
    )
    st.divider()
    simulate_tr_odds = st.checkbox(
        "🇹🇷 Türkiye Oranlarını Simüle Et",
        value=False,
        help="Gerçek İddaa entegrasyonu (nosyapi.com) hesap aktivasyonu "
        "bekliyor — bu, kullanıcının gerçek bet365/Bilyoner karşılaştırmasından "
        "türetilen bir TAHMİNDİR, gerçek veri değildir. Yalnızca gösterilen "
        "oran/EV/Kelly'yi küçültür; value tespiti (edge) hâlâ gerçek global "
        "konsensüse göre yapılır.",
    )
    tr_simulation_factor = None
    if simulate_tr_odds:
        tr_reduction_pct = st.slider(
            "Simüle edilen azaltma (%)",
            min_value=0, max_value=30,
            value=round((1 - config.TR_ODDS_SIMULATION_DEFAULT_FACTOR) * 100),
            help="Varsayılan ~%11, 3 gerçek maç karşılaştırmasından (Norveç-Danimarka, "
            "Türkiye-Fransa, Hollanda-Almanya) hesaplanan ortalama fark. Daha fazla "
            "gerçek karşılaştırma yaptıkça bu değeri güncelleyebilirsiniz.",
        )
        tr_simulation_factor = 1 - (tr_reduction_pct / 100)
        st.caption(f"Örnek: gerçek oran 2.00 ise simüle TR oranı ≈ {round(2.00 * tr_simulation_factor, 2)}")

    log_to_history = st.checkbox("Bu analizi geçmişe kaydet (DB)", value=True)
    fetch_clicked = st.button("Verileri Getir ve Analiz Et", type="primary")

    st.divider()
    st.caption(f"Sezon: {config.SEASON} — Plan: {'Pro' if is_pro else 'Ücretsiz'} "
               f"({limits['daily_request_cap']} istek/gün)")
    if api.BUDGET.calls_made:
        st.caption(f"Bu oturumda yapılan gerçek API isteği: {api.BUDGET.calls_made}")
        if api.BUDGET.last_remaining is not None:
            st.caption(f"Kalan günlük kota (API başlığından): {api.BUDGET.last_remaining}")

if not config.API_KEY:
    st.error(
        "API_FOOTBALL_KEY bulunamadı. Lütfen proje klasöründe bir `.env` dosyası "
        "oluşturup içine `API_FOOTBALL_KEY=senin_key_in` satırını ekleyin. "
        "Ücretsiz key almak için README.md dosyasındaki adımları izleyin."
    )
    st.stop()

if fetch_clicked:
    if not selected_leagues:
        st.warning("En az bir lig seçmelisiniz.")
    else:
        with st.spinner("Fikstürler, takım istatistikleri ve oranlar çekiliyor..."):
            analyzed, warnings = cb.fetch_and_analyze(
                selected_leagues, next_n, force=force_refresh,
                use_availability=use_availability, use_elo=use_elo,
                fixture_mode=fixture_mode,
            )
            recommendations = cb.build_recommendations(
                analyzed, min_edge=min_edge_pct / 100, tr_simulation_factor=tr_simulation_factor
            )
            recs_per_bookmaker = cb.build_recommendations_per_bookmaker(
                analyzed, min_edge=min_edge_pct / 100, tr_simulation_factor=tr_simulation_factor
            )
            combo_for_log = cb.build_combo(recs_per_bookmaker, n_legs=n_legs) if recs_per_bookmaker else None
        st.session_state["analyzed"] = analyzed
        st.session_state["warnings"] = warnings
        st.session_state["recommendations"] = recommendations
        st.session_state["recs_per_bookmaker"] = recs_per_bookmaker
        st.session_state["simulate_tr_odds"] = simulate_tr_odds
        st.session_state["is_backtest"] = is_backtest
        st.session_state["fixture_mode"] = fixture_mode
        st.session_state["selected_leagues"] = selected_leagues
        st.session_state["id_map"] = None
        st.session_state["saved_analysis_id"] = None

        if log_to_history and recommendations:
            id_map = history.log_predictions(recommendations)
            history.log_coupon(combo_for_log, id_map) if combo_for_log else None
            st.session_state["id_map"] = id_map
            st.toast(f"{len(recommendations)} tahmin geçmişe kaydedildi.")

            if is_backtest:
                with st.spinner("Backtest: bilinen sonuçlar yerleştiriliyor, isabet hesaplanıyor..."):
                    settled = history.settle_from_analyzed(analyzed)
                st.toast(f"{settled} tahmin anında sonuçlandırıldı (backtest).")

analyzed = st.session_state.get("analyzed")
warnings = st.session_state.get("warnings", [])
recommendations = st.session_state.get("recommendations")
recs_per_bookmaker = st.session_state.get("recs_per_bookmaker")

if warnings:
    with st.expander(f"⚠️ {len(warnings)} uyarı", expanded=False):
        for w in warnings:
            st.write("-", w)

if not analyzed:
    st.info("Analize başlamak için sol menüden ligleri seçip 'Verileri Getir ve Analiz Et' butonuna basın.")
    st.stop()

st.success(f"{len(analyzed)} maç analiz edildi, {len(recommendations)} value-bet fırsatı bulundu.")

if st.session_state.get("simulate_tr_odds"):
    st.warning(
        "🇹🇷 **Türkiye oranı simülasyonu AKTİF.** Gösterilen oranlar/EV/Kelly gerçek "
        "İddaa verisi DEĞİL — kullanıcının 3 gerçek maç karşılaştırmasından türetilen "
        "yaklaşık bir tahmindir (şirket adının yanında '[TR sim.]' ile işaretlidir). "
        "Value tespiti (edge) gerçek global konsensüse göre yapıldığından etkilenmez, "
        "ama gerçek bahis öncesi kendi yasal sitenizdeki güncel oranı mutlaka kontrol edin."
    )

if st.session_state.get("is_backtest") and recommendations:
    known = [
        (rec, db.is_correct(rec.pick.market, rec.pick.selection, rec.fixture.actual_home_goals, rec.fixture.actual_away_goals))
        for rec in recommendations
        if rec.fixture.actual_home_goals is not None
    ]
    known = [(rec, c) for rec, c in known if c is not None]
    if known:
        hits = sum(1 for _, c in known if c)
        st.info(
            f"🎯 **Backtest anlık sonucu (bu çalıştırma):** {hits}/{len(known)} tahmin tuttu "
            f"(%{hits / len(known) * 100:.0f}). Sezon: {config.SEASON}. "
            "Küçük örneklemde bu oran tek başına anlamlı değildir — sol menüden "
            "'📈 Geçmiş Performans' görünümündeki puan-aralığı kırılımı ve Brier skoru daha güvenilirdir."
        )

if recommendations:
    if st.session_state.get("saved_analysis_id"):
        st.caption(f"✅ Bu analiz kaydedildi (analiz ID={st.session_state['saved_analysis_id']}). "
                   "Sol menüden '💾 Kayıtlı Analizler' görünümünü seçerek tekrar görüntüleyebilir veya Telegram'a gönderebilirsiniz.")
    else:
        with st.expander("💾 Bu analizi isimle kaydet (daha sonra geri çağırıp Telegram'a gönderebilmek için)"):
            analysis_name = st.text_input(
                "Analiz adı", placeholder="ör. Hafta Sonu Süper Lig Analizi", key="analysis_name_input"
            )
            if st.button("Kaydet", key="save_analysis_button"):
                if not analysis_name.strip():
                    st.warning("Lütfen bir isim girin.")
                else:
                    with st.spinner("Kaydediliyor (1-7 bacaklı tüm kupon varyantları üretiliyor)..."):
                        saved_id = history.save_named_analysis(
                            analysis_name.strip(),
                            st.session_state.get("selected_leagues", selected_leagues),
                            st.session_state.get("fixture_mode", fixture_mode),
                            recommendations,
                            recs_per_bookmaker=recs_per_bookmaker,
                            id_map=st.session_state.get("id_map"),
                        )
                    st.session_state["saved_analysis_id"] = saved_id
                    st.success(f"'{analysis_name.strip()}' olarak kaydedildi (ID={saved_id}). "
                               "Sol menüden '💾 Kayıtlı Analizler' görünümüyle erişebilirsiniz.")
                    st.rerun()

tab_single, tab_combo, tab_bulletin = st.tabs(
    ["📋 Tekli Öneriler", "🎟️ Kombine Kupon", "🤖 Telegram Bülten"]
)

with tab_single:
    if not recommendations:
        st.warning("Seçilen kriterlere uygun value-bet bulunamadı. Minimum edge eşiğini düşürmeyi deneyin.")
    else:
        rows = []
        playable_flags = []
        for rec in recommendations:
            fx = rec.fixture
            p = rec.pick
            rows.append(
                {
                    "Lig": fx.league,
                    "Maç": f"{fx.home_team} - {fx.away_team}",
                    "Tarih": fx.date[:16].replace("T", " "),
                    "Pazar": p.market,
                    "Seçim": cb.translate_selection(p.selection),
                    "En İyi Oran": p.odd,
                    "Şirket": p.bookmaker,
                    "Model Olasılık %": round(p.model_prob * 100, 1),
                    "Konsensüs %": round(p.implied_prob * 100, 1),
                    "Edge %": round(p.edge * 100, 1),
                    "EV": round(p.expected_value, 3),
                    "Kelly Stake %": round(p.kelly_fraction * 100, 2),
                    "Güvenilir Örneklem": "Evet" if fx.probs.confident else "Hayır (düşük etkin veri)",
                    "Puan /100": rec.score.total,
                }
            )
            playable_flags.append(rec.score.is_playable())

        render_playable_callout(rows, playable_flags, "🟣 Oynanabilir Seviyede Bulunan Seçim(ler)")

        df = mark_playable_rows(pd.DataFrame(rows), playable_flags)
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config=SINGLE_TABLE_COLUMN_CONFIG,
        )

        if any(playable_flags):
            st.caption("🟣 işaretli satırlar: edge, oran aralığı, Kelly uygunluğu ve örneklem güveninin **hepsinin aynı anda** güçlü olduğu, 'oynanabilir seviye' seçimlerdir.")

        st.caption(
            "Konsensüs %: birden fazla bahis şirketinin power-method ile "
            "komisyonu arındırılmış olasılıklarının ortalaması (edge buna göre "
            "hesaplanır). En İyi Oran/Şirket: aynı seçim için taranan şirketler "
            "arasındaki en avantajlı fiyat (line shopping). "
            "Puan bileşenleri: Edge (0-40) + Oran aralığı 1.50-3.50 (0-25) + "
            "Kelly/bankroll uygunluğu (0-20) + örneklem güveni (0-15). "
            "Sütun başlıklarının üzerine gelerek her değerin ne ifade ettiğini görebilirsiniz."
        )

with tab_combo:
    if not recs_per_bookmaker:
        st.warning("Kombine kupon oluşturmak için önce value-bet fırsatı bulunmalı.")
    else:
        combo = cb.build_combo(recs_per_bookmaker, n_legs=n_legs)
        if combo is None:
            st.warning("Kombine kupon için yeterli sayıda farklı maç bulunamadı.")
        elif len({leg.pick.bookmaker for leg in combo.legs}) > 1:
            st.error("İç hata: kupon birden fazla şirket içeriyor — lütfen bildirin.")
        else:
            leg_rows = []
            leg_playable_flags = []
            for leg in combo.legs:
                fx = leg.fixture
                p = leg.pick
                leg_rows.append(
                    {
                        "Lig": fx.league,
                        "Maç": f"{fx.home_team} - {fx.away_team}",
                        "Pazar": p.market,
                        "Seçim": cb.translate_selection(p.selection),
                        "En İyi Oran": p.odd,
                        "Şirket": p.bookmaker,
                        "Model Olasılık %": round(p.model_prob * 100, 1),
                        "Edge %": round(p.edge * 100, 1),
                        "Bacak Puanı /100": leg.score.total,
                    }
                )
                leg_playable_flags.append(leg.score.is_playable())

            render_playable_callout(leg_rows, leg_playable_flags, "🟣 Oynanabilir Seviyede Bacak(lar)")

            st.dataframe(
                mark_playable_rows(pd.DataFrame(leg_rows), leg_playable_flags),
                use_container_width=True,
                hide_index=True,
                column_config=COMBO_TABLE_COLUMN_CONFIG,
            )
            if any(leg_playable_flags):
                st.caption("🟣 işaretli satırlar: 'oynanabilir seviye' bacaklar (bkz. Tekli Öneriler sekmesi açıklaması).")

            st.caption(f"✅ Tüm bacaklar tek bir şirkette ({combo.legs[0].pick.bookmaker}) mevcut — bu kupon o şirkette tek slip olarak oynanabilir.")

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Toplam Oran", combo.combined_odd)
            col2.metric("Birleşik Kazanma İhtimali", f"%{combo.combined_probability * 100:.2f}")
            col3.metric(
                "Kombine EV", f"%{combo.combined_ev * 100:+.1f}",
                help="Kuponun kendi beklenen değeri: (birleşik ihtimal × toplam oran) - 1. "
                "Kupon puanı bacakların ORTALAMASINI ölçer, bu ise kuponun BÜTÜN OLARAK "
                "beklenen getirisidir — ortalama bacak puanı yüksek olsa bile çok bacaklı "
                "kuponlarda ihtimaller çarpımsal küçüldüğü için kombine EV negatif olabilir.",
            )
            col4.metric("Kupon Puanı /100", combo.combo_score.total)

            stake_amount = combo.recommended_stake_fraction * bankroll
            st.metric(
                "Önerilen Bahis Miktarı (kesirli Kelly, bankroll'un en fazla %5'i)",
                f"₺{stake_amount:.2f}",
            )

            with st.expander("Kupon puanı nasıl hesaplandı?"):
                cs = combo.combo_score
                st.write(f"- Bacakların ortalama puanı: {cs.avg_leg_score}")
                st.write(f"- Bacak sayısı cezası: -{cs.leg_count_penalty}")
                st.write(f"- Aynı maçtan tekrar seçim cezası: -{cs.correlation_penalty}")
                st.write(f"- Farklı lig/maça yayılma bonusu: +{cs.diversification_bonus}")
                st.write(f"- **Toplam: {cs.total} / 100**")

            with st.expander("🎲 Monte Carlo Risk Simülasyonu"):
                st.caption(
                    "Bacaklar farklı maçlar olduğu için istatistiksel olarak BAĞIMSIZ "
                    "kabul edilir (bahis şirketlerinin kombine/parlay fiyatlaması da aynı "
                    "varsayımı yapar; paylaşılan bir gizli korelasyon faktörü için elimizde "
                    "veri yok). Simülasyonun asıl kattığı: analitik formülün doğrulanması, "
                    "'kaç bacak tuttu' dağılımı ve bu kuponu tekrar tekrar oynama stratejisinin "
                    "bankroll varyansı."
                )
                mc = monte_carlo.simulate_combo([leg.pick.model_prob for leg in combo.legs])
                mc_col1, mc_col2 = st.columns(2)
                mc_col1.metric(
                    "Simüle Edilmiş Kazanma İhtimali",
                    f"%{mc['simulated_win_probability'] * 100:.2f}",
                    help=f"{mc['n_simulations']} simülasyon — analitik %{combo.combined_probability * 100:.2f} ile "
                    "yakın çıkması formülün doğru çalıştığını doğrular.",
                )
                hits_rows = [
                    {"Tutan Bacak Sayısı": k, "Olasılık": f"%{p * 100:.1f}"}
                    for k, p in enumerate(mc["hits_distribution"])
                ]
                mc_col2.dataframe(pd.DataFrame(hits_rows), use_container_width=True, hide_index=True)

                bankroll_sim = monte_carlo.simulate_bankroll(
                    win_prob=combo.combined_probability, odd=combo.combined_odd,
                    stake_fraction=combo.recommended_stake_fraction,
                )
                st.write(
                    f"**Bu kuponu art arda {bankroll_sim['n_bets']} kez oynasanız** "
                    f"(her seferinde bankroll'un aynı payıyla, kazanınca ekler kaybedince "
                    f"düşer) — {bankroll_sim['n_paths']} simüle yol:"
                )
                bk_col1, bk_col2, bk_col3, bk_col4 = st.columns(4)
                bk_col1.metric("Medyan Sonuç", f"{bankroll_sim['median_multiplier']:.2f}x")
                bk_col2.metric("Kötü Senaryo (%5)", f"{bankroll_sim['p5_multiplier']:.2f}x")
                bk_col3.metric("İyi Senaryo (%95)", f"{bankroll_sim['p95_multiplier']:.2f}x")
                bk_col4.metric("İflas İhtimali", f"%{bankroll_sim['prob_ruin'] * 100:.1f}")

            st.divider()
            if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
                st.caption("Telegram bağlantısı kurulmadan bu kuponu kanala gönderemezsiniz (bkz. README).")
            elif st.button("📤 Bu Kuponu Telegram'a Gönder", key="send_live_combo"):
                try:
                    text = bulletin_module.format_combo(n_legs, combo)
                    telegram_bot.send_message(f"🎟️ MANUEL KUPON PAYLAŞIMI\n{text}")
                    st.success("Kupon Telegram'a gönderildi.")
                except telegram_bot.TelegramError as exc:
                    st.error(str(exc))

with tab_bulletin:
    st.subheader("Telegram Bülten Sistemi")
    _schedule_settings = bulletin_module.get_schedule_settings()
    st.caption(
        f"Bu araç, günlük {_schedule_settings['daily_hour']:02d}:{_schedule_settings['daily_minute']:02d}'te "
        f"yarının maçlarından günlük bülten, {bulletin_module.WEEKDAYS_TR[_schedule_settings['weekly_weekday']]} "
        f"{_schedule_settings['weekly_hour']:02d}:{_schedule_settings['weekly_minute']:02d}'te haftanın "
        "maçlarından haftalık bülten üretip Telegram kanalınıza gönderen bir zamanlayıcıyla "
        "(`python scheduler.py`) birlikte tasarlandı — saatleri/ligleri sol menüden "
        "'⚙️ Telegram Ayarları' ile değiştirebilirsiniz. Buradan, zamanlayıcıyı beklemeden "
        "şimdi elle tetikleyip test edebilirsiniz."
    )

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        st.warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID .env dosyasında tanımlı değil. "
            "Kurulum adımları için README.md'ye bakın."
        )
    else:
        st.success("Telegram bot ayarları bulundu.")
        if st.button("🔔 Bağlantıyı Test Et (kanala 'bot çalışıyor' mesajı gönder)"):
            try:
                telegram_bot.send_test_message()
                st.success("Test mesajı gönderildi, kanalınızı kontrol edin.")
            except telegram_bot.TelegramError as exc:
                st.error(str(exc))

        st.divider()
        col_d, col_w = st.columns(2)

        with col_d:
            st.write("**Günlük Bülten**")
            daily_target = st.date_input(
                "Hedef tarih (bülten bu günün maçlarını kapsar)",
                value=None,
                help="Boş bırakılırsa yarın kullanılır.",
                key="daily_bulletin_date",
            )
            force_daily = st.checkbox("Zaten gönderilmiş olsa bile yeniden oluştur", key="force_daily")
            if st.button("📅 Günlük Bülteni Şimdi Oluştur ve Gönder"):
                target_str = daily_target.strftime("%Y-%m-%d") if daily_target else None
                with st.spinner("Analiz ediliyor ve Telegram'a gönderiliyor..."):
                    try:
                        bulletin_id = bulletin_module.run_daily_bulletin(
                            _schedule_settings["leagues"], target_str, force_rebuild=force_daily
                        )
                    except Exception as exc:
                        st.error(f"Hata: {exc}")
                        bulletin_id = "error"
                if bulletin_id == "error":
                    pass
                elif bulletin_id is None:
                    st.info("Bu tarih için zaten bülten var (veya o gün seçili liglerde maç yok).")
                else:
                    st.success(f"Bülten gönderildi (bulletin_id={bulletin_id}).")

        with col_w:
            st.write("**Haftalık Bülten**")
            weekly_start = st.date_input(
                "Hafta başlangıcı (7 günlük pencere buradan başlar)",
                value=None,
                help="Boş bırakılırsa bugün kullanılır.",
                key="weekly_bulletin_date",
            )
            force_weekly = st.checkbox("Zaten gönderilmiş olsa bile yeniden oluştur", key="force_weekly")
            if st.button("🗓️ Haftalık Bülteni Şimdi Oluştur ve Gönder"):
                week_str = weekly_start.strftime("%Y-%m-%d") if weekly_start else None
                with st.spinner("Analiz ediliyor ve Telegram'a gönderiliyor..."):
                    try:
                        bulletin_id = bulletin_module.run_weekly_bulletin(
                            _schedule_settings["leagues"], week_str, force_rebuild=force_weekly
                        )
                    except Exception as exc:
                        st.error(f"Hata: {exc}")
                        bulletin_id = "error"
                if bulletin_id == "error":
                    pass
                elif bulletin_id is None:
                    st.info("Bu hafta için zaten bülten var (veya bu aralıkta seçili liglerde maç yok).")
                else:
                    st.success(f"Bülten gönderildi (bulletin_id={bulletin_id}).")

        st.divider()
        st.write("**Geçmiş Bültenler**")
        with db.get_conn() as _conn:
            _bulletins = _conn.execute(
                "SELECT id, bulletin_type, target_date, telegram_posted, created_at FROM bulletins ORDER BY id DESC LIMIT 20"
            ).fetchall()
        if _bulletins:
            _bulletin_rows = [
                {
                    "ID": b["id"],
                    "Tür": "Günlük" if b["bulletin_type"] == "daily" else "Haftalık",
                    "Hedef Tarih": b["target_date"],
                    "Telegram'a Gönderildi": "Evet" if b["telegram_posted"] else "Hayır",
                    "Oluşturulma Zamanı": b["created_at"][:16].replace("T", " "),
                }
                for b in _bulletins
            ]
            st.dataframe(pd.DataFrame(_bulletin_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("Henüz bülten oluşturulmadı.")

st.divider()
st.caption(RESPONSIBLE_GAMBLING_NOTICE)
