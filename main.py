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

# --- Pre-App Logging ---
print("LOG: main.py script execution started.")

try:
    import firebase_admin
    from firebase_admin import credentials, firestore, auth
    from google.cloud import secretmanager
    print("LOG: All major libraries imported successfully.")
except ImportError as e:
    print(f"CRITICAL IMPORT ERROR: {e}")

# --- Global variable for Firestore client ---
db = None

# --- Lifespan event for FastAPI ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    print("STARTUP: Lifespan event triggered.")
    try:
        if not firebase_admin._apps:
            print("STARTUP: Initializing Firebase...")
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
            if not project_id:
                raise ValueError("CRITICAL_ERROR: GOOGLE_CLOUD_PROJECT env var not found.")
            
            print(f"STARTUP: Project ID: {project_id}")
            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{project_id}/secrets/firebase-service-account-key/versions/latest"
            
            print(f"STARTUP: Accessing secret: {name}")
            response = client.access_secret_version(name=name)
            
            creds_dict = json.loads(response.payload.data.decode("UTF-8"))
            print("STARTUP: Credentials fetched.")
            
            cred = credentials.Certificate(creds_dict)
            firebase_admin.initialize_app(cred, {'projectId': project_id})
            print("STARTUP: Firebase Admin SDK initialized.")
        
        db = firestore.client()
        print("STARTUP: Firestore client is ready.")

    except Exception as e:
        print(f"CRITICAL STARTUP ERROR: {e}")
        db = None
    
    yield
    print("SHUTDOWN: Application shutting down.")


# --- FastAPI App Definition ---
app = FastAPI(title="Mail Sender by ROS", version="4.1.0", lifespan=lifespan)
print("LOG: FastAPI app object created.")

# --- Constants & Config ---
APP_PREFIX = "BULKMAILER"
SECRET_KEY = "R@O#S"
TRIAL_MAX_EMAILS = 10
ARTIFACTS_COLLECTION = "artifacts" 
BACKEND_APP_ID = "mail_sender_ros_backend_app_id" 
USER_DATA_SUBCOLLECTION = "userAppData"
LICENSE_DOC_ID = "license"
CONSUMED_CODES_DOC_ID = "consumedCodes"
APP_CONFIG_DOC_ID = "appConfig"

# --- CORS Middleware ---
origins = ["http://localhost", "http://localhost:8000", "http://127.0.0.1:8000", "https://mailsenderbyros2.web.app"]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
print("LOG: CORS middleware configured.")

# --- Pydantic Models ---
class User(BaseModel): uid: str; email: Optional[EmailStr] = None
class ActivationRequest(BaseModel): activation_code: str
class LicenseStatus(BaseModel): status: str; plan: str; expiry_date: Optional[str] = None; activation_date: Optional[str] = None; emails_sent_trial: int; activated_email: Optional[str] = None

# ** THIS IS THE FIX **
# Using Field(default_factory=...) is the correct way to generate a default value for each new instance.
class EmailAccount(BaseModel): 
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    account_name: str
    email: EmailStr
    password: str
    sender_name: Optional[str] = ""
    smtp_server: str
    smtp_port: int
    connection_type: str
    use_ssl: bool = False
    use_starttls: bool = True
    signature_html: Optional[str] = ""

class SendingParams(BaseModel): batchSize: int = 10; delay: float = 5.0; subjectRotation: int = 10; emailsPerAccount: int = 5
class AppConfig(BaseModel): emailAccounts: List[EmailAccount] = Field(default_factory=list, alias="emailAccounts"); subjectLines: List[str] = Field(default_factory=list, alias="subjectLines"); sendingParams: SendingParams = Field(default_factory=SendingParams, alias="sendingParams")
class Recipient(BaseModel): id: str; sl_no: int; Email: EmailStr; FirstName: Optional[str] = ""; CompanyName: Optional[str] = ""; STATUS: Optional[str] = "Pending"
class CampaignRequest(BaseModel): selected_accounts: List[str]; selected_subjects: List[str]; recipients: List[Recipient]; email_body_template: str; sending_params: SendingParams

# --- Dependencies & Services ---
async def get_db():
    if db is None: raise HTTPException(status_code=503, detail="Database service not available.")
    return db

# ... (The rest of your service classes and endpoints remain the same as the previous version) ...

# --- Utility Functions ---
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
    hasher = hashlib.sha256()
    hasher.update((data_string + SECRET_KEY).encode('utf-8'))
    return hasher.hexdigest()[:8].upper()

def _hash_full_code(code_str: str) -> str:
    return hashlib.sha256(code_str.encode('utf-8')).hexdigest()

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

