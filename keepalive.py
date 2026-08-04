import requests
import re
import os
import io
import sys
import traceback

try:
    import easyocr
    EASYOCR_READER = easyocr.Reader(['en'], gpu=False)
    HAVE_EASYOCR = True
except:
    HAVE_EASYOCR = False

try:
    from PIL import Image
    HAVE_PIL = True
except:
    HAVE_PIL = False

try:
    import pytesseract
    HAVE_TESSERACT = True
except:
    HAVE_TESSERACT = False

LEMOHOST_URL = "https://lemehost.com"
SERVER_ID = "10246336"
SESSION_COOKIE = os.environ.get("LEMO_SESSION_COOKIE")
if SESSION_COOKIE:
    SESSION_COOKIE = SESSION_COOKIE.strip()
MAX_RETRIES = 5

def log(msg):
    print(msg, flush=True)

def solve_captcha(session, html):
    match = re.search(r'id="extendfreeplanform-captcha-image"[^>]*src="([^"]+)"', html)
    if not match:
        return None
    img_url = match.group(1)
    img_resp = session.get(img_url)
    if img_resp.status_code != 200:
        return None

    img_bytes = img_resp.content
    log(f"Captcha image: {len(img_bytes)} bytes")

    candidates = {}

    if HAVE_EASYOCR:
        try:
            results = EASYOCR_READER.readtext(img_bytes, detail=0, paragraph=False)
            for text in results:
                text = re.sub(r'[^a-zA-Z]', '', text).lower()
                if 3 <= len(text) <= 12:
                    candidates['easyocr'] = text
                    log(f"  EasyOCR: '{text}'")
        except Exception as e:
            log(f"  EasyOCR error: {e}")

    if HAVE_TESSERACT and HAVE_PIL:
        try:
            img = Image.open(io.BytesIO(img_bytes))
            w, h = img.size
            best_t = None
            for scale in [4, 6]:
                for psm in [6, 7, 8]:
                    for oem in [1, 3]:
                        for thresh in [None, 90, 100, 110]:
                            try:
                                copy = img.copy().resize((w * scale, h * scale), Image.LANCZOS).convert("L")
                                if thresh is not None:
                                    copy = copy.point(lambda x, t=thresh: 0 if x < t else 255)
                                config = f"--psm {psm} --oem {oem}"
                                text = pytesseract.image_to_string(copy, config=config).strip()
                                text = re.sub(r'[^a-z]', '', text.lower())
                                if 3 <= len(text) <= 12:
                                    if not best_t or len(text) > len(best_t):
                                        best_t = text
                            except:
                                pass
            if best_t:
                candidates['tesseract'] = best_t
                log(f"  Tesseract: '{best_t}'")
        except Exception as e:
            log(f"  PIL error: {e}")

    if 'easyocr' in candidates:
        log(f"Captcha: '{candidates['easyocr']}' (EasyOCR)")
        return candidates['easyocr']
    if 'tesseract' in candidates:
        log(f"Captcha: '{candidates['tesseract']}' (Tesseract)")
        return candidates['tesseract']

    log("Captcha failed")
    return None

