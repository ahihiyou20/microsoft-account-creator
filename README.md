# outlook-bit-register  
  
A minimized fork of [outlook-auto-register](https://github.com/lxf746/outlook-auto-register)  
containing only the BitBrowser-based Microsoft account registration flow.  
  
Registers an `outlook.com` account through a real fingerprint browser  
(BitBrowser) driven by Playwright over CDP. The PerimeterX  
"press and hold" human-verification is solved **manually** - no external  
captcha APIs required.  
  
## How it works  
  
```  
run.py  
  └─ bit_register.register()  
        ├─ ensure_profile_for()   creates/reuses a BitBrowser profile via local API  
        ├─ clear_profile_data()   wipes cookies/cache  
        ├─ _open()                launches the profile, returns CDP websocket  
        └─ Playwright state machine fills email → password → DOB → name  
            └─ "prove you're human" → you hold the button yourself  
               in the BitBrowser window (up to 180s)  
```  
  
Credentials are appended to `created_accounts.txt`:  
  
```  
email----password----created=True----stage  
```  
  
## Installation  
  
1. **Download BitBrowser** - <https://www.bitbrowser.net> - launch it once so  
   its local API is listening on `http://127.0.0.1:54345`.  
2. **Install Python dependencies**  
  
   ```bat  
   pip install -r requirements.txt  
   playwright install chromium  
   ```  
  
   > `playwright install chromium` is optional - the script drives  
   > BitBrowser's own Chromium via CDP, but Playwright still needs its  
   > driver installed.  
  
3. **Run**  
  
   ```bat  
   python run.py  
   ```  
  
## Run args  
  
```bat  
python run.py [--proxy PROXY] [--count N] [--profile-id ID] [--close]  
```  
  
| Arg | Default | Description |  
|---|---|---|  
| `--proxy` | `""` | Proxy string, `host:port:user:pass` or `http://user:pass@host:port`. Empty = direct connection (profile created with `proxyType: noproxy`). |  
| `--count` | `1` | Number of accounts to register sequentially. |  
| `--profile-id` | `""` | Reuse an existing BitBrowser profile instead of creating one (equivalent to `BIT_PROFILE_ID`). |  
| `--close` | off | Close the BitBrowser window after the run (default: stays open). |  
  
## Hardcoded behavior (set in `run.py`)  
  
These are baked in via `os.environ.setdefault` - still overridable by real  
environment variables if needed:  
  
- **Manual captcha mode** - when the "prove you're human" widget appears,  
  you hold the button yourself in the BitBrowser window (~2 seconds; the  
  script waits up to 180 s).  
- **Window stays open** - unless `--close` is passed. Useful when Microsoft  
  shows the "Add an email address" screen so you can finish it by hand.  
- **No proofs/OAuth pipeline** - the post-signup recovery-email/token flow  
  is disabled; accounts are saved as email + password only.  
- **Fresh session** - cookies/cache are cleared before each registration.  
  
## Manual steps during a run  
  
1. **"Let's prove you're human"** - when prompted in the log, press and hold  
   the button in the BitBrowser window for ~2 seconds.  
2. **"Add an email address"** - Microsoft may ask for a recovery email after  
   signup. Type in an email you control and verify it by hand. Finish  
   quickly - the 22-step loop keeps polling while you do it.  
  
## Notes & limitations  
  
- Windows-only as configured (the auto HID press path is macOS/Linux;  
  manual mode works everywhere).  
- No token exchange: accounts are saved as email/password only.  
- Registering many accounts on the same IP quickly will hit Microsoft's  
  risk checks - use `--proxy` with a sticky-session template for volume.  
  
## Credits  
  
Upstream project: [lxf746/outlook-auto-register](https://github.com/lxf746/outlook-auto-register)  
(accounts SQLite store, IMAP recovery pipeline, web console, batch  
registration - all removed in this fork).  
