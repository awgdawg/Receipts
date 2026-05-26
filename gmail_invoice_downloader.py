import os
import base64
import re
import io
import email
import time
import logging
import pdfplumber
import pdfkit
from datetime import datetime
from email import policy
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.utils import parseaddr, parsedate_to_datetime
import requests
from bs4 import BeautifulSoup
import json

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROCESSED_FILE = os.path.join(SCRIPT_DIR, 'processed_ids.json')
DOWNLOAD_DIR = os.path.join(SCRIPT_DIR, 'downloaded_invoices')

# ===== CONFIG =====
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

PDF_QUERY = (
    'in:anywhere -in:sent '
    '(invoice OR receipt OR statement OR bill OR "bill is ready" OR "bill ready" OR "amount due" '
    'OR autopay OR "auto pay" OR foodservicedirect OR "foodservicedirect.com" OR crushglasskc.com OR "crush glass" OR "payment processed" OR butlerfoods OR "butler foods" OR info@butlerfoods.com) '
    'filename:pdf newer_than:365d'
)

HTML_QUERY = (
    'in:anywhere -in:sent '
    '(invoice OR receipt OR statement OR bill OR "bill is ready" OR "bill ready" OR "amount due" '
    'OR autopay OR "auto pay" OR foodservicedirect OR "foodservicedirect.com" OR crushglasskc.com OR "crush glass" OR "payment processed" OR butlerfoods OR "butler foods" OR info@butlerfoods.com) '
    'newer_than:365d'
)

KEYWORDS = ['invoice', 'receipt', 'statement', 'bill', 'payment']

PAID_KEYWORDS = [
    'paid',
    'payment received',
    'for your payment',
    'balance due: $0',
    'balance due $0',
    'amount due $0',
    'amount due: $0',  # FIX: was missing comma, silently concatenated with next line
    'status: paid',
    'payment confirmation',
    'receipt',
    'thank you for your purchase',
    'charged:'
]

UNPAID_KEYWORDS = [
    'unpaid',
    'payment due',
    'past due'
]

AUTOPAY_KEYWORDS = [
    "autopay is scheduled",
    "auto pay status: enrolled",
    "you are enrolled in autopay",
    "we will charge the bill amount",
    "your payment will be drafted on",
    "will be drafted on",
    "scheduled for autopay",
    "automatic payment",
    "enrolled in auto pay",
]

SKIP_KEYWORDS = [
    'order confirmation',
    'shipping notice',
    'catalog',
    'pre-selected',
    'trial',
    'limited-time offer',
    'promotional email',
    'super snail',
    'shipment notification',
    'eligible participant'
]

BALANCE_DUE_RE = re.compile(r'balance due\s*\$?([\d,]+(?:\.\d+)?)', re.IGNORECASE)
LEGAL_SUFFIXES = ['inc', 'inc\\.', 'llc', 'llc\\.', 'ltd', 'ltd\\.', 'co', 'co\\.', 'corp', 'corp\\.', 'company']
# FIX: was r'\\b...' which produced literal \b instead of word boundaries
SUFFIX_REGEX = re.compile(r'\b(' + '|'.join(LEGAL_SUFFIXES) + r')\b', re.IGNORECASE)

