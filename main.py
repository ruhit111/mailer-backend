# main.py - FINAL FIX (Replicates Desktop App Logging and Behavior)

import os
import json
import hashlib
import base64
from datetime import datetime, timedelta, timezone
import time
import re
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from email.header import Header
import uuid
import random

from fastapi import FastAPI, HTTPException, Depends, Body, BackgroundTasks, Request, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager

import firebase_admin
from firebase_admin import credentials, firestore, auth

# --- Global variable for Firestore client ---
db = None

# --- Lifespan event for FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    print("STARTUP: Lifespan event triggered.")
    try:
        if not firebase_admin._apps:
            print("STARTUP: Initializing Firebase Admin SDK...")
            service_account_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if not service_account_path or not os.path.exists(service_account_path):
                 raise ValueError("CRITICAL_ERROR: GOOGLE_APPLICATION_CREDENTIALS env var not found or file does not exist.")
            
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            print("STARTUP: Firebase Admin SDK initialized successfully.")
        
        db = firestore.client()
        print("STARTUP: Firestore client is ready.")
    except Exception as e:
        print(f"CRITICAL STARTUP ERROR: {e}")
        db = None
    yield
    print("SHUTDOWN: Application is shutting down.")


# --- FastAPI App Definition ---
app = FastAPI(title="Mail Sender by ROS", version="9.0.0", lifespan=lifespan)
print("LOG: FastAPI app object created.")

# --- Constants & Config ---
APP_PREFIX = "BULKMAILER"
SECRET_KEY = "R@O#S"
TRIAL_MAX_EMAILS = 50
ARTIFACTS_COLLECTION = "artifacts"
BACKEND_APP_ID = "mail_sender_ros_backend_app_id"
USER_DATA_SUBCOLLECTION = "userAppData"
LICENSE_DOC_ID = "license"
CONSUMED_CODES_DOC_ID = "consumedCodes"
APP_CONFIG_DOC_ID = "appConfig"
TRIAL_DATA_COLLECTION = "trials"
CAMPAIGN_COLLECTION = "campaigns"

# --- CORS Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
print("LOG: CORS middleware configured to allow all origins.")

# --- Pydantic Models ---
class User(BaseModel): uid: str; email: Optional[EmailStr] = None
class ActivationRequest(BaseModel): activation_code: str
class LicenseStatus(BaseModel): status: str; plan: str; expiry_date: Optional[str] = None; activation_date: Optional[str] = None; emails_sent_trial: int; activated_email: Optional[str] = None
class EmailAccount(BaseModel): id: str = Field(default_factory=lambda: str(uuid.uuid4())); account_name: str; email: EmailStr; password: str; sender_name: Optional[str] = ""; smtp_server: str; smtp_port: int; connection_type: str; use_ssl: bool = False; use_starttls: bool = True; signature_html: Optional[str] = ""
class SendingParams(BaseModel): batchSize: int = 10; delay: float = 5.0; subjectRotation: int = 10; emailsPerAccount: int = 5
class AppConfig(BaseModel): emailAccounts: List[EmailAccount] = Field(default_factory=list); subjectLines: List[str] = Field(default_factory=list); sendingParams: SendingParams = Field(default_factory=SendingParams)
class Recipient(BaseModel): id: str; sl_no: int; Email: EmailStr; FirstName: Optional[str] = ""; CompanyName: Optional[str] = ""; status: Optional[str] = "Pending"
class CampaignRequest(BaseModel): selected_accounts: List[str]; selected_subjects: List[str]; recipients: List[Recipient]; email_body_template: str; sending_params: SendingParams
class TrialCampaignRequest(BaseModel): selected_accounts: List[EmailAccount]; selected_subjects: List[str]; recipients: List[Recipient]; email_body_template: str; sending_params: SendingParams; trial_id: str

# --- Dependencies & Services ---
async def get_db():
    if db is None: raise HTTPException(status_code=503, detail="Database service not available.")
    return db
async def get_current_user(request: Request) -> User:
    authorization: str = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(status_code=401, detail="Not authenticated")
    id_token = authorization.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token.get("uid")
        if not uid: raise HTTPException(status_code=401, detail="Invalid token: UID not found")
        return User(uid=uid, email=decoded_token.get("email"))
    except auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Invalid ID token")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {e}")

