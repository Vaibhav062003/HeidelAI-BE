import asyncio

from pymongo import UpdateOne

from database import get_async_mongo_db
from services.utils import name_search_prefixes, phone_search_parts


async def main() -> None:
    database = get_async_mongo_db()
    for collection_name in await database.list_collection_names():
        if not collection_name.startswith("conversations_"):
            continue
        collection = database[collection_name]
        operations = []
        async for row in collection.find({}, {"customer_name": 1, "customer_id": 1, "platform": 1}):
            fields = {"customer_name_prefixes": name_search_prefixes(row.get("customer_name", ""))}
            source = {"_id": row["_id"], "customer_name": row.get("customer_name")}
            if row.get("platform") == "whatsapp":
                digits, suffixes = phone_search_parts(row.get("customer_id", ""))
                fields.update({"phone_digits": digits, "phone_search_suffixes": suffixes})
                source.update({"platform": "whatsapp", "customer_id": row.get("customer_id")})
            operations.append(UpdateOne(source, {"$set": fields}))
            if len(operations) == 500:
                await collection.bulk_write(operations, ordered=False)
                operations.clear()
        if operations:
            await collection.bulk_write(operations, ordered=False)


if __name__ == "__main__":
    asyncio.run(main())
