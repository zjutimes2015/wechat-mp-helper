#!/usr/bin/env python3
"""WeChat MP publishing agent v2 (API transport). Runs on the fixed-egress host.

Why API transport: from mainland cloud hosts, git transport to github.com is
unreliable (TLS cut mid-transfer), while api.github.com and
raw.githubusercontent.com stay reachable. So jobs and results move through
the GitHub Contents API only, authenticated with a fine-grained PAT scoped
to the single jobs repo (Contents read/write).

The PAT lives only on this host in ~/.wechat-mp/agent-config.json (mode 600),
entered through `setup` with hidden input.

Cycle:
  1. List jobs/*.json in the repo, fetch each manifest (base64 content).
  2. For status "pending": if results/<id>.json already records a successful
     draft, mark the job done without calling draft/add again. Otherwise
     fetch html/cover, run WeChat calls, then PUT results/<id>.json (with
     sha when the file already exists) and PUT the manifest done/failed.
  3. Print one JSON summary line.

Commands:
  setup   Prompt for the GitHub PAT, save config, verify repo access.
  run     Execute one cycle (default when no command is given).
"""
import base64, getpass, io, json, os, sys, time
import urllib.request, urllib.error
from contextlib import redirect_stdout

OWNER = "zjutimes2015"
REPO = "wechat-mp-jobs"
BRANCH = "main"
API = "https://api.github.com"
HOME_DIR = os.path.expanduser("~/.wechat-mp")
CONFIG = os.path.join(HOME_DIR, "agent-config.json")
TMP = os.path.join(HOME_DIR, "tmp")
sys.path.insert(0, HOME_DIR)


def load_token():
    if os.environ.get("WECHAT_JOBS_TOKEN"):
        return os.environ["WECHAT_JOBS_TOKEN"]
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            return json.load(f)["token"]
    return None


def gh(path, method="GET", payload=None, token=None, tries=2):
    url = f"{API}/repos/{OWNER}/{REPO}/contents/{path}"
    if method == "GET":
        url += f"?ref={BRANCH}"
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            last = RuntimeError(f"HTTP {e.code} on {path}: {body}")
            if e.code in (404, 422):
                raise last
        except Exception as e:
            last = RuntimeError(f"network on {path}: {e}")
        if attempt + 1 < tries:
            time.sleep(3)
    raise last


def get_file(path, token):
    resp = gh(path, token=token)
    raw = base64.b64decode(resp["content"])
    return raw, resp["sha"]


def put_file(path, raw, message, token, sha=None):
    payload = {"message": message, "branch": BRANCH,
               "content": base64.b64encode(raw).decode()}
    if sha:
        payload["sha"] = sha
    return gh(path, method="PUT", payload=payload, token=token)


def put_file_upsert(path, raw, message, token):
    """Create or update: Contents API requires sha when the path already exists."""
    sha = None
    try:
        _, sha = get_file(path, token)
    except RuntimeError as e:
        if "HTTP 404" not in str(e):
            raise
    return put_file(path, raw, message, token, sha=sha)


