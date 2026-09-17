import os
from dotenv import load_dotenv

# === Load Environment Variables ===
load_dotenv()

CHATBOT_BASE_URL = (os.getenv("CHATBOT_BASE_URL", "").strip() or "https://backend-chatbot.koyeb.app").rstrip("/")

IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
IG_ID = os.getenv("IG_ID")
IG_VERSION = os.getenv("IG_VERSION")
WA_VERSION = os.getenv("WA_VERSION")
BUSINESS_ID = os.getenv("BUSINESS_ID")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
APP_ID = os.getenv("APP_ID")
VERSION = os.getenv("VERSION")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
APP_SECRET = os.getenv("APP_SECRET")
wa_id = os.getenv("wa_id")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
FIRESTORE_CONTEXT_DB = os.getenv("FIRESTORE_CONTEXT_DB")

# MongoDB URIs
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_URI2 = os.getenv("MONGODB_URI2")
MONGODB_CLUSTER = os.getenv("MONGODB_CLUSTER")

# Supabase
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
SUPABASE_INSTA_DM_TABLE = os.environ.get('SUPABASE_INSTA_DM_TABLE')

# Instagram
INSTAGRAM_APP_ID = os.getenv("IG_APP_ID")
INSTAGRAM_APP_SECRET = os.getenv("IG_APP_SECRET")

# === External Service Configurations ===

# Cloudinary
CLOUDINARY_CLOUD_NAME=os.getenv('CLOUDINARY_CLOUD_NAME')
CLOUDINARY_API_KEY=os.getenv('CLOUDINARY_API_KEY')
CLOUDINARY_API_SECRET=os.getenv('CLOUDINARY_API_SECRET')

# IRIS IRP6 e-invoice integration. Taxpayer credentials are encrypted in MongoDB.
IRIS_IRP_ENV = os.getenv("IRIS_IRP_ENV", "sandbox").strip().lower()
IRIS_IRP_API_BASE_URL = os.getenv("IRIS_IRP_API_BASE_URL", "").strip().rstrip("/")
IRIS_IRP_CLIENT_ID = os.getenv("IRIS_IRP_CLIENT_ID", "").strip()
IRIS_IRP_CLIENT_SECRET = os.getenv("IRIS_IRP_CLIENT_SECRET", "").strip()
IRIS_IRP_PUBLIC_KEY_PEM = os.getenv("IRIS_IRP_PUBLIC_KEY_PEM", "").strip().replace("\\n", "\n")
IRP_CREDENTIAL_ENCRYPTION_KEY = os.getenv("IRP_CREDENTIAL_ENCRYPTION_KEY", "").strip()

# Firestore
SERVICE_ACCOUNT_FILE = os.getenv('FIRESTORE_SERVICE_ACCOUNT_FILE')

WA_ACCESS_TOKEN_ENCRYPTION_KEY = os.getenv('WA_ACCESS_TOKEN_ENCRYPTION_KEY')

# Firebase Cloud Messaging (Android app notifications)
FIREBASE_SERVICE_ACCOUNT_B64 = os.getenv("FIREBASE_SERVICE_ACCOUNT_B64")
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID")
PUSH_TOKEN_ENCRYPTION_KEY = os.getenv("PUSH_TOKEN_ENCRYPTION_KEY")

# JWT Key Set URL
JWKS_URL = os.getenv('JWKS_URL')
JWT_TOKEN_ISSUER = os.getenv('JWT_TOKEN_ISSUER')
JWT_HEADER_ALGORITHM = os.getenv('JWT_HEADER_ALGORITHM')

# Egenie Specific Configurations
ORG_ID = os.getenv("ORG_ID")
LEE_DEALER_ORG_ID = os.getenv("LEE_DEALER_ORG_ID")

#Sentence Transformers
SENTENCE_TRANSFORMERS_MODEL = os.getenv("sentence_transformers_model")
KOYEB2 = os.getenv("KOYEB2")

# products service
MEDIA_ROOT = os.getenv("MEDIA_ROOT", "./media")
BASE_MEDIA_URL = os.getenv("BASE_MEDIA_URL", "http://localhost:8000/media")  

#Cashfree Payment Gateway
CASHFREE_URL = os.getenv("CASHFREE_URL")
CASHFREE_APP_ID = os.getenv("CASHFREE_APP_ID")
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY")
CASHFREE_VERSION = os.getenv("CASHFREE_VERSION")

#Clerk
CLERK_TOKEN = os.getenv("CLERK_TOKEN_KEY")
CLERK_WEBHOOK_SIGNING_SECRET = os.getenv("CLERK_WEBHOOK_SIGNING_SECRET")

# Webhook for Dev
DEV_WEBHOOK_URL = os.getenv("DEV_WEBHOOK_URL", "").strip().strip("'\"")
TEST_WEBHOOK_URL = os.getenv("TEST_WEBHOOK_URL", "").strip().strip("'\"")

