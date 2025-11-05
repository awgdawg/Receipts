import os
import base64
import re
import io
import email
import pdfplumber
import pdfkit
from datetime import datetime
from email import policy
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from email.utils import parseaddr
import requests
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime
import json

PROCESSED_FILE = 'processed_ids.json'

# ===== CONFIG =====
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.modify',]
# Step A: download actual PDFs

PDF_QUERY  = 'in:anywhere -in:sent (invoice OR receipt OR statement OR "order summary" OR ") filename:pdf newer_than:30d'
# Step B: grab any remaining receipts without PDFs

HTML_QUERY = 'in:anywhere -in:sent (invoice OR receipt OR statement OR "order summary") newer_than:30d'
DOWNLOAD_DIR = 'downloaded_invoices'

KEYWORDS       = ['invoice', 'receipt', 'statement', 'bill', 'payment']
PAID_KEYWORDS  = [
    'paid',
    'payment received',
    'for your payment',
    'balance due: $0',
    'balance due $0',
    'amount due $0',
    'amount due: $0'
    'status: paid',
    'payment confirmation',
    'receipt',
    'thank you for your purchase'
]
UNPAID_KEYWORDS = [
    'unpaid',
    'payment due',
    'past due',
    'balance due'

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
BALANCE_DUE_RE  = re.compile(r'balance due\s*\$?([\d,]+(?:\.\d+)?)', re.IGNORECASE)
LEGAL_SUFFIXES = ['inc', 'inc\\.', 'llc', 'llc\\.', 'ltd', 'ltd\\.', 'co', 'co\\.', 'corp', 'corp\\.', 'company']
SUFFIX_REGEX   = re.compile(r'\\b(' + '|'.join(LEGAL_SUFFIXES) + r')\\b', re.IGNORECASE)



def is_paid(text: str) -> bool:
    t = text.lower()

    # 1) If there's a "balance due $X" line, parse it out:
    m = BALANCE_DUE_RE.search(t)
    if m:
        amt = float(m.group(1).replace(',', ''))
        # paid if exactly zero; unpaid otherwise
        return amt == 0.0

    # 2) No numeric balance-due found → fall back to keywords
    #    If any UNPAID_KEYWORD appears, it's unpaid
    if any(u in t for u in UNPAID_KEYWORDS):
        return False

    # 3) If we see a positive paid indicator, treat as paid
    if any(p in t for p in PAID_KEYWORDS):
        return True

    # 4) Otherwise default to unpaid
    return False

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
    # fetch all labels and return the ID for the one matching `name`
    labels = service.users().labels().list(userId='me').execute().get('labels', [])
    for lbl in labels:
        if lbl['name'] == name:
            return lbl['id']
    raise ValueError(f"Label '{name}' not found; please create it in Gmail UI.")

LABEL_NAME = 'Vendor/Receipts/Processed-Invoices'

def extract_info_from_pdf(pdf_bytes, sender_email, skip_log, email_date):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full_text = "\n".join(page.extract_text() for page in pdf.pages if page.extract_text())

    display_name, _ = parseaddr(sender_email)
    dn = display_name.lower().strip()

    # Extract invoice date
    match = re.search(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})', full_text)
    try:
        invoice_date = datetime.strptime(match.group(1), "%m/%d/%Y")
    except:
        try:
            invoice_date = datetime.strptime(match.group(1), "%m-%d-%Y")
        except:
            invoice_date = datetime.today()

    # Handle specific sender exceptions
    if 'noreply@ccproduce.net' in sender_email.lower():
        clean_name = 'CCProduce'
    elif 'shopify.com' in sender_email.lower():
        clean_name = 'ShopifyInc'
    elif 'drinkveritasmo.com' in sender_email.lower():
        clean_name = 'Veritas'
    elif dn == 'auto-receipt':
        clean_name = 'MidvaleIndemnity'
    elif dn == 'julie froneberger':
        clean_name = 'MdpServicesLlc'
    else:
        # 2) Try the display name if it has a legal suffix
        display_name, _ = parseaddr(sender_email)
        if display_name:
            # look for the suffix
            m = SUFFIX_REGEX.search(display_name)
            if m:
                # keep up through the end of the suffix
                raw = display_name[:m.end()]
            else:
                raw = display_name
        else:
            # 3) Your existing PDF‐text heuristic
            lines = [l.strip() for l in full_text.splitlines() if l.strip()]
            candidates = [
                l for l in lines[:6]
                if len(l) < 50
                   and 'the fix' not in l.lower()
                   and not any(x in l.lower() for x in ['invoice', 'receipt', 'date', 'page', 'statement', 'total'])
            ]
            raw = candidates[0] if candidates else sender_email.split('@')[1].split('.')[0]

        # clean up for filename
        clean_name = re.sub(r'[^A-Za-z0-9]', '', raw.title())

    filename = f"{invoice_date.strftime('%Y.%m.%d')}_{clean_name}.pdf"
    return filename

