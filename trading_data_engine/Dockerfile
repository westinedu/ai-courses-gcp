# Use a lightweight Python official image as base
FROM python:3.11-slim-buster

# Set working directory
WORKDIR /app

# Copy requirements.txt file to work directory
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code to the work directory
COPY . .

# Expose the application port
EXPOSE 8080

# Define the command to run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]