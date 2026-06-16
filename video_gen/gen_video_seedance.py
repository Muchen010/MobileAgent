"""
Seedance text-to-video generation via the XHS Runway gateway.

Gateway (from internal docs):
    base_url = https://runway.devops.xiaohongshu.com/openai/doubao
    auth     = header "api-key: <key>"   (NOT Authorization: Bearer)
    model    = endpoint id (ep-xxxxxxxx)

Workflow:
    1. POST {base}/contents/generations/tasks  -> create async task
    2. GET  {base}/contents/generations/tasks/{id} -> poll until succeeded
    3. Download the resulting video_url to a local file (default: Desktop)

Usage:
    cd video_gen
    python3 gen_video_seedance.py \
        --prompt "你的画面描述" \
        --ratio 9:16 --duration 5 \
        --output "/Users/xusazhao/Desktop/test_video.mp4"

Config is read from .env in this directory (or real env vars):
    SEEDANCE_API_KEY, SEEDANCE_BASE_URL, SEEDANCE_MODEL,
    SEEDANCE_RATIO, SEEDANCE_DURATION, OUTPUT_DIR
"""

import argparse
import os
import sys
import time
import json
import ssl
import urllib.request
import urllib.error


# SSL context: prefer certifi CA bundle; fall back to unverified (macOS often
# lacks a system CA bundle for Python's urllib).
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------
# Minimal .env loader (no external dependency)
# --------------------------------------------------------------------------
def load_env_file(env_file: str = ".env"):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, env_file)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    return path


# --------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# --------------------------------------------------------------------------
def _post_json(url, headers, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": {"message": body}}


def _get_json(url, headers):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": {"message": body}}


def _download(url, dest_path):
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    with urllib.request.urlopen(url, timeout=120, context=_SSL_CTX) as resp, open(dest_path, "wb") as f:
        total = 0
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


# --------------------------------------------------------------------------
# Seedance task lifecycle (Runway gateway)
# --------------------------------------------------------------------------
def create_task(base_url, api_key, model, prompt, ratio, duration,
                image_url=None, generate_audio=False, watermark=False):
    url = f"{base_url}/contents/generations/tasks"
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }
    content = [{"type": "text", "text": prompt}]
    if image_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": image_url},
            "role": "reference_image",
        })
    payload = {
        "model": model,
        "content": content,
        "ratio": ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "watermark": watermark,
    }
    return _post_json(url, headers, payload)


def poll_task(base_url, api_key, task_id, interval=5, timeout=1800):
    url = f"{base_url}/contents/generations/tasks/{task_id}"
    headers = {"api-key": api_key}
    start = time.time()
    while True:
        status, body = _get_json(url, headers)
        task_status = body.get("status", "unknown")
        elapsed = int(time.time() - start)
        print(f"  [{elapsed:>3}s] task status: {task_status}")
        if task_status == "succeeded":
            return body
        if task_status in ("failed", "cancelled"):
            raise RuntimeError(f"Task {task_status}: {json.dumps(body, ensure_ascii=False)}")
        if time.time() - start > timeout:
            raise TimeoutError(f"Polling timed out after {timeout}s (last status: {task_status})")
        time.sleep(interval)


def extract_video_url(task_body):
    content = task_body.get("content", {})
    if isinstance(content, dict) and content.get("video_url"):
        return content["video_url"]
    if task_body.get("video_url"):
        return task_body["video_url"]
    raise KeyError(f"No video_url found in task result: {json.dumps(task_body, ensure_ascii=False)}")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    load_env_file()

    parser = argparse.ArgumentParser(description="Seedance text-to-video (XHS Runway gateway)")
    parser.add_argument("--prompt", default="", help="Video scene description (required unless --resume).")
    parser.add_argument("--image_url", default=None, help="Optional reference image URL.")
    parser.add_argument("--ratio", default=os.environ.get("SEEDANCE_RATIO", "9:16"))
    parser.add_argument("--duration", type=int, default=int(os.environ.get("SEEDANCE_DURATION", "5")))
    parser.add_argument("--generate_audio", action="store_true")
    parser.add_argument("--api_key", default=os.environ.get("SEEDANCE_API_KEY"))
    parser.add_argument("--base_url", default=os.environ.get("SEEDANCE_BASE_URL", "https://runway.devops.xiaohongshu.com/openai/doubao"))
    parser.add_argument("--model", default=os.environ.get("SEEDANCE_MODEL"))
    parser.add_argument("--output", default=None)
    parser.add_argument("--resume", default=None,
                        help="Resume an existing task id (cgt-xxxx): skip creation, just poll+download.")
    parser.add_argument("--poll_timeout", type=int, default=1800,
                        help="Max seconds to poll before giving up (default 1800).")
    args = parser.parse_args()

    if not args.api_key:
        sys.exit("[ERROR] SEEDANCE_API_KEY is not set (env or --api_key).")
    if not args.model:
        sys.exit("[ERROR] SEEDANCE_MODEL (endpoint id ep-xxxx) is not set.")
    if not args.resume and not args.prompt:
        sys.exit("[ERROR] --prompt is required (unless using --resume).")

    out_path = args.output
    if not out_path:
        out_dir = os.environ.get("OUTPUT_DIR", os.path.expanduser("~/Desktop"))
        out_path = os.path.join(out_dir, f"seedance_{int(time.time())}.mp4")

    # File used to persist the last task id so a timed-out run can be resumed.
    here = os.path.dirname(os.path.abspath(__file__))
    last_id_file = os.path.join(here, "last_task_id.txt")

    print(f"[CONFIG] model    = {args.model}")
    print(f"[CONFIG] base_url = {args.base_url}")
    print(f"[CONFIG] output   = {out_path}")

    if args.resume:
        task_id = args.resume
        print(f"[RESUME] Using existing task_id = {task_id}\n")
    else:
        print(f"[CONFIG] ratio    = {args.ratio}  duration = {args.duration}s  audio = {args.generate_audio}")
        print(f"[PROMPT] {args.prompt}\n")
        print("[1/3] Creating video generation task ...")
        status, body = create_task(
            args.base_url, args.api_key, args.model, args.prompt,
            ratio=args.ratio, duration=args.duration,
            image_url=args.image_url, generate_audio=args.generate_audio,
        )
        if status >= 400 or "error" in body:
            sys.exit(f"[ERROR] Create task failed (HTTP {status}): {json.dumps(body, ensure_ascii=False)}")
        task_id = body.get("id") or body.get("task_id")
        if not task_id:
            sys.exit(f"[ERROR] No task id returned: {json.dumps(body, ensure_ascii=False)}")
        # Persist task id so a later run can --resume it if polling times out.
        try:
            with open(last_id_file, "w", encoding="utf-8") as f:
                f.write(task_id)
        except Exception:
            pass
        print(f"      task_id = {task_id}  (saved to {last_id_file})\n")

    print("[2/3] Polling until the video is ready ...")
    result = poll_task(args.base_url, args.api_key, task_id, timeout=args.poll_timeout)
    video_url = extract_video_url(result)
    print(f"      video_url = {video_url}\n")

    print("[3/3] Downloading video ...")
    size = _download(video_url, out_path)
    print(f"\n[DONE] Saved {size/1024/1024:.2f} MB to: {out_path}")


if __name__ == "__main__":
    main()
