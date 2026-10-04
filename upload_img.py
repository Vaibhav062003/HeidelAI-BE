import asyncio
from services.cloudinary_service import upload_media_to_cloudinary

async def run():
    class DummyFile:
        def __init__(self, filename, content):
            self.filename = filename
            self.content = content
            self.content_type = "image/jpeg"
            self.file = self
        
        def read(self):
            return self.content
            
    with open('assets/shakti_welcome.jpg', 'rb') as f:
        content = f.read()
        
    dummy = DummyFile('shakti_welcome.jpg', content)
    
    result = await upload_media_to_cloudinary(dummy, platform="whatsapp", org_id="org_2zV2OOhodLocEnTybzR8t9MIYCE", media_kind="image")
    print(f"URL: {result.get('url')}")

asyncio.run(run())
