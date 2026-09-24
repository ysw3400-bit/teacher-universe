import os
import re
import sys
import json
import logging
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("KerisCrawler")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

def init_firebase():
    """Initialize Firebase Admin SDK using FIREBASE_SERVICE_ACCOUNT environment variable."""
    if firebase_admin._apps:
        return firestore.client()

    sa_env = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not sa_env:
        logger.error("FIREBASE_SERVICE_ACCOUNT environment variable is not set!")
        sys.exit(1)

    try:
        # Check if environment variable is raw JSON or file path
        if os.path.exists(sa_env):
            cred = credentials.Certificate(sa_env)
            logger.info("Loaded service account credentials from file path: %s", sa_env)
        else:
            sa_data = json.loads(sa_env)
            cred = credentials.Certificate(sa_data)
            logger.info("Loaded service account credentials from JSON environment variable.")

        firebase_admin.initialize_app(cred)
        logger.info("Firebase Admin initialized successfully.")
        return firestore.client()
    except Exception as e:
        logger.exception("Failed to initialize Firebase Admin SDK: %s", e)
        sys.exit(1)

def parse_keris_course_page(url: str):
    """
    Crawls the KERIS Knowledge Fountain course detail page
    and extracts current enrolled count and max capacity.
    Returns a dict with {"enrolledCount": int, "maxCount": int} or None.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://educourse.keris.or.kr/"
    }

    try:
        response = requests.get(url, headers=headers, timeout=12)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"
    except requests.RequestException as e:
        logger.error("HTTP request error for URL %s: %s", url, e)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text()

    # Strategy 1: Look for common KERIS course count selector patterns
    candidate_selectors = [
        ".apply_status", ".course_info_count", ".count_box", ".capacity", 
        ".enroll_info", ".detail_info", ".edu_info", "table.view_table",
        ".info_list", "dl.info_dl", ".course_apply"
    ]

    for sel in candidate_selectors:
        elements = soup.select(sel)
        for el in elements:
            text = el.get_text(separator=" ", strip=True)
            # Pattern like "7 / 10명", "7/10", "신청 7 / 정원 10"
            m = re.search(r"(\d+)\s*(?:명)?\s*/\s*(\d+)\s*(?:명)?", text)
            if m:
                enrolled = int(m.group(1))
                capacity = int(m.group(2))
                logger.info("Found count via selector '%s': %d / %d", sel, enrolled, capacity)
                return {"enrolledCount": enrolled, "maxCount": capacity}

    # Strategy 2: Search specific keywords in surrounding text or table rows
    keywords = ["신청인원", "수강인원", "모집인원", "정원", "신청 현황", "수강 현황"]
    for kw in keywords:
        for tag in soup.find_all(["th", "dt", "td", "dd", "span", "div", "li"]):
            tag_text = tag.get_text(strip=True)
            if kw in tag_text:
                # Check next sibling or parent's sibling
                sibling = tag.find_next_sibling()
                check_text = sibling.get_text(separator=" ", strip=True) if sibling else ""
                parent_text = tag.parent.get_text(separator=" ", strip=True) if tag.parent else ""
                combined = f"{check_text} {parent_text}"

                # Match "7명 / 10명" or "7 / 10"
                m = re.search(r"(\d+)\s*(?:명)?\s*/\s*(\d+)\s*(?:명)?", combined)
                if m:
                    enrolled = int(m.group(1))
                    capacity = int(m.group(2))
                    logger.info("Found count via keyword '%s': %d / %d", kw, enrolled, capacity)
                    return {"enrolledCount": enrolled, "maxCount": capacity}

                # Match single number after "신청" e.g. "신청: 7명"
                m_single = re.search(r"(?:신청|현재)\s*[:：]?\s*(\d+)\s*명?", combined)
                if m_single:
                    enrolled = int(m_single.group(1))
                    logger.info("Found enrolled count via keyword '%s': %d", kw, enrolled)
                    return {"enrolledCount": enrolled, "maxCount": None}

    # Strategy 3: Global regex pattern match across page text
    match = re.search(r"(?:수강신청\s*인원|신청인원|모집인원|신청현황)\s*[:：]?\s*(\d+)\s*명?\s*/\s*(\d+)\s*명?", page_text)
    if match:
        enrolled = int(match.group(1))
        capacity = int(match.group(2))
        logger.info("Found count via global regex: %d / %d", enrolled, capacity)
        return {"enrolledCount": enrolled, "maxCount": capacity}

    # Strategy 4: Generic fraction pattern fallback
    match_fallback = re.search(r"(\d+)\s*명?\s*/\s*(\d+)\s*명", page_text)
    if match_fallback:
        enrolled = int(match_fallback.group(1))
        capacity = int(match_fallback.group(2))
        # Sanity check: capacity usually between 1 and 200
        if 1 <= capacity <= 200 and enrolled <= capacity:
            logger.info("Found count via fallback pattern: %d / %d", enrolled, capacity)
            return {"enrolledCount": enrolled, "maxCount": capacity}

    logger.warning("Could not parse enrolled count from page: %s", url)
    return None

def main():
    logger.info("Starting Knowledge Fountain (지식샘터) crawler job...")
    db = init_firebase()

    lectures_ref = db.collection("lectures")
    docs = lectures_ref.stream()

    total_lectures = 0
    updated_lectures = 0
    skipped_lectures = 0
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for doc in docs:
        total_lectures += 1
        data = doc.to_dict()
        url = data.get("url", "").strip()
        title = data.get("title", doc.id)

        if not url or url == "#" or ("keris.or.kr" not in url and "http" not in url):
            logger.info("Skipping '%s' (No valid URL registered)", title)
            skipped_lectures += 1
            continue

        logger.info("Crawling lecture: '%s' (ID: %s, URL: %s)", title, doc.id, url)
        result = parse_keris_course_page(url)

        if result:
            enrolled = result["enrolledCount"]
            update_payload = {
                "enrolledCount": enrolled,
                "currentCount": enrolled,  # Backwards-compatible field
                "lastCrawledAt": now_str,
                "crawlingStatus": "success"
            }
            if result.get("maxCount"):
                update_payload["maxCount"] = result["maxCount"]

            try:
                doc.reference.update(update_payload)
                updated_lectures += 1
                logger.info(
                    "Successfully updated '%s' -> enrolledCount: %d (max: %s)",
                    title, enrolled, result.get("maxCount", "unchanged")
                )
            except Exception as e:
                logger.error("Failed to update Firestore doc %s: %s", doc.id, e)
        else:
            try:
                doc.reference.update({
                    "lastCrawledAt": now_str,
                    "crawlingStatus": "parse_failed"
                })
            except Exception:
                pass

    logger.info(
        "Finished crawler job. Total: %d, Updated: %d, Skipped: %d",
        total_lectures, updated_lectures, skipped_lectures
    )

if __name__ == "__main__":
    main()
