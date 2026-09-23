"""Tek kullanıcılı basit giriş ekranı.

Kimlik bilgileri asla kodda/git'te saklanmaz: config.AUTH_* değerleri
st.secrets (Streamlit Cloud'da 'Secrets' panelinden, yerelde
.streamlit/secrets.toml'dan — ikisi de git'e girmez) üzerinden gelir.
Şifre düz metin değil, PBKDF2-HMAC-SHA256 hash'i olarak saklanır ve
karşılaştırma zamanlama saldırılarına karşı hmac.compare_digest ile yapılır.
"""
import hashlib
import hmac
import time

import streamlit as st

import config

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    ).hex()


def _check_credentials(username: str, password: str) -> bool:
    expected_user = config.AUTH_USERNAME
    expected_hash = config.AUTH_PASSWORD_HASH
    salt = config.AUTH_SALT
    if not expected_user or not expected_hash or not salt:
        st.error(
            "Giriş bilgileri yapılandırılmamış (AUTH_USERNAME / AUTH_PASSWORD_HASH / "
            "AUTH_SALT secrets'ta eksik)."
        )
        return False
    user_ok = hmac.compare_digest(username.strip().encode(), expected_user.encode())
    pass_ok = hmac.compare_digest(hash_password(password, salt).encode(), expected_hash.encode())
    return user_ok and pass_ok


def require_login() -> None:
    """app.py'nin en başında çağrılır; giriş doğrulanana kadar geri kalan
    her şeyin (API çağrıları, DB erişimi dahil) çalışmasını durdurur."""
    if st.session_state.get("authenticated"):
        return

    now = time.time()
    lockout_until = st.session_state.get("lockout_until", 0)

    st.title("🔒 Giriş")

    if now < lockout_until:
        remaining = int(lockout_until - now)
        st.error(f"Çok fazla başarısız deneme. {remaining} saniye sonra tekrar deneyin.")
        st.stop()

    with st.form("login_form"):
        username = st.text_input("Kullanıcı adı")
        password = st.text_input("Şifre", type="password")
        submitted = st.form_submit_button("Giriş yap")

    if submitted:
        if _check_credentials(username, password):
            st.session_state["authenticated"] = True
            st.session_state.pop("failed_attempts", None)
            st.session_state.pop("lockout_until", None)
            st.rerun()
        else:
            attempts = st.session_state.get("failed_attempts", 0) + 1
            st.session_state["failed_attempts"] = attempts
            if attempts >= MAX_ATTEMPTS:
                st.session_state["lockout_until"] = now + LOCKOUT_SECONDS
                st.session_state["failed_attempts"] = 0
                st.error(f"Çok fazla başarısız deneme. {LOCKOUT_SECONDS // 60} dakika kilitlendi.")
            else:
                st.error(f"Kullanıcı adı veya şifre hatalı. ({attempts}/{MAX_ATTEMPTS})")

    st.stop()


def render_logout_button() -> None:
    if st.session_state.get("authenticated") and st.sidebar.button("🚪 Çıkış yap"):
        st.session_state["authenticated"] = False
        st.rerun()