LABEL_NAME = 'Vendor/Receipts/Processed-Invoices'
NEGATED_PROMO_RE = re.compile(r"\bnot\s+a\s+(marketing\s+or\s+)?promotional\s+email\b", re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def execute_with_retry(request, max_retries=5):
    """Execute a Gmail API request with exponential backoff on rate-limit/server errors."""
    for attempt in range(max_retries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in (429, 500, 503) and attempt < max_retries - 1:
                wait = 2 ** attempt
                log.warning(f"API error {e.resp.status}, retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                raise


def save_processed(processed_ids):
    """Persist processed_ids to disk immediately."""
    with open(PROCESSED_FILE, 'w', encoding='utf-8') as f:
        json.dump(processed_ids, f, ensure_ascii=False, indent=2)


def is_paid(text: str) -> bool:
    t = text.lower()
    m = BALANCE_DUE_RE.search(t)
    if m:
        amt = float(m.group(1).replace(',', ''))
        return amt == 0.0
    if any(u in t for u in UNPAID_KEYWORDS):
        return False
    if any(p in t for p in PAID_KEYWORDS):
        return True
    return False


def is_autopay(text: str) -> bool:
    return any(k in text.lower() for k in AUTOPAY_KEYWORDS)


def is_paid_or_autopay(text: str) -> bool:
    if any(u in text.lower() for u in UNPAID_KEYWORDS):
        return False
    return is_paid(text) or is_autopay(text)


def keyword_check(text):
    return any(word in text.lower() for word in KEYWORDS)


def gmail_authenticate():
    creds = None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    creds_path = os.path.join(script_dir, 'credentials.json')
    token_path = os.path.join(script_dir, 'token.json')

    if os.path.exists(token_path):
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w') as token:
            token.write(creds.to_json())

    return build('gmail', 'v1', credentials=creds)


def get_label_id(service, name):
    labels = execute_with_retry(service.users().labels().list(userId='me')).get('labels', [])
    for lbl in labels:
        if lbl['name'] == name:
            return lbl['id']
    raise ValueError(f"Label '{name}' not found; please create it in Gmail UI.")


def extract_info_from_pdf(pdf_bytes, sender_email, skip_log, email_date):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(page.extract_text() for page in pdf.pages if page.extract_text())

    display_name, _ = parseaddr(sender_email)
    dn = display_name.lower().strip()

    # Extract invoice date — try multiple formats, fall back to today
    match = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', full_text)
    invoice_date = datetime.today()
    if match:
        for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"):
            try:
                invoice_date = datetime.strptime(match.group(1), fmt)
                break
            except ValueError:
                continue

    # Sender-specific name overrides
    if 'noreply@ccproduce.net' in sender_email.lower():
        clean_name = 'CCProduce'
    elif 'fairwave.com' in sender_email.lower() or 'sent-via.netsuite.com' in sender_email.lower():
        clean_name = 'Fairwave'
    elif 'shopify.com' in sender_email.lower():
        clean_name = 'ShopifyInc'
    elif 'drinkveritasmo.com' in sender_email.lower():
        clean_name = 'Veritas'
    elif dn == 'auto-receipt':
        clean_name = 'MidvaleIndemnity'
    elif dn == 'julie froneberger':
        clean_name = 'MdpServicesLlc'
    else:
        display_name, _ = parseaddr(sender_email)
        if display_name:
            m = SUFFIX_REGEX.search(display_name)
            raw = display_name[:m.end()] if m else display_name
        else:
            lines = [l.strip() for l in full_text.splitlines() if l.strip()]
            candidates = [
                l for l in lines[:6]
                if len(l) < 50
                   and 'the fix' not in l.lower()
                   and not any(x in l.lower() for x in ['invoice', 'receipt', 'date', 'page', 'statement', 'total'])
            ]
            raw = candidates[0] if candidates else sender_email.split('@')[1].split('.')[0]
        clean_name = re.sub(r'[^A-Za-z0-9]', '', raw.title())

    return f"{invoice_date.strftime('%Y.%m.%d')}_{clean_name}.pdf"


def unique_path(directory, filename):
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return os.path.join(directory, candidate)


def list_all_message_ids(service, query: str, page_size: int = 500, cap: int | None = None):
    """Paginate through messages.list with a 2-second inter-page delay.

    Gmail enforces a per-user rate limit of 250 quota units / 100 seconds.
    messages.list costs 5 units per call → max 50 calls / 100 s (1 per 2 s).
    Without the delay a tight loop bursts well past this and causes 429 errors
    that affect all other apps accessing the same account (e.g. ReceiptMe).
    """
    all_msgs = []
    page_token = None
    page = 0

    while True:
        if page > 0:
            time.sleep(2)  # stay within 50 messages.list calls / 100 s per-user limit

        resp = execute_with_retry(
            service.users().messages().list(
                userId='me',
                q=query,
                maxResults=page_size,
                pageToken=page_token,
            )
        )
        msgs = resp.get('messages', [])
        all_msgs.extend(msgs)
        page += 1

        if cap and len(all_msgs) >= cap:
            return all_msgs[:cap]

        page_token = resp.get('nextPageToken')
        if not page_token:
            break

    return all_msgs


# ── Main downloader ───────────────────────────────────────────────────────────

def download_invoices(service, label_id):  # FIX: label_id passed explicitly, not an implicit global
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    skip_log = []

    # Load processed IDs (persisted across runs)
    processed_ids = {}
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, 'r', encoding='utf-8') as f:
                data = f.read().strip()
                if data:
                    loaded = json.loads(data)
                    if isinstance(loaded, dict):
                        processed_ids = loaded
                    else:
                        # back-compat: plain list of IDs
                        processed_ids = {mid: None for mid in loaded}
        except (json.JSONDecodeError, ValueError):
            processed_ids = {}

    # ── A) Process actual PDF attachments ─────────────────────────────────────
    pdf_msgs = list_all_message_ids(service, PDF_QUERY, page_size=500)
    log.info(f"PDF query matched {len(pdf_msgs)} messages")

    for msg in pdf_msgs:
        msg_id = msg['id']
        if msg_id in processed_ids:
            continue

        msg_data = execute_with_retry(
            service.users().messages().get(userId='me', id=msg_id, format='raw')
        )
        raw_data = base64.urlsafe_b64decode(msg_data['raw'])
        mail = email.message_from_bytes(raw_data, policy=policy.default)

        # FIX: parse From header once at the top of the loop
        display_name, addr = parseaddr(mail['From'])
        sender_label = (display_name or addr).lower()
        addr_lower = addr.lower()
        display_lower = display_name.lower()

        # FIX: compute is_excel_linen once with consistent logic (was computed twice with differing checks)
        is_excel_linen = "excellinen" in addr_lower or "excel linen" in display_lower

        email_date = (
            parsedate_to_datetime(mail['Date']).strftime("%Y-%m-%d %H:%M")
            if mail.get('Date') else "unknown_date"
        )
        email_date_short = email_date.split()[0].replace('-', '.')
        email_subject = mail['Subject'] or ""
        body_part = mail.get_body(preferencelist=('html', 'plain'))
        email_body = body_part.get_content() if body_part else ""

        candidates = []
        for part in mail.walk():
            ct = (part.get_content_type() or "").lower()
            fn = (part.get_filename() or "")
            data = part.get_payload(decode=True) or b""

            is_probably_pdf = (
                ct == "application/pdf"
                or fn.lower().endswith(".pdf")
                or data[:5] == b"%PDF-"
            )
            if not is_probably_pdf:
                continue

            data = part.get_payload(decode=True)
            is_mdp = fn.lower() == "mdp services invoice.pdf"

            try:
                with pdfplumber.open(io.BytesIO(data)) as pdf:
                    pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            except Exception as e:
                log.warning(f"[{email_date}] Could not read PDF text from '{fn}': {e}")
                pdf_text = ""

            combined = "\n".join([pdf_text, email_subject, email_body, fn])

            # 0) Skip-keyword filter
            if not is_mdp and any(skip in combined.lower() for skip in SKIP_KEYWORDS):
                matched = next((kw for kw in SKIP_KEYWORDS if kw in combined.lower()), None)
                skip_log.append(
                    f"[{email_date}] Skipped PDF: skip-keyword match '{matched}'  - From: {display_name or addr}, File: {fn}")
                continue

            # 1) Invoice/receipt keyword filter
            if not is_mdp and not keyword_check(combined):
                skip_log.append(
                    f"[{email_date}] Skipped PDF: no invoice keywords  - From: {display_name or addr}, File: {fn}")
                continue

            is_fairwave = (
                "fairwave.com" in addr_lower
                or "sent-via.netsuite.com" in addr_lower
                or "cash sale" in email_subject.lower()
                or "cash sale" in fn.lower()
            )

            is_ccproduce = "ccproduce.net" in addr_lower or "noreply@ccproduce.net" in addr_lower
            is_credit_memo = (
                "credit memo" in combined.lower()
                or "cr. memo" in combined.lower()
                or bool(re.search(r'\$\s*-\s*\d', combined))
            )
            pass_paid_gate = is_ccproduce and is_credit_memo

            # 2) Paid-status filter
            is_central_soy = 'central soyfoods' in sender_label
            if not (is_mdp or is_central_soy or is_fairwave or is_excel_linen or pass_paid_gate) \
                    and not is_paid_or_autopay(combined):
                skip_log.append(
                    f"[{email_date}] Skipped PDF: not marked paid  - From: {display_name or addr}, File: {fn}"
                )
                continue

            score = 999 if is_mdp else (
                (3 if any(k in fn.lower() for k in KEYWORDS) else 0)
                + (2 if any(k in email_subject.lower() for k in KEYWORDS) else 0)
                + (1 if any(k in email_body.lower() for k in KEYWORDS) else 0)
                + (1 if any(k in pdf_text.lower() for k in KEYWORDS) else 0)
            )
            # FIX: append once (original code appended unconditionally then again inside `if score`)
            candidates.append((score, data, fn))

        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            _, best_pdf, best_fn = candidates[0]

            if is_excel_linen:
                try:
                    date_prefix = parsedate_to_datetime(mail['Date']).strftime("%Y.%m.%d")
                except Exception:
                    date_prefix = email_date_short
                new_name = f"{date_prefix}_ExcelLinenSupply.pdf"
            else:
                new_name = extract_info_from_pdf(best_pdf, mail['From'], skip_log, mail['Date'])

            if new_name:
                out = unique_path(DOWNLOAD_DIR, new_name)
                with open(out, 'wb') as f:
                    f.write(best_pdf)
                log.info(f"Saved PDF: {out}  - From: {display_name or addr}")

                execute_with_retry(
                    service.users().messages().modify(
                        userId='me', id=msg_id, body={'addLabelIds': [label_id]}
                    )
                )
                processed_ids[msg_id] = new_name
                # FIX: save progress after each download so a crash doesn't lose all work
                save_processed(processed_ids)

    # ── B) Fallback: HTML-only receipts ──────────────────────────────────────
    html_msgs = list_all_message_ids(service, HTML_QUERY, page_size=500)
    log.info(f"HTML query matched {len(html_msgs)} messages")

    for msg in html_msgs:
        msg_id = msg['id']
        if msg_id in processed_ids:
            continue

        msg_data = execute_with_retry(
            service.users().messages().get(userId='me', id=msg_id, format='raw')
        )
        raw_data = base64.urlsafe_b64decode(msg_data['raw'])
        mail = email.message_from_bytes(raw_data, policy=policy.default)

        # FIX: parse From header once at the top of the loop
        display_name, addr = parseaddr(mail['From'])
        addr_lower = addr.lower()
        display_lower = display_name.lower()

        is_webstaurant = 'webstaurantstore.com' in addr_lower or 'webstaurantstore' in display_lower

        email_date = (
            parsedate_to_datetime(mail['Date']).strftime("%Y-%m-%d %H:%M")
            if mail.get('Date') else "unknown_date"
        )
        email_date_short = email_date.split()[0].replace('-', '.')
        subj = mail['Subject'] or ""
        subj_lower = subj.lower()
        body_part = mail.get_body(preferencelist=('html', 'plain'))
        body = body_part.get_content() if body_part else ""

        # Butler Foods: require evidence of actual charge
        is_butlerfoods = "butlerfoods.com" in addr_lower or "info@butlerfoods.com" in addr_lower
        is_butler_order = is_butlerfoods and ("your butler foods" in subj_lower or "butler foods" in subj_lower)

        if is_butler_order:
            has_payment_method = re.search(r"payment\s*method", body, re.IGNORECASE) is not None
            has_card_amount = re.search(r"\bvisa\b|\bmastercard\b|\bamex\b|\bdiscover\b", body, re.IGNORECASE) is not None
            has_total_amount = re.search(r"\btotal\b.*?\$\s*[\d,]+(?:\.\d{2})", body, re.IGNORECASE | re.DOTALL) is not None
            if not (has_payment_method and (has_card_amount or has_total_amount)):
                skip_log.append(
                    f"[{email_date}] Skipped HTML: ButlerFoods order missing payment evidence  - From: {display_name or addr}"
                )
                continue

        # FoodServiceDirect: only capture order-total emails
        is_foodservicedirect = "foodservicedirect.com" in addr_lower
        is_fsd_order = (
            is_foodservicedirect
            and re.match(r"^your\s+foodservicedirect\.com\s+order\s+#?\d+", subj_lower) is not None
        )

        if is_foodservicedirect and not is_fsd_order:
            skip_log.append(
                f"[{email_date}] Skipped HTML: FoodServiceDirect non-total email  - From: {display_name or addr}")
            continue

        if is_fsd_order:
            if not re.search(r"grand\s+total.*?\$\s*[\d,]+(?:\.\d{2})", body, re.IGNORECASE | re.DOTALL):
                skip_log.append(
                    f"[{email_date}] Skipped HTML: FoodServiceDirect order missing Grand Total  - From: {display_name or addr}")
                continue

        html_combined = "\n".join([subj, body])
        t = html_combined.lower()

        # Skip-keyword filter (with "not a promotional email" negation carve-out)
        matched_skip = next((kw for kw in SKIP_KEYWORDS if kw in t), None)
        if matched_skip == "promotional email" and NEGATED_PROMO_RE.search(t):
            matched_skip = None

        if not (is_webstaurant or is_fsd_order) and matched_skip:
            skip_log.append(
                f"[{email_date}] Skipped HTML: skip-keyword match '{matched_skip}'  - From: {display_name or addr}"
            )
            continue

        if not (is_webstaurant or is_fsd_order) and not keyword_check(html_combined):
            skip_log.append(f"[{email_date}] Skipped HTML: no invoice keywords  - From: {display_name or addr}")
            continue

        if not (is_webstaurant or is_fsd_order or is_butler_order) and not is_paid_or_autopay(html_combined):
            skip_log.append(f"[{email_date}] Skipped HTML: not marked paid  - From: {display_name or addr}")
            continue

        # ── HOODZ: fetch PDF via download link ───────────────────────────────
        if 'hoodz of kansas city' in display_lower:
            soup = BeautifulSoup(body, 'html.parser')
            pdf_url = None

            img = soup.find('img', alt=lambda x: x and 'download invoice' in x.lower())
            if img and img.parent.name == 'a' and img.parent.get('href'):
                pdf_url = img.parent['href']

            if not pdf_url:
                for a in soup.find_all('a', href=True):
                    href = a['href'].rstrip('")')
                    if '/pdf' in href.lower():
                        pdf_url = href
                        break

            if pdf_url:
                resp = requests.get(pdf_url)
                if resp.status_code == 200:
                    filename = f"{email_date_short}_Hoodz.pdf"
                    out = unique_path(DOWNLOAD_DIR, filename)
                    with open(out, 'wb') as f:
                        f.write(resp.content)
                    log.info(f"Saved HOODZ PDF: {out}  - From: {display_name}")
                    execute_with_retry(
                        service.users().messages().modify(
                            userId='me', id=msg_id, body={'addLabelIds': [label_id]}
                        )
                    )
                    processed_ids[msg_id] = filename
                    save_processed(processed_ids)
                    continue
                else:
                    skip_log.append(f"[{email_date}] Failed HOODZ download (HTTP {resp.status_code})")
            # Fall through to generic HTML→PDF if no URL found or download failed

        # ── Sysco Pay: convert plain-text body to PDF ────────────────────────
        # FIX: removed the dead duplicate of this block that was misplaced inside the Hoodz failure handler
        if 'sysco pay' in subj_lower or 'sbs.sysco.com' in addr_lower:
            plain_part = mail.get_body(preferencelist=('plain',))
            plain = plain_part.get_content() if plain_part else ""
            html_snippet = f"<html><body><pre>{plain}</pre></body></html>"
            try:
                pdf_bytes = pdfkit.from_string(html_snippet, False)
            except Exception as e:
                skip_log.append(
                    f"[{email_date}] Skipped Sysco HTML→PDF error: {e}  - From: {display_name or addr}"
                )
                continue

            filename = f"{email_date_short}_SyscoPay.pdf"
            out = unique_path(DOWNLOAD_DIR, filename)
            with open(out, 'wb') as f:
                f.write(pdf_bytes)
            log.info(f"Saved SyscoPay PDF: {out}  - From: {display_name or addr}")
            execute_with_retry(
                service.users().messages().modify(
                    userId='me', id=msg_id, body={'addLabelIds': [label_id]}
                )
            )
            processed_ids[msg_id] = filename
            save_processed(processed_ids)
            continue

        # ── Crush Glass: convert plain-text body to PDF ──────────────────────
        is_crush_glass = "crushglasskc.com" in addr_lower or "crush glass" in display_lower
        if is_crush_glass:
            plain_part = mail.get_body(preferencelist=('plain',))
            plain = plain_part.get_content() if plain_part else ""
            html_snippet = f"<html><body><pre>{plain}</pre></body></html>"
            try:
                pdf_bytes = pdfkit.from_string(html_snippet, False)
            except Exception as e:
                skip_log.append(
                    f"[{email_date}] Skipped CrushGlass HTML→PDF error: {e}  - From: {display_name or addr}")
                continue

            filename = f"{email_date_short}_CrushGlass.pdf"
            out = unique_path(DOWNLOAD_DIR, filename)
            with open(out, 'wb') as f:
                f.write(pdf_bytes)
            log.info(f"Saved CrushGlass PDF: {out}  - From: {display_name or addr}")
            execute_with_retry(
                service.users().messages().modify(
                    userId='me', id=msg_id, body={'addLabelIds': [label_id]}
                )
            )
            processed_ids[msg_id] = filename
            save_processed(processed_ids)
            continue

        # ── Generic HTML → PDF ───────────────────────────────────────────────
        options = {
            'enable-local-file-access': None,
            'load-error-handling': 'ignore',
            'no-stop-slow-scripts': None,
        }
        clean_body = re.sub(
            r'<img[^>]+src=["\']cid:[^"\']+["\'][^>]*>',
            '',
            body,
            flags=re.IGNORECASE
        )
        try:
            pdf_bytes = pdfkit.from_string(clean_body, False, options=options)
        except Exception as e:
            skip_log.append(
                f"[{email_date}] Skipped HTML→PDF conversion error: {e}  - From: {display_name or addr}"
            )
            continue

        # Derive vendor name
        if is_webstaurant:
            vendor_raw = 'WebstaurantStore'
        elif display_lower == 'auto-receipt':
            vendor_raw = 'MidvaleIndemnity'
        elif display_lower == 'julie froneberger':
            vendor_raw = 'MdpServicesLlc'
        else:
            if display_name:
                m = SUFFIX_REGEX.search(display_name)
                vendor_raw = display_name[:m.end()] if m else display_name
            else:
                domain = addr.split('@')[-1].split('>')[0]
                vendor_raw = domain.split('.')[0].title()

        vendor = re.sub(r'[^A-Za-z0-9]', '', vendor_raw.title())
        filename = f"{email_date_short}_ButlerFoods.pdf" if is_butler_order else f"{email_date_short}_{vendor}.pdf"

        out = unique_path(DOWNLOAD_DIR, filename)
        with open(out, 'wb') as f:
            f.write(pdf_bytes)
        log.info(f"Saved HTML PDF: {out}  - From: {display_name or addr}")

        execute_with_retry(
            service.users().messages().modify(
                userId='me', id=msg_id, body={'addLabelIds': [label_id]}
            )
        )
        processed_ids[msg_id] = filename
        save_processed(processed_ids)

    # ── Write skip log ────────────────────────────────────────────────────────
    if skip_log:
        with open(os.path.join(DOWNLOAD_DIR, "skipped_files_log.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(skip_log))

    log.info(f"Done. {len(processed_ids)} messages total in processed history.")


if __name__ == '__main__':
    service = gmail_authenticate()
    label_id = get_label_id(service, LABEL_NAME)
    download_invoices(service, label_id)
