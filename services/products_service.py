import os
import pandas as pd
from io import BytesIO
from bson import ObjectId
import shutil, tempfile, zipfile
from datetime import datetime, timezone
from fastapi import UploadFile, HTTPException
from typing import Optional, List, Dict, Any, Tuple

from core.services import services
from database import get_mongo_db
from schemas.models import Product

def _products_collection(org_id: str):
    return db[f"products_{org_id}"]

def _orgs_collection():
    return db["organizations"]

def _org_metadata_collection():
    return db["organizations_metadata"]

db = get_mongo_db()

# def upload_image_to_storage(org_id: str, source_path: str, filename: str) -> str:
#     """
#     Default implementation: copy file into MEDIA_ROOT/{org_id}/filename and
#     return a media URL. Replace with S3/Cloudinary in production.
#     """
#     dest_dir = os.path.join(MEDIA_ROOT, str(org_id))
#     os.makedirs(dest_dir, exist_ok=True)
#     dest_path = os.path.join(dest_dir, filename)
#     shutil.copy2(source_path, dest_path)
#     return f"{BASE_MEDIA_URL}/{org_id}/{filename}"

def upload_image_to_storage(org_id: str, source_path: str, filename: str) -> str:
    """
    Disabled image upload. Always returns None.
    Placeholder kept for future use.
    """
    return None

def _read_spreadsheet_bytes(filename: str, data: bytes) -> pd.DataFrame:
    """
    Accepts bytes and filename to decide whether to use read_excel or read_csv.
    """
    name = filename.lower()
    bio = BytesIO(data)
    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(bio, engine="openpyxl")
    elif name.endswith(".csv"):
        df = pd.read_csv(bio)
    else:
        raise ValueError("Unsupported file type. Please upload .xlsx or .csv")
    return df

def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})
    colmap = {}
    for c in df.columns:
        cc = c.replace(" ", "_")
        if cc in ("product_name", "name", "title"):
            colmap[c] = "product_name"
        elif cc in ("description", "desc"):
            colmap[c] = "description"
        elif cc in ("price", "mrp", "cost"):
            colmap[c] = "price"
        elif cc in ("stock", "quantity", "qty"):
            colmap[c] = "stock"
        elif cc in ("product_image", "image", "image_name", "image_filename"):
            colmap[c] = "product_image"
        else:
            colmap[c] = c
    return df.rename(columns=colmap)

def get_products_for_org(org_id: str, limit: int = 100, skip: int = 0) -> List[Dict[str, Any]]:
    collection = _products_collection(org_id)
    docs = list(collection.find({}, skip=skip, limit=limit))

    for d in docs:
        d["_id"] = str(d["_id"])
    return docs

def _row_to_product(row: pd.Series, extra_cols: List[str]) -> Tuple[Optional[Product], Optional[str]]:
    """
    Returns (ProductModel, error_reason)
    """

    name = row.get("product_name")
    if not isinstance(name, str) or name.strip() == "":
        return None, "missing product_name" 

    product_data = {
        "product_name": name.strip(),
        "description": None if pd.isna(row.get("description")) else str(row.get("description")),
        "price": None,
        "stock": None,
        "product_image": None,
        "metadata": {},
    }

    price_val = row.get("price")
    if price_val is not None and not pd.isna(price_val):
        try:
            product_data["price"] = float(price_val)
        except:
            return None, "invalid price value"

    stock_val = row.get("stock")
    if stock_val is not None and not pd.isna(stock_val):
        try:
            product_data["stock"] = int(stock_val)
        except:
            product_data["stock"] = None

    img_val = row.get("product_image")
    if img_val is not None and not pd.isna(img_val):
        product_data["product_image"] = str(img_val).strip()

    for col in extra_cols:
        val = row.get(col)
        if val is not None and not pd.isna(val):
            product_data["metadata"][col] = val

    try:
        product_obj = Product(**product_data)
        return product_obj, None
    except Exception as e:
        return None, f"Model error: {str(e)}"

