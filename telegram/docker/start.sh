#!/bin/sh
# Run both commands

# Start FastAPI using python main.py
python main.py &

# Start uvicorn server
uvicorn main:app --host 0.0.0.0 --port 8000