def unique_path(directory, filename):
    """
    If filename exists in directory, append _1, _2, ... before the extension.
    Returns a path that doesn’t exist yet.
    """
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return os.path.join(directory, candidate)





from email.utils import parsedate_to_datetime



def download_invoices(service):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    skip_log = []

    # load processed IDs (persisted across runs)
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
                        # back-compat: list of IDs → map to None
                        processed_ids = {mid: None for mid in loaded}
        except (json.JSONDecodeError, ValueError):
            processed_ids = {}
    # ── A) Process actual PDF attachments ──
    pdf_results = service.users().messages().list(
        userId='me',
        q=PDF_QUERY,
        maxResults=500  # bump if you have a lot
    ).execute()
    pdf_msgs = pdf_results.get('messages', [])

    for msg in pdf_msgs:
        msg_id = msg['id']
        if msg_id in processed_ids:
            continue

        msg_data = service.users().messages().get(
            userId='me', id=msg_id, format='raw'
        ).execute()
        raw_data = base64.urlsafe_b64decode(msg_data['raw'])
        mail = email.message_from_bytes(raw_data, policy=policy.default)
        display_name, addr = parseaddr(mail['From'])
        sender_label = (display_name or addr).lower()

        # ← ADD THIS BLOCK at the top of Pass A
        email_date = parsedate_to_datetime(mail['Date']).strftime("%Y-%m-%d %H:%M") \
            if mail.get('Date') else "unknown_date"
        email_date_short = email_date.split()[0].replace('-', '.')

        email_subject = mail['Subject'] or ""
        email_body = (mail.get_body(preferencelist=('html', 'plain'))
                      .get_content() if mail.get_body() else "")

        candidates = []
        for part in mail.walk():
            if part.get_content_type() == 'application/pdf':
                fn = part.get_filename() or ""
                data = part.get_payload(decode=True)

                # — Exception: always accept MDP Services Invoice.pdf —
                if fn.lower() == "mdp services invoice.pdf":
                    is_mdp = True
                else:
                    is_mdp = False


                # extract PDF text
                try:
                    with pdfplumber.open(io.BytesIO(data)) as pdf:
                        pdf_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                except:
                    pdf_text = ""

                combined = "\n".join([pdf_text, email_subject, email_body, fn])

                # — 0) skip-keyword filter (unless it's MDP) —
                if not is_mdp and any(skip in combined.lower() for skip in SKIP_KEYWORDS):
                    matched = next((kw for kw in SKIP_KEYWORDS if kw in combined.lower()), None)
                    skip_log.append(
                        f"[{email_date}] Skipped PDF: skip-keyword match '{matched}'  - From: {display_name or addr}, File: {fn}")
                    continue

                # — 1) invoice/receipt filter (unless it's MDP) —
                if not is_mdp and not keyword_check(combined):
                    skip_log.append(
                        f"[{email_date}] Skipped PDF: no invoice keywords  - From: {display_name or addr}, File: {fn}")
                    continue

                # — 2) paid-status filter (unless it's MDP or Central Soyfoods) —
                is_central_soy = 'central soyfoods' in sender_label
                if not (is_mdp or is_central_soy) and not is_paid(combined):
                    skip_log.append(
                        f"[{email_date}] Skipped PDF: not marked paid  - From: {display_name or addr}, File: {fn}"
                    )
                    continue

                # — now score & collect (or save directly if you skip scoring) —
                score = 999 if is_mdp else (
                        (3 if any(k in fn.lower() for k in KEYWORDS) else 0)
                        + (2 if any(k in email_subject.lower() for k in KEYWORDS) else 0)
                        + (1 if any(k in email_body.lower() for k in KEYWORDS) else 0)
                        + (1 if any(k in pdf_text.lower() for k in KEYWORDS) else 0)
                )
                candidates.append((score, data, fn))

                if score:
                    candidates.append((score, data, fn))
                else:
                    skip_log.append(f"[{email_date}] Skipped PDF: not marked paid (File:{fn})  - From: {display_name or addr}")

        if candidates:
            candidates.sort(reverse=True, key=lambda x: x[0])
            _, best_pdf, best_fn = candidates[0]
            new_name = extract_info_from_pdf(best_pdf, mail['From'], skip_log, mail['Date'])
            if new_name:
                out = unique_path(DOWNLOAD_DIR, new_name)
                with open(out, 'wb') as f:
                    f.write(best_pdf)
                print(f"Saved PDF: {out}  - From: {display_name or addr}")

                # tag the email in Gmail
                service.users().messages().modify(
                    userId='me',
                    id=msg_id,
                    body={'addLabelIds': [LABEL_ID]}
                ).execute()

                processed_ids[msg_id] = new_name

        # ── B) Fallback: HTML‐only receipts ──
    html_results = service.users().messages().list(
        userId='me',
        q=HTML_QUERY,
        maxResults=500
    ).execute()
    html_msgs = html_results.get('messages', [])

    for msg in html_msgs:
        msg_id = msg['id']
        if msg_id in processed_ids:
            continue

        msg_data = service.users().messages().get(
            userId='me', id=msg_id, format='raw'
        ).execute()
        raw_data = base64.urlsafe_b64decode(msg_data['raw'])
        mail = email.message_from_bytes(raw_data, policy=policy.default)

        from_header = mail.get('From', '')
        display_name, addr = parseaddr(from_header)
        # WebstaurantStore HTML-pass exception
        is_webstaurant = (
                'webstaurantstore.com' in addr.lower()
                or 'webstaurantstore' in display_name.lower()
        )

        email_date = parsedate_to_datetime(mail['Date']) \
            .strftime("%Y-%m-%d %H:%M") if mail['Date'] else "unknown_date"
        email_date_short = email_date.split()[0].replace('-', '.')
        sender = mail['From'] or ""
        subj = mail['Subject'] or ""
        body = mail.get_body(preferencelist=('html', 'plain')).get_content() if mail.get_body() else ""

        # --- Combine sources for keyword & paid checks ---
        html_combined = "\n".join([subj, body])

        # 🚫 Skip-keyword filter (unless WebstaurantStore)
        if not is_webstaurant and any(skip in html_combined.lower() for skip in SKIP_KEYWORDS):
            matched = next((kw for kw in SKIP_KEYWORDS if kw in html_combined.lower()), None)
            skip_log.append(
                f"[{email_date}] Skipped HTML: skip-keyword match '{matched}'  - From: {display_name or addr}"
            )
            continue

        # 1) invoice/receipt filter (unless WebstaurantStore)
        if not is_webstaurant and not keyword_check(html_combined):
            skip_log.append(f"[{email_date}] Skipped HTML: no invoice keywords  - From: {display_name or addr}")
            continue

        # 2) paid-status filter (unless WebstaurantStore)
        if not is_webstaurant and not is_paid(html_combined):
            skip_log.append(f"[{email_date}] Skipped HTML: not marked paid  - From: {display_name or addr}")
            continue

        # HOODZ special case: download via their PDF link
        display_name, addr = parseaddr(mail['From'])
        if 'hoodz of kansas city' in display_name.lower():
            soup = BeautifulSoup(body, 'html.parser')
            pdf_url = None

            # 1) Try to find the <img alt="Download Invoice"> and grab its parent <a>
            img = soup.find('img', alt=lambda x: x and 'download invoice' in x.lower())
            if img and img.parent.name == 'a' and img.parent.get('href'):
                pdf_url = img.parent['href']

            # 2) Fallback: any <a href> containing "/pdf"
            if not pdf_url:
                for a in soup.find_all('a', href=True):
                    href = a['href'].rstrip('")')  # strip trailing quotes/parens
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
                    print(f"Saved HOODZ PDF: {out}  - From: {display_name}")

                    # tag the email in Gmail
                    service.users().messages().modify(
                        userId='me',
                        id=msg_id,
                        body={'addLabelIds': [LABEL_ID]}
                    ).execute()

                    processed_ids[msg_id] = filename
                    continue
                else:
                    skip_log.append(f"[{email_date}] Failed HOODZ download (HTTP {resp.status_code})")
                    # ── Sysco Pay special case ──
                    display_name, addr = parseaddr(mail['From'])
                    subj_lower = subj.lower()
                    if 'sysco pay' in subj_lower or 'sbs.sysco.com' in addr:
                        # grab the plain-text body (fallback if images/css break HTML→PDF)
                        plain = mail.get_body(preferencelist=('plain',)).get_content() or ""
                        # wrap it in a simple <pre> so pdfkit will render it legibly
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
                        print(f"Saved SyscoPay PDF: {out}  - From: {display_name or addr}")

                        # tag the email in Gmail
                        service.users().messages().modify(
                            userId='me',
                            id=msg_id,
                            body={'addLabelIds': [LABEL_ID]}
                        ).execute()

                        processed_ids[msg_id] = filename
                        continue

        # ── Sysco Pay special case ──
        display_name, addr = parseaddr(mail['From'])
        subj_lower = subj.lower()
        if 'sysco pay' in subj_lower or 'sbs.sysco.com' in addr:
            # grab the plain-text body (fallback if images/css break HTML→PDF)
            plain = mail.get_body(preferencelist=('plain',)).get_content() or ""
            # wrap it in a simple <pre> so pdfkit will render it legibly
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
            print(f"Saved SyscoPay PDF: {out}  - From: {display_name or addr}")

            # tag the email in Gmail
            service.users().messages().modify(
                userId='me',
                id=msg_id,
                body={'addLabelIds': [LABEL_ID]}
            ).execute()

            processed_ids[msg_id] = filename
            continue

        # Passed both checks: render to PDF

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
                f"[{email_date}] Skipped HTML→PDF conversion error after cid-clean: {e}  - From: {display_name or addr}"
            )
            continue

        # — derive vendor name with WebstaurantStore override, then display-name overrides and suffix/domain fallback —
        display_name, addr = parseaddr(mail['From'])
        dn = display_name.lower().strip()

        if is_webstaurant:
            vendor_raw = 'WebstaurantStore'
        elif dn == 'auto-receipt':
            vendor_raw = 'MidvaleIndemnity'
        elif dn == 'julie froneberger':
            vendor_raw = 'MdpServicesLlc'
        else:
            # try to capture up through any legal suffix
            if display_name:
                m = SUFFIX_REGEX.search(display_name)
                vendor_raw = display_name[:m.end()] if m else display_name
            else:
                # fallback to domain
                domain = addr.split('@')[-1].split('>')[0]
                vendor_raw = domain.split('.')[0].title()

        # clean and build filename
        vendor = re.sub(r'[^A-Za-z0-9]', '', vendor_raw.title())
        filename = f"{email_date_short}_{vendor}.pdf"

        # save the PDF
        out = unique_path(DOWNLOAD_DIR, filename)
        with open(out, 'wb') as f:
            f.write(pdf_bytes)
        print(f"Saved HTML PDF: {out}  - From: {display_name or addr}")

        # tag the email in Gmail
        service.users().messages().modify(
            userId='me',
            id=msg_id,
            body={'addLabelIds': [LABEL_ID]}
        ).execute()

        # mark this message as processed
        processed_ids[msg_id] = filename
        continue

    # ── Write skip log ──
    if skip_log:
        with open(
                os.path.join(DOWNLOAD_DIR, "skipped_files_log.txt"),
                "w",
                encoding="utf-8"
        ) as f:
            f.write("\n".join(skip_log))

    with open(PROCESSED_FILE, 'w', encoding='utf-8') as f:
        json.dump(processed_ids, f, ensure_ascii=False, indent=2)
    print(f"Remembered {len(processed_ids)} messages (with filenames), skipping them next time.")

if __name__ == '__main__':
    service = gmail_authenticate()
    LABEL_ID = get_label_id(service, LABEL_NAME)
    download_invoices(service)

