import os, sys, argparse  
  
p = argparse.ArgumentParser(description="BitBrowser signup")  
p.add_argument("--proxy", default="", help="host:port:user:pass or http://user:pass@host:port")  
p.add_argument("--count", type=int, default=1)  
p.add_argument("--profile-id", default="", help="reuse existing BitBrowser profile")
p.add_argument("--keep-open", action="store_false", default=True, help="keep the opened bit browser")
p.add_argument("-v", "--verbose", action="store_true", default=True)  
args = p.parse_args()
  
if args.profile_id:
    os.environ["BIT_PROFILE_ID"] = args.profile_id
if not args.keep_open:
    os.environ["BIT_KEEP_OPEN"] = "0"
  
from bit_register import register  
  
for _ in range(args.count):  
    res = register(proxy=args.proxy, verbose=args.verbose)  
    if res.get("email"):
        with open("created_accounts.txt", "a", encoding="utf-8") as f:  
            f.write(f"{res['email']}----{res['password']}----created={res.get('created')}----{res.get('stage')}\n")