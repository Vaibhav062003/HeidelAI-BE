import os
from dotenv import load_dotenv

# === Load Environment Variables ===
load_dotenv()

IG_ACCESS_TOKEN = os.getenv("IG_ACCESS_TOKEN")
IG_ID = os.getenv("IG_ID")
IG_VERSION = os.getenv("IG_VERSION")
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

# Firestore
SERVICE_ACCOUNT_FILE = os.getenv('FIRESTORE_SERVICE_ACCOUNT_FILE')

WA_ACCESS_TOKEN_ENCRYPTION_KEY = os.getenv('WA_ACCESS_TOKEN_ENCRYPTION_KEY')

# JWT Key Set URL
JWKS_URL = os.getenv('JWKS_URL')
JWT_TOKEN_ISSUER = os.getenv('JWT_TOKEN_ISSUER')
JWT_HEADER_ALGORITHM = os.getenv('JWT_HEADER_ALGORITHM')

# Egenie Specific Configurations
ORG_ID = os.getenv("ORG_ID")

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