def spin(text: str) -> str:
    pattern = re.compile(r'{([^{}]*)}')
    while True:
        match = pattern.search(text)
        if not match: break
        options = match.group(1).split('|')
        selected_option = random.choice(options)
        text = text[:match.start()] + selected_option + text[match.end():]
    return text
def _calculate_checksum(data_string: str) -> str:
    hasher = hashlib.sha256(); hasher.update((data_string + SECRET_KEY).encode('utf-8')); return hasher.hexdigest()[:8].upper()
def _hash_full_code(code_str: str) -> str:
    return hashlib.sha256(code_str.encode('utf-8')).hexdigest()

class LicenseService:
    def _get_user_license_doc_ref(self, user_id: str, db_client): return db_client.collection(ARTIFACTS_COLLECTION).document(BACKEND_APP_ID).collection("users").document(user_id).collection(USER_DATA_SUBCOLLECTION).document(LICENSE_DOC_ID)
    def _get_consumed_codes_doc_ref(self, db_client): return db_client.collection(ARTIFACTS_COLLECTION).document(BACKEND_APP_ID).collection(USER_DATA_SUBCOLLECTION).document(CONSUMED_CODES_DOC_ID)
    def get_license_status_sync(self, user_id: str, db_client) -> LicenseStatus:
        doc = self._get_user_license_doc_ref(user_id, db_client).get()
        if doc.exists: return LicenseStatus(**doc.to_dict())
        return LicenseStatus(status="TRIAL", plan="TRIAL", emails_sent_trial=0)
    def _validate_activation_code_structure(self, code_str: str) -> tuple[bool, Any]:
        parts = code_str.strip().split('-');
        if len(parts) != 4: return False, "Invalid format"
        prefix, plan_key, encoded_payload, checksum_from_code = parts
        if prefix != APP_PREFIX: return False, "Invalid prefix"
        if _calculate_checksum(f"{prefix}-{plan_key}-{encoded_payload}") != checksum_from_code: return False, "Invalid checksum"
        try: return True, json.loads(base64.urlsafe_b64decode(encoded_payload + '==').decode('utf-8'))
        except Exception as e: return False, f"Invalid payload: {e}"
    def activate_license_for_new_user_sync(self, activation_code: str, db_client) -> dict:
        is_valid, payload = self._validate_activation_code_structure(activation_code)
        if not is_valid: raise HTTPException(status_code=400, detail=payload)
        consumed_ref = self._get_consumed_codes_doc_ref(db_client)
        code_hash = _hash_full_code(activation_code)
        consumed_doc = consumed_ref.get()
        if consumed_doc.exists and code_hash in consumed_doc.to_dict().get("codes", []): raise HTTPException(status_code=400, detail="Code already used")
        try: new_user = auth.create_user(); uid = new_user.uid
        except Exception as e: raise HTTPException(status_code=500, detail=f"Cannot create user: {e}")
        plan, activation_date = payload["plan"], datetime.now(timezone.utc)
        if plan == "1M": expiry_date_obj = activation_date + timedelta(days=30)
        elif plan == "6M": expiry_date_obj = activation_date + timedelta(days=182)
        elif plan == "1Y": expiry_date_obj = activation_date + timedelta(days=365)
        elif plan == "LIFE": expiry_date_obj = "LIFETIME"
        else: raise HTTPException(status_code=400, detail="Unknown plan")
        new_license_data = {"status": "ACTIVE", "plan": plan, "activation_date": activation_date.strftime("%Y-%m-%d"), "expiry_date": expiry_date_obj.strftime("%Y-%m-%d") if isinstance(expiry_date_obj, datetime) else "LIFETIME", "emails_sent_trial": 0, "activated_email": payload.get("email", "N/A")}
        self._get_user_license_doc_ref(uid, db_client).set(new_license_data)
        if consumed_doc.exists: consumed_ref.update({"codes": firestore.ArrayUnion([code_hash])})
        else: consumed_ref.set({"codes": [code_hash]})
        return {"uid": uid, "idToken": auth.create_custom_token(uid), "license": new_license_data}

