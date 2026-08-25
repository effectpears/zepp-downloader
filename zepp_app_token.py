#!/usr/bin/env python3
"""Zepp Web API HTTP-Login Script.

Holt einen ZEPP App Token über die Web-Schnittstelle (com.huami.webapp)
ohne Registrierung eines Mobilgeräts und aktualisiert ausschließlich den APP_TOKEN in der .env (ohne Anführungszeichen).
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

REDIRECT_URI = "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def update_app_token_in_env(path: Path, app_token: str) -> None:
    """Ersetzt oder ergänzt ausschließlich APP_TOKEN in der .env-Datei (ohne Anführungszeichen)."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    
    line = f"APP_TOKEN={app_token}"
    pattern = re.compile(r"^APP_TOKEN\s*=.*$", re.MULTILINE)

    if pattern.search(existing):
        existing = pattern.sub(line, existing)
    else:
        existing = existing.rstrip() + ("\n" if existing.strip() else "") + line + "\n"

    # Atomares Schreiben der Datei (verhindert Datenverlust)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(existing, encoding="utf-8")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)


def test_web_login(email: str, password: str, country: str = "DE", env_file: str = ".env") -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Step 1: Web Registration Token
    print("[1/3] Sende Registrierungs-Anfrage an Web-API...", file=sys.stderr)
    reg_url = f"https://api-user.huami.com/registrations/{quote(email, safe='')}/tokens"
    reg_payload = {
        "client_id": "HuaMi",
        "country_code": country,
        "json_response": "true",
        "name": email,
        "password": password,
        "redirect_uri": REDIRECT_URI,
        "state": "REDIRECTION",
        "token": "access",
    }
    reg_headers = {
        "app_name": "com.huami.webapp",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://user.zepp.com",
        "referer": "https://user.zepp.com/",
        "x-request-id": str(uuid.uuid4()),
    }

    resp = session.post(reg_url, data=reg_payload, headers=reg_headers, allow_redirects=False)
    print(f"-> HTTP Status: {resp.status_code}", file=sys.stderr)

    access_code = None
    location = resp.headers.get("Location", "")
    if location:
        parsed = parse_qs(urlparse(location).query)
        access_code = parsed.get("access", parsed.get("code", [None]))[0]

    if not access_code and resp.status_code == 200:
        try:
            data = resp.json()
            access_code = data.get("access") or data.get("code")
        except ValueError:
            pass

    if not access_code:
        print("\n[FEHLER / CAPTCHA ERFORDERLICH]", file=sys.stderr)
        print(f"Server-Antwort: {resp.text[:400]}", file=sys.stderr)
        return

    print(f"-> Access Code erhalten: {access_code[:10]}...", file=sys.stderr)

    # Step 2: Client Login Exchange (Web-Client ohne Android-Profil)
    print("[2/3] Tausche Access Code gegen Web Login Token...", file=sys.stderr)
    login_url = "https://api-mifit.zepp.com/v2/client/login"
    login_payload = {
        "allow_registration": "false",
        "app_name": "com.huami.webapp",
        "app_version": "1.0.0",
        "code": access_code,
        "country_code": country,
        "device_id": f"web_{uuid.uuid4()}",
        "device_model": "web",
        "dn": "api-mifit.zepp.com,api-user.zepp.com,api-watch.zepp.com,auth.zepp.com",
        "grant_type": "access_token",
        "source": "com.huami.webapp",
        "third_name": "huami",
    }
    login_headers = {
        "app_name": "com.huami.webapp",
        "appname": "com.huami.webapp",
        "appplatform": "web",
        "origin": "https://user.zepp.com",
        "referer": "https://user.zepp.com/",
        "user-agent": USER_AGENT,
    }

    resp_login = session.post(login_url, data=login_payload, headers=login_headers)
    try:
        login_data = resp_login.json()
    except ValueError:
        print(f"\n[FEHLER beim Client-Login]: Keine JSON-Antwort (HTTP {resp_login.status_code})", file=sys.stderr)
        print(resp_login.text[:400], file=sys.stderr)
        return

    token_info = login_data.get("token_info") or {}
    login_token = token_info.get("login_token")
    user_id = token_info.get("user_id")

    if not login_token or not user_id:
        print("\n[FEHLER beim Client-Login] Token / User ID fehlen:", file=sys.stderr)
        print(login_data, file=sys.stderr)
        return

    print(f"-> Login Token erhalten. User ID: {user_id}", file=sys.stderr)

    # Step 3: App Token holen
    print("[3/3] Generiere ZEPP Web APP_TOKEN...", file=sys.stderr)
    token_url = "https://api-mifit.zepp.com/v1/client/app_tokens"
    token_params = {
        "app_name": "com.huami.webapp",
        "dn": "api-mifit.zepp.com,api-user.zepp.com,auth.zepp.com",
        "login_token": login_token,
    }

    resp_token = session.get(token_url, params=token_params, headers=login_headers)
    try:
        token_data = resp_token.json()
    except ValueError:
        print(f"\n[FEHLER beim App-Token Abruf]: Keine JSON-Antwort (HTTP {resp_token.status_code})", file=sys.stderr)
        print(resp_token.text[:400], file=sys.stderr)
        return

    app_token = token_data.get("token_info", {}).get("app_token")
    if app_token:
        env_path = Path(env_file)
        update_app_token_in_env(env_path, app_token)
        print(f"\n[ERFOLG] APP_TOKEN in '{env_path}' aktualisiert!", file=sys.stderr)
        print(f"APP_TOKEN={app_token}")
    else:
        print("\n[FEHLER beim App-Token Abruf]", file=sys.stderr)
        print(token_data, file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Zepp Web API Login & Token Updater")
    parser.add_argument("--env-file", default=".env", help="Pfad zur .env-Datei (Standard: .env)")
    parser.add_argument("--email", help="Zepp E-Mail Adresse")
    parser.add_argument("--country", default="DE", help="Ländercode (Standard: DE)")
    args = parser.parse_args()

    email = args.email or input("Zepp E-Mail: ").strip()
    password = getpass.getpass("Zepp Passwort: ")
    test_web_login(email, password, country=args.country, env_file=args.env_file)


if __name__ == "__main__":
    main()
