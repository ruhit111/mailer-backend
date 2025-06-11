# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the main application file into the container
COPY main.py .

# Define environment variable for the port, this will be provided by Cloud Run
ENV PORT 8080

# Run uvicorn server when the container launches.
# This is a very robust way to start the server.
CMD exec uvicorn main:app --host 0.0.0.0 --port $PORT