class ConfigService:
    def _get_user_config_doc_ref(self, user_id: str, db_client): return db_client.collection(ARTIFACTS_COLLECTION).document(BACKEND_APP_ID).collection("users").document(user_id).collection(USER_DATA_SUBCOLLECTION).document(APP_CONFIG_DOC_ID)
    def get_app_config_sync(self, user_id: str, db_client) -> AppConfig:
        doc = self._get_user_config_doc_ref(user_id, db_client).get()
        if doc.exists: return AppConfig(**doc.to_dict())
        return AppConfig()
    def save_app_config_sync(self, user_id: str, config_data: AppConfig, db_client):
        self._get_user_config_doc_ref(user_id, db_client).set(config_data.model_dump(by_alias=True))
        return config_data

class EmailService:
    def _log_to_console(self, user_or_campaign_id: str, message: str): print(f"ID: {user_or_campaign_id} | {message}")
    
    def _send_single_email(self, account_config: dict, to_email: str, subject: str, body: str):
        try:
            msg = MIMEMultipart("alternative"); msg["Subject"] = Header(subject, "utf-8")
            from_name = account_config.get("sender_name") or account_config.get("email")
            msg["From"] = formataddr((str(Header(from_name, 'utf-8')), account_config.get("email"))); msg["To"] = to_email
            msg.attach(MIMEText(body, "html", "utf-8"))
            context = ssl.create_default_context()
            if account_config.get("connection_type") == "SSL":
                server = smtplib.SMTP_SSL(account_config.get("smtp_server"), account_config.get("smtp_port"), context=context, timeout=60)
            else:
                server = smtplib.SMTP(account_config.get("smtp_server"), account_config.get("smtp_port"), timeout=60)
                if account_config.get("connection_type") == "STARTTLS": server.starttls(context=context)
            server.login(account_config.get("email"), account_config.get("password")); server.sendmail(account_config.get("email"), [to_email], msg.as_string()); server.quit()
            return True, "Sent"
        except Exception as e: return False, str(e)

    def _process_campaign_in_background(self, req: Any, campaign_id: str, db_client: Any, is_trial: bool, user_id: Optional[str] = None):
        campaign_doc_ref = db_client.collection(CAMPAIGN_COLLECTION).document(campaign_id)
        campaign_doc_ref.update({"status": "Processing"})

        if is_trial:
            active_accounts = [acc.model_dump() for acc in req.selected_accounts]
            trial_doc_ref = db_client.collection(TRIAL_DATA_COLLECTION).document(req.trial_id)
        else:
            if not user_id: self._log_to_console(campaign_id, "ERROR: User ID is required for non-trial campaigns."); return
            user_config = ConfigService().get_app_config_sync(user_id, db_client)
            available_accounts = {acc.account_name: acc.model_dump() for acc in user_config.emailAccounts}
            active_accounts = [available_accounts[name] for name in req.selected_accounts if name in available_accounts]

        if not active_accounts: 
            campaign_doc_ref.update({"status": "Failed: No valid accounts found."})
            return
        
        active_subjects = req.selected_subjects or ["Default Subject"]
        current_account_index, total_sent = 0, 0
        
        # Use a batch to update Firestore to avoid too many writes per second
        batch = db_client.batch()
        update_counter = 0

        for i, recipient in enumerate(req.recipients):
            if is_trial:
                trial_doc = trial_doc_ref.get()
                emails_sent_count = trial_doc.to_dict().get('emails_sent', 0) if trial_doc.exists else 0
                if emails_sent_count >= TRIAL_MAX_EMAILS:
                    self._log_to_console(campaign_id, "TRIAL limit reached.")
                    break
            
            # This status is now set on the frontend log based on Firestore changes
            # self._log_to_console(campaign_id, f"Sending email to {recipient.Email}")
            batch.update(campaign_doc_ref, {f'recipients.{i}.status': 'Processing...'})

            account_dict = active_accounts[current_account_index]
            processed_subject = spin(random.choice(active_subjects))
            processed_body = spin(req.email_body_template)
            
            placeholders = {"{{FirstName}}": recipient.FirstName or "", "{{CompanyName}}": recipient.CompanyName or "", "{{Email}}": recipient.Email, "{{SENDER_NAME}}": account_dict.get("sender_name") or account_dict.get("email")}
            for ph, val in placeholders.items():
                processed_subject, processed_body = processed_subject.replace(ph, val), processed_body.replace(ph, val)
            
            success, status_msg = self._send_single_email(account_dict, recipient.Email, processed_subject, processed_body)
            
            batch.update(campaign_doc_ref, {f'recipients.{i}.status': status_msg})
            update_counter += 2

            if success:
                total_sent += 1
                if is_trial: batch.set(trial_doc_ref, {'emails_sent': firestore.Increment(1)}, merge=True)
            
            # Commit the batch periodically
            if update_counter >= 400: # Firestore limit is 500 writes per batch
                batch.commit()
                batch = db_client.batch() # Start a new batch
                update_counter = 0

            current_account_index = (current_account_index + 1) % len(active_accounts)
            time.sleep(req.sending_params.delay)
            
        batch.update(campaign_doc_ref, {"status": "Completed"})
        batch.commit() # Commit any remaining updates
        self._log_to_console(campaign_id, f"BG processing finished. Total sent: {total_sent}.")

