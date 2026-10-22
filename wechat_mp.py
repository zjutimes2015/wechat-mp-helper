#!/usr/bin/env python3
"""WeChat Official Account API helper. RUNS ON the fixed-egress host only.

All api.weixin.qq.com calls must originate from the whitelisted host, so this
script lives on the Tencent Cloud Lighthouse box and is driven over SSH by the
local wrapper (../bin/wechat_mp.py).

Credentials: AppID + AppSecret live ONLY in ~/.wechat-mp/config.json (mode
600) on this host, written by the `setup` command with hidden input. The
secret never passes through chat, logs or any other machine.

Commands:
  setup                 Prompt for AppID/AppSecret, save config, test token.
  token                 Check the access token fetch works (never prints it).
  draft-list            Probe draft-box permission, list first drafts.
  upload-cover <img>    Upload a permanent image material, print media_id.
  upload-inline <img>   Upload an in-article image, print its URL.
  draft-add ...         Create a draft from an HTML content file.
  publish <media_id>    Submit a draft for publishing (freepublish).
  publish-status <id>   Check a publish job.
"""
import argparse, getpass, json, os, sys, time
import urllib.request, urllib.error

BASE = "https://api.weixin.qq.com"
HOME_DIR = os.path.expanduser("~/.wechat-mp")
CONFIG = os.path.join(HOME_DIR, "config.json")
TOKEN_CACHE = os.path.join(HOME_DIR, "token.json")

HINTS = {
    40001: "access_token invalid or expired (auto-refresh retries once)",
    40013: "invalid AppID - check the AppID in config",
    40125: "invalid AppSecret - re-run setup on this host",
    40164: "caller IP not in the account IP whitelist - add this host's public IP in the MP console",
    48001: "API not authorized for this account type - draft/publish APIs need a certified account",
    40007: "invalid media_id",
    45009: "API daily quota reached",
}


def fail(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.exit(1)


def call(url, data=None, raw=None, ctype=None):
    req = urllib.request.Request(url, data=data)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"errcode": -1, "errmsg": f"HTTP {e.code}"}
    except Exception as e:
        return {"errcode": -2, "errmsg": f"network: {e}"}


def checked(resp):
    if resp.get("errcode", 0) != 0:
        hint = HINTS.get(resp["errcode"], "")
        fail({"ok": False, "errcode": resp["errcode"],
              "errmsg": resp.get("errmsg", ""), "hint": hint})
    return resp


def load_config():
    env_id = os.environ.get("WECHAT_MP_APPID")
    env_secret = os.environ.get("WECHAT_MP_APPSECRET")
    if env_id and env_secret:
        return {"appid": env_id, "secret": env_secret}
    if not os.path.exists(CONFIG):
        fail({"ok": False, "error": "no config - run `setup` on this host first"})
    with open(CONFIG) as f:
        return json.load(f)


def fetch_token(force=False):
    if not force and os.path.exists(TOKEN_CACHE):
        with open(TOKEN_CACHE) as f:
            cache = json.load(f)
        if cache.get("expires_at", 0) > time.time() + 300:
            return cache["access_token"]
    cfg = load_config()
    url = (f"{BASE}/cgi-bin/token?grant_type=client_credential"
           f"&appid={cfg['appid']}&secret={cfg['secret']}")
    resp = checked(call(url))
    token = resp["access_token"]
    os.makedirs(HOME_DIR, exist_ok=True)
    with open(TOKEN_CACHE, "w") as f:
        json.dump({"access_token": token,
                   "expires_at": time.time() + resp.get("expires_in", 7200)}, f)
    os.chmod(TOKEN_CACHE, 0o600)
    return token


def api(path, payload=None, retry=True):
    token = fetch_token()
    url = f"{BASE}{path}?access_token={token}"
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    resp = call(url, data=data, ctype="application/json")
    if resp.get("errcode") == 40001 and retry:
        fetch_token(force=True)
        return api(path, payload, retry=False)
    return checked(resp)