ENVIRONMENT = os.getenv("ENVIRONMENT")

HEIDELAI_ORG_WA_ID = os.getenv("HEIDELAI_ORG_WA_ID")
HEIDELAI_ORG_IG_ID = os.getenv("HEIDELAI_ORG_IG_ID")

PARIJAT_ACADEMY_ORG_WA_ID = os.getenv("PARIJAT_ACADEMY_ORG_WA_ID")
LEE_DEALER_ORG_WA_ID = os.getenv("LEE_DEALER_ORG_WA_ID")
KARAN_ORG_WA_ID = os.getenv("KARAN_ORG_WA_ID")
X_INTERNAL_SECRET = os.getenv("X_INTERNAL_SECRET") # internal secret for making & validating API calls between different koyeb instances
INTERNAL_HMAC_SECRET = os.getenv("INTERNAL_HMAC_SECRET") or X_INTERNAL_SECRET
CRM_COMMERCIAL_BASE_URL = os.getenv("CRM_COMMERCIAL_BASE_URL")

# Google OAuth 
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

# Shopify OAuth and webhooks
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "").strip()
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "").strip()
SHOPIFY_PUBLIC_BASE_URL = os.getenv("SHOPIFY_PUBLIC_BASE_URL", "").strip().rstrip("/")

# Google Drive Webhook (push notifications for sheet changes)
# GOOGLE_WEBHOOK_BASE_URL: Public HTTPS URL where Google sends notifications
#   e.g. "https://api.yourapp.com" → webhook receives POSTs at /api/google-sheets/webhook
# GOOGLE_WEBHOOK_SECRET: Arbitrary secret used to generate watch channel tokens for validation
GOOGLE_WEBHOOK_BASE_URL = os.getenv("GOOGLE_WEBHOOK_BASE_URL", "").strip().strip("'\"")
GOOGLE_WEBHOOK_SECRET = os.getenv("GOOGLE_WEBHOOK_SECRET", "default-webhook-secret-change-me")
os.environ["LANGCHAIN_TRACING_V2"] = "true"
os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"

#Azure OpenAI
AZURE_MODEL=os.getenv("AZURE_MODEL")

#Moonshot / Kimi
MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY")
MOONSHOT_ENDPOINT = os.getenv("MOONSHOT_ENDPOINT")
MOONSHOT_DEPLOYMENT_NAME = os.getenv("MOONSHOT_DEPLOYMENT_NAME", "Kimi-K2.6-f-s-a")
MOONSHOT_DEPLOYMENT = MOONSHOT_DEPLOYMENT_NAME
AZURE_OPENAI_ENDPOINT=os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_SUBSCRIPTION_KEY=os.getenv("AZURE_OPENAI_SUBSCRIPTION_KEY")
AZURE_OPENAI_API_VERSION=os.getenv("AZURE_OPENAI_API_VERSION")
AZURE_DEPLOYMENT=os.getenv("AZURE_DEPLOYMENT")

if AZURE_OPENAI_SUBSCRIPTION_KEY:
    os.environ["AZURE_OPENAI_API_KEY"] = AZURE_OPENAI_SUBSCRIPTION_KEY
if AZURE_DEPLOYMENT:
    os.environ["AZURE_OPENAI_DEPLOYMENT"] = AZURE_DEPLOYMENT

PMA_ORG_ID=os.getenv("PMA_ORG_ID")
EGENIE_ORG_ID=os.getenv("EGENIE_ORG_ID")
LEE_DEALER_ORG_WA_ID=os.getenv("LEE_DEALER_ORG_WA_ID")

KARAN_ORG_ID=os.getenv("KARAN_ORG_ID")
KARAN_ORG_WA_ID=os.getenv("KARAN_ORG_WA_ID")

GIGA_ORG_ID=os.getenv("GIGA_ORG_ID")
GIGA_ORG_WA_ID=os.getenv("GIGA_ORG_WA_ID")

EGENIE_WA_ID=os.getenv("EGENIE_WA_ID")
EGENIE_IG_ID=os.getenv("EGENIE_IG_ID")

SAHIL_WA_ID=os.getenv("SAHIL_WA_ID")
SAHIL_IG_ID=os.getenv("SAHIL_IG_ID")

KOMAL_ORG_ID=os.getenv("KOMAL_ORG_ID")
KOMAL_ORG_WA_ID=os.getenv("KOMAL_ORG_WA_ID")

PRICING_INTELLIGENCE_ORG_IDS = {
    org_id.strip()
    for org_id in os.getenv("PRICING_INTELLIGENCE_ORG_IDS", "").split(",")
    if org_id.strip()
}


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive integer without making application import fail closed."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


PRICING_FLOOR_MAX_AGE_MINUTES = _positive_int_env("PRICING_FLOOR_MAX_AGE_MINUTES", 60)
