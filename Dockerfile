# Use the official lightweight Python image.
# https://hub.docker.com/_/python
FROM python:3.10-slim

# Allow statements and log messages to immediately appear in the Knative logs
ENV PYTHONUNBUFFERED True
ENV OPENAI_API_KEY=
ENV ANTHROPIC_API_KEY=
ENV ANTHROPIC_API_KEY_NAME=
ENV DISCORD_BOT_TOKEN=
ENV DISCORD_APP_ID=
ENV DISCORD_APP_SECRET=
ENV APPLICATION_PUBLIC_KEY=
# Copy local code to the container image.
ENV APP_HOME /app
WORKDIR $APP_HOME
COPY . ./

# Install production dependencies.
RUN pip install --no-cache-dir --upgrade pip -r requirements.txt

# Run the web service on container startup. Here we use the gunicorn
# webserver, with one worker process and 8 threads.
# For environments with multiple CPU cores, increase the number of workers
# to be equal to the cores available.
# Timeout is set to 0 to disable the timeouts of the workers to allow Cloud Run to handle instance scaling.
CMD exec gunicorn --bind :$PORT --workers 1 --timeout 0 main:app -k uvicorn.workers.UvicornWorker