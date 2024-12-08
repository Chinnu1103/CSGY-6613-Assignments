# Use Python base image
FROM python:3.10.11

# Set working directory
WORKDIR /usr/src/app

# Copy app files
COPY ./app /usr/src/app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose app port
EXPOSE 5000

CMD ["tail", "-f", "/dev/null"]