def multipart_image(path):
    boundary = "----wechatmpboundary"
    with open(path, "rb") as f:
        body_bytes = f.read()
    name = os.path.basename(path)
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="media"; filename="{name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + body_bytes + tail, f"multipart/form-data; boundary={boundary}"


def cmd_setup(_):
    os.makedirs(HOME_DIR, exist_ok=True)
    appid = input("AppID: ").strip()
    secret = getpass.getpass("AppSecret (hidden): ").strip()
    with open(CONFIG, "w") as f:
        json.dump({"appid": appid, "secret": secret}, f)
    os.chmod(CONFIG, 0o600)
    if os.path.exists(TOKEN_CACHE):
        os.remove(TOKEN_CACHE)
    fetch_token(force=True)
    print(json.dumps({"ok": True, "message": "config saved (mode 600), token fetch succeeded"}))


def cmd_token(_):
    fetch_token(force=True)
    print(json.dumps({"ok": True, "message": "access token fetch succeeded"}))


def cmd_draft_list(_):
    resp = api("/cgi-bin/draft/batchget", {"offset": 0, "count": 3, "no_content": 1})
    titles = []
    for item in resp.get("item", []):
        news = item.get("content", {}).get("news_item", [])
        if news:
            titles.append(news[0].get("title", ""))
    print(json.dumps({"ok": True, "total_count": resp.get("total_count", 0),
                      "titles": titles}, ensure_ascii=False))


def cmd_upload_cover(args):
    token = fetch_token()
    data, ctype = multipart_image(args.image)
    resp = checked(call(f"{BASE}/cgi-bin/material/add_material?access_token={token}&type=image",
                        data=data, ctype=ctype))
    print(json.dumps({"ok": True, "media_id": resp["media_id"]}, ensure_ascii=False))


def cmd_upload_inline(args):
    token = fetch_token()
    data, ctype = multipart_image(args.image)
    resp = checked(call(f"{BASE}/cgi-bin/media/uploadimg?access_token={token}",
                        data=data, ctype=ctype))
    print(json.dumps({"ok": True, "url": resp["url"]}, ensure_ascii=False))


def cmd_draft_add(args):
    with open(args.content_file, encoding="utf-8") as f:
        content = f.read()
    article = {"title": args.title, "author": args.author or "",
               "digest": args.digest or "", "content": content,
               "thumb_media_id": args.thumb_media_id,
               "need_open_comment": 1 if args.open_comment else 0,
               "only_fans_can_comment": 0}
    if args.source_url:
        article["content_source_url"] = args.source_url
    resp = api("/cgi-bin/draft/add", {"articles": [article]})
    print(json.dumps({"ok": True, "draft_media_id": resp["media_id"]}, ensure_ascii=False))


def cmd_publish(args):
    resp = api("/cgi-bin/freepublish/submit", {"media_id": args.media_id})
    print(json.dumps({"ok": True, "publish_id": resp["publish_id"]}, ensure_ascii=False))


def cmd_publish_status(args):
    resp = api("/cgi-bin/freepublish/get", {"publish_id": args.publish_id})
    print(json.dumps({"ok": True, "publish_status": resp.get("publish_status"),
                      "article_id": resp.get("article_id", "")}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("token").set_defaults(fn=cmd_token)
    sub.add_parser("draft-list").set_defaults(fn=cmd_draft_list)
    p = sub.add_parser("upload-cover"); p.add_argument("image"); p.set_defaults(fn=cmd_upload_cover)
    p = sub.add_parser("upload-inline"); p.add_argument("image"); p.set_defaults(fn=cmd_upload_inline)
    p = sub.add_parser("draft-add")
    p.add_argument("--title", required=True)
    p.add_argument("--author", default="")
    p.add_argument("--digest", default="")
    p.add_argument("--content-file", required=True)
    p.add_argument("--thumb-media-id", required=True)
    p.add_argument("--source-url", default="")
    p.add_argument("--open-comment", action="store_true")
    p.set_defaults(fn=cmd_draft_add)
    p = sub.add_parser("publish"); p.add_argument("media_id"); p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("publish-status"); p.add_argument("publish_id"); p.set_defaults(fn=cmd_publish_status)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
