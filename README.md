# zepp-downloader
script to generate fit files from zepp

# Dependencies
```
python3 -m venv .venv
```
```
.venv/bin/pip install requests fit-tool
```
## .env file

Create an .env file from the example.

Add the paths; the APP_TOKEN will be added automatically in the next step.

# how to get the token:

You only need to retrieve the APP_TOKEN once; enter your email address and password. The token will be automatically transferred to the .env file.

```
.venv/bin/python zepp_app_token.py
```

# Start the script with:
```
.venv/bin/python zepp_downloader.py
```
Run the script regularly, e.g. via cron, to generate the fit files.

# WIP / TO DO

check the sport mapping ! 

> [!NOTE]
Please open an issue if the assignment is incorrect; I haven't been able to test everything yet. The watches may also have different sports modes.

# gemini / copilot generated :-)
myself i wear a "Amazfit Stratos 3". It may be different for other watches, and the script may not work.
