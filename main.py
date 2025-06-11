# main.py - FINAL VERSION (with Real-time Status Updates)

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
            if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                raise ValueError("CRITICAL_ERROR: GOOGLE_APPLICATION_CREDENTIALS env var not found.")
            firebase_admin.initialize_app()
            print("STARTUP: Firebase Admin SDK initialized successfully.")
        db = firestore.client()
        print("STARTUP: Firestore client is ready.")
    except Exception as e:
        print(f"CRITICAL STARTUP ERROR: {e}")
        db = None
    yield
    print("SHUTDOWN: Application is shutting down.")


# --- FastAPI App Definition ---
app = FastAPI(title="Mail Sender by ROS", version="8.0.0", lifespan=lifespan)
print("LOG: FastAPI app object created.")

# --- Constants & Config ---
APP_PREFIX = "BULKMAILER"
SECRET_KEY = "R@O#S"
TRIAL_MAX_EMAILS = 50
CAMPAIGNS_COLLECTION = "campaigns"

# --- CORS Middleware ---
origins = ["http://localhost", "http://localhost:8000", "http://127.0.0.1:8000", "https://mailsenderbyros2.web.app"]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
print(f"LOG: CORS middleware configured for origins: {origins}")

# --- Pydantic Models ---
class Recipient(BaseModel): id: str; sl_no: int; Email: EmailStr; FirstName: Optional[str] = ""; CompanyName: Optional[str] = ""; status: str = "Pending"
class CampaignData(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    owner_id: str # Can be a trial_id or a user_uid
    created_at: datetime = Field(default_factory=datetime.utcnow)
    recipients: List[Recipient] = []

# ... Other models from previous versions ...
class User(BaseModel): uid: str; email: Optional[EmailStr] = None
class EmailAccount(BaseModel): id: str = Field(default_factory=lambda: str(uuid.uuid4())); account_name: str; email: EmailStr; password: str; sender_name: Optional[str] = ""; smtp_server: str; smtp_port: int; connection_type: str; use_ssl: bool = False; use_starttls: bool = True; signature_html: Optional[str] = ""
class SendingParams(BaseModel): batchSize: int = 10; delay: float = 5.0; subjectRotation: int = 10; emailsPerAccount: int = 5
class AppConfig(BaseModel): emailAccounts: List[EmailAccount] = Field(default_factory=list, alias="emailAccounts"); subjectLines: List[str] = Field(default_factory=list, alias="subjectLines"); sendingParams: SendingParams = Field(default_factory=SendingParams, alias="sendingParams")
class BaseCampaignRequest(BaseModel):
    selected_subjects: List[str]
    recipients: List[Recipient]
    email_body_template: str
    sending_params: SendingParams
class CampaignRequest(BaseCampaignRequest): selected_accounts: List[str]
class TrialCampaignRequest(BaseCampaignRequest): selected_accounts: List[EmailAccount]; trial_id: str

# --- Dependencies & Services ---
async def get_db():
    if db is None: raise HTTPException(status_code=503, detail="Database service not available.")
    return db
# ... (get_current_user, spin, checksum functions are unchanged) ...
async def get_current_user(request: Request) -> User:
    authorization: str = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer "): raise HTTPException(status_code=401, detail="Not authenticated")
    id_token = authorization.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(id_token)
        uid = decoded_token.get("uid")
        if not uid: raise HTTPException(status_code=401, detail="Invalid token")
        return User(uid=uid, email=decoded_token.get("email"))
    except Exception as e: raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
def spin(text: str) -> str:
    pattern = re.compile(r'{([^{}]*)}')
    while True:
        match = pattern.search(text)
        if not match: break
        options = match.group(1).split('|')
        selected_option = random.choice(options)
        text = text[:match.start()] + selected_option + text[match.end():]
    return text

class EmailService:
    def _log_to_console(self, user_id: str, message: str): print(f"UID/TrialID: {user_id} | {message}")
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

    def _process_campaign_in_background(self, campaign_id: str, req: Any, user_id_or_trial_id: str, db_client: Any, is_trial: bool):
        self._log_to_console(user_id_or_trial_id, f"BG processing started for campaign {campaign_id}.")
        
        campaign_doc_ref = db_client.collection(CAMPAIGNS_COLLECTION).document(campaign_id)

        if is_trial:
            active_accounts = [acc.model_dump() for acc in req.selected_accounts]
        else:
            user_config = ConfigService().get_app_config_sync(user_id_or_trial_id, db_client)
            available_accounts = {acc.account_name: acc.model_dump() for acc in user_config.emailAccounts}
            active_accounts = [available_accounts[name] for name in req.selected_accounts if name in available_accounts]

        if not active_accounts: self._log_to_console(user_id_or_trial_id, "No valid accounts."); return
            
        active_subjects = req.selected_subjects or ["Default Subject"]
        current_account_index, total_sent = 0, 0

        for i, recipient in enumerate(req.recipients):
            # Update status to Processing
            all_recipients = campaign_doc_ref.get().to_dict().get("recipients", [])
            for r in all_recipients:
                if r['id'] == recipient.id:
                    r['status'] = "Processing..."
                    break
            campaign_doc_ref.update({"recipients": all_recipients})
            
            account_dict = active_accounts[current_account_index]
            processed_subject = spin(active_subjects[0])
            processed_body = spin(req.email_body_template)
            
            placeholders = {"{{FirstName}}": recipient.FirstName or "", "{{CompanyName}}": recipient.CompanyName or "", "{{Email}}": recipient.Email, "{{SENDER_NAME}}": account_dict.get("sender_name") or account_dict.get("email")}
            for ph, val in placeholders.items():
                processed_subject, processed_body = processed_subject.replace(ph, val), processed_body.replace(ph, val)
            
            success, status_msg = self._send_single_email(account_dict, recipient.Email, processed_subject, processed_body)
            
            # Update final status
            all_recipients = campaign_doc_ref.get().to_dict().get("recipients", [])
            for r in all_recipients:
                if r['id'] == recipient.id:
                    r['status'] = status_msg
                    break
            campaign_doc_ref.update({"recipients": all_recipients})

            if success: total_sent += 1
            current_account_index = (current_account_index + 1) % len(active_accounts)
            time.sleep(req.sending_params.delay)
        self._log_to_console(user_id_or_trial_id, f"BG processing finished. Total sent: {total_sent}.")

# ... (ConfigService and LicenseService are unchanged) ...
class ConfigService:
    def _get_user_config_doc_ref(self, user_id: str, db_client): return db_client.collection("artifacts").document("mail_sender_ros_backend_app_id").collection("users").document(user_id).collection("userAppData").document("appConfig")
    def get_app_config_sync(self, user_id: str, db_client) -> AppConfig:
        doc = self._get_user_config_doc_ref(user_id, db_client).get()
        if doc.exists: return AppConfig(**doc.to_dict())
        return AppConfig()
    def save_app_config_sync(self, user_id: str, config_data: AppConfig, db_client):
        self._get_user_config_doc_ref(user_id, db_client).set(config_data.model_dump(by_alias=True))
        return config_data

# --- New Campaign Service ---
class CampaignService:
    def _create_campaign_doc(self, owner_id: str, recipients: List[Recipient], db_client: Any) -> str:
        new_campaign = CampaignData(owner_id=owner_id, recipients=recipients)
        campaign_doc_ref = db_client.collection(CAMPAIGNS_COLLECTION).document(new_campaign.id)
        campaign_doc_ref.set(new_campaign.model_dump())
        return new_campaign.id

    def start_campaign(self, req: Any, user_id_or_trial_id: str, db_client: Any, bg_tasks: BackgroundTasks, is_trial: bool):
        campaign_id = self._create_campaign_doc(user_id_or_trial_id, req.recipients, db_client)
        
        email_service = EmailService()
        bg_tasks.add_task(email_service._process_campaign_in_background, campaign_id, req, user_id_or_trial_id, db_client, is_trial)
        
        return {"message": "Campaign started successfully.", "campaign_id": campaign_id}

# --- Routers ---
campaign_router = APIRouter(prefix="/api/campaign", tags=["Campaign"])
campaign_service_instance = CampaignService()

@campaign_router.post("/start")
async def start_campaign(req: CampaignRequest, user: User = Depends(get_current_user), db_client=Depends(get_db), bg_tasks: BackgroundTasks = BackgroundTasks()):
    return campaign_service_instance.start_campaign(req, user.uid, db_client, bg_tasks, is_trial=False)

@campaign_router.post("/start-trial")
async def start_trial_campaign(req: TrialCampaignRequest, db_client=Depends(get_db), bg_tasks: BackgroundTasks = BackgroundTasks()):
    return campaign_service_instance.start_campaign(req, req.trial_id, db_client, bg_tasks, is_trial=True)

app.include_router(campaign_router)
# ... (Include other routers as before) ...
license_router = APIRouter(prefix="/api/license", tags=["License"])
config_router = APIRouter(prefix="/api/config", tags=["Configuration"])
license_service_instance = LicenseService()
config_service_instance = ConfigService()
@license_router.post("/activate-noauth")
def activate_no_auth(req: ActivationRequest, db_client=Depends(get_db)): return license_service_instance.activate_license_for_new_user_sync(req.activation_code, db_client)
@license_router.get("/status", response_model=LicenseStatus)
def get_license_status(user: User = Depends(get_current_user), db_client=Depends(get_db)): return license_service_instance.get_license_status_sync(user.uid, db_client)
@config_router.get("", response_model=AppConfig)
def get_config(user: User = Depends(get_current_user), db_client=Depends(get_db)): return config_service_instance.get_app_config_sync(user.uid, db_client)
@config_router.post("")
def save_config(data: AppConfig, user: User = Depends(get_current_user), db_client=Depends(get_db)): return config_service_instance.save_app_config_sync(user.uid, data, db_client)
app.include_router(license_router)
app.include_router(config_router)

@app.get("/")
def root(): return {"message": f"{app.title} v{app.version} is running! DB status: {'OK' if db else 'Error'}"}

print("LOG: main.py script execution finished.")