def capture(fn, args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(args)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


class Args:
    pass


def run_job(job, token):
    import wechat_mp as w
    os.makedirs(TMP, exist_ok=True)
    html_raw, _ = get_file(job["html"], token)
    cover_raw, _ = get_file(job["cover"], token)
    html_path = os.path.join(TMP, job["id"] + ".html")
    cover_path = os.path.join(TMP, job["id"] + ".png")
    with open(html_path, "wb") as f:
        f.write(html_raw)
    with open(cover_path, "wb") as f:
        f.write(cover_raw)
    a = Args(); a.image = cover_path
    cover = capture(w.cmd_upload_cover, a)
    d = Args()
    d.title = job["title"]; d.author = job.get("author", "")
    d.digest = job.get("digest", ""); d.content_file = html_path
    d.thumb_media_id = cover["media_id"]
    d.source_url = job.get("source_url", "")
    d.open_comment = bool(job.get("open_comment", False))
    draft = capture(w.cmd_draft_add, d)
    return {"cover_media_id": cover["media_id"],
            "draft_media_id": draft["draft_media_id"]}


def prior_success_result(job_id, token):
    """Return prior results JSON if it already recorded a successful draft."""
    try:
        raw, _ = get_file(f"results/{job_id}.json", token)
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    prev = json.loads(raw.decode("utf-8"))
    if prev.get("ok") and prev.get("draft_media_id"):
        return prev
    return None


def write_job_status(entry_name, job, job_sha, token):
    """Update jobs/<file>; refresh sha once on 409/422 conflict."""
    body = json.dumps(job, ensure_ascii=False, indent=1).encode()
    msg = f"agent: job {job['id']} {job['status']}"
    try:
        return put_file(f"jobs/{entry_name}", body, msg, token, sha=job_sha)
    except RuntimeError as e:
        if "HTTP 409" not in str(e) and "HTTP 422" not in str(e):
            raise
        _, fresh_sha = get_file(f"jobs/{entry_name}", token)
        return put_file(f"jobs/{entry_name}", body, msg, token, sha=fresh_sha)


def cycle(token):
    listing = gh("jobs", token=token)
    done, failed, skipped, pending_seen = [], [], [], 0
    for entry in listing:
        if entry.get("type") != "file" or not entry["name"].endswith(".json"):
            continue
        raw, sha = get_file(f"jobs/{entry['name']}", token)
        job = json.loads(raw.decode("utf-8"))
        if job.get("status") != "pending":
            continue
        pending_seen += 1
        result = {"id": job["id"], "ts": int(time.time())}
        try:
            prior = prior_success_result(job["id"], token)
            if prior:
                result.update({"ok": True, "draft_media_id": prior["draft_media_id"],
                               "cover_media_id": prior.get("cover_media_id", ""),
                               "reused_prior_result": True})
                job["status"] = "done"
                skipped.append(job["id"])
                done.append(job["id"])
            else:
                ids = run_job(job, token)
                result.update({"ok": True, **ids})
                job["status"] = "done"
                done.append(job["id"])
        except SystemExit:
            result.update({"ok": False, "error": "wechat_mp call failed (errcode in agent log)"})
            job["status"] = "failed"
            failed.append(job["id"])
        except Exception as e:
            result.update({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
            job["status"] = "failed"
            failed.append(job["id"])
        # Always persist result + status so a PUT glitch cannot leave status=pending
        # after draft/add (that loop caused the draft flood).
        try:
            put_file_upsert(f"results/{job['id']}.json",
                            json.dumps(result, ensure_ascii=False, indent=1).encode(),
                            f"agent: result {job['id']}", token)
        except Exception as e:
            result["result_put_error"] = f"{type(e).__name__}: {e}"[:200]
            try:
                put_file_upsert(f"results/{job['id']}.json",
                                json.dumps(result, ensure_ascii=False, indent=1).encode(),
                                f"agent: result {job['id']} (retry)", token)
            except Exception:
                pass
        try:
            write_job_status(entry["name"], job, sha, token)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"job status put failed for {job['id']}: {e}"},
                             ensure_ascii=False), file=sys.stderr)
    print(json.dumps({"ok": True, "pending_seen": pending_seen,
                      "done": done, "failed": failed, "skipped": skipped},
                     ensure_ascii=False))


def cmd_setup():
    os.makedirs(HOME_DIR, exist_ok=True)
    token = getpass.getpass("GitHub token for wechat-mp-jobs (hidden): ").strip()
    gh("jobs", token=token)
    with open(CONFIG, "w") as f:
        json.dump({"token": token}, f)
    os.chmod(CONFIG, 0o600)
    print(json.dumps({"ok": True, "message": "agent config saved (mode 600), repo access verified"}))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        cmd_setup()
        return
    token = load_token()
    if not token:
        print(json.dumps({"ok": False, "error": "no agent config - run `agent.py setup` first"}))
        sys.exit(1)
    cycle(token)


if __name__ == "__main__":
    main()
