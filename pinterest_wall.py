"""Generate two mosaic wallpapers from a public Pinterest board (no login needed)."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
from pathlib import Path
import random
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup
from PIL import Image, ImageOps
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LIMIT = 100
PRESETS = (("desktop", 3840, 2160, 42), ("phone", 1440, 3120, 7))


def board_url(value):
    parsed = urlsplit(value)
    parts = unquote(parsed.path).strip("/").split("/")
    if (parsed.scheme != "https" or not parsed.hostname
            or not (parsed.hostname == "pinterest.com" or parsed.hostname.endswith(".pinterest.com"))
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or len(parts) != 2
            or any(not part or part in (".", "..") for part in parts)
            or parts[0].lower() in ("pin", "ideas", "search", "resource", "login")):
        raise ValueError("Use a public board URL: https://www.pinterest.com/username/board/. Home tabId and pin links are not boards.")
    return "https://www.pinterest.com/" + "/".join(quote(part, safe="") for part in parts) + "/"


def session():
    client = requests.Session()
    client.headers["User-Agent"] = "Mozilla/5.0"
    retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    client.mount("https://", HTTPAdapter(max_retries=retries))
    return client


def initial_feed(html, url):
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("script", id="__PWS_INITIAL_PROPS__")
    if node is None:
        raise ValueError("Public board data unavailable: the board may be private, blocked, or Pinterest changed its page format.")
    state = json.loads(node.get_text())["initialReduxState"]
    requested = unquote(urlsplit(url).path).rstrip("/").casefold()
    boards = [board for board in state.get("boards", {}).values()
              if unquote(board.get("url", "")).rstrip("/").casefold() == requested
              and board.get("privacy") == "public"]
    if len(boards) != 1:
        raise ValueError("Could not identify the requested public board; refusing to collect recommendations.")
    board = boards[0]
    for key, feed in state.get("resources", {}).get("BoardFeedResource", {}).items():
        options = dict(json.loads(key))
        if str(options.get("board_id")) == str(board["id"]):
            return board, options, feed
    raise ValueError("The board has no readable public image feed.")


def pin_images(pin):
    """Return trusted image candidates; never follow external links attached to pins."""
    if pin.get("type") != "pin":
        return []
    images = pin.get("images") or {}
    urls = []
    for size in ("orig", "736x", "474x", "236x"):
        url = (images.get(size) or {}).get("url", "")
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname == "i.pinimg.com"
                and not parsed.username and not parsed.password and parsed.port in (None, 443)
                and parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))):
            if url not in urls:
                urls.append(url)
    return urls


def collect(url, client):
    response = client.get(url, timeout=30)
    response.raise_for_status()
    board, options, feed = initial_feed(response.text, url)
    pins = feed.get("data", [])
    bookmark = feed.get("nextBookmark")
    images = {}
    seen = set()
    for _ in range(30):
        for pin in pins:
            candidates = pin_images(pin)
            if not candidates:
                continue
            # Sizes of one image share a path suffix; do not count them as separate images.
            signature = pin.get("image_signature") or urlsplit(candidates[0]).path.split("/", 2)[-1]
            images.setdefault(signature, {"id": pin.get("id"), "urls": candidates})
            if len(images) >= LIMIT:
                return board, list(images.values())
        if bookmark == "-end-":
            break
        if not bookmark or bookmark in seen:
            raise ValueError("Pagination stalled before the board ended; refusing to report a partial collection as complete.")
        seen.add(bookmark)
        options["bookmarks"] = [bookmark]
        response = client.get("https://www.pinterest.com/resource/BoardFeedResource/get/",
                              params={"source_url": urlsplit(url).path,
                                      "data": json.dumps({"options": options, "context": {}})},
                              headers={"X-Pinterest-PWS-Handler": "www/[username]/[slug].js"}, timeout=30)
        response.raise_for_status()
        result = response.json()
        payload = result.get("resource_response", {})
        if payload.get("status") != "success" or not isinstance(payload.get("data"), list):
            raise ValueError("Pinterest did not return a valid board page. Try again later.")
        pins = payload["data"]
        bookmark = payload.get("bookmark")
        # The last page may omit response.bookmark and put -end- in resource.options instead.
        if not bookmark:
            bookmarks = result.get("resource", {}).get("options", {}).get("bookmarks", [])
            if isinstance(bookmarks, list) and len(bookmarks) == 1 and isinstance(bookmarks[0], str):
                bookmark = bookmarks[0]
    else:
        raise ValueError("Pagination limit reached before collecting 100 images or reaching the board end.")
    if not images:
        raise ValueError("The public board contains no readable images.")
    return board, list(images.values())


def download(item, cache):
    key = hashlib.sha256(item["urls"][0].encode()).hexdigest()
    path = cache / f"{key}.jpg"
    if path.exists():
        try:
            with Image.open(path) as image:
                image.verify()
            return path
        except (OSError, ValueError):
            pass
    with session() as client:
        for url in item["urls"]:
            try:
                with client.get(url, timeout=30, stream=True, allow_redirects=False) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        continue
                    content = bytearray()
                    for chunk in response.iter_content(65536):
                        content.extend(chunk)
                        if len(content) > 25 * 1024 * 1024:
                            raise ValueError("Image exceeds 25 MiB")
                with Image.open(BytesIO(content)) as image:
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    temporary = path.with_suffix(".tmp")
                    image.save(temporary, format="JPEG", quality=95)
                    temporary.replace(path)
                return path
            except (requests.RequestException, OSError, ValueError, Image.DecompressionBombError):
                continue
    raise ValueError(f"Could not download pin {item['id']}; rerun to retry. No wallpaper was generated.")


def mosaic_layout(count, width, height, rng):
    if count < 1:
        raise ValueError("No images to render")
    weights = [rng.uniform(0.6, 1.8) for _ in range(count)]

    def split(weights, left, top, right, bottom):
        if len(weights) == 1:
            return [(left, top, right, bottom)]
        pivot = max(1, min(len(weights) - 1, round(len(weights) * rng.uniform(0.35, 0.65))))
        fraction = sum(weights[:pivot]) / sum(weights)
        if (right - left) / (bottom - top) > rng.uniform(0.6, 0.95):
            cut = left + round((right - left) * fraction)
            first, second = (left, top, cut, bottom), (cut, top, right, bottom)
        else:
            cut = top + round((bottom - top) * fraction)
            first, second = (left, top, right, cut), (left, cut, right, bottom)
        if any(r <= l or b <= t for l, t, r, b in (first, second)):
            raise ValueError("Canvas is too small")
        return split(weights[:pivot], *first) + split(weights[pivot:], *second)

    return split(weights, 0, 0, width, height)


def render(paths, output, width, height, seed):
    paths = list(paths)
    rng = random.Random(seed)
    rng.shuffle(paths)
    tiles = mosaic_layout(len(paths), width, height, rng)
    canvas = Image.new("RGB", (width, height), "#121212")
    for path, (left, top, right, bottom) in zip(paths, tiles):
        left, top, right, bottom = left + 1, top + 1, right - 1, bottom - 1
        if right <= left or bottom <= top:
            raise ValueError("Canvas is too small for the image count")
        with Image.open(path) as image:
            tile = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGB"),
                                (right - left, bottom - top), Image.Resampling.LANCZOS)
            canvas.paste(tile, (left, top))
    temporary = output.with_suffix(".tmp")
    canvas.save(temporary, format="JPEG", quality=95, subsampling=0)
    temporary.replace(output)
    print(f"{output}: {width}x{height}, {len(paths)} images", flush=True)


def generate(url, output, cache):
    url = board_url(url)
    with session() as client:
        board, items = collect(url, client)
    print(f"Collected {len(items)} distinct images (maximum {LIMIT})", flush=True)
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        paths = list(pool.map(lambda item: download(item, cache), items))
    output.mkdir(parents=True, exist_ok=True)
    for name, width, height, seed in PRESETS:
        render(paths, output / f"{name}.jpg", width, height, seed)
    (output / "manifest.json").write_text(json.dumps({
        "board_url": url, "board_id": board["id"], "image_count": len(items),
        "limit": LIMIT, "images": items,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", help="Public Pinterest board URL (not a home tabId or pin URL)")
    parser.add_argument("--output", type=Path, default=Path("output/pinterest"))
    parser.add_argument("--cache", type=Path, default=Path("cache/pinterest"))
    args = parser.parse_args()
    try:
        generate(args.board, args.output, args.cache)
    except (ValueError, KeyError, TypeError, OSError, requests.RequestException) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