# --- Service Layer ---
class LicenseService:
    def _get_user_license_doc_ref(self, user_id: str, db_client): return db_client.collection(ARTIFACTS_COLLECTION).document(BACKEND_APP_ID).collection("users").document(user_id).collection(USER_DATA_SUBCOLLECTION).document(LICENSE_DOC_ID)
    def _get_consumed_codes_doc_ref(self, db_client): return db_client.collection(ARTIFACTS_COLLECTION).document(BACKEND_APP_ID).collection(USER_DATA_SUBCOLLECTION).document(CONSUMED_CODES_DOC_ID)
    async def get_license_status(self, user_id: str, db_client) -> LicenseStatus:
        doc = await self._get_user_license_doc_ref(user_id, db_client).get()
        if doc.exists: return LicenseStatus(**doc.to_dict())
        return LicenseStatus(status="TRIAL", plan="TRIAL", emails_sent_trial=0)
    def _validate_activation_code_structure(self, code_str: str) -> tuple[bool, Any]:
        parts = code_str.strip().split('-')
        if len(parts) != 4: return False, "Invalid format"
        prefix, plan_key, encoded_payload, checksum_from_code = parts
        if prefix != APP_PREFIX: return False, "Invalid prefix"
        if _calculate_checksum(f"{prefix}-{plan_key}-{encoded_payload}") != checksum_from_code: return False, "Invalid checksum"
        try: return True, json.loads(base64.urlsafe_b64decode(encoded_payload + '==').decode('utf-8'))
        except Exception as e: return False, f"Invalid payload: {e}"
    async def activate_license_for_new_user(self, activation_code: str, db_client) -> dict:
        is_valid, payload = self._validate_activation_code_structure(activation_code)
        if not is_valid: raise HTTPException(status_code=400, detail=payload)
        consumed_ref = self._get_consumed_codes_doc_ref(db_client)
        code_hash = _hash_full_code(activation_code)
        consumed_doc = await consumed_ref.get()
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
        await self._get_user_license_doc_ref(uid, db_client).set(new_license_data)
        if consumed_doc.exists: await consumed_ref.update({"codes": firestore.ArrayUnion([code_hash])})
        else: await consumed_ref.set({"codes": [code_hash]})
        return {"uid": uid, "idToken": auth.create_custom_token(uid).decode('utf-8'), "license": new_license_data}

class ConfigService:
    def _get_user_config_doc_ref(self, user_id: str, db_client): return db_client.collection(ARTIFACTS_COLLECTION).document(BACKEND_APP_ID).collection("users").document(user_id).collection(USER_DATA_SUBCOLLECTION).document(APP_CONFIG_DOC_ID)
    async def get_app_config(self, user_id: str, db_client) -> AppConfig:
        doc = await self._get_user_config_doc_ref(user_id, db_client).get()
        if doc.exists: return AppConfig(**doc.to_dict())
        return AppConfig()
    async def save_app_config(self, user_id: str, config_data: AppConfig, db_client) -> AppConfig:
        await self._get_user_config_doc_ref(user_id, db_client).set(config_data.model_dump(by_alias=True))
        return config_data

class EmailService:
    def _log_to_console(self, user_id: str, message: str): print(f"UID {user_id} Campaign: {message}")
    async def _process_campaign_in_background(self, req: CampaignRequest, uid: str, license_service: LicenseService, db_client: Any):
        self._log_to_console(uid, "Background email campaign processing started.")
        # ... (Email sending logic from previous version) ...
        self._log_to_console(uid, "Campaign processing finished.")
    async def start_send_bulk_emails(self, req: CampaignRequest, uid: str, license_service: LicenseService, db_client, bg_tasks: BackgroundTasks):
        bg_tasks.add_task(self._process_campaign_in_background, req, uid, license_service, db_client)
        return {"message": "Email campaign started. Check server logs for progress."}

# --- Routers ---
license_router, config_router, campaign_router = APIRouter(prefix="/api/license"), APIRouter(prefix="/api/config"), APIRouter(prefix="/api/send")
license_service, config_service, email_service = LicenseService(), ConfigService(), EmailService()

@license_router.post("/activate-noauth")
async def activate_no_auth(req: ActivationRequest, db_client=Depends(get_db)): return await license_service.activate_license_for_new_user(req.activation_code, db_client)
@license_router.get("/status", response_model=LicenseStatus)
async def get_license_status(user: User = Depends(get_current_user), db_client=Depends(get_db)): return await license_service.get_license_status(user.uid, db_client)
@config_router.get("", response_model=AppConfig)
async def get_config(user: User = Depends(get_current_user), db_client=Depends(get_db)): return await config_service.get_app_config(user.uid, db_client)
@config_router.post("")
async def save_config(data: AppConfig, user: User = Depends(get_current_user), db_client=Depends(get_db)): return await config_service.save_app_config(user.uid, data, db_client)
@campaign_router.post("/campaign")
async def start_campaign(req: CampaignRequest, bg_tasks: BackgroundTasks, user: User = Depends(get_current_user), db_client=Depends(get_db)): return await email_service.start_send_bulk_emails(req, user.uid, license_service, db_client, bg_tasks)

app.include_router(license_router, tags=["License"])
app.include_router(config_router, tags=["Configuration"])
app.include_router(campaign_router, tags=["Campaign"])
@app.get("/")
async def root(): return {"message": f"{app.title} v{app.version} is running! DB status: {'OK' if db else 'Error'}"}

print("LOG: main.py script execution finished.")