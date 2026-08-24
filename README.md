# zepp-downloader
script to generate fit files from zepp

# Dependencies
python3 -m venv .venv
.venv/bin/pip install requests fit-tool

# how to get the token:
from: https://github.com/rolandsz/Mi-Fit-and-Zepp-workout-exporter

1. Open the [GDPR page](https://user.huami.com/privacy2/index.html?loginPlatform=web&platform_app=com.xiaomi.hm.health)
2. Click `Export data`
3. Sign in to your account
4. Open the developer tools in your browser (F12)
5. Select the `Network` tab
6. Click on `Export data` again
7. Look for any request containing the `apptoken` header or cookie

insert the token in .env

# Start the script with:
.venv/bin/python zepp_downloader.py
