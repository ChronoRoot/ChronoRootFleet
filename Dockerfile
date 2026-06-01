# Use official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables to ensure Python output is sent straight to terminal
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Copy only the requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# (Note: We don't copy the /app folder here because docker-compose 
# is mounting it as a live volume for development)