async def bulk_upload_products_from_file(
    org_id: str,
    file: UploadFile,
    images_zip: Optional[UploadFile] = None
) -> Dict[str, Any]:

    content = await file.read()
    try:
        df = _read_spreadsheet_bytes(file.filename, content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{str(e)}")

    df = _normalize_columns(df)
    canonical = {"product_name", "description", "price", "stock", "product_image"}
    extra_cols = [c for c in df.columns if c not in canonical]

    products_to_insert = []
    current_product_names = set()
    errors = []
    inserted_count = 0
    collection = _products_collection(org_id)

    for idx, row in df.iterrows():

        product_obj, reason = _row_to_product(row, extra_cols)
        if product_obj is None:
            errors.append({"row": idx + 1, "reason": reason})
            continue

        if product_obj.product_name in current_product_names:
            errors.append({
                "row": idx + 1,
                "reason": f"duplicate product_name {product_obj.product_name} in upload file"
            })
            continue

        current_product_names.add(product_obj.product_name)
        product_obj.product_image = None
        # product_obj.image_url = None

        if product_obj.product_name:
            exists = collection.find_one({"product_name": product_obj.product_name})
            if exists:
                errors.append({
                    "row": idx + 1,
                    "reason": f"duplicate product_name {product_obj.product_name}"
                })
                continue

        mongo_doc = product_obj.to_mongo()
        mongo_doc["org_id"] = org_id 

        products_to_insert.append(mongo_doc)

    batch_size = services.BATCH_SIZE
    try:
        for i in range(0, len(products_to_insert), batch_size):
            batch = products_to_insert[i:i + batch_size]
            if batch:
                res = collection.insert_many(batch)
                inserted_count += len(res.inserted_ids)
    except Exception as e:
        return {"error": f"DB insert error: {str(e)}"}

    _org_metadata_collection().update_one(
        {"org_id": org_id},
        {
            "$set": {
                "product_info.products_uploaded": True,
                "product_info.last_updated_at": datetime.now(timezone.utc)
            },
            "$inc": {
                "product_info.products_count": inserted_count
            }
        },
        upsert=True
    )

    return {
        "inserted": inserted_count,
        "attempted": len(products_to_insert) + len(errors),
        "skipped": len(errors),
        "errors": errors,
    }

async def add_single_product(org_id: str, data: dict) -> dict:
    collection = _products_collection(org_id)

    product_name = data["product_name"]

    existing_product = collection.find_one({"product_name": product_name})
    if existing_product:
        raise HTTPException(
            status_code=400,
            detail=f"Product with product_name '{product_name}' already exists. Please delete the existing product to upload it again."
        )

    final_image_url = data.get("image_url")

    product = Product(
        product_name=product_name,
        description=data.get("description"),
        price=data.get("price"),
        stock=data.get("stock"),
        product_image=None, 
        image_url=final_image_url, 
        metadata={}
    )

    mongo_doc = product.to_mongo()
    mongo_doc["org_id"] = org_id

    result = collection.insert_one(mongo_doc)
    mongo_doc["_id"] = str(result.inserted_id)

    if result.inserted_id:
        _org_metadata_collection().update_one(
            {"org_id": org_id},
            {
                "$set": {
                    "product_info.products_uploaded": True,
                    "product_info.last_updated_at": datetime.now(timezone.utc)
                },
                "$inc": {
                    "product_info.products_count": 1
                }
            },
            upsert=True
        )

    return mongo_doc

def get_product_by_id(org_id: str, mongo_id: str) -> dict:
    collection = _products_collection(org_id)

    if not ObjectId.is_valid(mongo_id):
        raise HTTPException(status_code=400, detail="Invalid product Mongo ID")

    doc = collection.find_one({"_id": ObjectId(mongo_id)})

    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")

    doc["_id"] = str(doc["_id"])
    return doc

def delete_product_by_id(org_id: str, mongo_id: str) -> dict:
    collection = _products_collection(org_id)

    if not ObjectId.is_valid(mongo_id):
        raise HTTPException(400, "Invalid product Mongo ID")

    doc = collection.find_one({"_id": ObjectId(mongo_id)})
    if not doc:
        raise HTTPException(404, "Product not found")

    result = collection.delete_one({"_id": ObjectId(mongo_id)})

    has_products = _products_collection(org_id).count_documents({}) > 0

    if result.deleted_count:
        _org_metadata_collection().update_one(
            {"org_id": org_id},
            {
                "$set": {
                    "product_info.products_uploaded": has_products,
                    "product_info.last_updated_at": datetime.now(timezone.utc)
                },
                "$inc": {
                    "product_info.products_count": -1
                }
            },
            upsert=True
        )

    doc["_id"] = str(doc["_id"])
    return doc
