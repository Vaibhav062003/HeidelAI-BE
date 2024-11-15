
# Docker Commands for Chatbot Backend

Here are the main Docker commands to build, tag, and push the Docker image for your chatbot backend.

1. **Build the Docker Image**
   ```bash
   docker build -t egeniestores/chatbot:backend_chatbot-v2.1 .
   ```
### change the version

2. **Verify the Image**
   ```bash
   docker images
   ```

3. **Push the Image to Docker Hub**
   ```bash
   docker push egeniestores/chatbot:backend_chatbot-v2.1
   ```

---
