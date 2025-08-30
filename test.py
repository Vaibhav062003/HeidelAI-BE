from services.messaging import whatsapp_service, instagram_service
import asyncio

messaging_services = {
    "instagram": instagram_service.InstagramService(),
}

def instagram_send_message_mockup():
    return asyncio.run(
        messaging_services["instagram"].send_message(
            "17841438674306985", 
            "3512668315705890", 
            "Hello from Sahil's test!"
        )
    )

print(instagram_send_message_mockup())