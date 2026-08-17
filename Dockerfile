# Use an official Python runtime as a parent image.
# 3.9 reached end of life in October 2025 and receives no further security fixes.
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONUTF8=1
ENV WEB_PORT=8080

# Set the working directory in the container
WORKDIR /app

# ffmpeg muxes audio into v.redd.it videos (Reddit serves them as separate tracks)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container at /app
COPY . .

EXPOSE 8080

# Runs the web UI and the hourly saved-posts sync in one process.
# Set WEB_ENABLED=false for the original daemon-only behavior.
CMD ["python", "app.py"]