# --- Routers ---
api_router = APIRouter(prefix="/api")
license_router = APIRouter(prefix="/license", tags=["License"])
config_router = APIRouter(prefix="/config", tags=["Configuration"])
campaign_router = APIRouter(prefix="/campaign", tags=["Campaign"])

license_service_instance, config_service_instance, email_service_instance = LicenseService(), ConfigService(), EmailService()

@license_router.post("/activate-noauth")
def activate_no_auth(req: ActivationRequest, db_client=Depends(get_db)):
    return license_service_instance.activate_license_for_new_user_sync(req.activation_code, db_client)

@license_router.get("/status", response_model=LicenseStatus)
def get_license_status(user: User = Depends(get_current_user), db_client=Depends(get_db)):
    return license_service_instance.get_license_status_sync(user.uid, db_client)

@config_router.get("", response_model=AppConfig)
def get_config(user: User = Depends(get_current_user), db_client=Depends(get_db)):
    return config_service_instance.get_app_config_sync(user.uid, db_client)

@config_router.post("")
def save_config(data: AppConfig, user: User = Depends(get_current_user), db_client=Depends(get_db)):
    return config_service_instance.save_app_config_sync(user.uid, data, db_client)

@campaign_router.post("/start")
def start_campaign(req: CampaignRequest, bg_tasks: BackgroundTasks, user: User = Depends(get_current_user), db_client=Depends(get_db)):
    campaign_id = str(uuid.uuid4())
    campaign_doc_ref = db_client.collection(CAMPAIGN_COLLECTION).document(campaign_id)
    initial_recipients = [r.model_dump() for r in req.recipients]
    campaign_doc_ref.set({"recipients": initial_recipients, "status": "Initializing...", "userId": user.uid, "createdAt": firestore.SERVER_TIMESTAMP})
    
    bg_tasks.add_task(email_service_instance._process_campaign_in_background, req, campaign_id, db_client, is_trial=False, user_id=user.uid)
    return {"message": "Campaign for activated user has been started.", "campaign_id": campaign_id}

@campaign_router.post("/start-trial")
def start_trial_campaign(req: TrialCampaignRequest, bg_tasks: BackgroundTasks, db_client=Depends(get_db)):
    trial_doc_ref = db_client.collection(TRIAL_DATA_COLLECTION).document(req.trial_id)
    trial_doc = trial_doc_ref.get()
    emails_sent = trial_doc.to_dict().get('emails_sent', 0) if trial_doc.exists else 0
    if emails_sent >= TRIAL_MAX_EMAILS:
        raise HTTPException(status_code=403, detail=f"Trial limit of {TRIAL_MAX_EMAILS} emails reached.")
    
    campaign_id = str(uuid.uuid4())
    campaign_doc_ref = db_client.collection(CAMPAIGN_COLLECTION).document(campaign_id)
    
    initial_recipients = [r.model_dump() for r in req.recipients]
    campaign_doc_ref.set({"recipients": initial_recipients, "status": "Initializing...", "isTrial": True, "createdAt": firestore.SERVER_TIMESTAMP})
    
    bg_tasks.add_task(email_service_instance._process_campaign_in_background, req, campaign_id, db_client, is_trial=True)
    return {"message": f"Trial campaign started. You have sent {emails_sent} of {TRIAL_MAX_EMAILS} trial emails.", "campaign_id": campaign_id}

api_router.include_router(license_router)
api_router.include_router(config_router)
api_router.include_router(campaign_router)
app.include_router(api_router)

@app.get("/")
def root(): return {"message": f"{app.title} v{app.version} is running! DB status: {'OK' if db else 'Error'}"}

print("LOG: main.py script execution finished.")
