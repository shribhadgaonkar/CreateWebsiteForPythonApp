FROM python:3.9-slim

WORKDIR /deployer_app

# Install system dependencies that might be needed by some Python packages
# (e.g., for cryptography, Pillow, etc., though not strictly needed for this app yet)
# RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY app.py .
COPY templates ./templates/
COPY static ./static/
# Ensure CLONE_DIR is not copied if it exists, though .dockerignore is better for this
# RUN rm -rf temp_cloned_app # Should be handled by .dockerignore

# Gunicorn will listen on the port specified by Heroku via the PORT env var.
# The EXPOSE instruction is documentation for which port the container expects to be mapped.
# Heroku uses the PORT env var to tell Gunicorn what to bind to.
EXPOSE $PORT

# Run Gunicorn
# Heroku sets the PORT environment variable. Gunicorn binds to 0.0.0.0:$PORT.
# --log-file - directs logs to stdout/stderr for Heroku's log collection.
# --workers can be adjusted based on dyno size. For a free/hobby dyno, 1-2 is typical.
CMD gunicorn app:app --bind "0.0.0.0:\$PORT" --workers 2 --timeout 120 --log-file - --access-logfile - --error-logfile -