def do_post(session, form_url, csrf_token, extend_till, captcha_text=None):
    data = {
        "_csrf-frontend": csrf_token,
        "ExtendFreePlanForm[extendTill]": extend_till
    }
    if captcha_text:
        data["ExtendFreePlanForm[captcha]"] = captcha_text
    session.headers.update({
        "Referer": form_url,
        "Origin": LEMOHOST_URL,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    return session.post(form_url, data=data, allow_redirects=False)

def page_has_captcha(html):
    return re.search(r'id="extendfreeplanform-captcha-image"[^>]*src="([^"]+)"', html) is not None

def confirm_timer(session, r, before, form_url, label):
    """Check if the POST actually increased the timer. Handles 200 (timer in body)
    and 302 (follow redirect, then read timer). Returns True on real success."""
    m = re.search(r'id="countdown-free-plan"[^>]*>(\d+):(\d+):(\d+)<', r.text)
    if m:
        after = int(m.group(1)) * 60 + int(m.group(2))
        a_str = f"{m.group(1)}:{m.group(2)}:{m.group(3)}"
        log(f"Timer now: {a_str} ({after} min)")
        if after > (before or 0):
            log(f"SUCCESS! Timer increased {label}!")
            return True
    if r.status_code == 302:
        loc = r.headers.get("Location")
        if loc:
            url = loc if loc.startswith("http") else LEMOHOST_URL + loc
            log(f"Following redirect: {loc}")
            r2 = session.get(url, allow_redirects=True)
            m = re.search(r'id="countdown-free-plan"[^>]*>(\d+):(\d+):(\d+)<', r2.text)
            if m:
                after = int(m.group(1)) * 60 + int(m.group(2))
                a_str = f"{m.group(1)}:{m.group(2)}:{m.group(3)}"
                log(f"Timer now: {a_str} ({after} min)")
                if after > (before or 0):
                    log(f"SUCCESS! Timer increased {label}!")
                    return True
    return False

def keep_alive():
    session = requests.Session()
    session.cookies.set("_identity-frontend", SESSION_COOKIE, domain="lemehost.com")
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })

    form_url = f"{LEMOHOST_URL}/server/{SERVER_ID}/free-plan"

    log("Opening free-plan page...")
    resp = session.get(form_url, allow_redirects=True)
    log(f"Form page: {resp.status_code}")

    def get_timer(html):
        m = re.search(r'id="countdown-free-plan"[^>]*>(\d+):(\d+):(\d+)<', html)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2)), f"{m.group(1)}:{m.group(2)}:{m.group(3)}"
        return None, None

    before, b_str = get_timer(resp.text)
    if before:
        log(f"Countdown: {b_str} ({before} min)")
        if before >= 28:
            log(f"Already {before}min, skip")
            return True

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"\nAttempt {attempt}/{MAX_RETRIES}")

        csrf_match = re.search(r'name="_csrf-frontend"[^>]*value="([^"]+)"', resp.text)
        csrf_token = csrf_match.group(1) if csrf_match else None
        if not csrf_token:
            log("No CSRF token")
            return False

        till_match = re.search(r'name="ExtendFreePlanForm\[extendTill\]"[^>]*value="(\d+)"', resp.text)
        extend_till = till_match.group(1) if till_match else "1785315284"

        has_captcha = page_has_captcha(resp.text)

        if not has_captcha:
            # No captcha on this server right now: POST directly.
            log("No captcha on page, posting directly...")
            r = do_post(session, form_url, csrf_token, extend_till, None)
            log(f"POST (no captcha): {r.status_code}")
            if confirm_timer(session, r, before, form_url, "without captcha"):
                return True
            # Failed without captcha -> maybe captcha just got added, re-check next attempt
            log("No-captcha POST did not increase timer, re-fetching page...")
            resp = session.get(form_url, allow_redirects=True)
            continue

        # Captcha is present: solve it and POST with it.
        log("Captcha detected, solving...")
        captcha_text = solve_captcha(session, resp.text)
        if not captcha_text:
            log("Captcha failed, retrying...")
            resp = session.get(form_url, allow_redirects=True)
            continue

        r = do_post(session, form_url, csrf_token, extend_till, captcha_text)
        log(f"POST (with captcha): {r.status_code}")
        if confirm_timer(session, r, before, form_url, "with captcha"):
            return True

        if r.status_code == 200:
            err = re.search(r'class="help-block"[^>]*>([^<]+)<', r.text)
            if err:
                log(f"Error: {err.group(1).strip()}")
            elif "incorrect" in r.text.lower():
                log("Error: captcha incorrect")

        # Re-fetch form page for fresh CSRF + captcha
        resp = session.get(form_url, allow_redirects=True)

    log("All retries exhausted")
    return False

if __name__ == "__main__":
    if not SESSION_COOKIE:
        log("ERROR: No LEMO_SESSION_COOKIE")
        sys.exit(1)
    try:
        sys.exit(0 if keep_alive() else 1)
    except Exception as e:
        log(f"CRASH: {e}")
        traceback.print_exc()
        sys.exit(1)
